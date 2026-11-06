"""Work out which Box slot used how much filament during a print.

Pure logic, with no FilaMan or network code, so a recorded print can be replayed
through it. What it relies on was measured on a real Q2 print:

- ``print_stats.filament_used`` is signed. Each unload retracts about 150 mm while
  the old slot still reads as loaded, and that filament goes back on the spool.
- ``box_extras.loaded_slot`` is ``slot16`` for about 18 s during a change, and no
  filament moves in that time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ACTIVE_STATES = {"printing", "paused"}
# States in which print_stats still holds the finished job's total. Standby has
# already been reset to zero.
FINAL_STATES = {"complete", "cancelled", "error"}

# FilaMan only ever subtracts, so filament reported too early can't be handed back
# when an unload retracts it. A running slot keeps this much back until it settles.
HOLDBACK_MM = 200.0
# Don't report a running slot for less than this (about 1.5 g of PLA).
MIN_REPORT_MM = 500.0
DEFAULT_DENSITY_G_CM3 = 1.24
DEFAULT_DIAMETER_MM = 1.75


def parse_slot(value: object) -> int | None:
    """Return a slot number from ``slotN``, or None for nothing loaded (``slot16``)."""
    if not isinstance(value, str) or not value.startswith("slot"):
        return None
    number = value[4:]
    if not number.isdigit() or int(number) >= 16:
        return None
    return int(number)


def length_to_grams(
    length_mm: float, diameter_mm: float | None, density_g_cm3: float | None
) -> float:
    """Convert a length of filament to its weight."""
    radius_mm = (diameter_mm or DEFAULT_DIAMETER_MM) / 2
    return length_mm * math.pi * radius_mm**2 * (density_g_cm3 or DEFAULT_DENSITY_G_CM3) / 1000


@dataclass(frozen=True)
class Report:
    """Filament to charge to a slot's spool. The key makes a retry harmless."""

    slot: int
    length_mm: float
    key: str
    filename: str


class UsageTracker:
    """Keep a net length per slot for the running job, and say when to report it."""

    def __init__(self, printer_id: int) -> None:
        self._printer_id = printer_id
        self._in_job = False
        self._job_id = ""
        self._filename = ""
        self._last_used = 0.0
        self._last_duration = 0.0
        self._slot: int | None = None
        self._net: dict[int, float] = {}
        self._reported: dict[int, float] = {}
        self._sequence = 0
        # Filament that moved when it wasn't clear which spool it came from. It
        # isn't charged: an undercount is better than the wrong spool.
        self.unattributed_mm = 0.0
        self.resumed_mid_job = False

    @property
    def in_job(self) -> bool:
        return self._in_job

    @property
    def net_mm(self) -> dict[int, float]:
        return dict(self._net)

    def update(
        self,
        state: str,
        filename: str,
        filament_used: float,
        print_duration: float,
        loaded_slot: object,
        job_id: str,
    ) -> list[Report]:
        """Take one reading of the printer and return anything now due."""
        slot = parse_slot(loaded_slot)
        active = state in ACTIVE_STATES
        reports: list[Report] = []

        if (
            self._in_job
            and active
            and (filename != self._filename or print_duration < self._last_duration - 1)
        ):
            # A different job started while we weren't looking.
            reports += self._finish()

        if not self._in_job:
            if active:
                self._start(filename, filament_used, print_duration, slot, job_id)
            return reports

        if active or (state in FINAL_STATES and filament_used >= self._last_used - 500):
            reports += self._apply(filament_used, slot)
            self._last_duration = print_duration
        if not active:
            reports += self._finish()
        elif self._slot is not None:
            reports += self._running_report(self._slot)
        return reports

    def _start(
        self,
        filename: str,
        filament_used: float,
        print_duration: float,
        slot: int | None,
        job_id: str,
    ) -> None:
        self._in_job = True
        self._job_id = job_id
        self._filename = filename
        self._last_used = filament_used
        self._last_duration = print_duration
        self._slot = slot
        self._net = {}
        self._reported = {}
        self._sequence = 0
        # Anything used before we saw this job can't be placed on a slot.
        self.resumed_mid_job = filament_used > 0

    def _apply(self, filament_used: float, slot: int | None) -> list[Report]:
        delta = filament_used - self._last_used
        self._last_used = filament_used
        previous = self._slot
        reports: list[Report] = []

        if slot == previous:
            charge_to = slot
        elif slot is None:
            # Unloading: anything still moving belongs to the slot being unloaded.
            charge_to = previous
        elif previous is None:
            # Loading: the new filament is moving.
            charge_to = slot
        else:
            # Went straight from one slot to another, so readings were missed.
            charge_to = None

        if charge_to is not None:
            self._net[charge_to] = self._net.get(charge_to, 0.0) + delta
        elif delta:
            self.unattributed_mm += abs(delta)

        if slot != previous and previous is not None:
            reports += self._settle(previous)
        self._slot = slot
        return reports

    def _finish(self) -> list[Report]:
        reports: list[Report] = []
        for slot in sorted(self._net):
            reports += self._settle(slot)
        self._in_job = False
        self._slot = None
        return reports

    def _settle(self, slot: int) -> list[Report]:
        return self._report_up_to(slot, self._net.get(slot, 0.0))

    def _running_report(self, slot: int) -> list[Report]:
        target = self._net.get(slot, 0.0) - HOLDBACK_MM
        if target - self._reported.get(slot, 0.0) < MIN_REPORT_MM:
            return []
        return self._report_up_to(slot, target)

    def _report_up_to(self, slot: int, target: float) -> list[Report]:
        length = target - self._reported.get(slot, 0.0)
        if length <= 0:
            return []
        self._reported[slot] = target
        self._sequence += 1
        key = f"qidi_box:{self._printer_id}:{self._job_id}:{self._sequence}"
        return [Report(slot, length, key, self._filename)]

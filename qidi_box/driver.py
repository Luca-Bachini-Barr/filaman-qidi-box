"""FilaMan driver for QIDI Box printers, using only Moonraker's normal API.

Nothing is installed on the printer. The driver reads the Box's slots and the
running job from Moonraker, and charges filament to the spool assigned to the slot
that was feeding it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from app.core.database import async_session_maker
from app.core.event_bus import event_bus
from app.models.printer import PrinterSlot, PrinterSlotAssignment
from app.plugins.base import BaseDriver
from app.services.spool_service import SpoolService
from sqlalchemy import select

from .usage import Report, UsageTracker, length_to_grams, parse_slot

logger = logging.getLogger(__name__)

PRINT_POLL_SECONDS = 2.0
IDLE_POLL_SECONDS = 10.0
# A slot must read empty this long, with no print running, before its spool is
# released. A swapped spool then can't be charged for the old one's filament.
RELEASE_AFTER_SECONDS = 30.0
# You insert filament before selecting it on the printer's screen, so a restored
# spool stays on trial for this long in case the slot's filament then changes.
RESTORE_WATCH_SECONDS = 600.0
SLOTS_PER_BOX = 4
STEPPER_RE = re.compile(r"^box_stepper slot(\d+)$")


def _slot_name(slot: int) -> str:
    return f"Box {slot // SLOTS_PER_BOX + 1} Slot {'ABCD'[slot % SLOTS_PER_BOX]}"


def _slot_index(slot: int) -> str:
    return f"{slot // SLOTS_PER_BOX}-{slot % SLOTS_PER_BOX}"


def _box_filament(status: dict[str, Any], slot: int) -> dict[str, Any]:
    """Return what the Box says is in a slot. Empty slots keep stale entries here."""
    boxes = (status.get("box_extras") or {}).get("boxes") or []
    number, tray = divmod(slot, SLOTS_PER_BOX)
    if number >= len(boxes):
        return {}
    filaments = boxes[number].get("filament_info") or []
    return filaments[tray] if tray < len(filaments) else {}


def _box_color(filament: dict[str, Any]) -> str:
    colors = filament.get("color_rgb_str")
    if isinstance(colors, list) and colors and isinstance(colors[0], str):
        return colors[0].replace("#", "")[:6]
    return ""


def _box_identity(filament: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return what identifies a slot's filament, or None if the Box doesn't say."""
    color = _box_color(filament).lower()
    if not color:
        return None
    return (color, str(filament.get("filament_type") or ""), str(filament.get("filament_id") or ""))


class Driver(BaseDriver):
    driver_key = "qidi_box"

    def __init__(self, printer_id: int, config: dict[str, Any], emitter) -> None:
        super().__init__(printer_id, config, emitter)
        self._url = str(config.get("moonraker_url") or "").strip().rstrip("/")
        self._api_key = str(config.get("api_key") or "").strip()
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._tracker = UsageTracker(printer_id)
        self._connected = False
        self._last_error: str | None = None
        self._last_success_at: str | None = None
        self._state = ""
        self._filename = ""
        self._active_slot: int | None = None
        self._steppers: list[int] = []
        self._status: dict[str, Any] = {}
        self._slots: dict[int, dict[str, Any]] = {}
        self._assigned: dict[int, int] = {}
        self._assigned_filament: dict[int, dict[str, str]] = {}
        self._empty_since: dict[int, float] = {}
        # What the Box says is in each assigned slot, what to restore if that comes
        # back, and when a slot's spool was last restored.
        self._identity: dict[int, tuple[str, str, str]] = {}
        self._remembered: dict[int, tuple[int, tuple[str, str, str], dict[str, str]]] = {}
        self._restored_at: dict[int, float] = {}
        self._retry: list[Report] = []
        self._emitted: dict[int, dict[str, Any]] | None = None
        self._unassigned_mm = 0.0

    def validate_config(self) -> None:
        if not self._url.startswith(("http://", "https://")):
            raise ValueError("moonraker_url must start with http:// or https://")

    async def start(self) -> None:
        headers = {"X-Api-Key": self._api_key} if self._api_key else {}
        self._client = httpx.AsyncClient(headers=headers, timeout=10.0)
        self._assigned = await self._load_assignments()
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while self._running:
            try:
                await self._poll()
                self._connected = True
                self._last_error = None
                self._last_success_at = datetime.now(UTC).isoformat()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # keep polling through any printer or data fault
                message = str(err) or type(err).__name__
                # Log each new fault once, including one on the very first poll.
                if message != self._last_error:
                    logger.warning("QIDI Box printer %s: %s", self.printer_id, message)
                self._connected = False
                self._last_error = message
                self._steppers = []
            printing = self._tracker.in_job
            await asyncio.sleep(PRINT_POLL_SECONDS if printing else IDLE_POLL_SECONDS)

    # ------------------------------------------------------------------ #
    # Reading the printer                                                 #
    # ------------------------------------------------------------------ #

    async def _get(self, path: str) -> dict[str, Any]:
        response = await self._client.get(self._url + path)
        response.raise_for_status()
        result = response.json()["result"]
        self.log_debug("in", path, result)
        return result

    async def _poll(self) -> None:
        if not self._steppers:
            objects = (await self._get("/printer/objects/list")).get("objects", [])
            found = (STEPPER_RE.match(name) for name in objects)
            self._steppers = sorted(int(match[1]) for match in found if match)

        query = [
            "print_stats=state,filename,filament_used,print_duration",
            "box_extras",
        ]
        query += [quote(f"box_stepper slot{slot}") for slot in self._steppers]
        result = await self._get("/printer/objects/query?" + "&".join(query))
        status = result["status"]
        self._status = status

        job = status["print_stats"]
        self._state = str(job.get("state") or "")
        self._filename = str(job.get("filename") or "")
        box = status.get("box_extras") or {}
        self._active_slot = parse_slot(box.get("loaded_slot"))

        reports = self._tracker.update(
            state=self._state,
            filename=self._filename,
            filament_used=float(job.get("filament_used") or 0.0),
            print_duration=float(job.get("print_duration") or 0.0),
            loaded_slot=box.get("loaded_slot"),
            job_id=str(int(time.time())),
        )
        await self._send_reports(self._retry + reports)

        # Without its Box the printer still holds the last filaments, which must
        # not look like loaded spools or release anything.
        if box:
            await self._update_slots(status)

    async def _update_slots(self, status: dict[str, Any]) -> None:
        now = time.monotonic()
        slots: dict[int, dict[str, Any]] = {}

        for slot in self._steppers:
            stepper = status.get(f"box_stepper slot{slot}") or {}
            present = stepper.get("runout_button") != 1

            if present:
                self._track_identity(slot, _box_identity(_box_filament(status, slot)), now)

            if present or self._tracker.in_job or slot not in self._assigned:
                self._empty_since.pop(slot, None)
            elif now - self._empty_since.setdefault(slot, now) >= RELEASE_AFTER_SECONDS:
                logger.info("Releasing spool from empty slot %s", _slot_name(slot))
                self._empty_since.pop(slot, None)
                identity = self._identity.get(slot)
                if identity:
                    self._remembered[slot] = (
                        self._assigned[slot],
                        identity,
                        self._assigned_filament.get(slot, {}),
                    )
                self._release(slot)

            assigned = self._assigned_filament.get(slot, {})
            box_filament = _box_filament(status, slot) if present else {}
            slots[slot] = {
                "slot_index": _slot_index(slot),
                "slot_name": _slot_name(slot),
                "slot_kind": "tray",
                "tray_info_idx": "",
                "tray_type": assigned.get("material_type")
                or str(box_filament.get("filament_type") or ""),
                "tray_color": assigned.get("color") or _box_color(box_filament),
                "present": present,
                # FilaMan replaces the stored slot details with each event, and
                # refills them from this spool.
                "spool_id": self._assigned.get(slot),
            }

        self._slots = slots
        # Emit only changes: an event means database writes in FilaMan.
        if slots != self._emitted:
            self._emit_slots(slots)

    def _track_identity(self, slot: int, identity: tuple[str, str, str] | None, now: float) -> None:
        """Follow a loaded slot's filament, restoring or releasing its spool."""
        if slot in self._assigned:
            restored_at = self._restored_at.get(slot)
            if restored_at is not None and now - restored_at >= RESTORE_WATCH_SECONDS:
                del self._restored_at[slot]
                restored_at = None
            if (
                restored_at is not None
                and identity
                and identity != self._identity.get(slot)
                and not self._tracker.in_job
            ):
                logger.info("Filament changed in %s, releasing its spool", _slot_name(slot))
                self._release(slot)
            elif identity:
                self._identity[slot] = identity
        elif slot in self._remembered:
            spool_id, remembered, filament = self._remembered.pop(slot)
            if identity == remembered and spool_id not in self._assigned.values():
                logger.info("Same filament back in %s, restoring its spool", _slot_name(slot))
                self._assigned[slot] = spool_id
                self._assigned_filament[slot] = filament
                self._identity[slot] = remembered
                self._restored_at[slot] = now

    def _release(self, slot: int) -> None:
        del self._assigned[slot]
        self._assigned_filament.pop(slot, None)
        self._identity.pop(slot, None)
        self._restored_at.pop(slot, None)

    def _emit_slots(self, slots: dict[int, dict[str, Any]]) -> None:
        self._emitted = {slot: dict(data) for slot, data in slots.items()}
        self.emit(
            {
                "event_type": "slots_update",
                "slots": list(slots.values()),
                "ams_info": {"box_count": len(self._steppers) // SLOTS_PER_BOX},
            }
        )

    # ------------------------------------------------------------------ #
    # Spool assignment                                                    #
    # ------------------------------------------------------------------ #

    async def _load_assignments(self) -> dict[int, int]:
        async with async_session_maker() as db:
            rows = await db.execute(
                select(PrinterSlot.slot_no, PrinterSlotAssignment.spool_id)
                .join(PrinterSlotAssignment, PrinterSlotAssignment.slot_id == PrinterSlot.id)
                .where(
                    PrinterSlot.printer_id == self.printer_id,
                    PrinterSlotAssignment.spool_id.is_not(None),
                )
            )
            return dict(rows.all())

    async def send_filament_to_tray(
        self,
        ams_id: int,
        tray_id: int,
        filament_data: dict,
        spool_id: int | None = None,
    ) -> None:
        """Assign a spool to a slot. Called by FilaMan's spool page."""
        if spool_id is None:
            spool_id = filament_data.get("id")
        if isinstance(spool_id, bool) or not isinstance(spool_id, int):
            raise ValueError("spool_id must be an integer")
        slot = ams_id * SLOTS_PER_BOX + tray_id
        if slot not in self._slots:
            raise ValueError(f"Unknown slot {ams_id}-{tray_id}")

        filament = {
            "material_type": str(filament_data.get("material_type") or ""),
            "color": str(filament_data.get("color") or "").replace("#", "")[:6],
        }
        # A spool can only be in one place, so it leaves any slot it was in before.
        for other in [s for s, spool in self._assigned.items() if spool == spool_id and s != slot]:
            self._release(other)
            if other in self._slots:
                self._slots[other].update(spool_id=None, tray_type="", tray_color="")
        # A deliberate choice replaces anything waiting to be restored.
        self._remembered = {
            s: entry for s, entry in self._remembered.items() if s != slot and entry[0] != spool_id
        }
        self._identity.pop(slot, None)
        self._restored_at.pop(slot, None)
        self._assigned[slot] = spool_id
        self._assigned_filament[slot] = filament
        self._empty_since.pop(slot, None)
        # FilaMan's page confirms an assignment by this slot's colour in health().
        self._slots[slot].update(
            spool_id=spool_id,
            tray_type=filament["material_type"],
            tray_color=filament["color"],
        )
        self._emit_slots(self._slots)

    # ------------------------------------------------------------------ #
    # Charging spools                                                     #
    # ------------------------------------------------------------------ #

    async def _send_reports(self, reports: list[Report]) -> None:
        failed: list[Report] = []
        for report in reports:
            try:
                await self._record(report)
            except Exception as err:  # e.g. database busy: keep it and try again
                logger.warning("Couldn't record %s: %s", report.key, err)
                self._last_error = str(err) or type(err).__name__
                failed.append(report)
        self._retry = failed

    async def _record(self, report: Report) -> None:
        async with async_session_maker() as db:
            spool_id = (
                await db.execute(
                    select(PrinterSlotAssignment.spool_id)
                    .join(PrinterSlot, PrinterSlotAssignment.slot_id == PrinterSlot.id)
                    .where(
                        PrinterSlot.printer_id == self.printer_id,
                        PrinterSlot.slot_no == report.slot,
                    )
                )
            ).scalar_one_or_none()
            if spool_id is None:
                self._assigned.pop(report.slot, None)
                self._unassigned_mm += report.length_mm
                logger.info(
                    "No spool assigned to %s: %.0f mm not charged",
                    _slot_name(report.slot),
                    report.length_mm,
                )
                return

            service = SpoolService(db)
            spool = await service.get_spool(spool_id)
            if spool is None:
                return
            filament = spool.filament
            grams = length_to_grams(
                report.length_mm,
                filament.diameter_mm if filament else None,
                filament.density_g_cm3 if filament else None,
            )
            await service.record_consumption(
                spool=spool,
                delta_weight_g=grams,
                event_at=datetime.now(UTC),
                source="printer",
                note=f"{report.filename} ({_slot_name(report.slot)})",
                source_event_key=report.key,
            )
        await event_bus.publish({"event": "spools_changed"})

    # ------------------------------------------------------------------ #
    # What FilaMan's pages read                                           #
    # ------------------------------------------------------------------ #

    async def refresh_status(self) -> dict[str, Any]:
        return self.health()

    def health(self) -> dict[str, Any]:
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "moonraker_url": self._url,
            "state": self._state,
            "filename": self._filename,
            "active_slot": _slot_index(self._active_slot)
            if self._active_slot is not None
            else None,
            "slots": list(self._slots.values()),
            "slot_count": len(self._slots),
            "tracked_slot_spools": {
                _slot_index(slot): spool for slot, spool in self._assigned.items()
            },
            "remembered_slot_spools": {
                _slot_index(slot): entry[0] for slot, entry in self._remembered.items()
            },
            "net_mm": {_slot_index(s): round(v, 1) for s, v in self._tracker.net_mm.items()},
            "unassigned_mm": round(self._unassigned_mm, 1),
            "unattributed_mm": round(self._tracker.unattributed_mm, 1),
            "resumed_mid_job": self._tracker.resumed_mid_job,
            "reports_waiting": len(self._retry),
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        }

    async def get_display_state(self) -> dict[str, Any] | None:
        job = self._status.get("print_stats") or {}
        boxes = (self._status.get("box_extras") or {}).get("boxes") or []
        units = []
        for number, box in enumerate(boxes):
            trays = []
            for tray in range(SLOTS_PER_BOX):
                slot = self._slots.get(number * SLOTS_PER_BOX + tray)
                if slot:
                    filament = _box_filament(self._status, number * SLOTS_PER_BOX + tray)
                    known = slot["present"]
                    trays.append(
                        {
                            "slot": tray,
                            "material": slot["tray_type"],
                            "color": slot["tray_color"],
                            "color_name": filament.get("filament_str", "") if known else "",
                            "nozzle_min": filament.get("min_temp") if known else None,
                            "nozzle_max": filament.get("max_temp") if known else None,
                            "active": self._active_slot == number * SLOTS_PER_BOX + tray,
                        }
                    )
            # The firmware always lists four boxes, attached or not.
            if not trays:
                continue
            units.append(
                {
                    "ams_id": number,
                    "temperature": box.get("aht20_temp"),
                    "humidity": box.get("aht20_humidity"),
                    "slots": trays,
                }
            )
        return {
            "connected": self._connected,
            "state": self._state,
            "job": {"name": job.get("filename") or None},
            "active_tray": self._active_slot if self._active_slot is not None else 255,
            "ams": units,
        }

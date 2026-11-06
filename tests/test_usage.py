"""The accounting, including a replay of a real print on a Q2 with one Box."""

from __future__ import annotations

import json

import pytest
from conftest import CAPTURES

from qidi_box.usage import (
    HOLDBACK_MM,
    MIN_REPORT_MM,
    UsageTracker,
    length_to_grams,
    parse_slot,
)


def reading(state, used, slot, name="a.gcode", duration=None, job="1"):
    return {
        "state": state,
        "filename": name,
        "filament_used": used,
        "print_duration": used if duration is None else duration,
        "loaded_slot": slot,
        "job_id": job,
    }


def replay(tracker, readings):
    reports = []
    for item in readings:
        reports += tracker.update(**item)
    return reports


def totals(reports):
    out = {}
    for report in reports:
        out[report.slot] = out.get(report.slot, 0.0) + report.length_mm
    return out


def captured_readings():
    for line in CAPTURES[0].read_text().splitlines():
        record = json.loads(line)
        if "status" in record:
            job = record["status"]["print_stats"]
            yield reading(
                job["state"],
                job["filament_used"],
                record["status"]["box_extras"]["loaded_slot"],
                job["filename"],
                job["print_duration"],
            )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("slot0", 0), ("slot15", 15), ("slot16", None), (None, None), ("x", None), ("slotx", None)],
)
def test_parse_slot(value, expected):
    assert parse_slot(value) == expected


def test_length_to_grams():
    assert length_to_grams(1000, 1.75, 1.24) == pytest.approx(2.98, abs=0.01)
    assert length_to_grams(1000, None, None) == pytest.approx(2.98, abs=0.01)
    assert length_to_grams(1000, 2.85, 1.0) == pytest.approx(6.38, abs=0.01)


def test_real_print_matches_printer_history():
    """Each spool is charged its net use, and the total matches the printer's job."""
    tracker = UsageTracker(1)
    reports = replay(tracker, captured_readings())

    charged = totals(reports)
    assert charged[0] == pytest.approx(1984.7, abs=0.1)
    assert charged[2] == pytest.approx(593.5, abs=0.1)
    assert charged[3] == pytest.approx(90.8, abs=0.1)
    assert set(charged) == {0, 2, 3}
    assert sum(charged.values()) == pytest.approx(2669.0, abs=0.1)
    assert tracker.unattributed_mm == 0
    assert not tracker.in_job
    assert len({report.key for report in reports}) == len(reports)


def test_real_print_never_over_reports():
    """No report may exceed what the slot ends up having used: it can't be undone."""
    tracker = UsageTracker(1)
    charged = {}
    for item in captured_readings():
        for report in tracker.update(**item):
            charged[report.slot] = charged.get(report.slot, 0.0) + report.length_mm
    finals = tracker.net_mm
    for slot, total in charged.items():
        assert total <= finals[slot] + 0.01


def test_real_print_every_reading_stays_within_final_use():
    """Replayed up to any point, nothing is charged beyond the slot's final total."""
    finals = {0: 1984.7, 2: 593.5, 3: 90.8}
    tracker = UsageTracker(1)
    charged = {}
    for item in captured_readings():
        for report in tracker.update(**item):
            charged[report.slot] = charged.get(report.slot, 0.0) + report.length_mm
            assert charged[report.slot] <= finals[report.slot] + 0.1


def test_running_report_holds_back_for_the_unload():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 600, "slot0"),
            reading("printing", HOLDBACK_MM + MIN_REPORT_MM, "slot0"),
        ],
    )
    assert [r.length_mm for r in reports] == [MIN_REPORT_MM]

    # The unload retracts, then the slot empties: only what's left is charged.
    reports = replay(
        tracker,
        [
            reading("printing", 550, "slot0", duration=800),
            reading("printing", 550, "slot16", duration=801),
        ],
    )
    assert [r.length_mm for r in reports] == [50]


def test_unload_past_reported_amount_charges_nothing_more():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 700, "slot0"),
            reading("printing", 200, "slot0", duration=800),
            reading("printing", 200, "slot16", duration=801),
            reading("complete", 200, "slot16", duration=802),
        ],
    )
    assert [r.length_mm for r in reports] == [500]


def test_returning_to_a_slot_charges_only_the_new_use():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 100, "slot0"),
            reading("printing", 100, "slot16"),
            reading("printing", 100, "slot1"),
            reading("printing", 150, "slot1"),
            reading("printing", 150, "slot16", duration=151),
            reading("printing", 150, "slot0", duration=152),
            reading("printing", 180, "slot0"),
            reading("complete", 180, "slot0"),
        ],
    )
    assert [(r.slot, r.length_mm) for r in reports] == [(0, 100), (1, 50), (0, 30)]


def test_missed_slot_change_is_not_charged_to_either_spool():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 100, "slot0"),
            reading("printing", 300, "slot1"),
            reading("complete", 340, "slot1"),
        ],
    )
    assert [(r.slot, r.length_mm) for r in reports] == [(0, 100), (1, 40)]
    assert tracker.unattributed_mm == 200


def test_filament_moving_with_nothing_loaded_goes_to_the_loading_slot():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot16"),
            reading("printing", 25, "slot0"),
            reading("complete", 25, "slot0"),
        ],
    )
    assert [(r.slot, r.length_mm) for r in reports] == [(0, 25)]


def test_cancelled_print_settles():
    tracker = UsageTracker(1)
    reports = replay(tracker, [reading("printing", 0, "slot0"), reading("cancelled", 80, "slot16")])
    assert totals(reports) == {0: 80}
    assert not tracker.in_job


def test_pause_keeps_counting():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("paused", 50, "slot0"),
            reading("printing", 60, "slot0"),
            reading("complete", 60, "slot0"),
        ],
    )
    assert totals(reports) == {0: 60}


def test_reset_totals_in_standby_are_ignored():
    """Reaching standby without seeing the end must not count the reset to zero."""
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 100, "slot0"),
            reading("standby", 0, "slot16", name="", duration=0),
        ],
    )
    assert totals(reports) == {0: 100}


def test_final_reading_after_reset_is_ignored():
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 2000, "slot0", duration=10),
            reading("error", 0, "slot0", duration=0),
        ],
    )
    assert sum(r.length_mm for r in reports) == 2000


@pytest.mark.parametrize(
    "new_job",
    [
        reading("printing", 5, "slot1", name="b.gcode", duration=50, job="2"),
        reading("printing", 5, "slot1", duration=5, job="2"),
    ],
)
def test_new_job_while_away_settles_the_old_one(new_job):
    tracker = UsageTracker(1)
    reports = replay(
        tracker,
        [
            reading("printing", 0, "slot0"),
            reading("printing", 100, "slot0"),
            new_job,
            {**new_job, "filament_used": 25, "print_duration": 60},
            {**new_job, "state": "complete", "filament_used": 25, "print_duration": 60},
        ],
    )
    assert [(r.slot, r.length_mm) for r in reports] == [(0, 100), (1, 20)]
    assert reports[0].key != reports[1].key
    assert tracker.resumed_mid_job


def test_idle_readings_do_nothing():
    tracker = UsageTracker(1)
    assert tracker.update(**reading("standby", 0, "slot16", name="")) == []
    assert tracker.update(**reading("complete", 500, "slot0")) == []
    assert not tracker.in_job

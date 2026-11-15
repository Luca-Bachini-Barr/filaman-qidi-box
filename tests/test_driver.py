"""The driver's FilaMan wiring, with Moonraker and the database faked."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import sys
from types import SimpleNamespace

import pytest

from qidi_box import driver as module
from qidi_box.driver import Driver, _slot_index, _slot_name

FILAMENT_INFO = [
    {
        "filament_type": "PLA",
        "filament_str": "PLA Basic",
        "filament_id": "QFA00",
        "color_rgb_str": [color],
        "min_temp": 190,
        "max_temp": 240,
    }
    for color in ("#F28636", "#FAFAFA", "#060606", "#060606")
]
# Real firmware pads this list to four boxes, however many are attached.
BOX = {
    "loaded_slot": "slot16",
    "boxes": [
        {"aht20_temp": 21.0, "aht20_humidity": 52, "filament_info": FILAMENT_INFO},
        {"aht20_temp": 0, "aht20_humidity": 0, "filament_info": FILAMENT_INFO},
        {"aht20_temp": 0, "aht20_humidity": 0},
        {"aht20_temp": 0, "aht20_humidity": 0},
    ],
}
FILAMENT = SimpleNamespace(diameter_mm=1.75, density_g_cm3=1.24)


def status(state="standby", used=0.0, loaded="slot16", empty=(1,), box=True, name="a.gcode"):
    data = {
        "print_stats": {
            "state": state,
            "filename": name,
            "filament_used": used,
            "print_duration": used,
        },
        "box_extras": {**copy.deepcopy(BOX), "loaded_slot": loaded} if box else None,
    }
    for slot in range(4):
        data[f"box_stepper slot{slot}"] = {"runout_button": int(slot in empty)}
    return data


class FakeResult:
    def __init__(self, value):
        self.value = value

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, world):
        self.world = world

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query):
        if len(query.columns) == 2:
            return FakeResult(list(self.world.assignments.items()))
        return FakeResult(self.world.next_spool)


class World:
    """Moonraker plus FilaMan's database, as far as the driver can see them."""

    def __init__(self, monkeypatch):
        self.assignments = {}
        self.next_spool = 7
        self.spool = SimpleNamespace(id=7, filament=FILAMENT)
        self.recorded = []
        self.fail_record = False
        self.events = []
        self.status = status()
        self.objects = [f"box_stepper slot{slot}" for slot in range(4)] + ["extruder"]
        self.requests = []
        monkeypatch.setattr(module, "async_session_maker", lambda: FakeSession(self))
        world = self

        class Service:
            def __init__(self, _db):
                pass

            async def get_spool(self, _spool_id):
                return world.spool

            async def record_consumption(self, **kwargs):
                if world.fail_record:
                    raise RuntimeError("database busy")
                world.recorded.append(kwargs)

        monkeypatch.setattr(module, "SpoolService", Service)

    def driver(self):
        driver = Driver(3, {"moonraker_url": "http://printer:7125/"}, self.events.append)
        world = self

        class Response:
            def __init__(self, result):
                self.result = result

            def raise_for_status(self):
                pass

            def json(self):
                return {"result": self.result}

        class Client:
            async def get(self, url):
                world.requests.append(url)
                if world.status is None:
                    raise OSError("printer off")
                if url.endswith("objects/list"):
                    return Response({"objects": world.objects})
                return Response({"status": copy.deepcopy(world.status)})

        driver._client = Client()
        return driver


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def test_names_and_indexes():
    assert _slot_index(0) == "0-0"
    assert _slot_index(6) == "1-2"
    assert _slot_name(0) == "Box 1 Slot A"
    assert _slot_name(7) == "Box 2 Slot D"


def test_validate_config(world):
    world.driver().validate_config()
    with pytest.raises(ValueError):
        Driver(1, {}, world.events.append).validate_config()


def test_slots_are_discovered_from_the_printer(world):
    driver = world.driver()
    run(driver._poll())

    slots = world.events[-1]["slots"]
    assert [s["slot_index"] for s in slots] == ["0-0", "0-1", "0-2", "0-3"]
    assert [s["present"] for s in slots] == [True, False, True, True]
    assert slots[0]["tray_color"] == "F28636"
    assert slots[0]["tray_type"] == "PLA"
    # An empty slot's entry is left over from what was in it before.
    assert slots[1]["tray_color"] == ""
    assert slots[1]["tray_type"] == ""
    assert all(s["spool_id"] is None for s in slots)
    assert any("box_stepper%20slot3" in url for url in world.requests)


def test_only_changes_are_emitted(world):
    driver = world.driver()
    run(driver._poll())
    run(driver._poll())
    assert len(world.events) == 1

    world.status = status(empty=())
    run(driver._poll())
    assert len(world.events) == 2


def test_box_offline_changes_nothing(world):
    driver = world.driver()
    driver._assigned = {1: 9}
    world.status = status(box=False, empty=(0, 1, 2, 3))
    run(driver._poll())
    assert world.events == []
    assert driver._assigned == {1: 9}


def test_assigning_a_spool(world):
    driver = world.driver()
    run(driver._poll())

    filament = {"material_type": "PLA", "color": "#112233"}
    run(driver.send_filament_to_tray(0, 2, filament, spool_id=7))

    slot = world.events[-1]["slots"][2]
    assert slot["spool_id"] == 7
    assert slot["tray_color"] == "112233"
    assert driver.health()["slots"][2]["tray_color"] == "112233"
    assert driver.health()["tracked_slot_spools"] == {"0-2": 7}

    # Nothing changed on the printer, so nothing more to say, and it stays assigned.
    count = len(world.events)
    run(driver._poll())
    assert len(world.events) == count
    assert driver.health()["slots"][2]["spool_id"] == 7


@pytest.mark.parametrize(
    "args",
    [
        (0, 2, {}, None),
        (0, 2, {"id": True}, None),
        (0, 2, {"id": "7"}, None),
        (3, 0, {"id": 7}, None),
    ],
)
def test_bad_assignments_are_refused(world, args):
    driver = world.driver()
    run(driver._poll())
    with pytest.raises(ValueError):
        run(driver.send_filament_to_tray(*args))


def test_spool_id_can_come_from_filament_data(world):
    driver = world.driver()
    run(driver._poll())
    run(driver.send_filament_to_tray(0, 0, {"id": 12}))
    assert driver._assigned[0] == 12


def test_spool_is_released_only_after_staying_empty(world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    driver = world.driver()
    driver._assigned = {1: 9}

    run(driver._poll())
    clock[0] += module.RELEASE_AFTER_SECONDS - 1
    run(driver._poll())
    assert driver._assigned == {1: 9}

    # Back in: the wait starts again.
    world.status = status(empty=())
    run(driver._poll())
    world.status = status()
    run(driver._poll())
    clock[0] += module.RELEASE_AFTER_SECONDS - 1
    run(driver._poll())
    assert driver._assigned == {1: 9}

    clock[0] += 1
    run(driver._poll())
    assert driver._assigned == {}
    assert world.events[-1]["slots"][1]["spool_id"] is None


def test_spool_is_not_released_during_a_print(world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    driver = world.driver()
    driver._assigned = {1: 9}
    world.status = status("printing", loaded="slot0")
    run(driver._poll())
    clock[0] += 10 * module.RELEASE_AFTER_SECONDS
    run(driver._poll())
    assert driver._assigned == {1: 9}


def test_finished_print_charges_the_assigned_spool(world):
    driver = world.driver()
    world.status = status("printing", 0, "slot0")
    run(driver._poll())
    world.status = status("printing", 100, "slot0")
    run(driver._poll())
    world.status = status("complete", 100, "slot0")
    run(driver._poll())

    assert len(world.recorded) == 1
    record = world.recorded[0]
    assert record["spool"] is world.spool
    assert record["delta_weight_g"] == pytest.approx(0.298, abs=0.001)
    assert record["source"] == "printer"
    assert record["source_event_key"].startswith("qidi_box:3:")
    assert record["note"] == "a.gcode (Box 1 Slot A)"
    bus = sys.modules["app.core.event_bus"].event_bus
    assert {"event": "spools_changed"} in bus.events


def test_unassigned_slot_is_counted_not_charged(world):
    driver = world.driver()
    driver._assigned = {0: 7}
    world.next_spool = None
    world.status = status("printing", 0, "slot0")
    run(driver._poll())
    world.status = status("complete", 100, "slot0")
    run(driver._poll())
    assert world.recorded == []
    assert driver.health()["unassigned_mm"] == 100
    assert driver._assigned == {}


def test_missing_spool_is_skipped(world):
    driver = world.driver()
    world.spool = None
    world.status = status("printing", 0, "slot0")
    run(driver._poll())
    world.status = status("complete", 100, "slot0")
    run(driver._poll())
    assert world.recorded == []
    assert driver.health()["reports_waiting"] == 0


def test_failed_report_is_retried_with_the_same_key(world):
    driver = world.driver()
    world.status = status("printing", 0, "slot0")
    run(driver._poll())
    world.fail_record = True
    world.status = status("complete", 100, "slot0")
    run(driver._poll())
    assert driver.health()["reports_waiting"] == 1
    key = driver._retry[0].key

    world.fail_record = False
    world.status = status(name="")
    run(driver._poll())
    assert [r["source_event_key"] for r in world.recorded] == [key]
    assert driver.health()["reports_waiting"] == 0


def test_load_assignments(world):
    world.assignments = {2: 7}
    assert run(world.driver()._load_assignments()) == {2: 7}


def test_display_state(world):
    driver = world.driver()
    run(driver._poll())
    world.status = status("printing", 10, "slot0")
    run(driver._poll())

    state = run(driver.get_display_state())
    assert state["connected"] is False  # set by the polling loop, not by _poll
    assert state["state"] == "printing"
    assert state["job"]["name"] == "a.gcode"
    assert state["active_tray"] == 0
    unit = state["ams"][0]
    assert unit["temperature"] == 21.0
    assert unit["humidity"] == 52
    assert [t["active"] for t in unit["slots"]] == [True, False, False, False]
    assert [t["material"] for t in unit["slots"]] == ["PLA", "", "PLA", "PLA"]
    assert unit["slots"][0]["color"] == "F28636"
    assert unit["slots"][0]["color_name"] == "PLA Basic"
    assert unit["slots"][0]["nozzle_min"] == 190
    assert unit["slots"][1]["color_name"] == ""
    assert unit["slots"][1]["nozzle_max"] is None
    # Only the attached box, not the firmware's padding.
    assert len(state["ams"]) == 1


def test_display_state_with_nothing_loaded(world):
    driver = world.driver()
    assert run(driver.get_display_state())["ams"] == []
    run(driver._poll())
    assert run(driver.get_display_state())["active_tray"] == 255


def test_loop_survives_the_printer_going_away(world, monkeypatch, caplog):
    async def scenario():
        monkeypatch.setattr(module, "IDLE_POLL_SECONDS", 0)
        monkeypatch.setattr(module, "PRINT_POLL_SECONDS", 0)
        driver = world.driver()
        driver._running = True
        task = asyncio.create_task(driver._run())
        await asyncio.sleep(0.01)
        assert driver.health()["connected"] is True
        assert driver.health()["last_success_at"]

        world.status = None
        with caplog.at_level("WARNING"):
            await asyncio.sleep(0.01)
        assert [r.message for r in caplog.records].count("QIDI Box printer 3: printer off") == 1
        assert driver.health()["connected"] is False
        assert driver.health()["last_error"] == "printer off"

        world.status = status()
        await asyncio.sleep(0.01)
        assert driver.health()["connected"] is True
        assert driver.health()["last_error"] is None

        driver._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


def test_start_and_stop(world, monkeypatch):
    class Client:
        closed = False

        def __init__(self, headers, timeout):
            self.headers = headers

        async def get(self, url):
            raise OSError("not reachable")

        async def aclose(self):
            Client.closed = True

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    world.assignments = {0: 7}

    async def scenario():
        driver = Driver(3, {"moonraker_url": "http://printer", "api_key": "k"}, world.events.append)
        await driver.start()
        assert driver._client.headers == {"X-Api-Key": "k"}
        assert driver._assigned == {0: 7}
        assert driver.health()["running"] is True
        await asyncio.sleep(0)
        await driver.stop()
        assert driver.health()["running"] is False
        assert Client.closed
        # Stopping twice is harmless
        await driver.stop()

    run(scenario())


def test_refresh_status_returns_health(world):
    assert run(world.driver().refresh_status())["driver_key"] == "qidi_box"


def test_slot_with_missing_box_data_is_still_listed(world):
    """A second box's slots may report before its details do."""
    world.objects = [f"box_stepper slot{slot}" for slot in range(12)]
    world.status = status(empty=())
    for slot in range(4, 12):
        world.status[f"box_stepper slot{slot}"] = {"runout_button": 0}
    world.status["box_extras"]["boxes"][1]["filament_info"] = FILAMENT_INFO[:1]
    driver = world.driver()
    run(driver._poll())
    slots = world.events[-1]["slots"]
    assert len(slots) == 12
    assert slots[4]["tray_type"] == "PLA"
    assert slots[5]["tray_type"] == ""
    assert slots[8]["tray_type"] == ""


def test_slots_beyond_the_listed_boxes_have_no_filament(world):
    world.objects = ["box_stepper slot16"]
    world.status = status(empty=())
    world.status["box_stepper slot16"] = {"runout_button": 0}
    driver = world.driver()
    run(driver._poll())
    assert world.events[-1]["slots"][0]["tray_type"] == ""


def test_assigning_a_spool_moves_it_from_its_old_slot(world):
    driver = world.driver()
    run(driver._poll())
    filament = {"material_type": "PLA", "color": "000000"}
    run(driver.send_filament_to_tray(0, 2, filament, spool_id=64))
    run(driver.send_filament_to_tray(0, 3, filament, spool_id=64))

    assert driver._assigned == {3: 64}
    slots = world.events[-1]["slots"]
    assert slots[2]["spool_id"] is None
    assert slots[3]["spool_id"] == 64

    # The next reading shows the old slot's own filament again.
    run(driver._poll())
    assert world.events[-1]["slots"][2]["tray_color"] == "060606"


def test_another_spool_stays_where_it_is(world):
    driver = world.driver()
    run(driver._poll())
    filament = {"material_type": "PLA", "color": "000000"}
    run(driver.send_filament_to_tray(0, 2, filament, spool_id=64))
    run(driver.send_filament_to_tray(0, 3, filament, spool_id=1))
    assert driver._assigned == {2: 64, 3: 1}


def slot_with(color, filament_id="QFA00", empty=()):
    data = status(empty=empty)
    info = data["box_extras"]["boxes"][0]["filament_info"][0]
    info["color_rgb_str"] = [color]
    info["filament_id"] = filament_id
    return data


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves, with the Box already settled after power-up."""
    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "SETTLE_SECONDS", 0.0)
    return now


def emptied_slot(world, clock):
    """Return a driver whose slot 0 had spool 9, then stayed empty until released."""
    driver = world.driver()
    driver._assigned = {0: 9}
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#F28636", empty=(0,))
    run(driver._poll())
    clock[0] += module.RELEASE_AFTER_SECONDS
    run(driver._poll())
    assert driver._assigned == {}
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    return driver


def test_same_filament_back_restores_the_spool(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#f28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}
    assert world.events[-1]["slots"][0]["spool_id"] == 9
    assert driver.health()["remembered_slot_spools"] == {}


def test_restored_spool_keeps_its_assigned_colour(world, clock):
    driver = world.driver()
    run(driver._poll())
    run(driver.send_filament_to_tray(0, 0, {"material_type": "PLA", "color": "FF9016"}, 49))
    run(driver._poll())
    world.status = status(empty=(0,))
    run(driver._poll())
    clock[0] += module.RELEASE_AFTER_SECONDS
    run(driver._poll())
    assert world.events[-1]["slots"][0]["spool_id"] is None
    world.status = status(empty=())
    run(driver._poll())
    assert world.events[-1]["slots"][0]["spool_id"] == 49
    assert world.events[-1]["slots"][0]["tray_color"] == "FF9016"


@pytest.mark.parametrize(
    "different",
    [("#111111", "QFA00"), ("#F28636", "QFA90")],
)
def test_different_filament_stays_unassigned(world, clock, different):
    driver = emptied_slot(world, clock)
    world.status = slot_with(*different)
    run(driver._poll())
    assert driver._assigned == {}
    # It's forgotten, even if the first filament returns later.
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {}


def test_filament_selected_after_reload_releases_the_spool(world, clock):
    """Filament goes in first and is selected second, so the restore can be wrong."""
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}

    clock[0] += module.RESTORE_WATCH_SECONDS - 1
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {}
    assert driver.health()["remembered_slot_spools"] == {}
    assert world.events[-1]["slots"][0]["spool_id"] is None


def test_filament_edited_after_the_watch_keeps_the_spool(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    clock[0] += module.RESTORE_WATCH_SECONDS
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_explicit_assignment_ignores_filament_edits(world, clock):
    """Correcting a colour on the printer must not drop a spool you assigned."""
    driver = world.driver()
    driver._assigned = {0: 9}
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_filament_change_during_a_print_keeps_the_spool(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#111111")
    world.status["print_stats"]["state"] = "printing"
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_spool_assigned_elsewhere_is_not_restored(world, clock):
    driver = emptied_slot(world, clock)
    run(driver.send_filament_to_tray(0, 2, {"color": "000000"}, spool_id=9))
    assert driver.health()["remembered_slot_spools"] == {}
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {2: 9}


def test_new_assignment_to_the_slot_replaces_the_memory(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636", empty=(0,))
    run(driver.send_filament_to_tray(0, 0, {"color": "000000"}, spool_id=5))
    assert driver.health()["remembered_slot_spools"] == {}
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 5}


def test_memory_skips_a_spool_now_used_elsewhere(world, clock):
    driver = emptied_slot(world, clock)
    driver._assigned[2] = 9
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {2: 9}


def test_slot_without_filament_details_has_no_memory(world, clock):
    driver = world.driver()
    driver._assigned = {0: 9}
    world.status = status()
    world.status["box_extras"]["boxes"][0]["filament_info"] = []
    run(driver._poll())
    world.status = status(empty=(0,))
    run(driver._poll())
    clock[0] += module.RELEASE_AFTER_SECONDS
    run(driver._poll())
    assert driver._assigned == {}
    assert driver.health()["remembered_slot_spools"] == {}


def test_filament_change_during_a_print_releases_once_it_ends(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#111111")
    world.status["print_stats"]["state"] = "printing"
    run(driver._poll())
    assert driver._assigned == {0: 9}
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {}


def test_spool_held_by_another_printer_is_not_restored(world, clock):
    driver = emptied_slot(world, clock)
    world.assignments = {5: 9}  # FilaMan has spool 9 elsewhere
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {}
    assert driver.health()["remembered_slot_spools"] == {}


def test_spool_still_recorded_in_its_own_slot_is_restored(world, clock, monkeypatch):
    driver = emptied_slot(world, clock)
    monkeypatch.setattr(FakeResult, "all", lambda self: [(3, 0)])
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}


@pytest.mark.parametrize("field", ["filament_type", "filament_id"])
def test_partial_filament_details_are_not_an_identity(field):
    filament = {"color_rgb_str": ["#F28636"], "filament_type": "PLA", "filament_id": "QFA00"}
    assert module._box_identity(filament) == ("f28636", "PLA", "QFA00")
    filament[field] = ""
    assert module._box_identity(filament) is None


def test_unreadable_filament_keeps_the_memory(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636", filament_id="")
    run(driver._poll())
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}


REAL_MONOTONIC = module.time.monotonic


async def poll_loop_once(driver, monkeypatch):
    """Let the run loop poll a few times, then stop it.

    asyncio needs the real clock, so the test's clock stops while it runs.
    """
    fake = module.time.monotonic
    monkeypatch.setattr(module.time, "monotonic", REAL_MONOTONIC)
    monkeypatch.setattr(module, "IDLE_POLL_SECONDS", 0)
    driver._running = True
    task = asyncio.create_task(driver._run())
    await asyncio.sleep(0.01)
    driver._running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    monkeypatch.setattr(module.time, "monotonic", fake)


def test_box_defaults_after_power_up_are_ignored(world, clock, monkeypatch):
    """After power-up the Box reports default filament before its real settings."""
    driver = emptied_slot(world, clock)
    monkeypatch.setattr(module, "SETTLE_SECONDS", 60.0)
    world.status = None  # printer off
    run(poll_loop_once(driver, monkeypatch))
    world.status = slot_with("#FAFAFA")
    run(driver._poll())
    clock[0] += 59
    run(driver._poll())
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    clock[0] += 1
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_box_missing_restarts_the_settle_time(world, clock, monkeypatch):
    driver = emptied_slot(world, clock)
    monkeypatch.setattr(module, "SETTLE_SECONDS", 60.0)
    clock[0] += 61  # long settled before the Box goes missing
    world.status = status(box=False)
    run(driver._poll())
    world.status = slot_with("#FAFAFA")
    run(driver._poll())
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    clock[0] += 60
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_change_in_a_print_longer_than_the_watch_still_releases(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#111111")
    world.status["print_stats"]["state"] = "printing"
    run(driver._poll())
    clock[0] += module.RESTORE_WATCH_SECONDS + 60
    run(driver._poll())
    assert driver._assigned == {0: 9}
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {}


def test_change_reverted_during_the_print_keeps_the_spool(world, clock):
    driver = emptied_slot(world, clock)
    world.status = slot_with("#F28636")
    run(driver._poll())
    world.status = slot_with("#111111")
    world.status["print_stats"]["state"] = "printing"
    run(driver._poll())
    world.status = slot_with("#F28636")
    world.status["print_stats"]["state"] = "printing"
    run(driver._poll())
    clock[0] += module.RESTORE_WATCH_SECONDS
    world.status = slot_with("#111111")
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_failed_lookup_keeps_the_memory(world, clock):
    driver = emptied_slot(world, clock)

    async def broken(*_args):
        raise RuntimeError("database busy")

    driver._assigned_elsewhere = broken
    world.status = slot_with("#F28636")
    with pytest.raises(RuntimeError):
        run(driver._poll())
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    del driver._assigned_elsewhere  # back to the real lookup
    run(driver._poll())
    assert driver._assigned == {0: 9}


def test_assignment_during_the_lookup_wins(world, clock, monkeypatch):
    driver = emptied_slot(world, clock)

    async def assign_meanwhile(*_args):
        await driver.send_filament_to_tray(0, 0, {"color": "000000"}, spool_id=5)
        return False

    monkeypatch.setattr(driver, "_assigned_elsewhere", assign_meanwhile)
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 5}
    assert driver.health()["remembered_slot_spools"] == {}


def test_klipper_shutdown_freezes_the_slots_and_restarts_the_settle_time(
    world, clock, monkeypatch
):
    """With the Box unpowered, Klipper shuts down and Moonraker serves stale data."""
    driver = emptied_slot(world, clock)
    events = len(world.events)
    monkeypatch.setattr(module, "SETTLE_SECONDS", 60.0)
    clock[0] += 61
    world.status = slot_with("#FAFAFA")
    world.status["webhooks"] = {"state": "shutdown"}
    clock[0] += module.RELEASE_AFTER_SECONDS
    run(driver._poll())
    assert len(world.events) == events
    world.status = slot_with("#FAFAFA")
    run(driver._poll())
    assert driver.health()["remembered_slot_spools"] == {"0-0": 9}
    clock[0] += 60
    world.status = slot_with("#F28636")
    run(driver._poll())
    assert driver._assigned == {0: 9}

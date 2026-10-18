"""Real template replay for loading and rewinding the pre-run bento inventory."""
import asyncio
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import player_recovery_pc_actions as recovery
from plans.resonance_pc.src.actions import bento_consumption_pc_actions as consumption
from plans.resonance_pc.src.actions.love_bento_pc_actions import load_love_bento_catalog
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(recovery.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return now


@pytest.fixture
def vision():
    service = VisionService()
    service._submit_to_loop_and_wait = asyncio.run
    return service


class Cabinet:
    def __init__(self, clock, layout, *, level=0, ready_at=0, ignored=False, fail=False):
        self.clock, self.layout = clock, layout
        self.level, self.ready_at, self.ignored, self.fail = level, ready_at, ignored, fail
        self.drag_calls = []
        self.visible = True
        self.after_drag = lambda: None
        top = np.array(Image.open("tests/fixtures/bento_consumption/work_usable.png").convert("RGB"))
        vx, vy, vw, vh = layout["bento_navigation"]["viewport"]
        self.frames = [top]
        row = top[350:537, vx:vx+vw]
        for shift in (0, 60):
            frame = top.copy()
            lower = np.tile(row, (4, 1, 1))[shift:shift+vh]
            frame[vy:vy+vh, vx:vx+vw] = lower
            self.frames.append(frame)

    def capture(self, rect):
        frame = self.frames[self.level].copy()
        if self.clock[0] < self.ready_at:
            x, y, w, h = self.layout["bento_navigation"]["viewport"]
            frame[y:y+h, x:x+w] = 128
        if not self.visible:
            frame[:] = 128
        x, y, w, h = rect
        return NS(success=True, image=frame[y:y+h, x:x+w].copy())

    def drag(self, x1, y1, x2, y2, **kwargs):
        assert self.clock[0] >= self.ready_at, "Do not drag before food content renders"
        assert self.visible
        assert x1 == x2 and y2 > y1, "Rewind requires a downward drag"
        vx, vy, vw, vh = self.layout["bento_navigation"]["viewport"]
        assert vx < x1 < vx + vw and vy < y1 < y2 < vy + vh
        self.drag_calls.append((x1, y1, x2, y2))
        if not self.ignored and not self.fail:
            self.level = max(0, self.level - 1)
        self.after_drag()
        return NS(success=not self.fail)


def reader_for(clock, vision, **options):
    layout = recovery.load_recovery_layout(vision)
    app = Cabinet(clock, layout, **options)
    return recovery.RecoveryReader(app, None, vision, layout), app


@pytest.fixture
def consumption_session(clock, vision, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Consumption navigation must not scan or prepare inventory")

    monkeypatch.setattr(recovery.RecoveryReader, "prepare_bento_read", forbidden)
    monkeypatch.setattr(recovery.RecoveryReader, "read_work_meals", forbidden)
    monkeypatch.setattr(consumption.LoveBentoScanner, "read", forbidden)

    def make(**options):
        recovery_layout = recovery.load_recovery_layout(vision)
        app = Cabinet(clock, recovery_layout, **options)
        app.clicks = []
        app.click = lambda **kwargs: app.clicks.append(kwargs)
        store = PersistentDataService(tmp_path)
        store.set("user-info.json", None, {"sentinel": "unchanged"})
        session = consumption.BentoConsumptionSession(
            app, None, vision, store, consumption.load_consumption_layout(vision),
            recovery_layout, load_love_bento_catalog(vision, navigation_only=True),
        )
        return session, app
    return make


def locate_work(session):
    return session.locate({"kind": "work_meals", "issue_time": "05:00"})


def assert_no_consumption(session, app):
    assert app.clicks == []
    assert session.items == []
    assert session.store.read("user-info.json") == {"sentinel": "unchanged"}


def test_consumption_stationary_middle_does_not_authorize_fixed_work_slot(consumption_session):
    session, app = consumption_session(level=2, ignored=True)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_top_not_found"
    assert len(app.drag_calls) == session.catalog["scanner"]["stationary_confirmations"]
    assert session.selected_roi is None
    assert_no_consumption(session, app)


def test_consumption_blank_stable_content_does_not_authorize_drag(consumption_session):
    session, app = consumption_session(ready_at=999)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_content_not_ready"
    assert not app.drag_calls
    assert_no_consumption(session, app)


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("ready_at", [0, 1.2])
def test_consumption_real_top_and_loading_preserve_slot_coordinates(
        consumption_session, clock, level, ready_at):
    session, app = consumption_session(level=level, ready_at=ready_at)
    assert locate_work(session) == [232, 212]
    assert app.level == 0
    assert len(app.drag_calls) == level + session.catalog["scanner"]["stationary_confirmations"]
    assert session.selected_roi == session.layout["work_selected_rois"][0]
    assert clock[0] >= ready_at
    assert_no_consumption(session, app)


def test_consumption_missing_one_work_slot_cannot_prove_top(consumption_session):
    session, app = consumption_session()
    x, y, w, h = session.reader.layout["bento_slots"][1]["roi"]
    app.frames[0][y:y+h, x:x+w] = 128
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_top_not_found"
    assert session.selected_roi is None
    assert_no_consumption(session, app)


def test_consumption_rendered_empty_work_slots_are_valid_top(consumption_session):
    session, app = consumption_session()
    frame = app.frames[0]
    vx, vy, vw, vh = session.catalog["scanner"]["viewport"]
    absent = frame[160:265, 620:785].copy()
    frame[vy:vy+vh, vx:vx+vw] = 128
    for slot in session.reader.layout["bento_slots"]:
        x, y, w, h = slot["roi"]
        frame[y:y+h, x:x+w] = absent
    assert locate_work(session) == [232, 212]
    assert len(app.drag_calls) == session.catalog["scanner"]["stationary_confirmations"]
    assert_no_consumption(session, app)


@pytest.mark.parametrize("during_drag", [False, True])
def test_consumption_cancel_loading_or_rewind_prevents_next_input(
        consumption_session, clock, monkeypatch, during_drag):
    session, app = consumption_session(level=2, ready_at=0 if during_drag else 999)
    monkeypatch.setattr(consumption, "is_current_task_cancel_requested",
                        lambda: bool(app.drag_calls) if during_drag else clock[0] >= .4)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_cancelled"
    assert len(app.drag_calls) == int(during_drag)
    assert_no_consumption(session, app)


def test_consumption_failed_drag_cannot_authorize_slot(consumption_session):
    session, app = consumption_session(level=2, fail=True)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_input_failed"
    assert len(app.drag_calls) == 1
    assert session.selected_roi is None
    assert_no_consumption(session, app)


def test_consumption_rewind_preserves_drag_limit(consumption_session, monkeypatch):
    session, app = consumption_session(level=2, ignored=True)
    session.catalog["scanner"]["max_drags"] = 4
    original = app.drag

    def drag(*args, **kwargs):
        result = original(*args, **kwargs)
        app.level = 1 if app.level == 2 else 2
        return result

    monkeypatch.setattr(app, "drag", drag)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_top_not_found"
    assert len(app.drag_calls) == 4
    assert_no_consumption(session, app)


def test_consumption_page_lost_after_drag_stops_further_input(consumption_session):
    session, app = consumption_session(level=2)
    app.after_drag = lambda: setattr(app, "visible", False)
    with pytest.raises(recovery.StopTaskException, match="cabinet disappeared"):
        locate_work(session)
    assert len(app.drag_calls) == 1
    assert session.selected_roi is None
    assert_no_consumption(session, app)


def test_consumption_total_timeout_still_bounds_content_wait(consumption_session, clock):
    session, app = consumption_session(ready_at=999)
    session.deadline = .4
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_timeout"
    assert clock[0] <= .5
    assert not app.drag_calls
    assert_no_consumption(session, app)


def test_consumption_late_top_observation_cannot_authorize_slot(
        consumption_session, clock, monkeypatch):
    session, app = consumption_session()
    original = session.reader._bento_top_visible

    def late_top(frame):
        assert original(frame)
        clock[0] = session.deadline
        return True

    monkeypatch.setattr(session.reader, "_bento_top_visible", late_top)
    with pytest.raises(consumption.BentoConsumptionError) as error:
        locate_work(session)
    assert error.value.code == "bento_timeout"
    assert session.selected_roi is None
    assert_no_consumption(session, app)


def test_title_without_navigation_is_not_loaded_cabinet(clock, vision):
    reader, app = reader_for(clock, vision)
    assert reader.is_page("bento_cabinet")
    spec = reader.layout["templates"]["bento_train"]
    x, y, w, h = spec["roi"]
    app.frames[0][y:y+h, x:x+w] = 128
    assert not reader.is_page("bento_cabinet")


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("ready_at", [0, 1.2])
def test_loaded_content_then_multiple_downward_drags_confirm_top(clock, vision, level, ready_at):
    reader, app = reader_for(clock, vision, level=level, ready_at=ready_at)
    reader.prepare_bento_read()
    assert app.level == 0
    assert len(app.drag_calls) == level + 3
    assert clock[0] >= ready_at
    result = reader.read_work_meals()
    assert result["available_count"] == 2
    assert [row["available"] for row in result["slots"]] == [True, True, False]
    assert "degraded" not in result


def test_header_and_blank_list_do_not_authorize_drag(clock, vision):
    reader, app = reader_for(clock, vision, ready_at=999)
    assert reader.is_page("bento_cabinet")
    with pytest.raises(recovery.StopTaskException, match="did not finish loading"):
        reader.prepare_bento_read()
    assert not app.drag_calls


def test_rendered_empty_work_slots_are_valid_content(clock, vision):
    reader, app = reader_for(clock, vision)
    frame = app.frames[0]
    vx, vy, vw, vh = reader.layout["bento_navigation"]["viewport"]
    absent = frame[160:265, 620:785].copy()
    frame[vy:vy+vh, vx:vx+vw] = 128
    for slot in reader.layout["bento_slots"]:
        x, y, w, h = slot["roi"]
        frame[y:y+h, x:x+w] = absent
    reader.prepare_bento_read()
    assert len(app.drag_calls) == 3
    assert reader.read_work_meals()["available_count"] == 0


def test_unchanged_bottom_after_ignored_drags_is_not_top(clock, vision):
    reader, app = reader_for(clock, vision, level=2, ignored=True)
    with pytest.raises(recovery.StopTaskException, match="stopped away from the top"):
        reader.prepare_bento_read()
    assert len(app.drag_calls) == 3
    assert app.level == 2


def test_love_food_icons_at_work_slot_positions_do_not_prove_top(clock, vision):
    reader, app = reader_for(clock, vision)
    vx, vy, vw, vh = reader.layout["bento_navigation"]["viewport"]
    for path in reader.layout["bento_navigation"]["food_templates"]:
        food = np.array(Image.open(path).convert("RGB"))
        frame = np.full((vh, vw, 3), 128, dtype=np.uint8)
        for slot in reader.layout["bento_slots"]:
            x, y, _, _ = slot["roi"]
            frame[y-vy:y-vy+food.shape[0], x-vx:x-vx+food.shape[1]] = food
        assert not reader._bento_top_visible(frame), path


def test_drag_failure_stops_without_assuming_top(clock, vision):
    reader, app = reader_for(clock, vision, level=2, fail=True)
    with pytest.raises(recovery.StopTaskException, match="rewind input failed"):
        reader.prepare_bento_read()
    assert len(app.drag_calls) == 1


def test_page_lost_during_rewind_does_not_trigger_more_drags(clock, vision):
    reader, app = reader_for(clock, vision, level=2)
    app.after_drag = lambda: setattr(app, "visible", False)
    with pytest.raises(recovery.StopTaskException, match="did not finish loading"):
        reader.prepare_bento_read()
    assert len(app.drag_calls) == 1


@pytest.mark.parametrize("during_drag", [False, True])
def test_cancel_loading_or_rewind_prevents_next_drag(clock, vision, monkeypatch, during_drag):
    reader, app = reader_for(clock, vision, level=2, ready_at=0 if during_drag else 999)
    monkeypatch.setattr(recovery, "is_current_task_cancel_requested",
                        lambda: bool(app.drag_calls) if during_drag else clock[0] >= .4)
    with pytest.raises(recovery.StopTaskException, match="cancelled"):
        reader.prepare_bento_read()
    assert len(app.drag_calls) == int(during_drag)


def test_no_inventory_result_is_saved_if_preparation_fails(clock, vision, monkeypatch):
    reader, _ = reader_for(clock, vision, level=2, ignored=True)
    monkeypatch.setattr(reader, "move", lambda *args: None)
    monkeypatch.setattr(reader, "restore_profile", lambda: None)
    saved = []
    with pytest.raises(recovery.StopTaskException, match="stopped away"):
        reader.read(["work_meals", "love_bentos"], on_updated=lambda key: saved.append(key),
                    on_result=lambda *args: saved.append(args), love_catalog={})
    assert saved == []


def test_rewind_stops_at_maximum_drag_count(clock, vision, monkeypatch):
    reader, app = reader_for(clock, vision, level=2, ignored=True)
    reader.layout["bento_navigation"]["max_drags"] = 4
    original = app.drag

    def drag(*args, **kwargs):
        result = original(*args, **kwargs)
        app.level = 1 if app.level == 2 else 2
        return result

    monkeypatch.setattr(app, "drag", drag)
    with pytest.raises(recovery.StopTaskException, match="within drag limit"):
        reader.prepare_bento_read()
    assert len(app.drag_calls) == 4


@pytest.mark.parametrize("sections", [["work_meals"], ["love_bentos"], ["work_meals", "love_bentos"]])
def test_reader_rewinds_once_before_selected_reads_and_replaces_degraded_result(clock, vision, monkeypatch, sections):
    from plans.resonance_pc.src.actions import love_bento_pc_actions as love
    reader, app = reader_for(clock, vision, level=2)
    monkeypatch.setattr(reader, "move", lambda *args: None)
    love_starts = []

    def scan(scanner):
        assert app.level == 0
        love_starts.append(len(app.drag_calls))
        return {"count": 0, "items": [], "updated_at": "now"}

    monkeypatch.setattr(love.LoveBentoScanner, "read", scan)
    saved = {"work_meals": {"available_count": 0, "degraded": True}}
    result = reader.read(sections, on_updated=lambda key: None,
                         on_result=lambda key, value: saved.__setitem__(key, value),
                         love_catalog={"scanner": {"total_timeout_sec": 90}})
    assert len(app.drag_calls) == 5
    assert set(result) == set(sections)
    assert love_starts == ([5] if "love_bentos" in sections else [])
    if "work_meals" in sections:
        assert result["work_meals"]["available_count"] == 2
        assert "degraded" not in saved["work_meals"]
    else:
        assert saved["work_meals"]["degraded"] is True

"""Closed-loop runtime tests with scripted observations and no game input."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import json
import time

import pytest

from plans.resonance_pc.src.actions import _eternal_scuffle_runtime as runtime


class Store:
    def __init__(self):
        self.values = {"unrelated": {"value": "keep"}}

    async def get(self, key):
        return deepcopy(self.values.get(key))

    async def set(self, key, value):
        self.values[key] = deepcopy(value)

    async def delete(self, key):
        self.values.pop(key, None)


class Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def frame(scene, **values):
    controls = {
        "play": {"center": [100, 100]}, "confirm": {"center": [200, 200]},
        "min": {"center": [300, 200]}, "max": {"center": [400, 200]},
        "plus": {"center": [500, 200]}, "start": {"center": [600, 200]},
        "back": {"center": [50, 50]},
    }
    return {"valid": True, "scene": scene, "controls": controls, **values}


class Observer:
    def __init__(self):
        self.current = frame("home")
        self.queued = deque()
        self.candidates = []
        self.team = []
        self.observations = 0

    def observe(self):
        self.observations += 1
        return deepcopy(self.queued.popleft() if self.queued else self.current)

    def read_candidates(self, observation, kind, include_spine=False):
        return deepcopy(self.candidates)

    def read_team(self, observation, include_spine=False):
        return deepcopy(self.team)

    def read_selected_character(self, observation):
        selected = [row for row in self.team if row.get("selected")]
        if not observation.get("valid") or observation.get("scene") != "assign" or len(selected) != 1:
            return None
        row = selected[0]
        return {"id": row["id"], "screen_index": row["screen_index"],
                "center": deepcopy(row["center"]), "score": 1.0, "marker_score": 1.0, "selected": True}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    observer, store, bus = Observer(), Store(), Bus()
    catalog = {
        "characters": [{"id": i, "rank": i, "rarity": "SSR"} for i in range(1, 7)],
        "equipment": [
            {"id": 10, "rank": 1, "slot_type": "attack"},
            {"id": 11, "rank": 2, "slot_type": "attack"},
            {"id": 12, "rank": 3, "slot_type": "attack"},
            {"id": 13, "rank": 4, "slot_type": "attack"},
            {"id": 20, "rank": 5, "slot_type": "defense"},
            {"id": 30, "rank": 6, "slot_type": "support"},
        ],
    }
    state = {
        "schema": runtime.SCHEMA, "cid": "runtime-test", "coins_per_run": 1,
        "run_count": 1, "run_index": 1, "completed_runs": 0, "cleared_runs": 0,
        "abandoned_runs": 0, "status": "running", "stage": "coin", "sequence": 0,
        "log_dir": str(tmp_path / "logs"), "round": {
            "index": 1, "phase": "coin", "team": [], "positions": [],
            "pending_role": None, "pending_loot": None, "outcome": None,
            "returned_home": False, "started_at": time.time(), "opened_boxes": 0,
        },
    }
    monkeypatch.setattr(runtime, "catalog_for", lambda engine=None: catalog)
    monkeypatch.setattr(runtime, "make_observer", lambda *args: observer)
    monkeypatch.setattr(runtime, "is_current_task_cancel_requested", lambda: False)

    async def sleep(_seconds):
        await asyncio.sleep(0)

    async def poll_until(*, timeout, interval, probe, predicate):
        result = None
        for _ in range(8):
            result = probe()
            if predicate(result):
                return True, result
            await asyncio.sleep(0)
        return False, result

    monkeypatch.setattr(runtime, "aura_sleep", sleep)
    monkeypatch.setattr(runtime, "poll_until", poll_until)
    machine = runtime.ScuffleRuntime(state, store, object(), object(), bus)
    machine.clicks = []
    machine.on_click = lambda point: None

    def click(*, app, x, y):
        machine.clicks.append([x, y])
        machine.on_click([x, y])

    monkeypatch.setattr(runtime, "aura_click", click)
    machine.fake_observer = observer
    return machine


def test_click_retries_only_when_original_control_still_visible(rig):
    def clicked(_point):
        if len(rig.clicks) == 2:
            rig.fake_observer.current = frame("coin")
    rig.on_click = clicked
    result = asyncio.run(rig.click_transition("home", "play", ("coin",)))
    assert result["scene"] == "coin"
    assert rig.clicks == [[100, 100], [100, 100]]


@pytest.mark.parametrize("disappearance", [frame("unknown"), frame("home", controls={})])
def test_control_disappearing_waits_for_next_scene_without_reclick(rig, disappearance):
    def clicked(_point):
        rig.fake_observer.queued.extend([disappearance] * 3)
        rig.fake_observer.current = frame("coin")
    rig.on_click = clicked
    asyncio.run(rig.click_transition("home", "play", ("coin",)))
    assert len(rig.clicks) == 1


def test_invalid_frames_are_neither_departure_nor_retry_evidence(rig):
    rig.on_click = lambda _: rig.fake_observer.queued.extend([{"valid": False}] * 20)
    with pytest.raises(runtime.ScuffleError, match="observation_timeout"):
        asyncio.run(rig.click_transition("home", "play", ("coin",), allow_departure=True))
    assert len(rig.clicks) == 1


def test_clicks_stop_after_four_confirmed_ineffective_attempts(rig):
    with pytest.raises(runtime.ScuffleError, match="click_ineffective"):
        asyncio.run(rig.click_transition("home", "play", ("coin",)))
    assert len(rig.clicks) == 4


@pytest.mark.parametrize("count", range(1, 6))
def test_coin_buttons_are_counted_once_without_reading_number(rig, count):
    rig.state["coins_per_run"] = count
    def clicked(point):
        if point == [100, 100]:
            rig.fake_observer.current = frame("coin")
        elif point == [200, 200]:
            rig.fake_observer.current = frame("role_select")
    rig.on_click = clicked
    asyncio.run(rig.coin())
    middle = [[400, 200]] if count == 5 else [[300, 200]] + [[500, 200]] * (count - 1)
    assert rig.clicks == [[100, 100], *middle, [200, 200]]
    assert rig.round["phase"] == "draft"
    assert rig.state["stage"] == "choose_role"
    assert rig.store.values[rig.key]["round"]["phase"] == "draft"


def candidates(ids):
    return [{"id": id_, "index": index, "select_point": [300 + index * 200, 500]} for index, id_ in enumerate(ids)]


def test_role_and_equipment_are_committed_only_after_confirmed_transition(rig):
    rig.round["phase"] = "draft"
    rig.fake_observer.current = frame("role_select")
    rig.fake_observer.candidates = candidates([3, 1, 2])
    def role_click(_point):
        assert rig.round["pending_role"] is None
        assert rig.round["team"] == []
        rig.fake_observer.current = frame("initial_equipment")
    rig.on_click = role_click
    asyncio.run(rig.choose_role())
    assert rig.round["pending_role"] == 1
    assert rig.round["team"] == []
    rig.fake_observer.candidates = candidates([12, 11, 10])
    def equipment_click(_point):
        assert rig.round["team"] == []
        assert rig.round["pending_role"] == 1
        rig.fake_observer.current = frame("role_select")
    rig.on_click = equipment_click
    asyncio.run(rig.choose_initial_equipment())
    assert rig.round["pending_role"] is None
    assert rig.round["team"] == [{"id": 1, "screen_index": 0, "equipment": {"attack": 10, "defense": None, "support": None}}]
    assert rig.clicks == [[500, 500], [700, 500]]


def test_failed_role_transition_never_commits_selected_identity(rig):
    rig.round["phase"] = "draft"
    rig.fake_observer.current = frame("role_select")
    rig.fake_observer.candidates = candidates([3, 1, 2])
    with pytest.raises(runtime.ScuffleError, match="click_ineffective"):
        asyncio.run(rig.choose_role())
    assert rig.round["pending_role"] is None
    assert rig.round["team"] == []
    assert rig.store.values[rig.key]["round"]["pending_role"] is None


def test_changed_candidates_after_click_are_not_reclicked_or_committed(rig):
    rig.round["phase"] = "draft"
    rig.fake_observer.current = frame("role_select")
    rig.fake_observer.candidates = candidates([3, 1, 2])
    def clicked(_point):
        rig.fake_observer.candidates = candidates([4, 5, 6])
    rig.on_click = clicked
    with pytest.raises(runtime.ScuffleError, match="observation_timeout"):
        asyncio.run(rig.choose_role())
    assert len(rig.clicks) == 1
    assert rig.round["pending_role"] is None
    assert rig.round["team"] == []


def test_fallback_recipient_uses_current_team_screen_order(rig):
    rig.round["phase"] = "assign"
    rig.round["team"] = [{"id": i, "screen_index": i - 1,
        "equipment": {"attack": 10, "defense": 20, "support": 30}} for i in range(1, 6)]
    rig.round["pending_loot"] = {"equipment_id": 11, "candidates": candidates([13, 12, 11])}
    rig.fake_observer.current = frame("assign")
    rig.fake_observer.team = [{"id": i, "screen_index": index, "center": [100 + index * 150, 300],
        "slots": {slot: "occupied" for slot in runtime.SLOT_TYPES}, "selected": False}
        for index, i in enumerate([5, 2, 3, 4, 1])]
    def clicked(point):
        if point == [100, 300]:
            rig.fake_observer.team[0]["selected"] = True
        elif point == [200, 200]:
            assert all(member["equipment"]["attack"] == 10 for member in rig.round["team"])
            rig.fake_observer.current = frame("stage")
    rig.on_click = clicked
    asyncio.run(rig.assign_loot())
    assert rig.clicks == [[100, 300], [200, 200]]
    equipped = {member["id"]: member["equipment"]["attack"] for member in rig.round["team"]}
    assert equipped == {1: 10, 2: 10, 3: 10, 4: 10, 5: 11}


def configure_assignment(rig, *, selected=False):
    rig.round["phase"] = "assign"
    rig.round["team"] = [{"id": i, "screen_index": i - 1,
        "equipment": {"attack": 10, "defense": 20, "support": 30}} for i in range(1, 6)]
    rig.round["pending_loot"] = {"equipment_id": 11, "candidates": candidates([13, 12, 11])}
    rig.fake_observer.current = frame("assign")
    rig.fake_observer.team = [{"id": i, "screen_index": index, "center": [100 + index * 150, 300],
        "slots": {slot: "occupied" for slot in runtime.SLOT_TYPES}, "selected": selected and index == 0}
        for index, i in enumerate([5, 2, 3, 4, 1])]


def test_two_second_selected_confirmation_never_reads_full_team(rig, monkeypatch):
    configure_assignment(rig, selected=True)
    observed = []
    original = rig.fake_observer.read_selected_character

    def selected(observation):
        observed.append(observation["scene"])
        return original(observation)

    monkeypatch.setattr(rig.fake_observer, "read_selected_character", selected)
    monkeypatch.setattr(rig.fake_observer, "read_team", lambda *args, **kwargs: pytest.fail("2-second selection confirmation must not scan the full team"))
    result = asyncio.run(rig.selected_character(5, 0, timeout=2))
    assert result["id"] == 5
    assert result["screen_index"] == 0
    assert len(observed) >= 3


def test_already_selected_recipient_only_clicks_confirm(rig):
    configure_assignment(rig, selected=True)

    def clicked(point):
        assert point == [200, 200]
        rig.fake_observer.current = frame("stage")

    rig.on_click = clicked
    asyncio.run(rig.assign_loot())
    assert rig.clicks == [[200, 200]]
    assert next(row for row in rig.round["team"] if row["id"] == 5)["equipment"]["attack"] == 11


def test_clicking_wrong_character_never_confirms_equipment(rig):
    configure_assignment(rig)

    def clicked(point):
        assert point == [100, 300]
        # The input was sent to the intended point, but the game selected a
        # different card. Do not equate 'some SELECT exists' with success.
        rig.fake_observer.team[1]["selected"] = True

    rig.on_click = clicked
    with pytest.raises(runtime.ScuffleError):
        asyncio.run(rig.assign_loot())
    assert 1 <= len(rig.clicks) <= 4
    assert [200, 200] not in rig.clicks
    assert all(row["equipment"]["attack"] == 10 for row in rig.round["team"])
    assert rig.round["pending_loot"] is not None


@pytest.mark.parametrize("change", ["marker_disappears", "different_character", "different_position"])
def test_final_confirmation_rechecks_selected_identity_and_position(rig, monkeypatch, change):
    configure_assignment(rig, selected=True)
    original_record = rig.record

    async def record(kind, **fields):
        await original_record(kind, **fields)
        if kind == "assignment_intent":
            if change == "marker_disappears":
                rig.fake_observer.team[0]["selected"] = False
            elif change == "different_character":
                rig.fake_observer.team[0]["selected"] = False
                rig.fake_observer.team[1]["selected"] = True
            else:
                rig.fake_observer.team[0]["screen_index"] = 1

    monkeypatch.setattr(rig, "record", record)
    with pytest.raises(runtime.ScuffleError):
        asyncio.run(rig.assign_loot())
    assert rig.clicks == []
    assert all(row["equipment"]["attack"] == 10 for row in rig.round["team"])


@pytest.mark.parametrize("total,already_open", [(4, 0), (7, 0), (7, 2)])
def test_shared_box_loop_opens_only_remaining_boxes_once(rig, total, already_open):
    rig.round["phase"] = "ready_settlement"
    all_points = [[100 + 140 * i, 450] for i in range(total)]
    remaining = deepcopy(all_points[already_open:])
    def refresh():
        rig.fake_observer.current = frame("settlement", boxes=[{"center": point} for point in remaining],
            controls={} if remaining else {"back": {"center": [50, 50]}})
    refresh()
    def clicked(point):
        assert point in remaining
        remaining.remove(point)
        refresh()
    rig.on_click = clicked
    async def execute():
        for _ in range(total - already_open + 1):
            await rig.open_box()
    asyncio.run(execute())
    assert rig.clicks == all_points[already_open:]
    assert rig.round["opened_boxes"] == total - already_open
    assert rig.round["phase"] == "return_home"


def test_cancel_after_first_click_prevents_retry_and_coin_confirmation(rig, monkeypatch):
    cancelled = False
    monkeypatch.setattr(runtime, "is_current_task_cancel_requested", lambda: cancelled)
    def clicked(_point):
        nonlocal cancelled
        cancelled = True
    rig.on_click = clicked
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.coin())
    assert rig.clicks == [[100, 100]]
    assert rig.round["phase"] == "coin"


def test_action_wrapper_cancellation_persists_diagnostics_without_later_clicks(rig, monkeypatch):
    from plans.resonance_pc.src.actions.eternal_scuffle_pc_actions import eternal_scuffle_coin
    cancelled = False
    monkeypatch.setattr(runtime, "is_current_task_cancel_requested", lambda: cancelled)
    def clicked(_point):
        nonlocal cancelled
        cancelled = True
    rig.on_click = clicked
    asyncio.run(rig.store.set(rig.key, rig.state))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(eternal_scuffle_coin(session_key=rig.key, app=rig.app, vision=rig.vision,
                                        state_store=rig.store, event_bus=rig.event_bus))
    assert rig.clicks == [[100, 100]]
    saved = rig.store.values[rig.key]
    assert saved["status"] == "cancelled"
    assert saved["completed_runs"] == 0
    assert saved["stage"] == "coin"
    assert saved["round"]["phase"] == "coin"
    failure = json.loads((rig.log_dir / "failure.json").read_text(encoding="utf-8"))
    assert failure["state"]["status"] == "cancelled"
    assert failure["last_target"]["label"] == "play"
    assert rig.event_bus.events[-1].payload["status"] == "cancelled"


@pytest.mark.parametrize("result", [None, {}, {"nodes": {"finish": {"output": {"success": False, "status": "completed"}}}},
    {"nodes": {"finish": {"output": {"success": True, "status": "failed"}}}},
    {"user_data": {"success": True, "status": "completed"}}])
def test_child_checkpoint_rejects_absent_or_failed_framework_result(rig, result):
    with pytest.raises(runtime.ScuffleError, match="child_incomplete"):
        asyncio.run(rig.checkpoint(require_child=True, child_result=result))


def test_complete_round_requires_home_and_commits_exactly_once(rig):
    rig.round.update(phase="home", returned_home=True, outcome="cleared")
    rig.fake_observer.current = frame("settlement")
    with pytest.raises(runtime.ScuffleError, match="observation_timeout"):
        asyncio.run(rig.complete_round())
    assert rig.state["completed_runs"] == 0
    rig.fake_observer.current = frame("home")
    result = asyncio.run(rig.complete_round())
    assert result["success"] is True
    assert rig.state["completed_runs"] == 1
    with pytest.raises(runtime.ScuffleError):
        asyncio.run(rig.complete_round())
    assert rig.state["completed_runs"] == 1
    events = [json.loads(line) for line in (rig.log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([row for row in events if row["type"] == "round_completed"]) == 1


def test_finish_checks_journal_and_deletes_only_own_session(rig):
    rig.round.update(phase="home", returned_home=True, outcome="abandoned")
    asyncio.run(rig.complete_round())
    asyncio.run(rig.store.set("eternal_scuffle:another-cid", {"other": True}))
    result = asyncio.run(runtime.finish(rig.key, rig.store, rig.event_bus))
    assert result["completed_runs"] == 1
    assert result["abandoned_runs"] == 1
    assert result["rounds"][0]["outcome"] == "abandoned"
    assert rig.key not in rig.store.values
    assert rig.store.values["unrelated"] == {"value": "keep"}
    assert rig.store.values["eternal_scuffle:another-cid"] == {"other": True}
    assert json.loads((rig.log_dir / "summary.json").read_text(encoding="utf-8")) == result
    assert rig.event_bus.events[-1].payload["status"] == "completed"


def test_finish_rejects_missing_round_journal_and_preserves_session(rig):
    rig.round.update(phase="home", returned_home=True, outcome="cleared")
    asyncio.run(rig.complete_round())
    (rig.log_dir / "events.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(runtime.ScuffleError, match="journal_incomplete"):
        asyncio.run(runtime.finish(rig.key, rig.store))
    assert rig.key in rig.store.values


def test_finish_returns_summary_and_cleans_session_when_progress_bus_fails(rig):
    rig.round.update(phase="home", returned_home=True, outcome="cleared")
    asyncio.run(rig.complete_round())

    class FailingBus:
        async def publish(self, event):
            raise RuntimeError("simulated progress transport failure")

    result = asyncio.run(runtime.finish(rig.key, rig.store, FailingBus()))
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["completed_runs"] == 1
    assert result["rounds"][0]["outcome"] == "cleared"
    assert rig.key not in rig.store.values
    assert rig.store.values["unrelated"] == {"value": "keep"}
    assert json.loads((rig.log_dir / "summary.json").read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("successful_click", [1, 2])
def test_real_vision_candidate_click_guards_run_off_event_loop(rig, monkeypatch, successful_click):
    """Exercise the sync VisionService bridge in initial and retry click guards.

    Both capture frames and matching are real production inputs/code. Only the
    input boundary is replaced: clicking SELECT swaps screenshot 03 for 04.
    """
    from pathlib import Path
    import threading
    from types import SimpleNamespace

    import numpy as np
    from PIL import Image

    from plans.aura_base.src.actions._shared import poll_until
    from plans.aura_base.src.services.vision_service import VisionService
    from plans.resonance_pc.src.actions._eternal_scuffle_vision import ScuffleVision

    root = Path(__file__).resolve().parents[2]
    plan = root / "plans/resonance_pc"
    fixtures = root / "tests/fixtures/eternal_scuffle"
    role_image = np.array(Image.open(fixtures / "03.png").convert("RGB"))
    equipment_image = np.array(Image.open(fixtures / "04.png").convert("RGB"))
    capture = SimpleNamespace(success=True, image=role_image, quality_flags=[])
    app = SimpleNamespace(capture=lambda: capture)
    catalog = json.loads((plan / "data/meta/eternal_scuffle.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(runtime, "poll_until", poll_until)

    async def run():
        loop_thread = threading.get_ident()
        vision = VisionService()
        vision._loop = asyncio.get_running_loop()
        observer = ScuffleVision(app, vision, catalog, plan)
        rig.app, rig.vision, rig.observer, rig.catalog = app, vision, observer, catalog
        candidate_threads = []
        original_read = observer.read_candidates

        def read_candidates(*args, **kwargs):
            candidate_threads.append((len(rig.clicks), threading.get_ident()))
            return original_read(*args, **kwargs)

        monkeypatch.setattr(observer, "read_candidates", read_candidates)
        observation = await asyncio.to_thread(observer.observe)
        assert observation["scene"] == "role_select"
        rows = await asyncio.to_thread(observer.read_candidates, observation, "role")
        assert [r["id"] for r in rows] == [10000890, 10000154, 10001356]
        candidate_threads.clear()

        def clicked(point):
            assert point == rows[2]["select_point"]
            if len(rig.clicks) == successful_click:
                capture.image = equipment_image

        rig.on_click = clicked
        await rig.select_candidate("role_select", "role", rows, rows[2], "initial_equipment")
        assert rig.last_observation["scene"] == "initial_equipment"
        assert len(rig.clicks) == successful_click
        assert candidate_threads and all(thread != loop_thread for _, thread in candidate_threads)
        assert any(click_count == 0 for click_count, _ in candidate_threads)
        if successful_click == 2:
            assert any(click_count == 1 for click_count, _ in candidate_threads)

    asyncio.run(run())

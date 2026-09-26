"""Safety contracts for the board-only Deep Dive test workflow."""

from plans.resonance_pc.src.actions.consciousness_deep_dive_single_run_pc_actions import (
    _advance_state,
)
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as actions
import asyncio


def _state():
    return {
        "status": "running", "phase": "player_ready", "phase_started": 0,
        "turns_completed": 0, "round_budget": 5, "random_seed": 0,
        "choices_made": 0, "last_choice": None, "move_history": [],
        "events": [], "rotations": [], "outcome": "unknown",
    }


def _observation(scene, **fields):
    return {
        "valid": True, "scene": scene, "player_turn": True,
        "move_pending": False, "move_done": False,
        "rotate_pending": False, "rotate_done": False,
        "options": [], **fields,
    }


def test_clicking_move_and_event_entry_does_not_confirm_move():
    state = _state()
    action, _ = _advance_state(state, _observation("board", move_pending=True))
    assert action == "click_move_button"
    _advance_state(state, _observation("choose_move", options=[{"point": [500, 300]}]))
    assert state["phase"] == "move_followup"
    assert state["move_history"] == []
    _advance_state(state, _observation("event_entry", event_type="item", click=[1090, 542]))
    action, _ = _advance_state(state, _observation("event_entry", event_type="item", click=[1090, 542]))
    assert action == "click_event_enter"
    assert state["phase"] == "event_dispatch"
    assert state["move_history"] == []
    assert state["turns_completed"] == 0


def test_ambiguous_confirmation_card_cannot_confirm_move():
    state = _state()
    state["phase"] = "move_followup"
    state["pending_move"] = {"point": [500, 300], "event": None}
    action, point = _advance_state(
        state, _observation("ambiguous_event_entry", move_done=True)
    )
    assert (action, point) == ("wait", None)
    assert state["move_history"] == []


def test_move_and_rotation_require_separate_confirmations(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(actions.time, 'time', lambda: clock[0])
    state = _state()
    state["phase"] = "move_followup"
    state["pending_move"] = {"point": [500, 300], "event": None}
    _advance_state(state, _observation("board", move_done=True, rotate_pending=True))
    assert len(state["move_history"]) == 1
    assert state["turns_completed"] == 0
    _advance_state(state, _observation("choose_rotate", move_done=True,
                                       options=[{"point": [540, 140]}]))
    _advance_state(state, _observation("choose_rotate", move_done=True,
                                       options=[{"point": [540, 140]}]))
    assert state["phase"] == "rotate_preview"
    assert state["turns_completed"] == 0
    action, point = _advance_state(state, _observation("rotate_preview", click=[788, 542]))
    assert action == "click_rotate_confirm" and point == [788, 542]
    assert state["turns_completed"] == 0
    _advance_state(state, _observation("board", move_done=True, rotate_done=True))
    assert state['turns_completed'] == 0
    clock[0] += 1.1
    _advance_state(state, _observation('board', move_pending=True, rotate_pending=True))
    assert state["turns_completed"] == 1
    assert state["phase"] == "enemy_wait"


def test_settlement_preempts_event_and_leaves_one_run_completed():
    state = _state()
    state["phase"] = "event_wait"
    state["pending_move"] = {"point": [500, 300], "event": "vortex"}
    action, point = _advance_state(state, _observation("settlement", outcome="failure"))
    assert action == "settlement" and point is None
    assert state["status"] == "completed"
    assert state["outcome"] == "failure"
    assert state["move_history"] == []


def test_budget_wait_never_issues_another_move():
    state = _state()
    state["phase"] = "budget_wait"
    state["turns_completed"] = state["round_budget"]
    action, point = _advance_state(state, _observation("board", move_pending=True, rotate_pending=True))
    assert action == "wait" and point is None


def test_event_transition_frame_cannot_be_mistaken_for_completion(monkeypatch):
    state = _state()
    state["phase"] = "event_wait"
    state["phase_started"] = 100.0
    state["pending_move"] = {"point": [500, 300], "event": "vortex"}
    monkeypatch.setattr(actions.time, "time", lambda: 101.0)
    action, point = _advance_state(state, _observation("board", move_done=True))
    assert (action, point) == ("wait", None)
    assert state["move_history"] == []
    action, point = _advance_state(state, _observation("event_vortex_choice"))
    assert (action, point) == ("unsupported_event", None)
    assert state["phase"] == "event_wait"


def test_board_to_settlement_replay_never_runs_entry_flow(monkeypatch):
    class App:
        def get_window_size(self):
            return 1280, 720

    class Store:
        def __init__(self):
            self.data = {}

        async def set(self, key, value):
            self.data[key] = value

        async def get(self, key):
            return self.data.get(key)

        async def delete(self, key):
            self.data.pop(key, None)

    frames = [
        _observation("board", move_pending=True),
        _observation("board", move_pending=True),
        _observation("choose_move", options=[{"point": [500, 300]}]),
        _observation("choose_rotate", move_done=True, options=[{"point": [540, 140]}]),
        _observation("choose_rotate", move_done=True, options=[{"point": [540, 140]}]),
        _observation("choose_rotate", move_done=True, options=[{"point": [540, 140]}]),
        _observation("rotate_preview", click=[788, 542]),
        _observation("settlement", outcome="failure"),
    ]
    clicked = []

    async def click(_app, point):
        clicked.append(point)

    monkeypatch.setattr(actions, "_capture", lambda _app: (object(), frames.pop(0)))
    monkeypatch.setattr(actions, "_click", click)
    monkeypatch.setattr(actions, "_save_frame", lambda *_args: ".pytest_tmp/settlement.png")
    async def replay():
        store = Store()
        start = await actions.initialize_deep_dive_single_run(app=App(), state_store=store)
        assert start["status"] == "running"
        for _ in range(7):
            step = await actions.advance_deep_dive_single_run(
                session_key=start["session_key"], app=App(), state_store=store
            )
            if step["status"] == "completed":
                break
        return await actions.finish_deep_dive_single_run(
            session_key=start["session_key"], app=App(), state_store=store
        )

    result = asyncio.run(replay())
    assert result["status"] == "completed"
    assert result["terminal"] == "settlement"
    assert result["turns_completed"] == 1
    assert clicked == [[1100, 406], [500, 300], [540, 140], [788, 542]]

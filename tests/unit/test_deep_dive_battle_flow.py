"""Actual screenshot recognition and battle handoff in the existing movement task."""
from pathlib import Path

import cv2
import pytest

from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe
from plans.resonance_pc.src.actions._deep_dive_battle_vision import detect_battle_page
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime

IMAGES = Path(__file__).resolve().parents[2] / "docs/developer-guide/images/resonance-pc-deep-dive-simple"


def frame(name, brightness=1.):
    image = cv2.imread(str(IMAGES / name))
    assert image is not None
    if image.shape[:2] == (802, 1332):
        image = image[56:776, 25:1305]
    return cv2.cvtColor(cv2.convertScaleAbs(image, alpha=brightness), cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize("brightness", [.8, 1., 1.2])
@pytest.mark.parametrize("name,scene", [
    ("32-battle-formation.png", "battle_formation"),
    ("33-battle-victory.png", "battle_victory"),
    ("34-battle-reward-choices.png", "battle_reward_selection"),
    ("35-battle-reward-middle-selected.png", "battle_reward_selection"),
    ("36-battle-reward-obtained.png", "reward_obtained"),
])
def test_battle_screens(name, scene, brightness):
    result = observe(frame(name, brightness))
    assert result["scene"] == scene
    if name.startswith("34") or name.startswith("35"):
        assert len(result["cards"]) == 3
        assert result["cards"][1]["point"] == [641, 340]
        assert result["selected_count"] == int(name.startswith("35"))
        assert result["cards"][1]["selected"] is name.startswith("35")


@pytest.mark.parametrize("kind", ["battle", "elite_battle"])
def test_victory_rewards_finish_move_then_resume_rotation(monkeypatch, kind):
    clock = [100.]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    state = {"phase": "move_followup", "phase_started": clock[0], "status": "running",
             "pending_move": {"event": None, "point": [500, 300]},
             "move_history": [], "events": [], "turns_completed": 0, "rotations": []}
    entry = {"valid": True, "scene": "event_entry", "event_type": kind,
             "entry_kind": kind, "click": [1090, 542]}
    assert runtime._advance_state(state, entry)[0] == "transition"
    assert runtime._advance_state(state, entry)[0] == "click_battle_node_enter"
    assert state["phase"] == "battle_prepare"

    def stable(observation):
        clock[0] += 2
        assert runtime._advance_state(state, observation) == ("wait", None)
        clock[0] += .4
        return runtime._advance_state(state, observation)

    assert stable(observe(frame("32-battle-formation.png")))[0] == "click_battle_start"
    assert runtime._advance_state(state, {"valid": True, "scene": "unknown"}) == ("wait", None)
    assert stable(observe(frame("33-battle-victory.png")))[0] == "click_battle_next"
    assert state["move_history"] == []
    assert stable(observe(frame("34-battle-reward-choices.png"))) == ("click_middle_reward", [641, 340])
    # A stale unselected frame never triggers confirmation or a second card click.
    assert runtime._advance_state(state, observe(frame("34-battle-reward-choices.png"))) == ("wait", None)
    assert stable(observe(frame("35-battle-reward-middle-selected.png")))[0] == "click_reward_confirm"
    notice = observe(frame("36-battle-reward-obtained.png"))
    assert stable(notice)[0] == "dismiss_battle_reward"
    assert runtime._advance_state(state, notice) == ("wait", None)
    assert runtime._advance_state(state, notice) == ("wait", None)
    assert state["move_history"] == []
    board = {"valid": True, "scene": "board", "player_turn": True, "move_done": True}
    assert stable(board)[0] == "battle_completed"
    assert state["phase"] == "rotate_ready"
    assert len(state["move_history"]) == 1
    assert state["turns_completed"] == 0
    assert state["events"][0]["type"] == kind


def test_battle_settlement_preempts_rewards():
    state = {"phase": "battle_obtain", "status": "running"}
    result = runtime._advance_state(state, {"valid": True, "scene": "settlement", "outcome": "failure"})
    assert result == ("settlement", None)
    assert state["status"] == "completed"


@pytest.mark.parametrize("name", [
    "01-board.png", "03-rotate-options.png", "04-settlement.png",
    "15-healing-stone-three-options.png", "16-healing-stone-option-selected.png",
    "17-push-discard-selection.png", "18-push-discard-selected.png",
    "19-push-discard-result.png", "20-healing-stone-event-ending.png",
    "21-purple-workshop-options-disabled.png", "23-shop-exit-only.png",
])
def test_other_flow_pages_do_not_trigger_battle_controls(name):
    assert detect_battle_page(frame(name)) is None

"""Shop entry/back handling never buys and only commits a verified return."""
from pathlib import Path

import cv2
import pytest

from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe
from plans.resonance_pc.src.actions import consciousness_deep_dive_single_run_pc_actions as runtime

IMAGES = Path(__file__).resolve().parents[2] / "docs/developer-guide/images/resonance-pc-deep-dive-simple"


def frame(name, brightness=1):
    image = cv2.imread(str(IMAGES / name))
    assert image is not None
    if image.shape[:2] == (802, 1332):
        image = image[56:776, 25:1305]
    return cv2.cvtColor(cv2.convertScaleAbs(image, alpha=brightness), cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize("name", ["23-shop-exit-only.png", "37-shop-exit-only-alternate-stock.png"])
@pytest.mark.parametrize("brightness", [.8, 1., 1.2])
def test_shop_recognition_ignores_stock_and_currency(name, brightness):
    result = observe(frame(name, brightness))
    assert result["scene"] == "shop"
    assert 40 < result["click"][0] < 125
    assert 15 < result["click"][1] < 80


@pytest.mark.parametrize("name", [
    "01-board.png", "29-node-preview-shop.png", "32-battle-formation.png",
    "33-battle-victory.png", "34-battle-reward-choices.png",
    "20-healing-stone-event-ending.png", "21-purple-workshop-options-disabled.png",
])
def test_other_back_buttons_do_not_make_a_shop(name):
    assert observe(frame(name))["scene"] != "shop"


def test_shop_title_alone_does_not_supply_a_back_click():
    image = frame("37-shop-exit-only-alternate-stock.png")
    image[5:100, 20:155] = 0
    result = observe(image)
    assert result["scene"] != "shop"


def test_shop_chain_only_enters_and_returns_then_resumes_rotation(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    state = {"phase": "move_followup", "pending_move": {"event": None},
             "move_history": [], "events": [], "turns_completed": 0}
    entry = observe(frame("29-node-preview-shop.png"))
    assert runtime._advance_state(state, entry) == ("transition", None)
    assert runtime._advance_state(state, entry)[0] == "click_shop_node_enter"
    assert state["phase"] == "shop_wait"
    shop = observe(frame("37-shop-exit-only-alternate-stock.png"))
    assert runtime._advance_state(state, shop) == ("wait", None)
    action, point = runtime._advance_state(state, shop)
    assert action == "click_shop_back" and point[0] < 125 and point[1] < 80
    assert runtime._advance_state(state, shop) == ("wait", None)
    assert state["move_history"] == []
    clock[0] += 2
    board = {"valid": True, "scene": "board", "player_turn": True,
             "move_done": True, "rotate_pending": True}
    assert runtime._advance_state(state, board) == ("wait", None)
    assert runtime._advance_state(state, {"valid": False, "scene": "capture_failed"}) == ("wait", None)
    assert runtime._advance_state(state, board) == ("wait", None)
    assert runtime._advance_state(state, board) == ("shop_completed", None)
    assert state["phase"] == "rotate_ready"
    assert len(state["move_history"]) == 1
    assert state["turns_completed"] == 0
    assert state["events"] == [{"type": "shop", "status": "completed", "purchased": False}]


def test_settlement_interrupts_shop_return():
    state = {"phase": "shop_return", "status": "running"}
    assert runtime._advance_state(state, {"scene": "settlement", "outcome": "failure"}) == ("settlement", None)
    assert state["status"] == "completed"

"""Real-frame checks for pre-entry node type classification and safe handoff."""

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions._deep_dive_entry_card_vision import classify_entry_card
from plans.resonance_pc.src.actions._deep_dive_single_run_vision import observe
from plans.resonance_pc.src.actions.consciousness_deep_dive_single_run_pc_actions import _advance_state
from plans.resonance_pc.src.actions.consciousness_deep_dive_entry_card_probe_pc_actions import (
    probe_deep_dive_entry_card,
)


_IMAGE_DIR = Path(__file__).resolve().parents[2] / "docs" / "developer-guide" / "images" / "resonance-pc-deep-dive-simple"
_PREVIEWS = {
    "24-node-preview-empty.png": "empty",
    "25-node-preview-adventure.png": "adventure",
    "26-node-preview-reward.png": "reward",
    "27-node-preview-battle.png": "battle",
    "28-node-preview-elite-battle.png": "elite_battle",
    "29-node-preview-shop.png": "shop",
    "30-node-preview-heal.png": "healing",
    "09-move-event-item.png": "reward",
    "10-move-event-vortex.png": "adventure",
}


def _client_rgb(name: str) -> np.ndarray:
    image = cv2.imread(str(_IMAGE_DIR / name))
    assert image is not None
    if image.shape[:2] == (802, 1332):
        image = image[56:776, 25:1305]
    assert image.shape[:2] == (720, 1280)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


@pytest.mark.parametrize("name,kind", _PREVIEWS.items())
@pytest.mark.parametrize("brightness", (.8, 1.0, 1.2))
def test_known_confirmation_cards_have_unambiguous_type_evidence(name, kind, brightness):
    frame = cv2.convertScaleAbs(_client_rgb(name), alpha=brightness)
    result = classify_entry_card(frame)
    assert result["status"] == "recognized"
    assert result["kind"] == kind
    assert result["score_margin"] >= .08
    assert result["enter_point"] is not None


@pytest.mark.parametrize("name", (
    "01-board.png", "02-move-options.png", "03-rotate-options.png",
    "14-yellow-reward-popup.png", "15-healing-stone-three-options.png", "23-shop-exit-only.png",
))
def test_non_confirmation_pages_have_no_entry_card(name):
    assert classify_entry_card(_client_rgb(name))["status"] == "absent"


def test_conflicting_title_and_icon_stays_ambiguous():
    reward = _client_rgb("26-node-preview-reward.png")
    adventure = _client_rgb("25-node-preview-adventure.png")
    reward[105:280, 980:1230] = adventure[105:280, 980:1230]
    result = classify_entry_card(reward)
    assert result["status"] == "ambiguous"
    assert result["kind"] is None


@pytest.mark.parametrize("brightness", (.8, 1.0, 1.2))
def test_live_elite_card_with_new_enemy_portrait_is_recognized(brightness):
    frame = cv2.convertScaleAbs(
        _client_rgb("31-live-elite-preview-variable-portrait.png"), alpha=brightness
    )
    result = classify_entry_card(frame)
    assert result["status"] == "recognized"
    assert result["kind"] == "elite_battle"
    assert result["evidence_mode"] == "enter_and_title_with_variable_enemy_portrait"


def test_normal_battle_type_does_not_depend_on_enemy_portrait():
    battle = _client_rgb("27-node-preview-battle.png")
    other_portrait = _client_rgb("28-node-preview-elite-battle.png")
    battle[105:280, 980:1230] = other_portrait[105:280, 980:1230]
    result = classify_entry_card(battle)
    assert result["status"] == "recognized"
    assert result["kind"] == "battle"


def test_healing_kind_is_recognized_and_routed_without_clicking_yet():
    frame = _client_rgb("30-node-preview-heal.png")
    observation = observe(frame)
    assert observation["scene"] == "event_entry"
    assert observation["entry_kind"] == "healing"
    state = {"phase": "move_followup", "pending_move": {"event": None}}
    action, point = _advance_state(state, observation)
    assert action == "transition"
    assert point is None
    assert state["pending_move"]["event"] == "healing"


@pytest.mark.parametrize("name,event_type", (
    ("09-move-event-item.png", "item"),
    ("10-move-event-vortex.png", "vortex"),
))
def test_existing_entry_handlers_keep_their_event_types(name, event_type):
    observation = observe(_client_rgb(name))
    assert observation["scene"] == "event_entry"
    assert observation["event_type"] == event_type


def test_probe_only_captures_and_never_sends_input():
    class App:
        def __init__(self):
            self.capture_calls = 0

        def capture(self):
            self.capture_calls += 1
            return SimpleNamespace(success=True, image=_client_rgb("27-node-preview-battle.png"))

        def click(self, *_args, **_kwargs):
            raise AssertionError("read-only probe must never click")

    app = App()
    result = probe_deep_dive_entry_card(app=app)
    assert app.capture_calls == 1
    assert result["success"] is True
    assert result["status"] == "recognized"
    assert result["kind"] == "battle"

"""Known card bounds are retained; selection and levels must stabilize together."""

from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment
from plans.resonance_pc.src.actions._trade_goods_investment_vision import (
    InvestmentRecognitionError,
    InvestmentVision,
)


CARD = {"rect": (62, 154, 114, 129), "partial": False, "occluded": False,
        "selected": False, "locked": False, "maximum": False}
LEVELS = {"current": 0, "preview": 1, "max_level": False}


@pytest.fixture
def select_rig(monkeypatch):
    state = SimpleNamespace(now=0., captures=0, observations=[], active={},
                            selection_reads=0, level_reads=0, clicks=[])

    def capture(app):
        state.active = state.observations[min(state.captures, len(state.observations) - 1)]
        state.captures += 1
        return np.zeros((720, 1280, 3), np.uint8)

    class Reader:
        def match(self, frame, name):
            assert name == "success"
            return {"found": state.active.get("toast", False)}

        def read_card_selection(self, frame, card):
            state.selection_reads += 1
            assert card is CARD
            return {**card, "selected": state.active.get("selected", True),
                    "maximum": state.active.get("maximum", False),
                    "frame_score": state.captures / 100.}

        def read_levels(self, frame):
            state.level_reads += 1
            if state.active.get("unreadable"):
                raise InvestmentRecognitionError("level_uncertain", "Delayed panel")
            return state.active.get("levels", LEVELS)

        def detect_cards(self, frame):
            pytest.fail("Selection must not relocate cards or fall back to a list scan")

    app = SimpleNamespace(click=lambda **point: state.clicks.append(point))
    reader = Reader()
    monkeypatch.setattr(investment, "_capture", capture)
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    monkeypatch.setattr(investment.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(investment, "_pause", lambda seconds: setattr(state, "now", state.now + seconds))
    monkeypatch.setattr(investment, "_levels", lambda *args: pytest.fail("Do not start a second level wait"))
    state.run = lambda: investment._select(app, reader, CARD)
    return state


def test_selection_and_levels_share_two_observations(select_rig):
    state = select_rig
    state.observations = [{}, {}]
    _, card, levels = state.run()
    assert card["rect"] == CARD["rect"] and card["selected"]
    assert levels == LEVELS
    assert state.captures == state.selection_reads == state.level_reads == 2
    assert state.clicks == [{"x": 119, "y": 218}]
    assert state.now == pytest.approx(.6)


def test_not_selected_does_not_accept_right_panel_levels(select_rig):
    state = select_rig
    state.observations = [{"selected": False}, {}, {}]
    state.run()
    assert state.captures == 3 and state.level_reads == 2
    assert len(state.clicks) == 1


def test_changed_levels_reset_joint_stability(select_rig):
    state = select_rig
    changed = {"current": 4, "preview": 5, "max_level": False}
    state.observations = [{}, {"levels": changed}, {}, {}]
    _, _, levels = state.run()
    assert state.captures == 4 and levels == LEVELS


def test_lost_selection_resets_joint_stability(select_rig):
    state = select_rig
    state.observations = [{}, {"selected": False}, {}, {}]
    state.run()
    assert state.captures == 4 and state.level_reads == 3


@pytest.mark.parametrize("interruption", [{"toast": True}, {"unreadable": True}])
def test_interrupted_observation_must_stabilize_again(select_rig, interruption):
    state = select_rig
    state.observations = [{}, interruption, {}, {}]
    state.run()
    assert state.captures == 4 and len(state.clicks) == 1


def test_maximum_badge_and_panel_accept_covered_selection_corner(select_rig):
    state = select_rig
    maximum = {"selected": False, "maximum": True,
               "levels": {"current": 20, "preview": None, "max_level": True}}
    state.observations = [maximum, maximum]
    _, card, levels = state.run()
    assert card["maximum"] and not card["selected"]
    assert levels["current"] == 20 and state.captures == 2


@pytest.mark.parametrize("observation", [
    {"selected": False},
    {"selected": False, "maximum": True},
    {"selected": True, "maximum": True},
])
def test_unconfirmed_selection_times_out_without_retry_or_fallback(select_rig, observation):
    state = select_rig
    state.observations = [observation]
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        state.run()
    assert error.value.code == "investment_state_timeout"
    assert len(state.clicks) == 1


@pytest.fixture
def native_reader(monkeypatch):
    reader = InvestmentVision()
    for name in ("_headers", "_top_partials", "detect_cards"):
        monkeypatch.setattr(reader, name, lambda *args: pytest.fail("No card discovery during selection"))
    return reader


def marker_frame(reader, card):
    image = np.zeros((720, 1280, 3), np.uint8)
    spec = reader._templates["selected_corner"]
    marker = reader._pixels[spec["file"]]
    x, y, _, _ = card["rect"]
    h, w = marker.shape
    image[y + 3:y + 3 + h, x:x + w] = np.where(
        marker[..., None] >= 128, np.array([255, 190, 0], np.uint8), 0,
    )
    return image


def test_native_corner_is_read_at_known_rectangle(native_reader):
    result = native_reader.read_card_selection(marker_frame(native_reader, CARD), CARD)
    assert result["selected"] and not result["maximum"]
    assert result["rect"] == CARD["rect"] and CARD["selected"] is False


def test_other_cards_corner_does_not_select_target(native_reader):
    other = {**CARD, "rect": (189, 154, 114, 129)}
    result = native_reader.read_card_selection(marker_frame(native_reader, other), CARD)
    assert not result["selected"] and not result["maximum"]


@pytest.mark.parametrize("card", [
    {**CARD, "partial": True},
    {**CARD, "rect": (62, 130, 114, 129)},
    {**CARD, "rect": (62, 154, 0, 129)},
    {"rect": None},
])
def test_incomplete_or_invalid_bounds_are_rejected(native_reader, card):
    with pytest.raises(InvestmentRecognitionError) as error:
        native_reader.read_card_selection(np.zeros((720, 1280, 3), np.uint8), card)
    assert error.value.code == "invalid_card"

"""Investment checks share observations without extra scans or submissions."""

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment
from plans.resonance_pc.src.actions._trade_goods_investment_vision import InvestmentVision


FRAME = np.zeros((720, 1280, 3), np.uint8)
CARD = {"rect": (62, 154, 114, 129), "partial": False, "occluded": False,
        "selected": False, "locked": False, "maximum": False}


@pytest.fixture
def clock_rig(monkeypatch):
    state = SimpleNamespace(now=0., captures=0, scene={}, scenes=[], matches=[], scans=0, clicks=[])

    def capture(app):
        if state.scenes:
            state.scene = state.scenes[min(state.captures, len(state.scenes) - 1)]
        state.captures += 1
        return FRAME

    monkeypatch.setattr(investment, "_capture", capture)
    monkeypatch.setattr(investment, "_cancel_check", lambda: None)
    monkeypatch.setattr(investment.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(investment, "_pause", lambda seconds: setattr(state, "now", state.now + seconds))
    state.app = SimpleNamespace(click=lambda **point: state.clicks.append(point))
    return state


def list_reader(state):
    def match(frame, name):
        state.matches.append(name)
        return {"found": state.scene.get("toast", False) if name == "success"
                else state.scene.get("anchor", True)}

    def detect(frame, *, success_visible):
        assert success_visible is False
        state.scans += 1
        return [{**CARD, **state.scene.get("card", {})}]

    return SimpleNamespace(match=match, locate_cards=detect)


def test_page_and_cards_use_one_stable_wait(clock_rig):
    state = clock_rig
    _, cards = investment._cards(state.app, list_reader(state))
    assert cards == [CARD]
    assert state.captures == state.scans == 2
    assert state.matches == ["success", "page_anchor"] * 2
    assert state.now == pytest.approx(.2)


@pytest.mark.parametrize("interruption", [
    {"toast": True}, {"anchor": False}, {"card": {"occluded": True}},
])
def test_interrupted_list_observation_restarts_stability(clock_rig, interruption):
    state = clock_rig
    state.scenes = [{}, interruption, {}, {}]
    investment._cards(state.app, list_reader(state))
    assert state.captures == 4
    assert state.scans == (4 if "card" in interruption else 3)


def test_preparation_checks_once_and_commit_uses_that_confirmed_button(clock_rig, monkeypatch):
    state = clock_rig
    level_reads = []
    levels = {"current": 9, "preview": 10, "max_level": False}

    def stable_levels(*args):
        level_reads.append(None)
        return FRAME, levels

    def match(frame, name):
        state.matches.append(name)
        return {"found": name == "confirm_enabled", "center": [1001, 561]}

    reader = SimpleNamespace(match=match, read_levels=lambda frame: {
        "current": 10, "preview": 11, "max_level": False})
    monkeypatch.setattr(investment, "_levels", stable_levels)
    operation = investment._prepare_upgrade(state.app, reader, levels, 10, lambda **data: None)
    assert len(level_reads) == 1 and state.captures == 0
    assert state.matches == ["confirm_enabled"]
    monkeypatch.setattr(investment, "_levels", lambda *args: pytest.fail("No repeated pre-submit check"))
    result = investment._commit(state.app, reader, operation)
    assert result["confirmed"] and state.captures == 2
    assert state.matches.count("confirm_enabled") == 1
    assert state.clicks == [{"x": 1001, "y": 561}]


def test_preparation_rejects_changed_preview_before_single_submit(clock_rig, monkeypatch):
    state = clock_rig
    monkeypatch.setattr(investment, "_levels", lambda *args: (FRAME, {"current": 9, "preview": 11}))
    reader = SimpleNamespace(match=lambda *args: pytest.fail("Changed preview cannot enable submission"))
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._prepare_upgrade(state.app, reader, {"current": 9, "preview": 10}, 10, lambda **data: None)
    assert error.value.code == "investment_preview_changed" and not state.clicks


def test_commit_requires_a_confirmed_click_point(clock_rig):
    with pytest.raises(investment.TradeGoodsInvestmentError) as error:
        investment._commit(clock_rig.app, object(), {"current_level": 0, "upgrade_level": 1})
    assert error.value.code == "investment_click_unconfirmed" and not clock_rig.clicks


@pytest.mark.parametrize("level", range(21))
def test_native_level_tokens_keep_dual_core_recognition(level):
    reader = InvestmentVision()
    entry = next(row for row in reader._manifest["levels"] if row["level"] == level
                 and row["file"].endswith("_client.png"))
    template = reader._pixels[entry["file"]]
    x, y, w, h = cv2.boundingRect(cv2.findNonZero(template))
    glyph = template[y:y + h, x:x + w]
    image = FRAME.copy()
    rx, ry, rw, rh = reader._manifest["level_rois"]["current"]
    image[ry:ry + h, rx:rx + w] = np.repeat(glyph[..., None], 3, axis=2)
    assert reader._read_level(image, (rx, ry, rw, rh), "current") == level

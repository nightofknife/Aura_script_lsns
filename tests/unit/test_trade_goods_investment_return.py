"""Return retries require fresh, positively identified source-page observations."""
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as investment


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(now=0.0, captures=0, clicks=[], cancelled=False,
                            scene=lambda: "investment", match_delay=0.0)
    frames = {}

    def capture():
        state.captures += 1
        scene = state.scene()
        frames[state.captures] = scene
        image = np.full((720, 1280, 3), state.captures, dtype=np.uint16)
        return SimpleNamespace(success=True, image=image)

    def match(*, source_image, template_image, **kwargs):
        serial = int(source_image[0, 0, 0])
        scene = frames[serial]
        state.now += state.match_delay
        is_menu = Path(template_image).as_posix().endswith("commerce/trade/shop/menu_ready.png")
        found = scene in {"shop", "ambiguous"} if is_menu else scene != "missing_back"
        return SimpleNamespace(found=found, confidence=float("nan") if scene == "invalid" else .99,
                               center_point=(serial, 37), debug_info={})

    def pause(seconds):
        check_cancel()
        state.now += seconds

    def check_cancel():
        if state.cancelled:
            raise investment.TradeGoodsInvestmentError("trade_cancelled", "cancelled")

    class Reader:
        def match(self, image, name):
            scene = frames[int(image[0, 0, 0])]
            if name == "page_anchor":
                return {"found": scene in {"investment", "toast", "ambiguous", "missing_back", "bad_levels"}}
            assert name == "success", "Return confirmation must not depend on investment-entry availability"
            return {"found": scene == "toast"}

        def read_levels(self, image):
            if frames[int(image[0, 0, 0])] == "bad_levels":
                raise investment.InvestmentRecognitionError("unreadable", "unreadable")
            return {"current": 5, "preview": 6}

    state.app = SimpleNamespace(capture=capture,
                               click=lambda **point: state.clicks.append((state.now, point, state.captures)))
    state.vision = SimpleNamespace(find_template=match)
    state.reader = Reader()
    state.run = lambda: investment._return_shop(state.app, state.vision, state.reader)
    monkeypatch.setattr(investment, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(investment, "_pause", pause)
    monkeypatch.setattr(investment, "_cancel_check", check_cancel)
    return state


def test_already_in_shop_needs_two_observations_and_no_click(rig):
    rig.scene = lambda: "shop"
    rig.run()
    assert rig.captures == 2
    assert not rig.clicks


@pytest.mark.parametrize("effective_click", [1, 2, 3])
def test_dropped_clicks_retry_with_fresh_points_and_three_second_spacing(rig, effective_click):
    rig.scene = lambda: "shop" if len(rig.clicks) >= effective_click else "investment"
    rig.run()
    assert len(rig.clicks) == effective_click
    assert all(b[0] - a[0] >= 3.0 for a, b in zip(rig.clicks, rig.clicks[1:]))
    assert all(point == {"x": serial, "y": 37} for _, point, serial in rig.clicks)


def test_delayed_transition_before_retry_is_not_clicked_again(rig):
    rig.scene = lambda: "shop" if rig.clicks and rig.now - rig.clicks[0][0] >= 2.75 else "investment"
    rig.run()
    assert len(rig.clicks) == 1


def test_transition_on_final_preclick_capture_prevents_input(rig):
    rig.scene = lambda: "shop" if rig.captures >= 3 else "investment"
    rig.run()
    assert not rig.clicks


@pytest.mark.parametrize("scene", ["unknown", "toast", "missing_back", "bad_levels", "invalid", "ambiguous"])
def test_unknown_or_unsafe_page_times_out_without_input(rig, scene):
    rig.scene = lambda: scene
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        rig.run()
    assert exc.value.code == "investment_return_shop_timeout"
    assert rig.now == 20.0
    assert not rig.clicks


def test_single_shop_observation_latches_even_if_investment_reappears(rig):
    rig.scene = lambda: "shop" if rig.captures == 4 else "investment"
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        rig.run()
    assert len(rig.clicks) == 1
    assert exc.value.detail["shop_seen"] is True


def test_retry_exhaustion_waits_for_late_return_until_deadline(rig):
    rig.scene = lambda: "shop" if rig.now >= 19.0 else "investment"
    rig.run()
    assert len(rig.clicks) == 3


def test_exhaustion_preserves_diagnostics(rig):
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        rig.run()
    assert len(rig.clicks) == 3
    assert exc.value.detail["click_attempts"] == 3
    assert exc.value.detail["last_observation"]["page"] == "investment"
    assert rig.now == 20.0


def test_slow_recognition_cannot_authorize_click_past_deadline(rig):
    rig.match_delay = 11.0
    with pytest.raises(investment.TradeGoodsInvestmentError):
        rig.run()
    assert not rig.clicks


def test_cancellation_during_wait_prevents_retry(rig):
    def scene():
        if rig.clicks:
            rig.cancelled = True
        return "investment"

    rig.scene = scene
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        rig.run()
    assert exc.value.code == "trade_cancelled"
    assert len(rig.clicks) == 1


def test_return_failure_keeps_confirmed_investment_without_resubmission(rig, monkeypatch):
    card = {"rect": (60, 150, 110, 120), "partial": False, "locked": False}
    rig.reader.read_entry_state = lambda frame: {"availability": "available"}
    rig.reader.city_product_count = 1
    rig.reader.VIEWPORT = (33, 130, 410, 460)
    monkeypatch.setattr(investment, "InvestmentVision", lambda **kwargs: rig.reader)
    monkeypatch.setattr(investment, "_wait", lambda *args: (None, {"center": (840, 490), "availability": "available", "white_score": .99, "gray_score": .1}))
    monkeypatch.setattr(investment, "_cards", lambda *args: (None, [card]))
    monkeypatch.setattr(investment, "_current_card", lambda app, reader, card: (None, card))
    monkeypatch.setattr(investment, "_select", lambda *args: (None, card, {"current": 0, "preview": 1}))
    operation = {"current_level": 0, "upgrade_level": 5, "stop_after_commit": True}
    monkeypatch.setattr(investment, "_prepare_upgrade", lambda *args: operation)
    commits = []

    def commit(*args):
        commits.append(operation)
        return {"current_level": 5, "confirmed": True}

    monkeypatch.setattr(investment, "_commit", commit)
    reports = []
    with pytest.raises(investment.TradeGoodsInvestmentError) as exc:
        investment.execute_trade_goods_investment_from_shop(
            mode=10, city_name="test", app=rig.app, vision=rig.vision,
            progress=lambda state, detail: reports.append((state, detail)),
        )
    assert exc.value.code == "investment_return_shop_timeout"
    assert len(commits) == 1
    assert exc.value.detail["transaction_count"] == 1
    assert exc.value.detail["transactions"][0]["to_level"] == 5
    assert reports[-1][0] == "failed"
    assert reports[-1][1]["transactions"] == exc.value.detail["transactions"]

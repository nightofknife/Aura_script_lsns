"""Offline entry state-machine checks; no game capture or input."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _city_shop_entry as entry
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade

POINT = {"city_key": "lanxin_city", "city_name": "岚心城", "x": 670, "y": 240}


def hit(found=False, **extra):
    return {"found": found, "confidence": .99 if found else .2, **extra}


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(now=0., clicks=[], city_probes=[], target_probes=[],
                            target=lambda: hit(), city=lambda: hit(True, city_key=POINT["city_key"]),
                            cancel=lambda: None)
    monkeypatch.setattr(entry.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(entry.time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds))

    def city(**kwargs):
        state.city_probes.append(state.now)
        return state.city()

    def target():
        state.target_probes.append(state.now)
        return state.target()

    monkeypatch.setattr(entry, "probe_city", city)
    state.run = lambda: entry.enter_city_shop(
        app=object(), vision=object(), point=POINT, entry_type="test_shop", page_state="shop_menu",
        target_probe=target, click=lambda: state.clicks.append(state.now), check_cancelled=state.cancel)
    return state


@pytest.mark.parametrize("required_clicks", [1, 2, 3])
def test_success_on_first_second_or_third_click(rig, required_clicks):
    rig.target = lambda: hit(len(rig.clicks) >= required_clicks)
    result = rig.run()
    assert result["success"] and result["page_state"] == "shop_menu"
    assert result["stable_matches"] == 2
    assert result["click_attempts"] == len(rig.clicks) == required_clicks
    assert rig.clicks[0] >= .3  # Original city is confirmed twice even for the initial click.
    assert all(b - a >= 3 for a, b in zip(rig.clicks, rig.clicks[1:]))
    assert rig.now < 15


def test_already_open_menu_does_not_probe_city_or_click(rig):
    rig.target = lambda: hit(True)
    result = rig.run()
    assert result["polls"] == 2
    assert rig.clicks == rig.city_probes == []


def test_slow_transition_does_not_retry_without_city_evidence(rig):
    rig.target = lambda: hit(bool(rig.clicks) and rig.now >= 10)
    rig.city = lambda: hit(not rig.clicks, city_key=POINT["city_key"])
    assert rig.run()["success"]
    assert len(rig.clicks) == 1


def test_target_appearing_during_city_probe_suppresses_click(rig):
    probes = iter([False, False, True, True])
    rig.target = lambda: hit(next(probes))
    assert rig.run()["success"]
    assert rig.clicks == []


def test_transient_menu_hit_resets_and_requires_city_reconfirmation(rig):
    probes = iter([False, False, False, True, False, False, False, True, True])
    rig.target = lambda: hit(next(probes))
    result = rig.run()
    assert result["success"] and result["stable_matches"] == 2
    assert len(rig.clicks) == 1
    assert len(rig.city_probes) == 5


@pytest.mark.parametrize("city", [hit(), hit(True, city_key="other_city"),
                                  hit(True, city_key=POINT["city_key"], confidence=float("nan")), {}])
def test_unknown_or_different_city_never_authorizes_click(rig, city):
    rig.city = lambda: city
    with pytest.raises(entry.CityShopEntryError) as caught:
        rig.run()
    assert caught.value.code == "city_shop_entry_timeout"
    assert rig.clicks == []
    assert rig.now == pytest.approx(15)


@pytest.mark.parametrize("target", [hit(confidence=float("inf")), hit(valid=False),
                                    hit(reason="capture_failed"), {}, None, hit(error="failed")])
def test_unknown_target_never_authorizes_city_probe_or_click(rig, target):
    rig.target = lambda: target
    with pytest.raises(entry.CityShopEntryError):
        rig.run()
    assert rig.clicks == rig.city_probes == []


def test_city_streak_resets_on_different_city(rig):
    cities = iter([hit(True, city_key=POINT["city_key"]), hit(True, city_key="other"),
                   hit(True, city_key=POINT["city_key"]), hit(True, city_key=POINT["city_key"])])
    rig.city = lambda: next(cities)
    rig.target = lambda: hit(bool(rig.clicks))
    assert rig.run()["success"]
    assert rig.clicks == [pytest.approx(.9)]


def test_max_clicks_still_waits_for_late_success(rig):
    rig.target = lambda: hit(len(rig.clicks) == 3 and rig.now >= 13)
    assert rig.run()["success"]
    assert len(rig.clicks) == 3 and rig.now >= 13


def test_single_total_deadline_and_diagnostic_details(rig):
    with pytest.raises(entry.CityShopEntryError) as caught:
        rig.run()
    assert rig.now == pytest.approx(15)
    detail = caught.value.detail
    assert detail["click_attempts"] == len(rig.clicks) == 3
    assert detail["city_key"] == POINT["city_key"]
    assert detail["last_match"]["found"] is False
    assert detail["last_city_match"]["city_key"] == POINT["city_key"]


def test_probe_overrunning_budget_never_clicks_or_claims_success(rig):
    def slow():
        rig.now += 16
        return hit(True)
    rig.target = slow
    with pytest.raises(entry.CityShopEntryError):
        rig.run()
    assert rig.clicks == []


def test_cancel_during_target_probe_prevents_city_input(rig):
    def target():
        rig.cancel = lambda: (_ for _ in ()).throw(asyncio.CancelledError())
        return hit()
    rig.target = target
    # The callback reads the dynamically changed cancellation state.
    cancel = lambda: rig.cancel()
    with pytest.raises(asyncio.CancelledError):
        entry.enter_city_shop(app=None, vision=None, point=POINT, entry_type="exchange",
                             page_state="shop_page", target_probe=rig.target,
                             click=lambda: rig.clicks.append(rig.now), check_cancelled=cancel)
    assert rig.clicks == []


def test_city_technical_failure_is_propagated_not_retried(rig):
    def fail():
        raise entry.CityPanelVisionError("city_identity_capture_failed", "Capture failed")
    rig.city = fail
    with pytest.raises(entry.CityShopEntryError) as caught:
        rig.run()
    assert caught.value.code == "city_identity_capture_failed"
    assert rig.clicks == [] and rig.now == 0


@pytest.mark.parametrize("fault", ["capture_failed", "wrong_size", "match_error", "malformed"])
def test_menu_probe_technical_failures(fault):
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(
        success=fault != "capture_failed", image=np.zeros((2 if fault == "wrong_size" else 3, 4, 3), np.uint8)))
    vision = SimpleNamespace(find_template=lambda **kwargs: SimpleNamespace(
        found=True, confidence="invalid" if fault == "malformed" else .99,
        debug_info={"error": "bad template"} if fault == "match_error" else {}))
    with pytest.raises(entry.CityShopEntryError):
        entry.probe_menu_template(app=app, vision=vision, template="test", region=[0, 0, 4, 3],
                                 threshold=.86, check_cancelled=lambda: None)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_menu_score_is_unknown(score):
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=True, image=np.zeros((3, 4, 3), np.uint8)))
    vision = SimpleNamespace(find_template=lambda **kwargs: SimpleNamespace(found=True, confidence=score, debug_info={}))
    result = entry.probe_menu_template(app=app, vision=vision, template="test", region=[0, 0, 4, 3],
                                       threshold=.86, check_cancelled=lambda: None)
    assert result["valid"] is False and result["found"] is False


def test_exchange_wrapper_and_normal_trade_share_one_confirmation(rig, monkeypatch):
    operations = []
    app = SimpleNamespace(click=lambda **kwargs: rig.clicks.append(rig.now))
    shops = SimpleNamespace(resolve_shop_point=lambda **kwargs: dict(POINT))
    monkeypatch.setattr(trade, "_probe_shop_menu", lambda *args: hit(bool(rig.clicks)))
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", lambda *args: pytest.fail("Duplicate menu wait"))
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", lambda **kwargs: operations.append("node") or {})
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", lambda **kwargs: operations.append("sell") or
                        {"success": True, "page_state": "shop_page", "sold_confirmed": True})
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", lambda **kwargs: operations.append("main") or
                        {"success": True, "page_state": "city_main"})
    result = trade._execute_city_trade_inside_current_city_scoped(
        current_city="岚心城", buy_products=[], books_used=0, sell_raise_to_cap=False, buy_bargain_to_cap=False,
        app=app, ocr=None, vision=object(), city_shop_data=shops)
    assert result["success"] and result["shop_menu_ready"]["stable_matches"] == 2
    assert operations == ["node", "sell", "main"] and len(rig.clicks) == 1


def test_exchange_timeout_never_enters_sell_or_investment(rig, monkeypatch):
    monkeypatch.setattr(trade, "_probe_shop_menu", lambda *args: hit())
    monkeypatch.setattr(trade, "resonance_pc_click_shop_menu_node", lambda **kwargs: pytest.fail("Entry not ready"))
    monkeypatch.setattr(trade, "_run_trade_goods_investment_task", lambda **kwargs: pytest.fail("Entry not ready"))
    params = dict(current_city="岚心城", buy_products=[], books_used=0, sell_raise_to_cap=False,
                  buy_bargain_to_cap=False, app=SimpleNamespace(click=lambda **kwargs: rig.clicks.append(rig.now)),
                  ocr=None, vision=object(), city_shop_data=SimpleNamespace(resolve_shop_point=lambda **kwargs: dict(POINT)))
    with pytest.raises(trade.CityTradeFlowError) as caught:
        trade._execute_city_trade_inside_current_city_scoped(**params)
    assert caught.value.code == "shop_menu_not_ready"
    rig.now, rig.clicks = 0, []
    with pytest.raises(trade.CityTradeFlowError) as caught:
        asyncio.run(trade._execute_freight_city_trade(**params, auto_trade_goods_investment=True,
                                                     context=object(), engine=object()))
    assert caught.value.code == "shop_menu_not_ready" and len(rig.clicks) == 3

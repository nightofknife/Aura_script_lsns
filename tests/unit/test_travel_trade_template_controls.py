"""Arrival and purchase controls use template evidence and confirmed settlement."""

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _city_panel_vision as panel
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.actions import city_travel_pc_actions as travel
from tests.unit.test_trade_buy_flow import buy_rig


def test_arrival_delegates_to_shared_visit_entry_probe(monkeypatch):
    calls = []
    app, vision = object(), object()
    evidence = {"found": True, "confidence": .96, "center": [1098, 480],
                "region": [1000, 450, 250, 70]}

    def probe(**kwargs):
        calls.append(kwargs)
        kwargs["check_cancelled"]()
        return dict(evidence)

    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    monkeypatch.setattr(travel, "probe_visit_entry", probe)
    result = travel._detect_city_main_entry(app, vision, poll_count=7)
    assert len(calls) == 1
    assert calls[0]["app"] is app and calls[0]["vision"] is vision
    assert result == {**evidence, "method": "visit_entry_template", "hit": evidence}


@pytest.mark.parametrize("found", [True, False])
def test_arrival_uses_small_visit_roi_and_existing_opaque_template(monkeypatch, found):
    captures, matches = [], []

    def capture(*, rect):
        captures.append(rect)
        return SimpleNamespace(success=True, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8))

    def match(**kwargs):
        matches.append(kwargs)
        return SimpleNamespace(found=found, confidence=.97 if found else .4,
                               center_point=[70, 25], debug_info={})

    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    result = travel._detect_city_main_entry(SimpleNamespace(capture=capture),
                                           SimpleNamespace(find_template=match), poll_count=1)
    assert captures == [(1000, 450, 250, 70)]
    assert len(matches) == 1
    assert matches[0]["threshold"] == .9
    assert matches[0]["match_method"] == cv2.TM_SQDIFF_NORMED
    assert matches[0]["use_grayscale"] is False
    assert matches[0]["preprocess"] == "none"
    assert np.array_equal(matches[0]["template_image"], panel.load_templates()[3])
    assert result["found"] is found
    assert result["center"] == ([1070, 475] if found else None)
    assert result["hit"] is not None if found else result["hit"] is None


def test_arrival_propagates_capture_failure_without_claiming_city_entry(monkeypatch):
    monkeypatch.setattr(travel, "_check_intercity_cancelled", lambda: None)
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=False, image=None))
    with pytest.raises(travel.IntercityDestinationError) as error:
        travel._detect_city_main_entry(app, object(), poll_count=1)
    assert error.value.code == "city_identity_capture_failed"


def test_primary_buy_button_reuses_existing_template_and_clicks_its_observed_center(buy_rig):
    result = buy_rig.run()
    calls = [call for call in buy_rig.calls if call[0] == "template_hit"]
    assert len(calls) == 1
    assert calls[0][1][2:] == ("templates/commerce/books/buy_page_ready.png", [850, 590, 380, 110])
    assert calls[0][2]["threshold"] == .8
    click = next(call for call in buy_rig.calls if call[0] == "click")
    assert click[1][1]["center"] == [1050, 650]
    assert result["buy_button"]["method"] == "template"
    assert result["buy_button"]["template"] == "templates/commerce/books/buy_page_ready.png"
    assert result["confirm_panel_found"] is False
    assert result["settlement"]["closed"] is True


@pytest.mark.parametrize("hit", [
    {"found": False}, {"found": True}, {"found": True, "center": []},
    {"found": True, "center": [1000]},
])
def test_missing_primary_button_or_center_cannot_issue_click(buy_rig, monkeypatch, hit):
    monkeypatch.setattr(trade, "_wait_template", lambda *args, **kwargs: dict(hit))
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run()
    assert error.value.code == "buy_button_not_found"
    assert not {"click", "settlement", "back"} & set(buy_rig.names())


def test_purchase_control_flow_accepts_no_ocr_service(buy_rig):
    result = trade.resonance_pc_buy_goods_on_buy_page(
        product_list=buy_rig.requested, app=object(), vision=object(), ocr=None)
    assert result["success"] is True
    assert result["buy_button"]["method"] == "template"


def confirmation_scenario(buy_rig, monkeypatch, *, panel_found=True, button_found=True,
                          final_closed=True):
    events = []
    settlement_results = iter([False, final_closed])

    def wait_template(app, vision, template, region, **kwargs):
        events.append(("template", template, list(region), kwargs["threshold"]))
        found = panel_found if template == "templates/commerce/trade/buy/preview_title.png" else (
            button_found if list(region) == [900, 620, 330, 70] else True)
        return {"valid": True, "found": found, "center": [1060, 652] if found else None, "confidence": .95}

    def click(app, hit):
        events.append(("click", hit["center"]))
        return {"clicked": True}

    def settlement(*args, **kwargs):
        closed = next(settlement_results)
        events.append(("settlement", closed))
        return {"closed": closed, "settlement_confirmed": closed,
                "final_absence_verified": closed, "observation_unknown": False}

    monkeypatch.setattr(trade, "_wait_template", wait_template)
    monkeypatch.setattr(trade, "_click_hit", click)
    monkeypatch.setattr(trade, "_close_settlement", settlement)
    return events


def test_extra_confirmation_requires_title_then_existing_button_then_final_settlement(buy_rig, monkeypatch):
    events = confirmation_scenario(buy_rig, monkeypatch)
    result = buy_rig.run()
    assert events == [
        ("template", "templates/commerce/books/buy_page_ready.png", [850, 590, 380, 110], .8),
        ("click", [1060, 652]), ("settlement", False),
        ("template", "templates/commerce/trade/buy/preview_title.png", [850, 80, 180, 60], .86),
        ("template", "templates/commerce/books/buy_page_ready.png", [900, 620, 330, 70], .8),
        ("click", [1060, 652]), ("settlement", True),
    ]
    assert result["success"] is True and result["confirm_panel_found"] is True
    assert result["confirm_click"]["method"] == "template"
    assert result["settlement"]["closed"] is False
    assert result["settlement_after_confirm"]["closed"] is True


def test_missing_confirmation_title_cannot_trigger_second_button_or_success(buy_rig, monkeypatch):
    events = confirmation_scenario(buy_rig, monkeypatch, panel_found=False)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run()
    assert error.value.code == "buy_transaction_not_confirmed"
    assert [event[0] for event in events] == ["template", "click", "settlement", "template"]
    assert events[-1][1] == "templates/commerce/trade/buy/preview_title.png"


def test_missing_confirmation_button_cannot_click_a_guessed_position(buy_rig, monkeypatch):
    events = confirmation_scenario(buy_rig, monkeypatch, button_found=False, final_closed=False)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run()
    assert error.value.code == "buy_transaction_not_confirmed"
    assert sum(event[0] == "click" for event in events) == 1
    assert error.value.detail["confirm_click"]["clicked"] is False
    assert error.value.detail["confirm_click"]["reason"] == "template_not_found"


def test_matching_both_controls_still_cannot_replace_final_settlement_confirmation(buy_rig, monkeypatch):
    events = confirmation_scenario(buy_rig, monkeypatch, final_closed=False)
    with pytest.raises(trade.CityTradeFlowError) as error:
        buy_rig.run()
    assert error.value.code == "buy_transaction_not_confirmed"
    assert sum(event[0] == "click" for event in events) == 2
    assert error.value.detail["confirm_panel"]["found"] is True
    assert error.value.detail["settlement_after_confirm"]["closed"] is False


@pytest.mark.parametrize("template,region", [
    ("templates/commerce/books/buy_page_ready.png", [850, 590, 380, 110]),
    ("templates/commerce/trade/buy/preview_title.png", [850, 80, 180, 60]),
])
def test_buy_template_capture_offsets_match_center_to_client_coordinates(template, region):
    captures, calls = [], []
    image = np.zeros((region[3], region[2], 3), dtype=np.uint8)

    def capture(*, rect):
        captures.append(rect)
        return SimpleNamespace(success=True, image=image)

    def match(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(found=True, confidence=.97, center_point=(40, 20))

    result = trade._match_template(SimpleNamespace(capture=capture),
                                   SimpleNamespace(find_template=match), template, region, .86)
    assert captures == [tuple(region)]
    assert calls[0]["source_image"] is image
    assert calls[0]["use_grayscale"] is True
    assert Path(calls[0]["template_image"]).name == Path(template).name
    assert result["center"] == [region[0] + 40, region[1] + 20]
    assert result["found"] is True


def test_reused_button_and_new_title_are_decodable_nonempty_template_assets():
    from plans.resonance_pc.src.actions import purchase_book_pc_actions as books
    root = Path(trade.__file__).resolve().parents[2]
    for template in (books._BUY_PAGE_READY_TEMPLATE, trade._BUY_CONFIRM_PANEL_TEMPLATE):
        image = cv2.imdecode(np.frombuffer((root / template).read_bytes(), np.uint8),
                             cv2.IMREAD_COLOR)
        assert image is not None and image.shape[2] == 3
        assert image.std() > 1
        if template == trade._BUY_CONFIRM_PANEL_TEMPLATE:
            assert image.shape[:2] == (26, 90)

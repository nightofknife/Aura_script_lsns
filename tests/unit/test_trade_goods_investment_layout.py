"""Geometry discovery and one-slot state reads remain separate."""

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _trade_goods_investment_vision as native


FRAME = np.zeros((720, 1280, 3), np.uint8)
CARD = {"rect": (62, 154, 115, 129), "partial": False, "occluded": False,
        "selected": False, "locked": None, "maximum": False}


def forbidden(*args, **kwargs):
    pytest.fail("No full-frame/state scan at this stage")


def test_locating_full_top_list_reads_geometry_only(monkeypatch):
    reader = native.InvestmentVision(city_name="淘金乐园")
    headers = [{"x": x, "y": y, "header_score": 1.}
               for y in (154, 286, 418) for x in (62, 189, 315)]
    monkeypatch.setattr(reader, "_headers", lambda image: list(reversed(headers)))
    monkeypatch.setattr(reader, "_top_partials", forbidden)
    monkeypatch.setattr(reader, "_match_box", forbidden)
    monkeypatch.setattr(native, "_best", forbidden)
    cards = reader.locate_cards(FRAME, success_visible=False)
    assert len(cards) == 7
    assert [card["rect"][:2] for card in cards] == [(h["x"], h["y"]) for h in headers[:7]]
    assert all(card["locked"] is None and not card["selected"] and not card["maximum"] for card in cards)


def test_scrolled_top_partial_is_located_without_reading_states(monkeypatch):
    reader = native.InvestmentVision()
    headers = [{"x": x, "y": 240, "header_score": 1.} for x in (62, 189, 315)]
    calls = []
    monkeypatch.setattr(reader, "_headers", lambda image: list(headers))
    monkeypatch.setattr(reader, "_top_partials", lambda image, candidates:
                        calls.append(candidates[:]) or [{"x": x, "y": 108,
                                                        "header_score": 0., "frame_score": .9}
                                                       for x in (62, 189, 315)])
    monkeypatch.setattr(reader, "_match_box", forbidden)
    cards = reader.locate_cards(FRAME, success_visible=False)
    assert calls == [headers]
    assert cards[0]["partial"] and cards[0]["rect"][:2] == (62, 108)
    assert all(card["partial"] for card in cards[:3])
    assert all(not card["partial"] for card in cards[3:])


def test_geometry_marks_success_occlusion_without_reading_slots(monkeypatch):
    reader = native.InvestmentVision()
    monkeypatch.setattr(reader, "_headers", lambda image: [{"x": x, "y": y, "header_score": 1.}
                                                          for y in (154, 286) for x in (62, 189, 315)])
    monkeypatch.setattr(reader, "_match_box", forbidden)
    assert [card["occluded"] for card in reader.locate_cards(FRAME, success_visible=True)] == [False] * 3 + [True] * 3


def test_cached_state_reads_only_tiny_header_and_current_slot(monkeypatch):
    reader = native.InvestmentVision()
    for name in ("_headers", "_top_partials", "detect_cards", "locate_cards"):
        monkeypatch.setattr(reader, name, forbidden)
    matching = []

    def best(source, template, mask):
        matching.append((source.shape, template.shape))
        return .99, (3, 3)

    boxes = []

    def match_box(image, name, box):
        boxes.append((name, box))
        return {"found": name == "lock"}

    monkeypatch.setattr(native, "_best", best)
    monkeypatch.setattr(reader, "_match_box", match_box)
    state = reader.read_card_state(FRAME, CARD)
    assert state["locked"] and not state["selected"]
    assert state["rect"] == CARD["rect"]
    assert len(matching) == len(reader._manifest["cards"]["header_variants"])
    assert all(source[:2] == (20, 28) and template[:2] == (14, 22)
               for source, template in matching)
    assert {name for name, _ in boxes} == {"selected_corner", "maximum_card_badge", "lock"}
    assert all(box[2] < CARD["rect"][2] and box[3] < CARD["rect"][3] for _, box in boxes)


def test_missing_cached_header_stops_before_state_reads(monkeypatch):
    reader = native.InvestmentVision()
    monkeypatch.setattr(native, "_best", lambda *args: (.1, (0, 0)))
    monkeypatch.setattr(reader, "_match_box", forbidden)
    with pytest.raises(native.InvestmentRecognitionError) as error:
        reader.read_card_state(FRAME, CARD)
    assert error.value.code == "card_position_uncertain"


def test_artwork_touching_frame_edge_does_not_invalidate_local_header(monkeypatch):
    reader = native.InvestmentVision()
    frame = FRAME.copy()
    card = {**CARD, "rect": (62, 418, 115, 129)}
    header = reader._pixels["cards/locked_header.png"]
    frame[418:432, 109:131] = header
    # Simulate the gold commodity art obscuring the old full-frame mask.
    frame[442:522, 62:177] = [210, 160, 70]
    for name in ("_headers", "_top_partials", "detect_cards", "locate_cards"):
        monkeypatch.setattr(reader, name, forbidden)
    state = reader.read_card_state(frame, card)
    assert state["header_score"] >= reader._manifest["cards"]["header_threshold"]
    assert state["rect"] == card["rect"]


@pytest.mark.parametrize("overrides", [{"partial": True}, {"occluded": True},
                                       {"rect": (62, 108, 115, 129)}, {"rect": (62, 154, 0, 129)}])
def test_partial_or_invalid_current_slot_cannot_be_read(monkeypatch, overrides):
    reader = native.InvestmentVision()
    monkeypatch.setattr(native, "_best", forbidden)
    with pytest.raises(native.InvestmentRecognitionError) as error:
        reader.read_card_state(FRAME, {**CARD, **overrides})
    assert error.value.code == "invalid_card"


def test_city_identity_and_products_are_ordered_and_instance_owned():
    reader = native.InvestmentVision(city_name="onederland")
    assert reader.city_id == "9"
    assert [row["investment_index"] for row in reader.city_products] == list(range(1, 8))
    assert [row["name"] for row in reader.city_products] == [
        "石英砂", "铁矿石", "牛仔裤", "漆黑矿渣", "玛瑙", "青金石", "沙金",
    ]
    reader.city_products[0]["name"] = "Changed"
    assert native.InvestmentVision(city_name="淘金乐园").city_products[0]["name"] == "石英砂"


def test_fresh_entry_origin_requires_top_first_card_selection(monkeypatch):
    reader = native.InvestmentVision()
    monkeypatch.setattr(reader, "read_card_selection", lambda image, card: {**card, "selected": True})
    assert reader.initial_order_confirmed(FRAME, [CARD])
    assert not reader.initial_order_confirmed(FRAME, [])
    assert not reader.initial_order_confirmed(FRAME, [{**CARD, "partial": True}])
    assert not reader.initial_order_confirmed(FRAME, [{**CARD, "rect": (62, 240, 115, 129)}])
    monkeypatch.setattr(reader, "read_card_selection", lambda image, card: {**card, "selected": False})
    assert not reader.initial_order_confirmed(FRAME, [CARD])

"""Ordinal cache writes require a complete grid and a proved list origin."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import trade_goods_investment_pc_actions as action
from plans.resonance_pc.src.actions._trade_goods_investment_vision import (
    InvestmentRecognitionError,
    InvestmentVision,
)


FRAME = np.zeros((720, 1280, 3), np.uint8)


def city_cards(*, scrolled=False):
    """The seven native goods occupy three columns, with one final-row item."""
    first_y = 108 if scrolled else 154
    return [
        {"rect": (x, first_y + 132 * (index // 3), 115, 129),
         "partial": scrolled and index < 3, "occluded": False,
         "selected": False, "maximum": False, "locked": None}
        for index, x in enumerate((62, 189, 315, 62, 189, 315, 62))
    ]


@pytest.mark.parametrize("scrolled", [False, True])
@pytest.mark.parametrize("missing", [0, 1, 4, 6])
def test_missing_visible_slot_cannot_be_compressed_into_another_goods_index(scrolled, missing):
    reader = InvestmentVision(city_name="淘金乐园")
    cards = city_cards(scrolled=scrolled)
    cards.pop(missing)
    with pytest.raises(InvestmentRecognitionError):
        reader.validate_card_grid(cards, first_index=1)


@pytest.mark.parametrize("scrolled", [False, True])
def test_complete_seven_goods_grid_accepts_final_single_item_row(scrolled):
    reader = InvestmentVision(city_name="淘金乐园")
    reader.validate_card_grid(city_cards(scrolled=scrolled), first_index=1)


def test_missing_entire_visible_row_cannot_shift_persistent_ordinals():
    reader = InvestmentVision(city_name="淘金乐园")
    cards = city_cards()
    with pytest.raises(InvestmentRecognitionError):
        reader.validate_card_grid(cards[:3] + cards[6:], first_index=1)


def test_initial_indexing_rejects_gap_even_when_first_selection_is_valid(monkeypatch):
    reader = InvestmentVision(city_name="淘金乐园")
    cards = city_cards()
    cards.pop(1)
    monkeypatch.setattr(reader, "initial_order_confirmed", lambda *_: True)
    monkeypatch.setattr(action, "_wait", lambda app, predicate: (FRAME, predicate(FRAME)))
    with pytest.raises((InvestmentRecognitionError, action.TradeGoodsInvestmentError)):
        action._initial_card_indices(object(), reader, FRAME, cards)


@pytest.mark.parametrize("levels,accepted", [
    ({"current": 20, "preview": None, "max_level": True}, True),
    ({"current": 19, "preview": 20, "max_level": False}, False),
    ({"current": 20, "preview": None, "max_level": False}, False),
])
def test_maximum_origin_requires_matching_actual_maximum_panel(monkeypatch, levels, accepted):
    reader = InvestmentVision(city_name="淘金乐园")
    cards = city_cards()
    monkeypatch.setattr(reader, "read_card_selection",
                        lambda image, card: {**card, "selected": False, "maximum": True})
    monkeypatch.setattr(reader, "read_levels", lambda image: levels)
    assert reader.initial_order_confirmed(FRAME, cards) is accepted


def test_maximum_badge_with_unreadable_panel_cannot_prove_origin(monkeypatch):
    reader = InvestmentVision(city_name="淘金乐园")
    monkeypatch.setattr(reader, "read_card_selection",
                        lambda image, card: {**card, "selected": False, "maximum": True})

    def unreadable(image):
        raise InvestmentRecognitionError("level_uncertain", "unconfirmed maximum panel")

    monkeypatch.setattr(reader, "read_levels", unreadable)
    try:
        accepted = reader.initial_order_confirmed(FRAME, city_cards())
    except InvestmentRecognitionError:
        accepted = False
    assert not accepted


def test_maximum_badge_below_top_row_never_proves_origin(monkeypatch):
    reader = InvestmentVision(city_name="淘金乐园")
    cards = city_cards()
    cards = [{**card, "rect": (card["rect"][0], card["rect"][1] + 70, 115, 129)}
             for card in cards]
    monkeypatch.setattr(reader, "read_card_selection",
                        lambda image, card: {**card, "selected": False, "maximum": True})
    monkeypatch.setattr(reader, "read_levels",
                        lambda image: {"current": 20, "preview": None, "max_level": True})
    assert not reader.initial_order_confirmed(FRAME, cards)


def test_return_to_top_needs_two_different_stationary_drag_spans(monkeypatch):
    reader = InvestmentVision(city_name="淘金乐园")
    # An initial drag reaches the top. A single stationary drag after that is
    # insufficient proof; a second, different span must also leave it unchanged.
    state = SimpleNamespace(drags=[], observations=iter((FRAME + 30, FRAME + 30, FRAME + 30)))
    app = SimpleNamespace(drag=lambda **params: state.drags.append(params))
    monkeypatch.setattr(action, "_cancel_check", lambda: None)
    monkeypatch.setattr(action, "_pause", lambda seconds: None)
    monkeypatch.setattr(action, "_wait", lambda app, predicate: (FRAME, True))
    monkeypatch.setattr(action, "_cards", lambda *args: (next(state.observations), city_cards()))
    frame, cards = action._reset_list_top(app, reader, FRAME, city_cards())
    assert len(state.drags) == 3
    last_spans = [row["end_y"] - row["start_y"] for row in state.drags[-2:]]
    assert all(span > 0 for span in last_spans)
    assert last_spans[0] != last_spans[1]
    assert np.array_equal(frame, FRAME + 30)
    assert cards == city_cards()

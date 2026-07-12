import pytest
from types import SimpleNamespace

from plans.resonance_pc.src.actions.purchase_book_pc_actions import (
    PurchaseBookUseError,
    _CONFIRM_BUTTON_TEMPLATE,
    _FIRST_ITEM_USE_BUTTON_TEMPLATE,
    _USE_ITEM_BUTTON_TEMPLATE,
    _click_template_or_point,
    _coerce_book_count,
    _resolve_template_path,
)


def test_purchase_book_count_accepts_zero_and_limit():
    assert _coerce_book_count(0, 10) == 0
    assert _coerce_book_count("8", 10) == 8
    assert _coerce_book_count(10, 10) == 10


def test_purchase_book_count_rejects_values_above_ui_limit():
    with pytest.raises(PurchaseBookUseError) as exc_info:
        _coerce_book_count(11, 10)

    assert exc_info.value.code == "books_used_exceeds_ui_limit"


def test_purchase_book_button_templates_exist():
    for template in (
        _USE_ITEM_BUTTON_TEMPLATE,
        _FIRST_ITEM_USE_BUTTON_TEMPLATE,
        _CONFIRM_BUTTON_TEMPLATE,
    ):
        assert _resolve_template_path(template).is_file()


def test_purchase_book_template_miss_does_not_use_fallback_point():
    class FakeApp:
        def __init__(self):
            self.clicks = []
            self.moves = []

        def capture(self, rect):
            return SimpleNamespace(success=True, image=object())

        def move_to(self, *args, **kwargs):
            self.moves.append((args, kwargs))

        def click(self, *args, **kwargs):
            self.clicks.append((args, kwargs))

    class FakeVision:
        def find_template(self, **kwargs):
            return SimpleNamespace(found=False, confidence=0.104, center_point=None)

    app = FakeApp()
    with pytest.raises(PurchaseBookUseError) as exc_info:
        _click_template_or_point(
            app,
            FakeVision(),
            _USE_ITEM_BUTTON_TEMPLATE,
            [1010, 80, 145, 55],
            (1080, 105),
            threshold=0.82,
        )

    assert exc_info.value.code == "purchase_book_template_not_found"
    assert app.moves == []
    assert app.clicks == []

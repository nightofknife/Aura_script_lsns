import pytest
from types import SimpleNamespace

from plans.resonance_pc.src.actions import purchase_book_pc_actions
from plans.resonance_pc.src.actions.purchase_book_pc_actions import (
    PurchaseBookUseError,
    _CONFIRM_BUTTON_TEMPLATE,
    _FIRST_ITEM_USE_BUTTON_TEMPLATE,
    _USE_ITEM_BUTTON_TEMPLATE,
    _click_template_or_point,
    _coerce_book_count,
    _resolve_template_path,
    _split_book_batches,
    resonance_pc_use_purchase_books,
)


def test_purchase_book_count_accepts_zero_and_limit():
    assert _coerce_book_count(0, 10) == 0
    assert _coerce_book_count("8", 10) == 8
    assert _coerce_book_count(10, 10) == 10


def test_purchase_book_count_accepts_values_above_single_batch_limit():
    assert _coerce_book_count(11, 10) == 11


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (0, []),
        (1, [1]),
        (10, [10]),
        (14, [10, 4]),
        (20, [10, 10]),
        (21, [10, 10, 1]),
    ],
)
def test_purchase_book_batches_respect_single_dialog_limit(requested, expected):
    assert _split_book_batches(requested, 10) == expected


def _successful_batch_result(batch_size):
    return {
        "ok": True,
        "used": batch_size,
        "item_name": "进货采买书",
        "plus_clicks": batch_size - 1,
        "open_click": {"clicked": True},
        "item_use_click": {"clicked": True},
        "confirm_click": {"clicked": True},
        "buy_page_ready": True,
    }


def test_purchase_books_runs_fourteen_as_ten_then_four(monkeypatch):
    calls = []

    def fake_use_purchase_book_batch(**kwargs):
        batch_size = kwargs["batch_size"]
        calls.append(batch_size)
        return _successful_batch_result(batch_size)

    monkeypatch.setattr(purchase_book_pc_actions, "_use_purchase_book_batch", fake_use_purchase_book_batch)

    result = resonance_pc_use_purchase_books(
        books_used=14,
        app=object(),
        ocr=object(),
        vision=object(),
    )

    assert calls == [10, 4]
    assert result["requested"] == 14
    assert result["used"] == 14
    assert result["batch_count"] == 2
    assert result["batch_sizes"] == [10, 4]
    assert result["plus_clicks"] == 12


def test_purchase_books_reports_completed_usage_when_later_batch_fails(monkeypatch):
    calls = []

    def fake_use_purchase_book_batch(**kwargs):
        batch_size = kwargs["batch_size"]
        calls.append(batch_size)
        if batch_size == 4:
            raise PurchaseBookUseError("test_batch_failure", "second batch failed")
        return _successful_batch_result(batch_size)

    monkeypatch.setattr(purchase_book_pc_actions, "_use_purchase_book_batch", fake_use_purchase_book_batch)

    with pytest.raises(PurchaseBookUseError) as exc_info:
        resonance_pc_use_purchase_books(
            books_used=14,
            app=object(),
            ocr=object(),
            vision=object(),
        )

    assert calls == [10, 4]
    assert exc_info.value.code == "purchase_book_batch_failed"
    assert exc_info.value.detail["used_before_failure"] == 10
    assert exc_info.value.detail["failed_batch_index"] == 2
    assert exc_info.value.detail["failed_batch_size"] == 4
    assert exc_info.value.detail["cause"]["code"] == "test_batch_failure"


def test_purchase_books_requires_buy_page_before_starting_next_batch(monkeypatch):
    calls = []

    def fake_use_purchase_book_batch(**kwargs):
        batch_size = kwargs["batch_size"]
        calls.append(batch_size)
        result = _successful_batch_result(batch_size)
        result["buy_page_ready"] = False
        return result

    monkeypatch.setattr(purchase_book_pc_actions, "_use_purchase_book_batch", fake_use_purchase_book_batch)

    with pytest.raises(PurchaseBookUseError) as exc_info:
        resonance_pc_use_purchase_books(
            books_used=14,
            app=object(),
            ocr=object(),
            vision=object(),
        )

    assert calls == [10]
    assert exc_info.value.code == "purchase_book_batch_return_not_ready"
    assert exc_info.value.detail["used_before_failure"] == 10
    assert exc_info.value.detail["failed_batch_index"] == 2
    assert exc_info.value.detail["failed_batch_size"] == 4


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

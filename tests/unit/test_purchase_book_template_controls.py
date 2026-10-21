"""Public purchase-book flow stays template-driven after removing dead coordinates."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import purchase_book_pc_actions as books


class BookUI:
    """An input-driven modal; template centers deliberately differ from old points."""

    def __init__(self, missing=None, cancel_on_move=None):
        self.page = "buy"
        self.missing = missing
        self.cancel_on_move = cancel_on_move
        self.cancelled = False
        self.clicks = []
        self.moves = []
        self.matches = []
        self.confirm_matches = 0
        self.confirmations = 0
        self.plus_clicks = 0
        self.centers = {
            books._USE_ITEM_BUTTON_TEMPLATE: (1041, 101),
            books._FIRST_ITEM_USE_BUTTON_TEMPLATE: (891, 166),
            books._CONFIRM_BUTTON_TEMPLATE: (911, 531),
            books._CANCEL_BUTTON_TEMPLATE: (291, 531),
        }
        self.regions = {
            books._USE_ITEM_BUTTON_TEMPLATE: books._USE_ITEM_BUTTON_REGION,
            books._FIRST_ITEM_USE_BUTTON_TEMPLATE: books._FIRST_ITEM_USE_BUTTON_REGION,
            books._CONFIRM_BUTTON_TEMPLATE: books._CONFIRM_BUTTON_REGION,
            books._CANCEL_BUTTON_TEMPLATE: books._CANCEL_BUTTON_REGION,
        }

    def capture(self, rect):
        return SimpleNamespace(success=True, image=np.zeros((2, 2, 3), dtype=np.uint8))

    def find_template(self, **kwargs):
        template = Path(kwargs["template_image"]).relative_to(books._PLAN_ROOT).as_posix()
        self.matches.append((template, kwargs))
        visible = {
            "buy": {books._USE_ITEM_BUTTON_TEMPLATE, books._BUY_PAGE_READY_TEMPLATE},
            "modal": {books._ITEM_MODAL_HEADER_TEMPLATE, books._ITEM_IDENTITY_TEMPLATE,
                      books._FIRST_ITEM_USE_BUTTON_TEMPLATE},
            "quantity": {books._QUANTITY_PROMPT_TEMPLATE, books._CONFIRM_BUTTON_TEMPLATE,
                         books._CANCEL_BUTTON_TEMPLATE},
        }[self.page]
        if template == books._CONFIRM_BUTTON_TEMPLATE and self.page == "quantity":
            self.confirm_matches += 1
        # The dialog guards have seen two stable observations before and after
        # quantity input; the consuming click must still rematch its button.
        missing = template == self.missing or (
            self.missing == "confirm_rematch" and
            template == books._CONFIRM_BUTTON_TEMPLATE and self.confirm_matches > 4
        )
        center = self.centers.get(template, (10, 10))
        region = self.regions.get(template, [0, 0, 0, 0])
        return SimpleNamespace(found=template in visible and not missing, confidence=.97,
                               center_point=(center[0] - region[0], center[1] - region[1]))

    def move_to(self, x, y, duration):
        self.moves.append((x, y))
        if (x, y) == self.centers.get(self.cancel_on_move):
            self.cancelled = True

    def click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == self.centers[books._USE_ITEM_BUTTON_TEMPLATE]:
            assert self.page == "buy"
            self.page = "modal"
        elif (x, y) == self.centers[books._FIRST_ITEM_USE_BUTTON_TEMPLATE]:
            assert self.page == "modal"
            self.page = "quantity"
        elif (x, y) == books._PLUS_ONE_POINT:
            assert self.page == "quantity"
            self.plus_clicks += 1
        elif (x, y) == self.centers[books._CONFIRM_BUTTON_TEMPLATE]:
            assert self.page == "quantity"
            self.confirmations += 1
            self.page = "buy"
        elif (x, y) == self.centers[books._CANCEL_BUTTON_TEMPLATE]:
            assert self.page == "quantity"
            self.page = "buy"
        else:
            pytest.fail(f"Unexpected fixed-point input: {(x, y)}")


@pytest.fixture
def run_books(monkeypatch):
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(books, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))

    def run(ui, count=3):
        monkeypatch.setattr(books, "is_current_task_cancel_requested", lambda: ui.cancelled)
        return books.resonance_pc_use_purchase_books(
            count, app=ui, vision=ui, open_timeout_sec=.3,
            dialog_timeout_sec=.3, click_interval_sec=.05,
        )

    return run


def test_relocated_template_centers_are_used_and_consumption_happens_once(run_books):
    ui = BookUI()
    result = run_books(ui)
    assert result["used"] == 3
    assert result["plus_clicks"] == ui.plus_clicks == 2
    assert ui.confirmations == 1
    assert ui.moves == [ui.centers[key] for key in (
        books._USE_ITEM_BUTTON_TEMPLATE, books._FIRST_ITEM_USE_BUTTON_TEMPLATE,
        books._CONFIRM_BUTTON_TEMPLATE,
    )]
    for template, match in ui.matches:
        assert match["threshold"] == (
            .82 if template in (books._USE_ITEM_BUTTON_TEMPLATE, books._FIRST_ITEM_USE_BUTTON_TEMPLATE)
            else .86 if template == books._ITEM_MODAL_HEADER_TEMPLATE else .8
        )
        assert match["use_grayscale"] is True
        assert match["preprocess"] == "none"


@pytest.mark.parametrize("missing,click_count,cause", [
    (books._USE_ITEM_BUTTON_TEMPLATE, 0, "purchase_book_use_items_button_not_found"),
    (books._FIRST_ITEM_USE_BUTTON_TEMPLATE, 1, "purchase_book_first_use_button_not_found"),
])
def test_missing_template_stops_public_flow_without_coordinate_input(run_books, missing, click_count, cause):
    ui = BookUI(missing=missing)
    with pytest.raises(books.PurchaseBookUseError) as caught:
        run_books(ui)
    detail = caught.value.detail["cause"]
    assert detail["code"] == cause
    assert "fallback_point_disabled" not in detail["detail"]
    assert detail["detail"]["cleanup"]["attempted"] is False
    assert len(ui.clicks) == click_count
    assert ui.confirmations == ui.plus_clicks == 0


def test_confirm_must_be_rematched_and_failure_only_cancels_dialog(run_books):
    ui = BookUI(missing="confirm_rematch")
    with pytest.raises(books.PurchaseBookUseError) as caught:
        run_books(ui)
    cause = caught.value.detail["cause"]
    assert cause["code"] == "purchase_book_confirm_button_not_found"
    assert cause["detail"]["cleanup"]["succeeded"] is True
    assert ui.confirmations == 0
    assert ui.clicks[-1] == ui.centers[books._CANCEL_BUTTON_TEMPLATE]
    assert ui.page == "buy"


def test_cancel_during_confirm_pointer_move_never_consumes_or_runs_cleanup(run_books):
    ui = BookUI(cancel_on_move=books._CONFIRM_BUTTON_TEMPLATE)
    with pytest.raises(asyncio.CancelledError):
        run_books(ui)
    assert ui.confirmations == 0
    assert ui.plus_clicks == 2
    assert ui.page == "quantity"
    assert ui.centers[books._CANCEL_BUTTON_TEMPLATE] not in ui.clicks

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
        self.events = []
        self.frame_templates = {}
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
        frame_id = len(self.frame_templates) + 1
        visible = {
            "buy": {books._USE_ITEM_BUTTON_TEMPLATE, books._BUY_PAGE_READY_TEMPLATE},
            "modal": {books._ITEM_MODAL_HEADER_TEMPLATE, books._ITEM_IDENTITY_TEMPLATE,
                      books._FIRST_ITEM_USE_BUTTON_TEMPLATE},
            "quantity": {books._QUANTITY_PROMPT_TEMPLATE, books._CONFIRM_BUTTON_TEMPLATE,
                         books._CANCEL_BUTTON_TEMPLATE},
        }[self.page]
        self.frame_templates[frame_id] = visible
        self.events.append(("capture", frame_id, tuple(rect), self.page, self.plus_clicks))
        # The application returns pixels covering the requested ROI. Each image
        # keeps its own state even if the UI changes before matching finishes.
        return SimpleNamespace(
            success=True, image=np.full((rect[3], rect[2], 3), frame_id, dtype=np.uint8),
        )

    def find_template(self, **kwargs):
        template = Path(kwargs["template_image"]).relative_to(books._PLAN_ROOT).as_posix()
        self.matches.append((template, kwargs))
        frame_id = int(kwargs["source_image"][0, 0, 0])
        self.events.append(("match", frame_id, template))
        visible = self.frame_templates[frame_id]
        if template == books._CONFIRM_BUTTON_TEMPLATE and template in visible:
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
        self.events.append(("move", (x, y)))
        self.moves.append((x, y))
        if (x, y) == self.centers.get(self.cancel_on_move):
            self.cancelled = True

    def click(self, x, y):
        self.events.append(("click", (x, y)))
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


def test_public_flow_reuses_each_round_but_refreshes_at_stability_and_input_boundaries(run_books):
    ui = BookUI()
    result = run_books(ui)
    captures = [event for event in ui.events if event[0] == "capture"]
    matches_by_frame = {
        capture[1]: [event[2] for event in ui.events
                     if event[0] == "match" and event[1] == capture[1]]
        for capture in captures
    }
    quantity_templates = [books._QUANTITY_PROMPT_TEMPLATE, books._CONFIRM_BUTTON_TEMPLATE]
    return_templates = [books._BUY_PAGE_READY_TEMPLATE, books._QUANTITY_PROMPT_TEMPLATE,
                        books._CONFIRM_BUTTON_TEMPLATE, books._CANCEL_BUTTON_TEMPLATE]

    # Five state guards require two independent rounds each. Three input
    # controls require a separate capture to locate their current buttons.
    assert len(captures) == 13
    assert [state["attempts"] for state in result["batches"][0]["state_trace"]] == [2] * 5
    assert [state["stable_count"] for state in result["batches"][0]["state_trace"]] == [2] * 5
    assert [matches_by_frame[capture[1]] for capture in captures] == [
        [books._USE_ITEM_BUTTON_TEMPLATE],
        [books._ITEM_MODAL_HEADER_TEMPLATE], [books._ITEM_MODAL_HEADER_TEMPLATE],
        [books._ITEM_IDENTITY_TEMPLATE], [books._ITEM_IDENTITY_TEMPLATE],
        [books._FIRST_ITEM_USE_BUTTON_TEMPLATE],
        quantity_templates, quantity_templates, quantity_templates, quantity_templates,
        [books._CONFIRM_BUTTON_TEMPLATE], return_templates, return_templates,
    ]

    # Both pre-increment rounds see quantity zero; both later rounds see the
    # changed image after the two plus clicks. No cached frame crosses input.
    quantity_frames = [capture for capture in captures
                       if matches_by_frame[capture[1]] == quantity_templates]
    assert [capture[4] for capture in quantity_frames] == [0, 0, 2, 2]
    assert len({capture[1] for capture in quantity_frames}) == 4
    for template in (books._USE_ITEM_BUTTON_TEMPLATE, books._FIRST_ITEM_USE_BUTTON_TEMPLATE,
                     books._CONFIRM_BUTTON_TEMPLATE):
        click_index = ui.events.index(("click", ui.centers[template]))
        capture, match, move = ui.events[click_index - 3:click_index]
        assert capture[0] == "capture"
        assert match == ("match", capture[1], template)
        assert move == ("move", ui.centers[template])
        assert matches_by_frame[capture[1]] == [template]
        assert ui.events[click_index + 1][0] == "capture"
        assert ui.events[click_index + 1][1] > capture[1]


@pytest.mark.parametrize("states,required_absent,expected_attempts,matched", [
    # Stable success must use distinct snapshots; an intervening miss resets it.
    ([{"prompt", "confirm"}, {"prompt"}, {"prompt", "confirm"}, {"prompt", "confirm"}],
     False, 4, True),
    # Neither image satisfies all_of: combining one match from each would lie.
    ([{"prompt"}, {"confirm"}] * 5, False, 5, False),
    # Neither image satisfies all_of + none_of: a stale positive page marker
    # must not be combined with a newer absence of the modal.
    ([{"ready", "prompt"}, set()] * 5, True, 5, False),
])
def test_state_guards_never_mix_pixels_or_reuse_previous_stability_round(
    run_books, states, required_absent, expected_attempts, matched,
):
    template_names = {
        "prompt": books._QUANTITY_PROMPT_TEMPLATE,
        "confirm": books._CONFIRM_BUTTON_TEMPLATE,
        "ready": books._BUY_PAGE_READY_TEMPLATE,
    }

    class ChangingUI(BookUI):
        def capture(self, rect):
            capture = super().capture(rect)
            frame_id = int(capture.image[0, 0, 0])
            self.frame_templates[frame_id] = {template_names[name] for name in states[frame_id - 1]}
            return capture

    ui = ChangingUI()
    prompt = books._template_spec("prompt", books._QUANTITY_PROMPT_TEMPLATE,
                                  books._QUANTITY_PROMPT_REGION)
    confirm = books._template_spec("confirm", books._CONFIRM_BUTTON_TEMPLATE,
                                   books._CONFIRM_BUTTON_REGION)
    ready = books._template_spec("ready", books._BUY_PAGE_READY_TEMPLATE,
                                 books._BUY_PAGE_READY_REGION)
    result = books._wait_for_template_state(
        ui, ui, state_name="changing_frame", all_of=[ready] if required_absent else [prompt, confirm],
        none_of=[prompt] if required_absent else [], timeout_sec=.2, interval_sec=.05,
    )
    assert result["matched"] is matched
    assert result["attempts"] == expected_attempts
    captures = [event for event in ui.events if event[0] == "capture"]
    matches = [event for event in ui.events if event[0] == "match"]
    assert len(captures) == expected_attempts
    assert [event[1] for event in matches] == [frame for frame in range(1, expected_attempts + 1)
                                             for _ in range(2)]
    expected_rect = (420, 400, 810, 300) if required_absent else (420, 400, 850, 185)
    assert all(event[2] == expected_rect for event in captures)
    shapes = {template: kwargs["source_image"].shape for template, kwargs in ui.matches}
    assert shapes[books._QUANTITY_PROMPT_TEMPLATE] == (90, 430, 3)
    if required_absent:
        assert shapes[books._BUY_PAGE_READY_TEMPLATE] == (110, 380, 3)
    else:
        assert shapes[books._CONFIRM_BUTTON_TEMPLATE] == (85, 620, 3)

"""Offline contracts for viewport scanning, local badge feedback and single submit."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from plans.resonance_pc.src.actions import black_moon_local_select_pc_actions as select


def card(item="a", *, x=600, y=150, state="unselected", partial=False):
    return {"item_id": item, "rect": (x, y, 320, 120), "state": state,
            "partial": partial, "geometry_score": .99}


class App:
    def __init__(self, pages, *, fail_clicks=0, unknown_after_click=False, view_keys=None):
        self.pages = deepcopy(pages)
        self.view_keys = list(range(len(pages))) if view_keys is None else view_keys
        self.page = self.captures = self.purchase_clicks = 0
        self.snapshots, self.clicks, self.drags = {}, [], []
        self.fail_clicks, self.unknown_after_click = fail_clicks, unknown_after_click
        self.ready, self.capture_error = True, False

    def capture(self):
        self.captures += 1
        token = self.captures % 255
        self.snapshots[token] = {"cards": deepcopy(self.pages[self.page]),
                                 "ready": self.ready, "page": self.page}
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[139:650, 590:1270] = 20 + self.view_keys[self.page] * 7
        for current in self.pages[self.page]:
            x, y, w, h = current["rect"]
            frame[max(139, y):min(650, y + h), x:x + w] = {
                "selected": 200, "unselected": 60, "unknown": 120}[current["state"]]
        frame[0, 0] = token
        return SimpleNamespace(success=not self.capture_error, image=frame)

    def click(self, x, y):
        self.clicks.append((x, y, self.captures))
        if y >= 660:
            self.purchase_clicks += 1
            return
        for current in self.pages[self.page]:
            cx, cy, w, h = current["rect"]
            if cx <= x < cx + w and cy <= y < cy + h:
                if self.unknown_after_click:
                    current["state"] = "unknown"
                elif len(self.clicks) > self.fail_clicks:
                    current["state"] = "selected"
                return
        raise AssertionError(f"Unexpected click outside a card: {(x, y)}")

    def drag(self, **kwargs):
        self.drags.append(kwargs)
        assert kwargs["end_y"] < kwargs["start_y"]
        self.page = min(self.page + 1, len(self.pages) - 1)


class Reader:
    VIEWPORT = (590, 139, 680, 511)
    known_ids = {"a", "b", "c"}

    def __init__(self, app):
        self.app = app
        self.identity_calls, self.mark_calls, self.sold_pages = [], [], set()
        self.button_hit = {"found": True, "center": (1180, 680)}

    def snapshot(self, frame):
        return self.app.snapshots[int(frame[0, 0, 0])]

    def ready(self, frame):
        return self.snapshot(frame)["ready"]

    def total_zero(self, frame):
        return not any(row["state"] == "selected" for row in self.snapshot(frame)["cards"])

    def cards(self, frame):
        return deepcopy(self.snapshot(frame)["cards"])

    def identify(self, frame, current, candidates=None):
        self.identity_calls.append((self.app.captures, current["item_id"], candidates))
        if current["state"] not in {"selected", "unselected"}:
            return None
        if current["item_id"] not in ({"a"} if candidates is None else candidates):
            return None
        return {"item_id": current["item_id"], "score": .97}

    def selected_mark(self, frame, current):
        self.mark_calls.append((self.app.captures, current["rect"]))
        return {"found": any(fresh["rect"] == current["rect"] and fresh["state"] == "selected"
                             for fresh in self.snapshot(frame)["cards"])}

    def sold_out_markers(self, frame):
        if self.snapshot(frame)["page"] in self.sold_pages:
            return [{"row_top": 412, "column": 600, "score": .99,
                     "marker_rect": [800, 440, 80, 40]}]
        return []

    def button(self, frame):
        return deepcopy(self.button_hit)


@pytest.fixture(autouse=True)
def fast_wait(monkeypatch):
    clock = SimpleNamespace(now=0.)
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        clock.now += seconds
        await original_sleep(0)

    monkeypatch.setattr(select, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(select.asyncio, "sleep", sleep)
    monkeypatch.setattr(select, "_POLL_SECONDS", .001)
    monkeypatch.setattr(select, "_SETTLE_SECONDS", .001)
    monkeypatch.setattr(select, "_WAIT_SECONDS", .012)
    return clock


def run_selection(pages=None, table=None, *, app=None, reader=None):
    app = app or App(pages)
    reader = reader or Reader(app)
    result = asyncio.run(select.execute_black_moon_select(
        purchase_table=table or {"a": True, "b": False, "c": False}, app=app, reader=reader))
    return result, app, reader


def completed_selection(has_goods=True):
    return {"success": True, "scan_complete": True, "end_reason": "unchanged_view",
            "bottom_confirmed": False, "scroll_count": 2, "unchanged_drag_count": 2,
            "has_selected_goods": has_goods, "matched_item_ids": ["a"] if has_goods else []}


def test_normalize_purchase_table_preserves_bool_settings_without_mutation():
    table = {"a": True, "b": False}
    assert select.normalize_purchase_table(table, known_ids={"a", "b", "c"}) == table
    assert table == {"a": True, "b": False}


@pytest.mark.parametrize("table", [None, [], {}, {"a": 1}, {"a": "true"}, {"a": None}, {"unknown": True}])
def test_invalid_purchase_table_is_rejected(table):
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        select.normalize_purchase_table(table, known_ids={"a", "b", "c"})


def test_preselected_target_is_identified_without_reclick():
    result, app, reader = run_selection([[card(state="selected"), card("b", x=940)]])
    assert result["has_selected_goods"] and result["matched_item_ids"] == ["a"]
    assert result["scan_complete"] and not result["bottom_confirmed"]
    assert not app.clicks and any(item == "a" for _, item, _ in reader.identity_calls)


def test_only_enabled_targets_are_selected_without_entry_counts():
    result, app, reader = run_selection([[card(), card("b", x=940)]])
    assert result["has_selected_goods"] and result["matched_item_ids"] == ["a"]
    assert len(app.clicks) == 1 and not app.purchase_clicks
    assert all(candidates is None or set(candidates) == {"a"} for _, _, candidates in reader.identity_calls)
    assert not {"selected_items", "selected_new_count", "selected_existing_count"}.intersection(result)


def test_same_product_on_two_cards_selects_both_but_records_only_presence():
    result, app, _ = run_selection([[card(), card(x=940)]])
    assert len(app.clicks) == 2
    assert result["has_selected_goods"] and result["matched_item_ids"] == ["a"]


def test_partial_and_unknown_cards_are_skipped_without_identity_or_click():
    result, app, reader = run_selection([[card(partial=True), card(x=940, state="unknown")]])
    assert result["scan_complete"] and not result["has_selected_goods"]
    assert not app.clicks and not reader.identity_calls


def test_no_target_on_first_page_still_scans_later_pages():
    result, app, _ = run_selection([[card("b")], [card("c")], [card()]])
    assert result["has_selected_goods"] and app.page == 2 and len(app.clicks) == 1
    assert result["scroll_count"] == 4 and result["unchanged_drag_count"] == 2


def test_absent_target_stops_after_two_unchanged_full_view_drags():
    result, app, _ = run_selection([[card("b")], [card("b")]])
    assert result["scan_complete"] and result["end_reason"] == "unchanged_view"
    assert not result["has_selected_goods"] and result["scroll_count"] == 3
    assert app.page == 1 and not app.clicks


def test_full_view_change_resets_consecutive_unchanged_counter():
    app = App([[card("b")] for _ in range(5)], view_keys=[0, 0, 1, 1, 1])
    result, app, _ = run_selection(app=app)
    # One unchanged drag, a change only in non-target list pixels, then two unchanged drags.
    assert result["end_reason"] == "unchanged_view" and result["scroll_count"] == 4
    assert result["unchanged_drag_count"] == 2 and app.page == 4


def test_selection_feedback_changes_are_excluded_from_pre_drag_baseline():
    result, app, _ = run_selection([[card()]])
    assert len(app.clicks) == 1 and result["scroll_count"] == 2
    assert result["unchanged_drag_count"] == 2


def test_empty_purchase_targets_do_not_capture_click_or_drag():
    result, app, reader = run_selection([[card()]], table={"a": False, "b": False})
    assert result["success"] and not result["has_selected_goods"]
    assert not result["scan_complete"] and result["reason"] == "no_purchase_targets"
    assert not app.captures and not app.clicks and not app.drags and not reader.identity_calls


def test_click_failure_uses_relative_card_badge_and_fresh_probe_before_supplement():
    app = App([[card(x=940, y=281)]], fail_clicks=1)
    result, app, reader = run_selection(app=app)
    assert result["has_selected_goods"] and len(app.clicks) == 2
    assert app.clicks[1][2] > app.clicks[0][2]
    assert all(rect == (940, 281, 320, 120) for _, rect in reader.mark_calls)
    assert max(capture for capture, _ in reader.mark_calls) > app.clicks[-1][2]


def test_delayed_selected_badge_does_not_cause_supplemental_click():
    app = App([[card()]])
    reader = Reader(app)
    original_mark = reader.selected_mark

    def mark(frame, current):
        hit = original_mark(frame, current)
        return {"found": False} if app.clicks and len(reader.mark_calls) < 5 else hit

    reader.selected_mark = mark
    result, app, _ = run_selection(app=app, reader=reader)
    assert result["has_selected_goods"] and len(app.clicks) == 1


def test_unknown_feedback_never_allows_blind_reclick():
    app = App([[card()]], unknown_after_click=True)
    with pytest.raises(select.BlackMoonLocalSelectError) as error:
        run_selection(app=app)
    assert error.value.code == "black_moon_select_identity_changed"
    assert len(app.clicks) == 1 and not app.drags


def test_repeated_click_failure_has_only_three_supplements():
    app = App([[card()]], fail_clicks=99)
    with pytest.raises(select.BlackMoonLocalSelectError) as error:
        run_selection(app=app)
    assert error.value.code == "black_moon_select_click_exhausted"
    assert len(app.clicks) == 4 and not app.purchase_clicks
    assert all(after[2] > before[2] for before, after in zip(app.clicks, app.clicks[1:]))


def test_fresh_identity_mismatch_rejects_before_click():
    app = App([[card()]])
    reader = Reader(app)
    original_identify = reader.identify

    def identify(frame, current, candidates=None):
        return None if candidates is not None else original_identify(frame, current, candidates)

    reader.identify = identify
    with pytest.raises(select.BlackMoonLocalSelectError) as error:
        run_selection(app=app, reader=reader)
    assert error.value.code == "black_moon_select_identity_changed" and not app.clicks


def test_retained_position_is_used_without_initial_zero_check_or_reset():
    app = App([[card()], [card("b")], [card(state="selected")]])
    app.page = 2
    reader = Reader(app)
    reader.total_zero = lambda frame: pytest.fail("Initial total must not be checked")
    result, app, _ = run_selection(app=app, reader=reader)
    assert result["has_selected_goods"] and app.page == 2 and not app.clicks
    assert all(drag["end_y"] < drag["start_y"] for drag in app.drags)


def test_sold_out_ends_after_current_page_target_is_selected():
    app = App([[card()]])
    reader = Reader(app)
    reader.sold_pages = {0}
    result, app, _ = run_selection(app=app, reader=reader)
    assert result["end_reason"] == "sold_out_boundary" and result["has_selected_goods"]
    assert result["boundary_evidence"]["current_page_processed"] is True
    assert result["boundary_evidence"]["stable_frames"] == 2
    assert len(app.clicks) == 1 and not app.drags


def test_eleventh_drag_page_is_processed_before_limit_stops_scan():
    app = App([[card("b")] for _ in range(11)] + [[card()], [card("c")]])
    result, app, _ = run_selection(app=app)
    assert result["scan_complete"] and result["end_reason"] == "drag_limit"
    assert result["has_selected_goods"] and result["scroll_count"] == len(app.drags) == 11
    assert app.page == 11 and len(app.clicks) == 1
    assert all(drag["hold_before_release_sec"] == .5 and drag["duration"] == .45
               for drag in app.drags)


@pytest.mark.parametrize("capture_error,ready", [(True, True), (False, False)])
def test_capture_failure_or_nonready_page_never_reaches_input(capture_error, ready):
    app = App([[card()]])
    app.capture_error, app.ready = capture_error, ready
    with pytest.raises(RuntimeError):
        run_selection(app=app)
    assert not app.clicks and not app.drags


@pytest.mark.parametrize("after_click", [False, True])
def test_cancellation_stops_without_another_input(monkeypatch, after_click):
    app = App([[card()]])

    def cancel():
        if not after_click or app.clicks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(select, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        run_selection(app=app)
    assert len(app.clicks) == int(after_click) and not app.drags


def submit(selection, app=None, reader=None):
    app = app or App([[card(state="selected")]])
    reader = reader or Reader(app)
    return asyncio.run(select.execute_black_moon_submit(selection=selection, app=app, reader=reader)), app


def test_submit_has_exactly_one_click_without_claiming_verified_purchase():
    result, app = submit(completed_selection())
    assert app.purchase_clicks == len(app.clicks) == 1 and not app.drags
    assert result["status"] == "purchase_submitted" and result["purchase_verified"] is False
    assert result["has_selected_goods"] and result["matched_item_ids"] == ["a"]


def test_no_target_selection_skips_payment_with_completed_scan_for_exit():
    result, app = submit(completed_selection(False))
    assert not app.captures and not app.clicks and not result["purchase_submitted"]
    assert result["reason"] == "no_selected_goods" and result["scan"]["scan_complete"]
    assert not result["scan"]["has_selected_goods"]


@pytest.mark.parametrize("selection", [None, {}, {**completed_selection(), "success": False},
                                      {**completed_selection(), "scan_complete": False}])
def test_failed_or_unfinished_scan_cannot_submit(selection):
    app = App([[card(state="selected")]])
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        submit(selection, app=app)
    assert not app.clicks


@pytest.mark.parametrize("presence", [{"has_selected_goods": 1}, {"has_selected_goods": "true"},
                                     {"has_selected_goods": False}, {"matched_item_ids": []},
                                     {"matched_item_ids": ["a", "a"]}])
def test_inconsistent_or_nonboolean_presence_rejected_without_click(presence):
    app = App([[card(state="selected")]])
    with pytest.raises(ValueError):
        submit({**completed_selection(), **presence}, app=app)
    assert not app.captures and not app.clicks


def test_missing_purchase_button_cannot_submit():
    app = App([[card(state="selected")]])
    reader = Reader(app)
    reader.button_hit = {"found": False, "center": None}
    with pytest.raises(RuntimeError):
        submit(completed_selection(), app, reader)
    assert not app.clicks


def test_task_defaults_to_select_only_and_validates_before_input():
    root = Path(__file__).resolve().parents[2]
    task = yaml.safe_load((root / "plans/resonance_pc/tasks/black_moon_local_select_pc.yaml").read_text(
        encoding="utf-8"))["black_moon_local_select_pc"]
    inputs = {row["name"]: row for row in task["meta"]["inputs"]}
    assert inputs["purchase_table"]["required"] and inputs["submit_purchase"]["default"] is False
    steps = task["steps"]
    assert steps["require_resolution"]["depends_on"] == "validate_table"
    assert "candidate_count > 0" in steps["enable_batch"]["when"]
    assert steps["select_goods"]["depends_on"] == {"enable_batch": "success|skipped"}
    assert "require_empty_selection" not in steps["select_goods"]["params"]
    assert steps["submit"]["when"] == "{{ inputs.submit_purchase }}"

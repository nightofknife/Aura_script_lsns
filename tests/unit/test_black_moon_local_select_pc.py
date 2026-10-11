"""Offline behavioral contracts for LOCAL target selection and single submit."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from plans.resonance_pc.src.actions import black_moon_local_select_pc_actions as select


def card(item="a", *, x=600, y=150, state="unselected", partial=False):
    return {"item_id": item, "rect": (x, y, 320, 120), "state": state, "partial": partial}


class App:
    def __init__(self, pages, *, fail_clicks=0, unknown_after_click=False):
        self.pages = deepcopy(pages)
        self.page = 0
        self.captures = 0
        self.snapshots = {}
        self.clicks = []
        self.drags = []
        self.fail_clicks = fail_clicks
        self.unknown_after_click = unknown_after_click
        self.ready = True
        self.capture_error = False
        self.purchase_clicks = 0

    def capture(self):
        self.captures += 1
        token = self.captures % 255
        self.snapshots[token] = {"cards": deepcopy(self.pages[self.page]), "ready": self.ready,
                                 "page": self.page}
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[139:650, 590:1270] = self.page * 10 + 30
        for current in self.pages[self.page]:
            x, y, w, h = current["rect"]
            frame[max(139, y):min(650, y + h), x:x + w] = {
                "selected": 180, "unselected": 80, "unknown": 120}[current["state"]]
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

    def drag(self, start_x, start_y, end_x, end_y, duration, hold_before_release_sec):
        self.drags.append((start_x, start_y, end_x, end_y))
        if end_y < start_y:
            self.page = min(self.page + 1, len(self.pages) - 1)
        else:
            self.page = max(0, self.page - 1)


class Reader:
    VIEWPORT = (590, 139, 680, 511)
    known_ids = {"a", "b", "c"}

    def __init__(self, app):
        self.app = app
        self.identity_calls = []
        self.identity_states = []
        self.state_calls = []
        self.card_calls = []
        self.zero = None
        self.wanted = {"a"}
        self.button_hit = {"found": True, "center": (1180, 680)}
        self.before_identify = None

    def snapshot(self, frame):
        return self.app.snapshots[int(frame[0, 0, 0])]

    def ready(self, frame):
        return self.snapshot(frame)["ready"]

    def total_zero(self, frame):
        return self.zero if self.zero is not None else not any(
            row["state"] == "selected" for row in self.snapshot(frame)["cards"])

    def cards(self, frame):
        self.card_calls.append(self.app.captures)
        return deepcopy(self.snapshot(frame)["cards"])

    def identify(self, frame, current, candidates=None):
        self.identity_calls.append((self.app.captures, current["item_id"], candidates))
        self.identity_states.append(current["state"])
        if self.before_identify:
            self.before_identify(current, candidates)
        if current["item_id"] not in (self.wanted if candidates is None else candidates):
            return None
        return {"item_id": current["item_id"], "score": .97}

    def state(self, frame, current):
        self.state_calls.append(self.app.captures)
        for fresh in self.snapshot(frame)["cards"]:
            if fresh["rect"] == current["rect"]:
                return fresh["state"]
        return "unknown"

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
    monkeypatch.setattr(select, "_MAX_DRAGS", 12)
    return clock


def run_selection(pages, table=None, *, app=None, reader=None, require_empty_selection=False):
    app = app or App(pages)
    reader = reader or Reader(app)
    result = asyncio.run(select.execute_black_moon_select(
        purchase_table=table or {"a": True, "b": False, "c": False}, app=app, reader=reader,
        require_empty_selection=require_empty_selection))
    return result, app, reader


def test_normalize_purchase_table_preserves_bool_settings_without_mutating_input():
    table = {"a": True, "b": False}
    result = select.normalize_purchase_table(table, known_ids={"a", "b", "c"})
    assert result == table and table == {"a": True, "b": False}


@pytest.mark.parametrize("table", [None, [], {}, {"a": 1}, {"a": "true"}, {"a": None}, {"unknown": True}])
def test_invalid_purchase_table_is_rejected(table):
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        select.normalize_purchase_table(table, known_ids={"a", "b", "c"})


def test_selected_card_is_skipped_without_identity_matching_or_click():
    result, app, reader = run_selection([[card(state="selected"), card("b", x=940)]])
    assert result["success"] and result["bottom_confirmed"]
    assert result["selected_new_count"] == 0 and not app.clicks
    assert not any(item == "a" for _, item, _ in reader.identity_calls)


def test_only_true_targets_are_matched_and_selected():
    result, app, reader = run_selection([[card(), card("b", x=940)]])
    assert result["selected_new_count"] == 1
    assert result["selected_items"][0]["item_id"] == "a"
    assert len(app.clicks) == 1 and app.purchase_clicks == 0
    assert all(candidates is None or set(candidates) == {"a"} for _, _, candidates in reader.identity_calls)


def test_same_item_on_two_independent_cards_is_selected_twice():
    result, app, _ = run_selection([[card(), card(x=940)]])
    assert result["selected_new_count"] == 2 and len(app.clicks) == 2
    assert [row["item_id"] for row in result["selected_items"]] == ["a", "a"]


def test_partial_and_unknown_cards_never_click_or_match_identity():
    result, app, reader = run_selection([[card(partial=True), card(x=940, state="unknown")], [card("b")]])
    assert result["selected_new_count"] == 0 and not app.clicks
    assert not any(item == "a" for _, item, _ in reader.identity_calls)


def test_no_target_on_first_page_still_scrolls_until_later_target_and_bottom():
    result, app, _ = run_selection([[card("b")], [card("c")], [card()]])
    assert result["bottom_confirmed"] and result["selected_new_count"] == 1
    assert app.page == 2 and len(app.clicks) == 1


def test_absent_target_terminates_at_bottom_not_when_target_count_reached():
    result, app, _ = run_selection([[card("b")], [card("c")]])
    assert result["success"] and result["bottom_confirmed"]
    assert result["selected_new_count"] == 0 and app.page == 1


def test_empty_purchase_targets_do_not_click_or_drag():
    result, app, reader = run_selection([[card()]], table={"a": False, "b": False})
    assert result["success"] and result["selected_new_count"] == 0
    assert not result["bottom_confirmed"] and result["reason"] == "no_purchase_targets"
    assert not app.clicks and not app.drags and not reader.identity_calls


def test_scroll_overlap_selected_target_skips_identity_but_unselected_copy_does_not():
    result, app, reader = run_selection([[card(state="selected")], [card(state="selected"), card(x=940)]])
    assert result["selected_new_count"] == 1 and len(app.clicks) == 1
    assert all(state == "unselected" for state in reader.identity_states)


def test_click_failure_is_retried_with_fresh_observation_then_selected():
    app = App([[card()]], fail_clicks=1)
    result, app, reader = run_selection([], app=app)
    assert result["selected_new_count"] == 1 and len(app.clicks) == 2
    assert result["selected_items"][0]["click_count"] == 2
    assert app.clicks[1][2] > app.clicks[0][2] and reader.state_calls


def test_delayed_selected_feedback_waits_without_supplemental_click():
    app = App([[card()]])
    reader = Reader(app)
    original_state = reader.state

    def state(frame, current):
        observed = original_state(frame, current)
        return "unselected" if len(reader.state_calls) < 5 else observed

    reader.state = state
    result, app, _ = run_selection([], app=app, reader=reader)
    assert result["selected_new_count"] == 1 and len(app.clicks) == 1
    assert len(reader.state_calls) >= 6


def test_selected_arriving_during_fresh_retry_probe_prevents_reclick():
    app = App([[card()]])
    reader = Reader(app)
    original_state = reader.state

    def state(frame, current):
        original_state(frame, current)
        return "unselected"

    reader.state = state
    result, app, _ = run_selection([], app=app, reader=reader)
    assert result["selected_new_count"] == 1 and len(app.clicks) == 1
    assert result["selected_items"][0]["click_count"] == 1


def test_unknown_state_after_click_never_allows_blind_reclick():
    app = App([[card()]], unknown_after_click=True)
    with pytest.raises(RuntimeError):
        run_selection([], app=app)
    assert len(app.clicks) == 1


def test_repeated_click_failure_has_only_three_supplements():
    app = App([[card()]], fail_clicks=99)
    with pytest.raises(RuntimeError):
        run_selection([], app=app)
    assert len(app.clicks) == 4 and app.purchase_clicks == 0
    assert all(after[2] > before[2] for before, after in zip(app.clicks, app.clicks[1:]))


def test_nonempty_preselection_check_rejects_before_any_input():
    app = App([[card()]])
    reader = Reader(app)
    reader.zero = False
    with pytest.raises(RuntimeError):
        run_selection([], app=app, reader=reader, require_empty_selection=True)
    assert not app.clicks and not app.drags


def test_capture_failure_never_reaches_input():
    app = App([[card()]])
    app.capture_error = True
    with pytest.raises(RuntimeError):
        run_selection([], app=app)
    assert not app.clicks and not app.drags


def test_selection_does_not_submit_purchase():
    result, app, _ = run_selection([[card()]])
    assert result["selected_new_count"] == 1 and app.purchase_clicks == 0


def test_after_click_confirmation_does_not_rematch_item_identity():
    result, app, reader = run_selection([[card()]])
    assert result["selected_new_count"] == 1
    assert all(capture <= app.clicks[0][2] for capture, _, _ in reader.identity_calls)
    assert reader.state_calls and min(reader.state_calls) > app.clicks[0][2]


def test_fresh_local_identity_mismatch_skips_card_without_click():
    app = App([[card()]])
    reader = Reader(app)
    original_identify = reader.identify

    def identify(frame, current, candidates=None):
        hit = original_identify(frame, current, candidates)
        return None if candidates is not None else hit

    reader.identify = identify
    result, app, _ = run_selection([], app=app, reader=reader)
    assert result["selected_new_count"] == 0 and not app.clicks


def test_retained_scroll_position_is_reset_before_scan():
    app = App([[card()], [card("b")], [card("c")]])
    app.page = 2
    result, app, _ = run_selection([], app=app)
    assert result["selected_new_count"] == 1
    assert app.drags[0][3] > app.drags[0][1]


def test_top_clipped_card_at_bottom_does_not_prevent_completion():
    result, app, _ = run_selection([[card("b")], [card("b", y=90, partial=True), card(y=300)]])
    assert result["bottom_confirmed"] and result["selected_new_count"] == 1


def test_bottom_clipped_card_at_stopped_list_is_not_complete():
    app = App([[card(y=600, partial=True)]])
    with pytest.raises(select.BlackMoonLocalSelectError):
        run_selection([], app=app)
    assert not app.clicks


def test_unknown_list_layout_is_not_treated_as_empty_store():
    app = App([[]])
    with pytest.raises(select.BlackMoonLocalSelectError):
        run_selection([], app=app)
    assert not app.clicks


def test_nonready_page_never_reaches_click_or_scroll():
    app = App([[card()]])
    app.ready = False
    with pytest.raises(select.BlackMoonLocalSelectError):
        run_selection([], app=app)
    assert not app.clicks and not app.drags


def test_cancellation_before_selection_never_reaches_input(monkeypatch):
    app = App([[card()]])

    def cancel():
        raise asyncio.CancelledError()

    monkeypatch.setattr(select, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        run_selection([], app=app)
    assert not app.clicks and not app.drags


def test_cancellation_after_click_stops_without_another_input(monkeypatch):
    app = App([[card()]])

    def cancel():
        if app.clicks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(select, "_cancel_check", cancel)
    with pytest.raises(asyncio.CancelledError):
        run_selection([], app=app)
    assert len(app.clicks) == 1


def submit(selection, app=None, reader=None):
    app = app or App([[card(state="selected")]])
    reader = reader or Reader(app)
    if isinstance(selection, dict):
        selection = {"started_empty": True, "selected_items": [], **selection}
    return asyncio.run(select.execute_black_moon_submit(selection=selection, app=app, reader=reader)), app


def test_submit_has_exactly_one_click_and_does_not_claim_verified_purchase():
    result, app = submit({"success": True, "bottom_confirmed": True, "selected_new_count": 2})
    assert app.purchase_clicks == 1 and len(app.clicks) == 1
    assert result["status"] == "purchase_submitted"
    assert result["purchase_submitted"] and result["purchase_verified"] is False
    assert not app.drags


def test_zero_selection_submit_is_skipped_without_click():
    result, app = submit({"success": True, "bottom_confirmed": True, "selected_new_count": 0})
    assert not app.clicks and not result.get("purchase_submitted", False)


@pytest.mark.parametrize("selection", [None, {}, {"success": False, "bottom_confirmed": True, "selected_new_count": 1},
                                      {"success": True, "bottom_confirmed": False, "selected_new_count": 1}])
def test_unfinished_or_failed_selection_cannot_submit(selection):
    app = App([[card(state="selected")]])
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        submit(selection, app=app)
    assert not app.clicks


def test_missing_purchase_button_cannot_submit():
    app = App([[card(state="selected")]])
    reader = Reader(app)
    reader.button_hit = {"found": False, "center": None}
    with pytest.raises(RuntimeError):
        submit({"success": True, "bottom_confirmed": True, "selected_new_count": 1}, app, reader)
    assert not app.clicks


@pytest.mark.parametrize("count", [-1, True, "1", 1.5, float("inf")])
def test_submit_rejects_invalid_selected_count_without_click(count):
    app = App([[card(state="selected")]])
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        submit({"success": True, "bottom_confirmed": True, "selected_new_count": count}, app=app)
    assert not app.clicks


def test_scan_is_bounded_when_many_pages_never_reach_bottom():
    app = App([[card("b")] for _ in range(24)])
    with pytest.raises(select.BlackMoonLocalSelectError) as error:
        run_selection([], app=app)
    assert error.value.code == "black_moon_select_scroll_limit"
    assert not app.clicks
    assert len(app.drags) <= select._MAX_DRAGS * 2 + 2


def test_task_defaults_to_select_only_and_validates_before_input():
    root = Path(__file__).resolve().parents[2]
    task = yaml.safe_load((root / "plans/resonance_pc/tasks/black_moon_local_select_pc.yaml").read_text(
        encoding="utf-8"))["black_moon_local_select_pc"]
    inputs = {row["name"]: row for row in task["meta"]["inputs"]}
    assert inputs["purchase_table"]["required"]
    assert inputs["submit_purchase"]["default"] is False
    steps = task["steps"]
    assert steps["require_resolution"]["depends_on"] == "validate_table"
    assert "candidate_count > 0" in steps["enable_batch"]["when"]
    assert steps["select_goods"]["depends_on"] == {"enable_batch": "success|skipped"}
    assert steps["select_goods"]["params"]["require_empty_selection"] == "{{ inputs.submit_purchase }}"
    assert steps["submit"]["when"] == "{{ inputs.submit_purchase }}"

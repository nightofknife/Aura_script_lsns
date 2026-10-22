"""Offline weekly visit orchestration and pre-click journal boundaries."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.context.persistence import PersistentDataService
from plans.resonance_pc.src.actions import _black_moon_local_shop_state as ledger
from plans.resonance_pc.src.actions import black_moon_local_exit_pc_actions as exits
from plans.resonance_pc.src.actions import black_moon_local_select_pc_actions as select
from plans.resonance_pc.src.actions import black_moon_local_visit_pc_actions as visit_actions


WEEK = "2026-10-12"


class City:
    def resolve_city(self, name):
        return {"city_key": name, "city_name": name}


@pytest.fixture
def store(tmp_path):
    return PersistentDataService(tmp_path)


@pytest.fixture(autouse=True)
def frozen_catalog(monkeypatch):
    monkeypatch.setattr(visit_actions, "load_black_moon_catalog", lambda: {
        "products": [{"item_id": "a"}, {"item_id": "b"}],
        "eligible_cities": ["city"], "refresh_policy": {},
    })
    monkeypatch.setattr(visit_actions, "resolve_purchase_week", lambda policy: WEEK)


def begin(store, table=None, city="city", target="rest_menu"):
    return visit_actions.resonance_pc_begin_black_moon_local_visit(
        city, {"a": True} if table is None else table, exit_target=target,
        resonance_pc_city_shop_data=City(), persistent_data=store)


def record(store, visit):
    return ledger.read_city_record(store, **{key: visit[key] for key in ("scope", "week_key", "city_key")})


def scan(items=None, **overrides):
    items = [{"item_id": "a", "score": .97}] if items is None else items
    return {"success": True, "has_selected_goods": bool(items),
            "matched_item_ids": sorted({item["item_id"] for item in items}),
            "scan_complete": True, "end_reason": "unchanged_view", "bottom_confirmed": False,
            "scroll_count": 2, "unchanged_drag_count": 2} | overrides


def submit(store, visit, selection=None, app=None):
    return asyncio.run(visit_actions.resonance_pc_submit_black_moon_local_visit(
        visit, scan() if selection is None else selection, app=app, persistent_data=store))


def complete(store, visit, submission):
    return asyncio.run(visit_actions.resonance_pc_complete_black_moon_local_visit(
        visit, submission, app=object(), persistent_data=store))


def finish(store, visit, completion):
    return asyncio.run(visit_actions.resonance_pc_finish_black_moon_local_visit(
        visit, completion, app=object(), persistent_data=store))


@pytest.fixture
def fake_ui(monkeypatch):
    events = []

    async def fake_submit(selection, app, before_submit):
        events.append("validated")
        if not selection["has_selected_goods"]:
            return {"success": True, "status": "skipped", "purchase_submitted": False}
        before_submit()
        events.append("clicked")
        return {"success": True, "status": "purchase_submitted", "purchase_submitted": True}

    async def fake_wait(submission, app):
        events.append("settled")
        return {"success": True, "ready_to_exit": True, "purchase_verified": True,
                "page_state": "local_shop"}

    async def fake_exit(completion, target, app):
        events.append(f"exit:{target}")
        return {"success": True, "page_state": target, "exited": True, "click_count": 1}

    monkeypatch.setattr(visit_actions, "execute_black_moon_submit", fake_submit)
    monkeypatch.setattr(visit_actions, "execute_black_moon_wait_purchase", fake_wait)
    monkeypatch.setattr(visit_actions, "execute_black_moon_exit", fake_exit)
    return events


@pytest.mark.parametrize("table", [{}, {"a": False}, {"a": False, "b": False}])
def test_empty_table_gate_never_initializes_ledger(store, table):
    result = begin(store, table)
    assert result["run"] is False and result["reason"] == "empty_selection"
    assert result["purchase_table"] == {"a": False, "b": False}
    assert not store.exists(ledger.PURCHASES_FILE)


def test_ineligible_city_gate_never_initializes_ledger(store):
    result = begin(store, city="elsewhere")
    assert result["run"] is False and result["reason"] == "city_not_eligible"
    assert not store.exists(ledger.PURCHASES_FILE)


@pytest.mark.parametrize("table", [{"unknown": True}, {"a": 1}, {"a": "true"},
                                    {"a": None}, [], "a"])
def test_bad_or_nonboolean_table_rejected_before_ledger_write(store, table):
    with pytest.raises(ValueError):
        begin(store, table)
    assert not store.exists(ledger.PURCHASES_FILE)


def test_complete_table_fills_missing_keys_and_does_not_alias_input():
    supplied = {"a": True}
    normalized = visit_actions.complete_purchase_table(supplied)
    supplied["a"] = False
    assert normalized == {"a": True, "b": False}


def test_same_week_same_wishlist_skips_but_changed_wishlist_runs(store, fake_ui):
    first = begin(store)
    complete(store, first, submit(store, first, scan([])))
    same = begin(store)
    assert same["run"] is False and same["reason"] == "completed_this_week"
    changed = begin(store, {"a": True, "b": True})
    assert changed["run"] is True and changed["visit_id"] != first["visit_id"]
    assert record(store, changed)["previous_visits"][0]["completed"] is True


def test_changed_week_does_not_reuse_previous_completion(store, fake_ui, monkeypatch):
    first = begin(store)
    complete(store, first, submit(store, first, scan([])))
    monkeypatch.setattr(visit_actions, "resolve_purchase_week", lambda _: "2026-10-19")
    new = begin(store)
    assert new["run"] is True and new["week_key"] == "2026-10-19"


def test_pending_written_immediately_before_mocked_click(store, fake_ui, monkeypatch):
    visit = begin(store)
    original = visit_actions.mark_pending_batch

    def journal(*args, **kwargs):
        assert fake_ui == ["validated"]
        result = original(*args, **kwargs)
        fake_ui.append("journaled")
        return result

    monkeypatch.setattr(visit_actions, "mark_pending_batch", journal)
    submission = submit(store, visit)
    assert fake_ui == ["validated", "journaled", "clicked"]
    assert record(store, visit)["batches"][submission["batch_id"]]["status"] == "pending"


def test_failed_ui_validation_does_not_leave_pending_purchase(store, monkeypatch):
    visit = begin(store)

    async def invalid(selection, app, before_submit):
        raise ValueError("UI validation failed")

    monkeypatch.setattr(visit_actions, "execute_black_moon_submit", invalid)
    with pytest.raises(ValueError):
        submit(store, visit)
    assert record(store, visit)["batches"] == {}


def test_journal_write_error_suppresses_confirmation_click(store, fake_ui, monkeypatch):
    visit = begin(store)

    def broken(*args, **kwargs):
        raise OSError("write unavailable")

    monkeypatch.setattr(visit_actions, "mark_pending_batch", broken)
    with pytest.raises(OSError):
        submit(store, visit)
    assert fake_ui == ["validated"]
    assert record(store, visit)["batches"] == {}


@pytest.mark.parametrize("error", [TimeoutError("not settled"), asyncio.CancelledError()])
def test_timeout_or_cancel_retains_pending_and_cannot_complete(store, fake_ui, monkeypatch, error):
    visit = begin(store)
    submission = submit(store, visit)

    async def unsuccessful(*args):
        raise error

    monkeypatch.setattr(visit_actions, "execute_black_moon_wait_purchase", unsuccessful)
    with pytest.raises(type(error)):
        complete(store, visit, submission)
    saved = record(store, visit)
    assert saved["completed"] is False
    assert saved["batches"][submission["batch_id"]]["status"] == "pending"
    assert not any(event.startswith("exit:") for event in fake_ui)


@pytest.mark.parametrize("override", [
    {"success": 1}, {"success": False}, {"has_selected_goods": 1},
    {"has_selected_goods": False}, {"matched_item_ids": []},
    {"matched_item_ids": None}, {"matched_item_ids": "a"},
    {"matched_item_ids": ["a", "a"]}, {"matched_item_ids": ["b"]},
    {"matched_item_ids": [""]}, {"matched_item_ids": [" a"]},
    {"scan_complete": False}, {"scan_complete": 1},
    {"unchanged_drag_count": 1}, {"scroll_count": True},
])
def test_invalid_presence_target_or_scan_proof_cannot_submit(store, fake_ui, override):
    visit = begin(store)
    with pytest.raises(ValueError):
        submit(store, visit, scan(**override))
    assert fake_ui == [] and record(store, visit)["batches"] == {}


def test_zero_matching_scan_completes_week_and_exits_without_purchase(store, fake_ui):
    visit = begin(store)
    submission = submit(store, visit, scan([]))
    assert submission["batch_id"] is None and record(store, visit)["batches"] == {}
    completion = complete(store, visit, submission)
    assert completion["reason"] == "no_matching_goods" and completion["purchase_verified"] is False
    assert record(store, visit)["completed"] is True
    result = finish(store, visit, completion)
    assert result["page_state"] == "rest_menu" and result["has_selected_goods"] is False
    assert result["matched_item_ids"] == []
    assert fake_ui == ["validated", "exit:rest_menu"]


def test_completed_record_survives_exit_failure_and_avoids_future_repurchase(store, fake_ui, monkeypatch):
    visit = begin(store, target="main")
    completion = complete(store, visit, submit(store, visit))

    async def failed_exit(*args):
        assert record(store, visit)["completed"] is True
        raise TimeoutError("exit failed")

    monkeypatch.setattr(visit_actions, "execute_black_moon_exit", failed_exit)
    with pytest.raises(TimeoutError):
        finish(store, visit, completion)
    assert record(store, visit)["completed"] is True
    assert begin(store)["reason"] == "completed_this_week"


@pytest.mark.parametrize("target", ["rest_menu", "main"])
def test_skipped_visit_restores_requested_endpoint(store, fake_ui, target):
    visit = begin(store, {}, target=target)
    result = finish(store, visit, {})
    assert result["page_state"] == target and result["status"] == "skipped"
    assert fake_ui == ([] if target == "rest_menu" else ["exit:main"])


def test_freeze_week_rejects_submission_before_ui_calls(store, fake_ui, monkeypatch):
    visit = begin(store)
    monkeypatch.setattr(visit_actions, "resolve_purchase_week", lambda _: "2026-10-19")
    with pytest.raises(ValueError, match="frozen"):
        submit(store, visit)
    assert fake_ui == [] and record(store, visit)["batches"] == {}


def test_week_crossing_between_ui_validation_and_hook_prevents_click(store, fake_ui, monkeypatch):
    visit = begin(store)
    weeks = iter([WEEK, "2026-10-19"])
    monkeypatch.setattr(visit_actions, "resolve_purchase_week", lambda _: next(weeks))
    with pytest.raises(ValueError, match="frozen"):
        submit(store, visit)
    assert fake_ui == ["validated"] and record(store, visit)["batches"] == {}


def test_week_crossing_during_settlement_does_not_complete_old_visit(store, fake_ui, monkeypatch):
    visit = begin(store)
    submission = submit(store, visit)
    weeks = iter([WEEK, "2026-10-19"])
    monkeypatch.setattr(visit_actions, "resolve_purchase_week", lambda _: next(weeks))
    with pytest.raises(ValueError, match="frozen"):
        complete(store, visit, submission)
    assert record(store, visit)["completed"] is False


class SubmitApp:
    def __init__(self):
        self.captures = 0
        self.clicks = []
        self.events = []

    def capture(self):
        self.captures += 1
        self.events.append("capture")
        return SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), np.uint8))

    def click(self, x, y):
        self.events.append("click")
        self.clicks.append((x, y))


class SubmitReader:
    def __init__(self, ready=True):
        self.is_ready = ready

    def ready(self, frame):
        return self.is_ready

    def total_zero(self, frame):
        return False

    def button(self, frame, *args):
        return {"found": True, "center": (1180, 680)}


@pytest.fixture
def fast_ui(monkeypatch):
    clock = SimpleNamespace(now=0.)
    sleep = asyncio.sleep

    async def advance(seconds):
        clock.now += seconds
        await sleep(0)

    for module in (select, exits):
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
        monkeypatch.setattr(module, "_WAIT_SECONDS", 1.2)
        monkeypatch.setattr(module, "_POLL_SECONDS", .1)
        monkeypatch.setattr(module, "_SETTLE_SECONDS", .1)
    monkeypatch.setattr(asyncio, "sleep", advance)


def test_actual_submit_hook_runs_after_stable_ui_immediately_before_click(fast_ui):
    app = SubmitApp()

    def journal():
        assert app.captures == 2 and not app.clicks
        app.events.append("journal")

    result = asyncio.run(select.execute_black_moon_submit(scan(), app, SubmitReader(), before_submit=journal))
    assert result["purchase_submitted"] is True
    assert app.events == ["capture", "capture", "journal", "click"]


def test_actual_submit_never_calls_hook_on_ui_validation_failure(fast_ui):
    app = SubmitApp()
    with pytest.raises(select.BlackMoonLocalSelectError):
        asyncio.run(select.execute_black_moon_submit(scan(), app, SubmitReader(False),
                    before_submit=lambda: app.events.append("journal")))
    assert "journal" not in app.events and not app.clicks


def test_actual_submit_hook_failure_cannot_click(fast_ui):
    app = SubmitApp()

    def failed_write():
        raise OSError("durable journal unavailable")

    with pytest.raises(OSError):
        asyncio.run(select.execute_black_moon_submit(scan(), app, SubmitReader(), before_submit=failed_write))
    assert not app.clicks


def test_actual_submit_zero_cards_does_not_call_hook_or_capture(fast_ui):
    app = SubmitApp()
    result = asyncio.run(select.execute_black_moon_submit(scan([]), app, SubmitReader(),
                         before_submit=lambda: app.events.append("journal")))
    assert result["purchase_submitted"] is False and app.events == []


def test_existing_exit_helper_accepts_rest_menu_as_source_for_main(fast_ui):
    app = SubmitApp()

    class ExitReader(SubmitReader):
        VIEWPORT = (594, 143, 684, 500)

        def observe(self, frame):
            return {"page_state": "main" if app.clicks else "rest_menu",
                    "shop_ready": False, "total_zero": False}

    result = asyncio.run(exits.execute_black_moon_exit(
        {"success": True, "ready_to_exit": True}, "main", app, ExitReader()))
    assert result["page_state"] == "main" and len(app.clicks) == 1


def test_cancellation_during_durable_journal_keeps_pending_without_payment(store, fast_ui, monkeypatch):
    from plans.resonance_pc.src.actions import black_moon_local_prepare_pc_actions as prepare

    visit = begin(store)
    app = SubmitApp()
    cancellation = {"requested": False}
    original_journal = visit_actions.mark_pending_batch

    def journal_then_cancel(*args, **kwargs):
        saved = original_journal(*args, **kwargs)
        cancellation["requested"] = True
        return saved

    async def actual_submit(selection, app, before_submit):
        return await select.execute_black_moon_submit(
            selection, app, SubmitReader(), before_submit=before_submit)

    monkeypatch.setattr(visit_actions, "mark_pending_batch", journal_then_cancel)
    monkeypatch.setattr(visit_actions, "execute_black_moon_submit", actual_submit)
    monkeypatch.setattr(prepare, "is_current_task_cancel_requested", lambda: cancellation["requested"])
    with pytest.raises(asyncio.CancelledError):
        submit(store, visit, app=app)
    saved = record(store, visit)
    assert app.captures == 2 and app.clicks == []
    assert saved["completed"] is False
    assert len(saved["batches"]) == 1
    assert next(iter(saved["batches"].values()))["status"] == "pending"


def test_actual_submit_cancel_during_journal_keeps_pending_without_click(store, fast_ui, monkeypatch):
    visit = begin(store)
    app = SubmitApp()
    cancelled = False

    def cancel_check():
        if cancelled:
            raise asyncio.CancelledError()

    def journal():
        nonlocal cancelled
        ledger.mark_pending_batch(store, **{key: visit[key] for key in (
            "scope", "week_key", "city_key", "visit_id")},
            batch_id="cancelled-before-click", items=[{"item_id": "a"}])
        cancelled = True

    monkeypatch.setattr(select, "_cancel_check", cancel_check)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(select.execute_black_moon_submit(scan(), app, SubmitReader(), before_submit=journal))
    saved = record(store, visit)
    assert saved["batches"]["cancelled-before-click"]["status"] == "pending"
    assert saved["completed"] is False and not app.clicks


def test_same_product_multiple_cards_uses_one_presence_receipt_and_completes_week(store, fake_ui):
    visit = begin(store)
    selection = scan([{"item_id": "a"}, {"item_id": "a"}])
    submission = submit(store, visit, selection)
    batch = record(store, visit)["batches"][submission["batch_id"]]
    assert batch["status"] == "pending" and batch["items"] == [{"item_id": "a"}]
    assert submission["scan"]["has_selected_goods"] is True
    assert submission["scan"]["matched_item_ids"] == ["a"]
    assert "selected_card_count" not in submission["scan"]
    completion = complete(store, visit, submission)
    result = finish(store, visit, completion)
    assert result["has_selected_goods"] is True and result["matched_item_ids"] == ["a"]
    assert begin(store)["reason"] == "completed_this_week"
    assert fake_ui == ["validated", "clicked", "settled", "exit:rest_menu"]


def test_actual_submit_journal_timeout_keeps_pending_and_blocks_replay(store, monkeypatch):
    clock = SimpleNamespace(now=0.0)

    async def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(select, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(asyncio, "sleep", advance)
    visit = begin(store)
    app = SubmitApp()
    original_journal = visit_actions.mark_pending_batch

    def journal_then_expire(*args, **kwargs):
        saved = original_journal(*args, **kwargs)
        clock.now += select._WAIT_SECONDS
        return saved

    async def actual_submit(selection, app, before_submit):
        return await select.execute_black_moon_submit(
            selection, app, SubmitReader(), before_submit=before_submit)

    monkeypatch.setattr(visit_actions, "mark_pending_batch", journal_then_expire)
    monkeypatch.setattr(visit_actions, "execute_black_moon_submit", actual_submit)
    with pytest.raises(select.BlackMoonLocalSelectError) as error:
        submit(store, visit, app=app)
    assert error.value.code == "black_moon_submit_unconfirmed"
    saved = record(store, visit)
    assert saved["completed"] is False and not app.clicks
    assert len(saved["batches"]) == 1
    assert next(iter(saved["batches"].values()))["status"] == "pending"
    with pytest.raises(ValueError, match="Unresolved"):
        submit(store, visit, app=app)
    with pytest.raises(ValueError, match="Unresolved"):
        begin(store)
    assert record(store, visit) == saved
    assert not app.clicks

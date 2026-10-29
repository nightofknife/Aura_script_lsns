"""Offline visible-viewport transfer orchestration and stock-accounting checks.

No capture service, OCR engine, game client, or actual input is used here.
"""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions._prison_warehouse_runtime import (
    WarehouseTransferError,
    WarehouseTransferRuntime,
)


PRODUCTS = {"113": "涤纶", "114": "测试成品", "115": "屏外成品"}


class OfflineScope:
    def activate(self):
        return nullcontext()

    def checkpoint(self):
        pass

    def child(self, _timeout):
        return self


def forbidden_scroll(*_args, **_kwargs):
    pytest.fail("Visible-viewport transfer must never scroll or return to list top")


def card(goods_id, quantity, *, target=True, point=(630, 470)):
    return {
        "id": goods_id,
        "name": PRODUCTS.get(goods_id, goods_id),
        "quantity": quantity,
        "quantity_result": {"value": quantity},
        "target": target,
        "point": point,
        "bbox": [point[0] - 40, point[1] - 40, 80, 80],
    }


def view(*cards, full=False, unresolved=(), list_state=None):
    return {
        "page": "warehouse",
        "connection": "train_product",
        "cards": list(cards),
        "unresolved": list(unresolved),
        "list_state": list_state or ("populated" if cards else "empty"),
        "capacity": {"current": 50 if full else 0, "limit": 50, "full": full},
        "signature": tuple((c["id"], c["quantity"], tuple(c["point"])) for c in cards),
    }


def runtime():
    instance = object.__new__(WarehouseTransferRuntime)
    instance.app = SimpleNamespace(drag=forbidden_scroll, scroll=forbidden_scroll,
                                   wheel=forbidden_scroll)
    # Trap old helpers as well as app input, including helpers removed in production.
    instance._top = instance._drag = instance._move_down = forbidden_scroll
    instance.scope = OfflineScope()
    instance.cancel_check = lambda: None
    instance.options = {"max_withdrawals": 225, "effect_timeout_sec": 1, "poll_sec": 0}
    instance.products = dict(PRODUCTS)
    instance.ui = SimpleNamespace(goods_catalog={
        "products": {goods_id: {"supported": True} for goods_id in PRODUCTS},
    })
    instance.session = SimpleNamespace(receipts=[])
    instance.frame = instance.last = None
    instance.phase = "prepare"
    instance.trace = []
    instance.before = {}
    instance.after = {}
    instance.effect_after = {}
    instance.clicked = []
    instance.pending = None
    instance.quantity_evidence = {}
    instance.request = {"goods_ids": None, "quantity_threshold": 1}
    instance.rescan = {
        "complete": False, "scope": "visible", "inventory_complete": False,
        "unresolved": [], "coverage_notes": [], "quantities": {},
    }
    instance.returned = instance.return_attempted = False
    instance.stop_reason = "not_started"
    instance.reconciled = None
    return instance


def scripted_run(initial_view, changes=(), *, goods=None, threshold=1, scan_view=None):
    """Retain the real pass/rescan/ledger/result while replacing external services."""
    instance = runtime()
    state = {"view": initial_view}
    changes = iter(changes)
    taken = []
    stable_reads = []
    transitions = []

    def capture(**_kwargs):
        instance.last = {"page": "overview"}
        return instance.last

    def prepare():
        instance.last = state["view"]
        return state["view"]

    def take_one(current, selected):
        assert current is state["view"]
        assert selected in current["cards"]
        goods_id = str(selected["id"])
        taken.append((goods_id, selected["point"]))
        instance.before[goods_id] = selected["quantity"]
        after, explicit_remaining = next(changes)
        if explicit_remaining is not None:
            instance.effect_after[goods_id] = explicit_remaining
        instance.clicked.append(goods_id)
        instance.last = state["view"] = after
        return after

    def stable():
        stable_reads.append(True)
        instance.last = scan_view if scan_view is not None else state["view"]
        return instance.last

    def transition(stage, target, source, control):
        transitions.append((stage, control))
        assert source(instance.last)
        instance.return_attempted = True
        instance.last = {"page": "overview"}
        assert target(instance.last)

    instance._capture = capture
    instance._prepare = prepare
    instance._take_one = take_one
    instance._stable = stable
    instance._transition = transition
    result = instance.run(goods=goods, quantity_threshold=threshold)
    return instance, result, taken, stable_reads, transitions


def test_run_takes_relocated_visible_items_once_and_returns_without_scrolling():
    first = view(card("113", 20), card("114", 3, point=(730, 470)))
    # An already processed ID may remain after a partial take. The next item has
    # moved, so its old coordinates must not be reused and the first ID not retried.
    relocated = view(card("113", 5), card("114", 3, point=(630, 570)))
    final = view(card("113", 5))
    instance, result, taken, reads, transitions = scripted_run(
        first, [(relocated, 5), (final, 0)],
    )
    assert taken == [("113", (630, 470)), ("114", (630, 570))]
    assert instance.clicked == ["113", "114"]
    assert len(reads) == 1  # A final read only, never a second withdrawal pass.
    assert transitions == [("return_overview", "back")]
    assert result["success"] is True
    assert result["total_taken"] == 18
    assert result["reconcile_complete"] is True
    assert result["scan_complete"] is True
    assert result["scan_scope"] == "visible"
    assert result["inventory_complete"] is False
    assert result["returned_to_overview"] is True
    assert result["page_after"] == "overview"
    assert result["stop_reason"] == "no_visible_eligible_goods"


def test_run_empty_visible_inventory_confirms_zero_remainder_without_scrolling():
    _, result, taken, reads, _ = scripted_run(
        view(card("113", 20)), [(view(), 0)], goods="涤纶",
    )
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["total_taken"] == 20
    assert result["inventory_complete"] is True
    assert result["scan_scope"] == "visible"
    assert taken == [("113", (630, 470))]
    assert len(reads) == 1


@pytest.mark.parametrize("visible,goods,threshold,reason", [
    (view(card("113", 2)), None, 3, "no_visible_eligible_goods"),
    (view(card("113", 20, target=False)), "115", 1, "no_visible_eligible_goods"),
    (view(card("113", 20), full=True), None, 1, "train_full"),
])
def test_run_stops_at_current_view_for_threshold_offscreen_target_and_full_train(
        visible, goods, threshold, reason):
    _, result, taken, reads, transitions = scripted_run(
        visible, goods=goods, threshold=threshold,
    )
    assert result["success"] is True
    assert result["stop_reason"] == reason
    assert result["goods_taken"] == []
    assert result["total_taken"] == 0
    assert taken == []
    assert len(reads) == 1
    assert transitions == [("return_overview", "back")]
    assert result["inventory_complete"] is False


def test_take_pass_never_clicks_unknown_zero_boolean_or_non_target_quantities():
    instance = runtime()
    instance._take_one = lambda *_args: pytest.fail("No eligible visible product")
    visible = view(card("113", None), card("114", 0), card("115", True),
                   card("other", 20, target=False))
    assert instance._take_pass(visible) == (visible, "no_visible_eligible_goods")


def test_take_pass_rejects_ambiguous_visible_identity_without_input():
    instance = runtime()
    instance._take_one = lambda *_args: pytest.fail("Ambiguous goods must not be clicked")
    with pytest.raises(WarehouseTransferError) as error:
        instance._take_pass(view(card("113", 20), card("113", 10)))
    assert error.value.reason == "warehouse_duplicate_visible_goods_id"


def test_rescan_reads_fresh_current_view_once_and_preserves_coverage_limit():
    instance = runtime()
    notes = [{"reason": "clipped_card", "coverage_only": True, "blocking": False}]
    fresh = view(card("114", 3), unresolved=notes, list_state="partial")
    calls = []

    def stable():
        calls.append(True)
        return fresh

    instance._stable = stable
    instance._rescan(view(card("113", 20)))
    assert len(calls) == 1
    assert instance.after == {"114": 3}
    assert instance.rescan["scope"] == "visible"
    assert instance.rescan["inventory_complete"] is False
    assert instance.rescan["coverage_notes"] == notes
    assert instance.rescan["unresolved"] == []


@pytest.mark.parametrize("current", [
    view(unresolved=[{"reason": "identity_unknown", "blocking": True}], list_state="partial"),
    view(card("113", None)),
    view(card("113", 3), card("113", 3)),
])
def test_rescan_unknown_and_duplicate_cards_cannot_claim_inventory_complete(current):
    instance = runtime()
    instance._stable = lambda: current
    instance._rescan(current)
    assert instance.rescan["complete"] is False
    assert instance.rescan["inventory_complete"] is False
    assert instance.rescan["unresolved"]


@pytest.mark.parametrize("explicit_after,expected", [(0, 20), (5, 15)])
def test_explicit_single_withdrawal_remainder_is_sufficient_for_accounting(explicit_after, expected):
    instance = runtime()
    instance.before = {"113": 20, "115": 100}
    instance.clicked = ["113"]
    instance.effect_after = {"113": explicit_after}
    instance.rescan.update(complete=True, inventory_complete=False)
    ledger = instance._reconcile()
    assert ledger["reconcile_complete"] is True
    assert ledger["goods_taken"] == [{"goods_id": "113", "goods": "涤纶", "quantity": expected}]
    assert ledger["total_taken"] == expected
    assert ledger["unknown_goods"] == []


def test_final_view_missing_clicked_id_without_effect_evidence_remains_unknown():
    instance = runtime()
    instance.before = {"113": 20}
    instance.clicked = ["113"]
    instance.rescan.update(complete=True, inventory_complete=False)
    ledger = instance._reconcile()
    assert ledger["goods_taken"] == []
    assert ledger["total_taken"] == 0
    assert ledger["reconcile_complete"] is False
    assert ledger["unknown_goods"][0]["reason"] == "missing_after_quantity"
    assert ledger["unknown_goods"][0]["after_quantity"] is None


@pytest.mark.parametrize("remaining,method,expected", [
    (view(card("113", 5)), "source_quantity_decreased", 5),
    (view(), "product_list_became_empty", 0),
    (view(card("114", 3)), "ordered_neighbors_shifted", 0),
])
def test_effect_keeps_explicit_remainder_evidence_before_final_rescan(remaining, method, expected):
    instance = runtime()
    before = view(card("113", 20), card("114", 3, point=(730, 470)))
    instance.pending = {"goods_id": "113"}
    instance._capture = lambda **_kwargs: remaining

    def confirm(probe, **kwargs):
        assert kwargs["stable_scans"] == 2
        first, second = probe(), probe()
        assert first is not None and second is not None
        assert kwargs["equivalent"](first, second)
        return second

    instance.interaction = SimpleNamespace(confirm=confirm)
    assert instance._effect(before, "113", 20) is remaining
    assert instance.effect_after == {"113": expected}
    assert instance.pending["effect_confirmed"] is True
    assert instance.pending["effect"] == method
    assert instance.pending["observed_after_quantity"] == expected

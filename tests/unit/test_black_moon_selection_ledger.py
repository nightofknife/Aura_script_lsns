"""Wishlist-run evidence stays distinct from inventory-wide completion evidence."""
from copy import deepcopy

import pytest

from packages.aura_core.context.persistence import PersistentDataService
from plans.resonance_pc.src.actions import _black_moon_local_shop_state as ledger


KEY = {"scope": "default", "week_key": "2026-10-12", "city_key": "city"}


@pytest.fixture
def store(tmp_path):
    return PersistentDataService(tmp_path)


def begin(store, selected=None, **keys):
    return ledger.begin_selection_visit(store, **(KEY | keys), selected_item_ids=selected or ["a", "b"])


def legacy_scan(items=None, selected=None, **overrides):
    items = [] if items is None else items
    proof = scan(items, selected)
    proof.pop("has_selected_goods")
    proof.pop("matched_item_ids")
    return proof | {"selected_card_count": len(items), "selected_items": items} | overrides


def scan(items=None, selected=None, **overrides):
    items = [] if items is None else items
    return {"type": "wishlist_scan", "recognized": True,
            "scan_complete": True, "end_reason": "unchanged_view", "bottom_confirmed": False,
            "scroll_count": 2, "unchanged_drag_count": 2,
            "selected_item_ids": selected or ["a", "b"],
            "has_selected_goods": bool(items),
            "matched_item_ids": sorted({item["item_id"] for item in items})} | overrides


def payment(**overrides):
    return {"type": "wishlist_purchase_settled", "recognized": True,
            "purchase_applied": True, "ui_settled": True} | overrides


def pending(store, record, items, batch="batch"):
    return ledger.mark_pending_batch(store, **KEY, visit_id=record["visit_id"], batch_id=batch, items=items)


def confirm(store, record, batch="batch", proof=None):
    return ledger.confirm_batch(store, **KEY, visit_id=record["visit_id"], batch_id=batch,
                                evidence=payment() if proof is None else proof)


def complete(store, record, proof=None, reason="no_matching_goods"):
    return ledger.complete_visit(store, **KEY, visit_id=record["visit_id"], reason=reason,
                                 evidence=scan() if proof is None else proof)


def test_changed_completed_wishlist_archives_receipt_and_keeps_same_file(store):
    first = begin(store)
    first = complete(store, first)
    assert ledger.selection_visit_completed(first, ["b", "a"])
    assert not ledger.selection_visit_completed(first, ["a"])
    assert begin(store, ["b", "a"]) == first
    second = begin(store, ["a"])
    assert second["visit_id"] != first["visit_id"]
    assert not second["completed"]
    assert second["selected_item_ids"] == ["a"]
    assert second["previous_visits"][0]["completion"] == first["completion"]
    assert store.exists(ledger.PURCHASES_FILE)
    assert ledger.read_city_record(store, **KEY) == second


def test_incomplete_changed_wishlist_archives_and_same_wishlist_resumes(store):
    first = begin(store)
    assert begin(store, ["b", "a"]) == first
    second = begin(store, ["b"])
    assert second["previous_visits"][0]["visit_id"] == first["visit_id"]
    third = begin(store, ["a"])
    assert len(third["previous_visits"]) == 2
    assert "previous_visits" not in third["previous_visits"][1]


@pytest.mark.parametrize("selected", [["a", "b"], ["a"]])
def test_unresolved_payment_blocks_resume_changed_wishlist_and_completion(store, selected):
    first = begin(store)
    first = pending(store, first, [{"item_id": "a"}])
    saved = store.read(ledger.PURCHASES_FILE)
    with pytest.raises(ValueError, match="Unresolved"):
        begin(store, selected)
    with pytest.raises(ValueError, match="Unresolved"):
        ledger.selection_visit_completed(first, selected)
    with pytest.raises(ValueError, match="Unresolved"):
        complete(store, first)
    assert store.read(ledger.PURCHASES_FILE) == saved


def test_pending_in_history_blocks_new_visit(store):
    first = begin(store)
    pending(store, first, [{"item_id": "a"}])
    document = store.read(ledger.PURCHASES_FILE)
    record = document["scopes"]["default"]["weeks"][KEY["week_key"]]["cities"]["city"]
    old = deepcopy(record)
    old.pop("previous_visits")
    record.update(visit_id="next", batches={}, previous_visits=[old])
    store.update(file=ledger.PURCHASES_FILE, updater=lambda _: document)
    with pytest.raises(ValueError, match="Unresolved"):
        begin(store, ["b"])


def test_unrelated_pending_does_not_block_other_city_week_or_account(store):
    first = begin(store)
    pending(store, first, [{"item_id": "a"}])
    for change in ({"city_key": "other"}, {"week_key": "2026-10-19"}, {"scope": "other"}):
        assert begin(store, **change)["completed"] is False


def test_confirmed_cards_are_not_inventory_units_and_persist_before_exit(store):
    record = begin(store)
    items = [{"item_id": "a", "score": 0.97}, {"item_id": "a", "score": 0.96}, {"item_id": "b"}]
    pending(store, record, items)
    confirmed = confirm(store, record)
    assert confirmed["batches"]["batch"]["status"] == "confirmed"
    assert ledger.read_city_record(store, **KEY) == confirmed
    completed = complete(store, record, scan(items), reason="wishlist_processed")
    assert completed["completed"]
    assert completed["completion"]["evidence"]["matched_item_ids"] == ["a", "b"]
    assert "purchase_units" not in completed["batches"]["batch"]["items"][0]
    assert ledger.selection_visit_completed(completed, ["a", "b"])


@pytest.mark.parametrize("flag", ["recognized", "purchase_applied", "ui_settled"])
@pytest.mark.parametrize("value", [False, None, 1])
def test_wishlist_confirmation_requires_explicit_true_flags(store, flag, value):
    record = begin(store)
    pending(store, record, [{"item_id": "a"}])
    with pytest.raises(ValueError, match="Wishlist confirmation"):
        confirm(store, record, proof=payment(**{flag: value}))
    assert ledger.read_city_record(store, **KEY)["batches"]["batch"]["status"] == "pending"


@pytest.mark.parametrize("override", [
    {"recognized": False}, {"bottom_confirmed": True}, {"started_empty": 1},
    {"recognized": 1}, {"has_selected_goods": 1}, {"has_selected_goods": True},
    {"matched_item_ids": ["a"]}, {"matched_item_ids": None}, {"selected_item_ids": ["a"]},
    {"matched_item_ids": ["a", "a"]}, {"matched_item_ids": ["unknown"]},
    {"scan_complete": False}, {"unchanged_drag_count": 1},
    {"selected_item_ids": ["a", "a"]}, {"cancelled": True}, {"error": "failed"},
])
def test_invalid_wishlist_scan_rejected_without_mutation(store, override):
    record = begin(store)
    saved = store.read(ledger.PURCHASES_FILE)
    with pytest.raises(ValueError):
        complete(store, record, scan(**override))
    assert store.read(ledger.PURCHASES_FILE) == saved


@pytest.mark.parametrize("items", [["a"], ["unknown"], [""]])
def test_selected_cards_require_valid_known_identity_and_payment(store, items):
    record = begin(store)
    proof = scan(has_selected_goods=True, matched_item_ids=items)
    with pytest.raises(ValueError):
        complete(store, record, proof, reason="wishlist_processed")


def test_confirmed_products_must_exactly_match_scan(store):
    record = begin(store)
    pending(store, record, [{"item_id": "a"}])
    confirm(store, record)
    for items in ([], [{"item_id": "b"}], [{"item_id": "a"}, {"item_id": "b"}]):
        with pytest.raises(ValueError, match="current-visit confirmed"):
            complete(store, record, scan(items), reason="wishlist_processed")


def test_previous_visit_payment_cannot_back_new_visit_cards(store):
    first = begin(store)
    pending(store, first, [{"item_id": "a"}])
    confirm(store, first)
    complete(store, first, scan([{"item_id": "a"}]), reason="wishlist_processed")
    second = begin(store, ["a"])
    with pytest.raises(ValueError, match="current-visit confirmed"):
        complete(store, second, scan([{"item_id": "a"}], selected=["a"]), reason="wishlist_processed")


def test_confirmed_payment_without_completion_restarts_same_wishlist_safely(store):
    first = begin(store)
    pending(store, first, [{"item_id": "a"}, {"item_id": "a"}])
    confirmed = confirm(store, first)
    assert not confirmed["completed"]
    resumed = begin(store, ["b", "a"])
    assert resumed["visit_id"] != first["visit_id"]
    assert resumed["batches"] == {}
    assert resumed["previous_visits"][0]["batches"] == confirmed["batches"]
    assert not ledger.selection_visit_completed(resumed, ["a", "b"])
    result = complete(store, resumed, scan(), reason="no_matching_goods")
    assert ledger.selection_visit_completed(result, ["a", "b"])
    assert result["previous_visits"][0]["batches"]["batch"]["status"] == "confirmed"
    assert begin(store, ["a", "b"]) == result


def test_zero_card_scan_has_explicit_no_matching_reason(store):
    record = begin(store)
    with pytest.raises(ValueError, match="no_matching_goods"):
        complete(store, record, reason="wishlist_processed")
    assert complete(store, record)["completed"]


def test_stale_visit_cannot_complete_or_pending_after_rollover(store):
    first = begin(store)
    begin(store, ["a"])
    with pytest.raises(ValueError, match="Stale"):
        complete(store, first)
    with pytest.raises(ValueError, match="Stale"):
        pending(store, first, [{"item_id": "a"}])


def test_corrupt_document_does_not_get_reinitialized(store):
    store.update(file=ledger.PURCHASES_FILE, updater=lambda _: {"schema_version": 1, "scopes": []})
    with pytest.raises(ValueError, match="Malformed"):
        begin(store)
    assert store.read(ledger.PURCHASES_FILE)["scopes"] == []


def test_corrupt_persisted_wishlist_completion_fails_read(store):
    record = complete(store, begin(store))
    document = store.read(ledger.PURCHASES_FILE)
    city = document["scopes"]["default"]["weeks"][KEY["week_key"]]["cities"]["city"]
    city["completion"]["evidence"]["has_selected_goods"] = True
    store.update(file=ledger.PURCHASES_FILE, updater=lambda _: document)
    with pytest.raises(ValueError, match="Selected-target presence"):
        ledger.read_city_record(store, **KEY)
    with pytest.raises(ValueError, match="Selected-target presence"):
        ledger.selection_visit_completed(city, record["selected_item_ids"])


def test_old_full_scan_contract_remains_strict_and_valid(store):
    record = ledger.begin_visit(store, **KEY, selected_item_ids=["a", "b"])
    pending(store, record, [{"item_id": "a", "purchase_units": 3}])
    with pytest.raises(ValueError, match="confirmed=true"):
        confirm(store, record, proof={"recognized": True, "purchase_applied": True})
    confirm(store, record, proof={"recognized": True, "confirmed": True})
    for proof in ({"recognized": True, "scan_status": "COMPLETE"},
                  {"recognized": True, "scan_status": "PARTIAL", "outcomes": {"a": "purchased", "b": "missing"}}):
        with pytest.raises(ValueError):
            complete(store, record, proof, reason="checked")
    result = complete(store, record, {"recognized": True, "scan_status": "COMPLETE",
                                    "outcomes": {"a": "purchased", "b": "sold_out"}}, reason="checked")
    assert result["completed"]


def test_corrupt_archived_wishlist_completion_fails_read(store):
    complete(store, begin(store))
    begin(store, ["a"])
    document = store.read(ledger.PURCHASES_FILE)
    city = document["scopes"]["default"]["weeks"][KEY["week_key"]]["cities"]["city"]
    city["previous_visits"][0]["completion"]["evidence"]["matched_item_ids"] = ["a"]
    store.update(file=ledger.PURCHASES_FILE, updater=lambda _: document)
    with pytest.raises(ValueError, match="Selected-target presence"):
        ledger.read_city_record(store, **KEY)


@pytest.mark.parametrize("selected", [[], ["a", "a"], [" a"], [1], "a"])
def test_new_visit_rejects_invalid_selection_even_if_record_exists(store, selected):
    begin(store)
    with pytest.raises(ValueError):
        ledger.begin_selection_visit(store, **KEY, selected_item_ids=selected)
    with pytest.raises(ValueError):
        ledger.selection_visit_completed(None, selected)


@pytest.mark.parametrize("cards", [1, 2, 4])
def test_presence_completion_tracks_product_ids_without_card_multiplicity(store, cards):
    record = begin(store)
    pending(store, record, [{"item_id": "a"}] * cards)
    confirm(store, record)
    result = complete(store, record, scan([{"item_id": "a"}]), reason="wishlist_processed")
    assert ledger.selection_visit_completed(result, ["a", "b"])
    assert result["completion"]["evidence"]["matched_item_ids"] == ["a"]
    assert "selected_card_count" not in result["completion"]["evidence"]
    assert ledger.read_city_record(store, **KEY) == result


@pytest.mark.parametrize("matched", [[], ["b"], ["a", "b"]])
def test_presence_completion_requires_exact_current_confirmed_product_set(store, matched):
    record = begin(store)
    pending(store, record, [{"item_id": "a"}])
    confirm(store, record)
    with pytest.raises(ValueError, match="current-visit confirmed"):
        complete(store, record, scan([{"item_id": item} for item in matched]), reason="wishlist_processed")
    assert ledger.read_city_record(store, **KEY)["completed"] is False


@pytest.mark.parametrize("field", ["has_selected_goods", "matched_item_ids"])
def test_partial_presence_proof_cannot_fall_back_to_legacy_counts(store, field):
    record = begin(store)
    proof = legacy_scan()
    proof[field] = False if field == "has_selected_goods" else []
    with pytest.raises(ValueError):
        complete(store, record, proof)


def test_legacy_empty_card_completion_is_rejected_without_mutation(store):
    record = begin(store)
    with pytest.raises(ValueError):
        complete(store, record, legacy_scan())
    assert ledger.read_city_record(store, **KEY) == record


@pytest.mark.parametrize("override", [
    {"selected_card_count": True}, {"selected_card_count": -1},
    {"selected_card_count": 1}, {"selected_items": None},
])
def test_legacy_card_evidence_still_rejects_invalid_count_accounting(store, override):
    record = begin(store)
    with pytest.raises(ValueError):
        complete(store, record, legacy_scan(**override))
    assert ledger.read_city_record(store, **KEY)["completed"] is False


def test_previous_visit_payment_cannot_back_new_presence_proof(store):
    first = begin(store)
    pending(store, first, [{"item_id": "a"}])
    confirm(store, first)
    complete(store, first, scan([{"item_id": "a"}]), reason="wishlist_processed")
    second = begin(store, ["a"])
    with pytest.raises(ValueError, match="current-visit confirmed"):
        complete(store, second, scan([{"item_id": "a"}], selected=["a"]), reason="wishlist_processed")

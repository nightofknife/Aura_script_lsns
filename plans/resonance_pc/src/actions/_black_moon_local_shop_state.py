"""Weekly shop ledger, with cooperative session and read/modify/write locks.

Hold ``async with purchase_session_lock(service)`` over the entire shop action.
Call ``mark_pending_batch`` before the one confirmation click, then persist
recognized confirmation synchronously with ``confirm_batch`` even if the action
has been cancelled. Pending entries are evidence to reconcile, never work to
replay. Every writer of this ledger must use this module's write guard; the
persistence service's atomic replacement is not compare-and-swap.
"""
from __future__ import annotations

import asyncio
import copy
import math
import os
import threading
import time as monotonic_time
import uuid
import weakref
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.aura_core.context.persistence.persistent_data_errors import PersistentDataNotFoundError


PURCHASES_FILE = "black-moon-shop-purchases.json"
SCHEMA_VERSION = 1
_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[str, Any] = {}
_SESSION_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_OUTCOMES = frozenset({"purchased", "sold_out", "missing"})
_INCOMPLETE_REASONS = frozenset({"disabled", "error", "cancel", "cancelled", "canceled",
                               "empty", "empty_selection", "aborted", "failed"})


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a nonempty, unpadded string")
    return value


def _item_ids(value: Any, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("selected_item_ids must be a list")
    result = [_name(item, "item_id") for item in value]
    if len(set(result)) != len(result) or (not result and not allow_empty):
        raise ValueError("selected_item_ids must be nonempty and unique")
    return result


def _json_copy(value: Any, label: str) -> Any:
    def check(current: Any) -> None:
        if current is None or type(current) in (str, bool, int):
            return
        if type(current) is float and math.isfinite(current):
            return
        if isinstance(current, list):
            for child in current:
                check(child)
            return
        if isinstance(current, dict) and all(type(key) is str for key in current):
            for child in current.values():
                check(child)
            return
        raise ValueError(f"{label} must contain only finite JSON values")
    check(value)
    return copy.deepcopy(value)


def _evidence(value: Any) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError("evidence must be a nonempty JSON object")
    return _json_copy(value, "evidence")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> None:
    stamp = datetime.fromisoformat(_name(value, "timestamp"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")


def resolve_purchase_week(refresh_policy: Mapping, now: datetime | None = None) -> str:
    """Return the ISO timestamp of the latest local weekly reset (Monday=0)."""
    if not isinstance(refresh_policy, Mapping):
        raise ValueError("refresh_policy must be an object")
    weekday = refresh_policy.get("weekday")
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("refresh_policy.weekday must be an integer from 0 to 6")
    clock = _name(refresh_policy.get("time"), "refresh_policy.time")
    reset_time = time.fromisoformat(clock)
    if reset_time.tzinfo is not None or reset_time.microsecond or reset_time.isoformat() != clock:
        raise ValueError("refresh_policy.time must use HH:MM:SS local time")
    zone_name = _name(refresh_policy.get("timezone"), "refresh_policy.timezone")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        if zone_name != "Asia/Shanghai":
            raise
        # The Chinese client's reset policy remains UTC+8 without bundled tzdata.
        zone = timezone(timedelta(hours=8), name="Asia/Shanghai")
    if now is None:
        now = datetime.now(zone)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    local = now.astimezone(zone)
    day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    reset = datetime.combine(day, reset_time, tzinfo=zone)
    if local < reset:
        reset = datetime.combine(day - timedelta(days=7), reset_time, tzinfo=zone)
    return reset.isoformat()


def _root(persistent_data: Any) -> Path:
    root = Path(persistent_data.root).resolve()
    return root


def _try_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_busy(error: OSError) -> bool:
    import errno
    return error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)


@contextmanager
def _write_guard(persistent_data: Any):
    root = _root(persistent_data)
    key = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        local_lock = _WRITE_LOCKS.setdefault(key, threading.RLock())
    if not local_lock.acquire(timeout=30):
        raise TimeoutError("Shop ledger write lock is busy")
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / f".{PURCHASES_FILE}.write.lock").open("a+b") as handle:
            deadline = monotonic_time.monotonic() + 30
            while True:
                try:
                    _try_lock(handle)
                    break
                except OSError as exc:
                    if not _lock_busy(exc):
                        raise
                    if monotonic_time.monotonic() >= deadline:
                        raise TimeoutError("Shop ledger write lock is busy") from exc
                    monotonic_time.sleep(0.05)
            try:
                yield
            finally:
                _unlock(handle)
    finally:
        local_lock.release()


@asynccontextmanager
async def purchase_session_lock(persistent_data: Any):
    """Serialize complete shop sessions across tasks, loops and processes.

    The lock is global per user-data root, not per profile or city, because shop
    interactions share the game window. Cancellation releases the session lock;
    it never clears pending entries or gates synchronous evidence writes.
    """
    root = _root(persistent_data)
    key = os.path.normcase(str(root))
    loop = asyncio.get_running_loop()
    with _LOCKS_GUARD:
        locks = _SESSION_LOCKS.setdefault(loop, {})
        lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        root.mkdir(parents=True, exist_ok=True)
        with (root / f".{PURCHASES_FILE}.session.lock").open("a+b") as handle:
            acquired = False
            try:
                while not acquired:
                    try:
                        _try_lock(handle)
                        acquired = True
                    except OSError as exc:
                        if not _lock_busy(exc):
                            raise
                        await asyncio.sleep(0.05)
                yield
            finally:
                if acquired:
                    _unlock(handle)


def _validate_visit(visit: Any) -> None:
    if not isinstance(visit, dict):
        raise ValueError("Ledger visit must be an object")
    if not {"visit_id", "selected_item_ids", "started_at", "batches"} <= visit.keys():
        raise ValueError("Ledger visit is missing required fields")
    _name(visit.get("visit_id"), "visit_id")
    selected = _item_ids(visit.get("selected_item_ids"), allow_empty=False)
    _timestamp(visit.get("started_at"))
    batches = visit.get("batches")
    if not isinstance(batches, dict):
        raise ValueError("Ledger batches must be an object")
    for batch_id, batch in batches.items():
        _name(batch_id, "batch_id")
        if not isinstance(batch, dict) or batch.get("batch_id") != batch_id:
            raise ValueError("Ledger batch identity is invalid")
        if not {"batch_id", "items", "pending_at", "status", "evidence"} <= batch.keys():
            raise ValueError("Ledger batch is missing required fields")
        _batch_items(batch.get("items"), selected)
        _timestamp(batch.get("pending_at"))
        if batch.get("status") == "pending":
            if batch.get("evidence") is not None:
                raise ValueError("Pending batch cannot have confirmation evidence")
        elif batch.get("status") == "confirmed":
            _confirmation_evidence(batch.get("evidence"))
            _timestamp(batch.get("confirmed_at"))
        elif batch.get("status") == "not_purchased":
            _nonpurchase_evidence(batch.get("evidence"))
            _timestamp(batch.get("resolved_at"))
        else:
            raise ValueError("Ledger batch status is invalid")


def _validate_record(record: Any) -> None:
    if not isinstance(record, dict) or type(record.get("completed")) is not bool:
        raise ValueError("Ledger city record must include a boolean completed")
    if not {"completed", "completion", "previous_visits", "updated_at"} <= record.keys():
        raise ValueError("Ledger city record is missing required fields")
    _validate_visit(record)
    _timestamp(record.get("updated_at"))
    history = record.get("previous_visits")
    if not isinstance(history, list):
        raise ValueError("Ledger previous_visits must be a list")
    visit_ids = set()
    batch_ids = set()
    pending_count = 0
    for visit in [*history, record]:
        _validate_visit(visit)
        if visit["visit_id"] in visit_ids or batch_ids.intersection(visit["batches"]):
            raise ValueError("Ledger visit and batch IDs must be unique per city/week")
        visit_ids.add(visit["visit_id"])
        batch_ids.update(visit["batches"])
        pending_count += sum(batch["status"] == "pending" for batch in visit["batches"].values())
    if pending_count > 1:
        raise ValueError("Ledger city/week cannot contain multiple unresolved batches")
    completion = record.get("completion")
    if record["completed"]:
        if not isinstance(completion, dict) or completion.get("visit_id") != record["visit_id"]:
            raise ValueError("Completed record must have current-visit completion evidence")
        if _name(completion.get("reason"), "completion.reason").lower() in _INCOMPLETE_REASONS:
            raise ValueError("Failed or skipped visits cannot be marked completed")
        _timestamp(completion.get("completed_at"))
        _completion_evidence(record, completion.get("evidence"))
    elif completion is not None:
        raise ValueError("Incomplete record cannot contain completion evidence")


def _validate_document(document: Any) -> dict:
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
        raise ValueError("Malformed shop ledger: missing schema_version")
    if document["schema_version"] != SCHEMA_VERSION or not isinstance(document.get("scopes"), dict):
        raise ValueError("Malformed or unsupported shop ledger schema")
    _json_copy(document, "ledger")
    for scope, scoped in document["scopes"].items():
        _name(scope, "scope")
        if not isinstance(scoped, dict) or not isinstance(scoped.get("weeks"), dict):
            raise ValueError("Ledger scope.weeks must be an object")
        for week, weekly in scoped["weeks"].items():
            _name(week, "week_key")
            if not isinstance(weekly, dict) or not isinstance(weekly.get("cities"), dict):
                raise ValueError("Ledger week.cities must be an object")
            for city, record in weekly["cities"].items():
                _name(city, "city_key")
                _validate_record(record)
    return document


def _load(persistent_data: Any) -> dict | None:
    try:
        document = persistent_data.read(file=PURCHASES_FILE)
    except PersistentDataNotFoundError as exc:
        if exc.code != "persistent_data_file_not_found":
            raise
        return None
    return _validate_document(document)


def _identity(scope: str, week_key: str, city_key: str) -> None:
    for label, value in (("scope", scope), ("week_key", week_key), ("city_key", city_key)):
        _name(value, label)


def _city(document: dict, scope: str, week_key: str, city_key: str) -> dict | None:
    return document["scopes"].get(scope, {}).get("weeks", {}).get(week_key, {}).get("cities", {}).get(city_key)


def read_city_record(persistent_data: Any, *, scope: str = "default", week_key: str,
                     city_key: str) -> dict | None:
    """Only a genuinely missing file/city returns None; corruption propagates."""
    _identity(scope, week_key, city_key)
    with _write_guard(persistent_data):
        document = _load(persistent_data)
        return copy.deepcopy(_city(document, scope, week_key, city_key)) if document is not None else None


def _mutate(persistent_data: Any, scope: str, week_key: str, city_key: str, mutation: Any) -> dict:
    _identity(scope, week_key, city_key)
    with _write_guard(persistent_data):
        missing = _load(persistent_data) is None

        def update(document: dict) -> dict:
            # Only the service's missing-file sentinel may initialize a ledger.
            if missing and document == {}:
                document = {"schema_version": SCHEMA_VERSION, "scopes": {}}
            else:
                _validate_document(document)
            scoped = document["scopes"].setdefault(scope, {"weeks": {}})
            weekly = scoped["weeks"].setdefault(week_key, {"cities": {}})
            cities = weekly["cities"]
            cities[city_key] = mutation(cities.get(city_key))
            return _validate_document(document)

        result = persistent_data.update(file=PURCHASES_FILE, updater=update)
        return copy.deepcopy(_city(result.new_value, scope, week_key, city_key))


def _visits(record: dict) -> list[dict]:
    return [*record["previous_visits"], record]


def _has_pending(record: dict) -> bool:
    return any(batch["status"] == "pending" for visit in _visits(record)
               for batch in visit["batches"].values())


def _current(record: dict | None, visit_id: str | None) -> dict:
    if record is None:
        raise ValueError("begin_visit must precede shop ledger mutations")
    if visit_id is not None and _name(visit_id, "visit_id") != record["visit_id"]:
        raise ValueError("Stale shop visit identity")
    return record


def begin_visit(persistent_data: Any, *, scope: str = "default", week_key: str,
                city_key: str, selected_item_ids: list[str]) -> dict:
    """Create a city/week snapshot once; every existing record stays frozen."""
    def mutation(record: dict | None) -> dict:
        if record is not None:
            return record
        selected = _item_ids(selected_item_ids, allow_empty=False)
        stamp = _stamp()
        return {"completed": False, "visit_id": uuid.uuid4().hex,
                "selected_item_ids": selected, "started_at": stamp, "updated_at": stamp,
                "batches": {}, "previous_visits": [], "completion": None}

    return _mutate(persistent_data, scope, week_key, city_key, mutation)


def _batch_items(items: Any, selected: list[str]) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValueError("Batch items must be a nonempty list of objects")
    for item in items:
        if not isinstance(item, dict) or _name(item.get("item_id"), "item.item_id") not in selected:
            raise ValueError("Every batch item must belong to the visit selection snapshot")
        for field, minimum in (("row_ordinal", 0), ("purchase_units", 1)):
            if field in item and (type(item[field]) is not int or item[field] < minimum):
                raise ValueError(f"item.{field} must be an integer >= {minimum}")
        _stock_counts(item)
        for field in ("stock_snapshot", "stock_before"):
            if field in item:
                if not isinstance(item[field], dict):
                    raise ValueError(f"item.{field} must be an object")
                _stock_counts(item[field])
    return _json_copy(items, "items")


def _stock_counts(snapshot: dict) -> None:
    for field in ("total_count", "soldout_count", "sold_out_count"):
        if field in snapshot and (type(snapshot[field]) is not int or snapshot[field] < 0):
            raise ValueError(f"{field} must be a nonnegative integer")
    if "total_count" in snapshot:
        for field in ("soldout_count", "sold_out_count"):
            if snapshot.get(field, 0) > snapshot["total_count"]:
                raise ValueError("Sold-out tally cannot exceed total_count")
    for field in ("item_counts", "available_counts"):
        if field in snapshot:
            counts = snapshot[field]
            if not isinstance(counts, dict):
                raise ValueError(f"{field} must be an object")
            for item_id, count in counts.items():
                _name(item_id, f"{field}.item_id")
                if type(count) is not int or count < 0:
                    raise ValueError(f"{field} values must be nonnegative integers")


def mark_pending_batch(persistent_data: Any, *, scope: str = "default", week_key: str,
                       city_key: str, batch_id: str, items: list[dict],
                       visit_id: str | None = None) -> dict:
    """Persist click intent once; duplicate IDs and unresolved intent never replay."""
    batch_id = _name(batch_id, "batch_id")

    def mutation(record: dict | None) -> dict:
        record = _current(record, visit_id)
        if record["completed"]:
            raise ValueError("Completed city/week cannot accept a purchase batch")
        payload = _batch_items(items, record["selected_item_ids"])
        if any(batch_id in visit["batches"] for visit in _visits(record)):
            raise ValueError("Batch ID already exists; reconcile evidence, never replay its click")
        if _has_pending(record):
            raise ValueError("Unresolved pending batch must be reconciled before another click")
        stamp = _stamp()
        record["batches"][batch_id] = {"batch_id": batch_id, "status": "pending",
                                      "items": payload, "pending_at": stamp, "evidence": None}
        record["updated_at"] = stamp
        return record

    return _mutate(persistent_data, scope, week_key, city_key, mutation)


def _confirmation_evidence(value: Any) -> dict:
    evidence = _evidence(value)
    if evidence.get("confirmed") is not True or evidence.get("recognized") is not True:
        raise ValueError("Confirmation requires confirmed=true and recognized=true evidence")
    return evidence


def _find_batch(record: dict, batch_id: str, visit_id: str | None) -> dict:
    for visit in _visits(record):
        if batch_id in visit["batches"]:
            if visit_id is not None and visit["visit_id"] != _name(visit_id, "visit_id"):
                raise ValueError("Batch does not belong to the supplied visit_id")
            return visit["batches"][batch_id]
    raise ValueError("Purchase batch must have a persisted pending entry")


def confirm_batch(persistent_data: Any, *, scope: str = "default", week_key: str,
                  city_key: str, batch_id: str, evidence: dict,
                  visit_id: str | None = None) -> dict:
    """Persist recognized purchase evidence, independent of cancellation state."""
    batch_id = _name(batch_id, "batch_id")
    proof = _confirmation_evidence(evidence)

    def mutation(record: dict | None) -> dict:
        record = _current(record, None)
        batch = _find_batch(record, batch_id, visit_id)
        if batch["status"] == "confirmed":
            if batch["evidence"] != proof:
                raise ValueError("Cannot overwrite existing purchase evidence")
            return record
        if record["completed"] or batch["status"] != "pending":
            raise ValueError("Only a pending batch can become confirmed")
        stamp = _stamp()
        batch.update(status="confirmed", evidence=proof, confirmed_at=stamp)
        record["updated_at"] = stamp
        return record

    return _mutate(persistent_data, scope, week_key, city_key, mutation)


def _nonpurchase_evidence(value: Any) -> dict:
    evidence = _evidence(value)
    if evidence.get("purchase_not_applied") is not True or evidence.get("recognized") is not True:
        raise ValueError("Reconciliation requires recognized purchase_not_applied=true evidence")
    return evidence


def resolve_pending_batch(persistent_data: Any, *, scope: str = "default", week_key: str,
                          city_key: str, batch_id: str, evidence: dict,
                          visit_id: str | None = None) -> dict:
    """Close unexecuted/failed intent only on positive nonpurchase evidence.

    Cancellation, absence of a receipt, or a timeout is not such evidence.
    Any later click must receive a new batch_id and a new pending entry.
    """
    batch_id = _name(batch_id, "batch_id")
    proof = _nonpurchase_evidence(evidence)

    def mutation(record: dict | None) -> dict:
        record = _current(record, None)
        batch = _find_batch(record, batch_id, visit_id)
        if batch["status"] == "not_purchased" and batch["evidence"] == proof:
            return record
        if record["completed"] or batch["status"] != "pending":
            raise ValueError("Only pending intent can be resolved as not purchased")
        stamp = _stamp()
        batch.update(status="not_purchased", evidence=proof, resolved_at=stamp)
        record["updated_at"] = stamp
        return record

    return _mutate(persistent_data, scope, week_key, city_key, mutation)


def _completion_evidence(record: dict, value: Any) -> dict:
    proof = _evidence(value)
    if proof.get("scan_status") != "COMPLETE" or proof.get("recognized") is not True:
        raise ValueError("Completion requires a COMPLETE recognized shop scan")
    if any(proof.get(flag) not in (None, False) for flag in ("cancelled", "canceled", "disabled", "error")):
        raise ValueError("Cancellation, disabled actions and errors cannot complete a visit")
    _stock_counts(proof)
    if "stock_snapshot" in proof:
        if not isinstance(proof["stock_snapshot"], dict):
            raise ValueError("Completion stock_snapshot must be an object")
        _stock_counts(proof["stock_snapshot"])
    for field in ("unsold_item_ids", "remaining_selected_item_ids"):
        if field in proof and set(_item_ids(proof[field])).intersection(record["selected_item_ids"]):
            raise ValueError("Unsold selected stock prevents completion")
    outcomes = proof.get("outcomes")
    if not isinstance(outcomes, dict) or set(outcomes) != set(record["selected_item_ids"]):
        raise ValueError("Completion must account for every snapshotted selection")
    purchased = {item["item_id"] for visit in _visits(record)
                 for batch in visit["batches"].values() if batch["status"] == "confirmed"
                 for item in batch["items"]}
    for item_id, outcome in outcomes.items():
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ValueError("Selection outcome must be purchased, sold_out or missing")
        if outcome == "purchased" and item_id not in purchased:
            raise ValueError("Purchased outcome requires persisted confirmation evidence")
    if _has_pending(record):
        raise ValueError("Unresolved purchase intent prevents visit completion")
    return proof


def complete_visit(persistent_data: Any, *, scope: str = "default", week_key: str,
                   city_key: str, reason: str, evidence: dict,
                   visit_id: str | None = None) -> dict:
    """Complete a recognized check, including absent or already-sold-out targets."""
    reason = _name(reason, "reason")
    if reason.lower() in _INCOMPLETE_REASONS:
        raise ValueError("Skipped, cancelled and failed visits cannot complete a week")

    def mutation(record: dict | None) -> dict:
        record = _current(record, visit_id)
        if record["completed"]:
            return record
        proof = _completion_evidence(record, evidence)
        stamp = _stamp()
        record.update(completed=True, updated_at=stamp, completion={
            "visit_id": record["visit_id"], "reason": reason,
            "evidence": proof, "completed_at": stamp,
        })
        return record

    return _mutate(persistent_data, scope, week_key, city_key, mutation)


__all__ = ["PURCHASES_FILE", "SCHEMA_VERSION", "resolve_purchase_week", "read_city_record",
           "begin_visit", "mark_pending_batch", "confirm_batch", "complete_visit",
           "resolve_pending_batch", "purchase_session_lock"]

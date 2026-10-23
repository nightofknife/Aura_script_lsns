"""Request parsing and quantity accounting for product warehouse withdrawals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from numbers import Integral
from typing import Any


def _catalog(products: Mapping[str, str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    if not isinstance(products, Mapping):
        raise ValueError("products must map goods IDs to names")
    names: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    for goods_id, name in products.items():
        if (not isinstance(goods_id, str) or not goods_id.strip()
                or goods_id != goods_id.strip()
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("products must contain nonempty string IDs and names")
        names[goods_id] = name.strip()
        by_name.setdefault(name.strip(), []).append(goods_id)
    return names, by_name


def _identifier(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("goods must contain goods names or IDs")
    if isinstance(value, Integral):
        return str(int(value))
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("goods must contain nonempty goods names or IDs")


def normalize_request(
    goods: Any,
    quantity_threshold: Any,
    products: Mapping[str, str],
) -> dict[str, Any]:
    """Resolve requested names or IDs; ``None`` and empty lists mean all goods.

    All invalid requests raise ``ValueError`` before any game input. A threshold
    of zero remains zero; the caller separately requires a positive stock count.
    """
    if (isinstance(quantity_threshold, bool)
            or not isinstance(quantity_threshold, Integral)
            or quantity_threshold < 0):
        raise ValueError("quantity_threshold must be a nonnegative integer")
    names, by_name = _catalog(products)
    if goods is None or isinstance(goods, (list, tuple)) and not goods:
        goods_ids = None
    else:
        requested = goods if isinstance(goods, (list, tuple)) else [goods]
        goods_ids = []
        seen: set[str] = set()
        for item in requested:
            identity = _identifier(item)
            if identity in names:
                goods_id = identity
            else:
                candidates = by_name.get(identity, [])
                if not candidates:
                    raise ValueError(f"unknown goods name or ID: {identity}")
                if len(candidates) != 1:
                    raise ValueError(f"ambiguous goods name: {identity}; use a goods ID")
                goods_id = candidates[0]
            if goods_id not in seen:
                seen.add(goods_id)
                goods_ids.append(goods_id)
    return {"goods_ids": goods_ids, "quantity_threshold": int(quantity_threshold)}


def _nonnegative_quantity(value: Any) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, Integral) and value >= 0)


def reconcile_taken(
    before: dict[str, int],
    clicked_ids: Iterable[str],
    after: dict[str, int],
    *,
    scan_complete: bool,
    unresolved: bool,
    products: Mapping[str, str],
) -> dict[str, Any]:
    """Account only for IDs whose input and withdrawal effect were confirmed.

    ``clicked_ids`` is supplied by the caller after those confirmations. Missing
    final stock means zero only after a complete scan without unresolved cards.
    Explicit final zero is valid even when other parts of the scan are unknown.
    Quantities describe observed page changes, not independent server persistence.
    """
    if not isinstance(scan_complete, bool) or not isinstance(unresolved, bool):
        raise ValueError("scan_complete and unresolved must be booleans")
    names, _ = _catalog(products)
    goods_taken: list[dict[str, Any]] = []
    unknown_goods: list[dict[str, Any]] = []
    seen: set[str] = set()
    identities = [clicked_ids] if isinstance(clicked_ids, str) else clicked_ids
    for raw_id in identities:
        goods_id = _identifier(raw_id)
        if goods_id in seen:
            continue
        seen.add(goods_id)
        initial = before.get(goods_id)
        final = after.get(goods_id)
        reason = ""
        if goods_id not in names:
            reason = "unknown_goods_id"
        elif goods_id not in before:
            reason = "missing_before_quantity"
        elif not _nonnegative_quantity(initial):
            reason = "invalid_before_quantity"
        elif goods_id in after:
            if not _nonnegative_quantity(final):
                reason = "invalid_after_quantity"
        elif scan_complete and not unresolved:
            final = 0
        else:
            reason = "missing_after_quantity"

        if not reason:
            quantity = int(initial) - int(final)
            if quantity < 0:
                reason = "stock_increased"
            elif quantity > int(initial):
                reason = "quantity_exceeds_source"
        if reason:
            unknown_goods.append({
                "goods": names.get(goods_id, goods_id),
                "goods_id": goods_id,
                "reason": reason,
                "before_quantity": int(initial) if _nonnegative_quantity(initial) else None,
                "after_quantity": int(final) if _nonnegative_quantity(final) else None,
            })
        else:
            goods_taken.append({
                "goods": names[goods_id], "goods_id": goods_id, "quantity": quantity,
            })
    return {
        "goods_taken": goods_taken,
        "unknown_goods": unknown_goods,
        "reconcile_complete": not unknown_goods,
        "total_taken": sum(item["quantity"] for item in goods_taken),
    }

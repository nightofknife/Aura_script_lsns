"""Plan-private buy-lot row normalization shared by market and planner loaders."""

from __future__ import annotations

from typing import Any, Callable, Dict


def normalize_city_product_buy_lot(
    raw: Any, *, error_factory: Callable[..., Exception]
) -> Dict[str, Dict[str, int]]:
    """Keep loader coercions and ordering while retaining its domain error type."""
    if not isinstance(raw, dict):
        raise error_factory(
            code="buy_lot_invalid",
            message="buy_lot payload must include object field city_product_buy_lot.",
        )
    normalized: Dict[str, Dict[str, int]] = {}
    for city_id, products in raw.items():
        if not isinstance(products, dict):
            continue
        ckey = str(city_id).strip()
        if not ckey:
            continue
        row: Dict[str, int] = {}
        for product_id, lot in products.items():
            pkey = str(product_id).strip()
            if not pkey:
                continue
            try:
                value = int(lot)
            except (TypeError, ValueError) as exc:
                raise error_factory(
                    code="buy_lot_invalid",
                    message=f"buy_lot '{ckey}->{pkey}' must be integer.",
                ) from exc
            if value < 0:
                raise error_factory(
                    code="buy_lot_invalid",
                    message=f"buy_lot '{ckey}->{pkey}' must be >= 0.",
                )
            row[pkey] = value
        normalized[ckey] = row
    return normalized

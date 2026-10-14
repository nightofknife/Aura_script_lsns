"""Shared completion proof for physical-bottom and sold-out-boundary scans.

Only old records without any new scan fields may use the historical
``bottom_confirmed`` contract. A partial or explicitly incomplete new proof
must never fall back to that legacy contract.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


_SCAN_FIELDS = frozenset({"scan_complete", "end_reason", "top_confirmed", "boundary_evidence"})


def _finite_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _finite_json(child)
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for child in value.values():
            _finite_json(child)
        return
    raise ValueError("Scan completion evidence must contain only finite JSON values")


def validated_scan_completion(value: Any) -> dict:
    """Validate completion and copy its fields without inventing bottom proof."""
    if not isinstance(value, dict):
        raise ValueError("Scan completion evidence must be an object")
    if not _SCAN_FIELDS.intersection(value):
        if value.get("bottom_confirmed") is not True:
            raise ValueError("Legacy wishlist scan requires confirmed physical bottom")
        return {"bottom_confirmed": True}

    if value.get("scan_complete") is not True:
        raise ValueError("Wishlist scan must explicitly be complete")
    reason = value.get("end_reason")
    if reason == "list_bottom":
        if value.get("bottom_confirmed") is not True:
            raise ValueError("Physical-bottom completion requires bottom confirmation")
        if "top_confirmed" in value and type(value["top_confirmed"]) is not bool:
            raise ValueError("top_confirmed must be a boolean")
    elif reason == "sold_out_boundary":
        if value.get("bottom_confirmed") is not False or value.get("top_confirmed") is not True:
            raise ValueError("Sold-out completion requires a confirmed top, not physical-bottom proof")
        boundary = value.get("boundary_evidence")
        if (not isinstance(boundary, dict)
                or boundary.get("recognized") is not True
                or boundary.get("upper_region_processed") is not True):
            raise ValueError("Sold-out boundary and all preceding goods must be processed")
        frames = boundary.get("stable_frames")
        if type(frames) is not int or frames < 2:
            raise ValueError("Sold-out boundary requires at least two stable frames")
        for field in ("row_top", "column"):
            if type(boundary.get(field)) is not int or boundary[field] < 0:
                raise ValueError(f"Sold-out boundary {field} must be a nonnegative integer")
        score = boundary.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Sold-out boundary score must be finite and between zero and one")
    else:
        raise ValueError("Scan end_reason must be list_bottom or sold_out_boundary")

    fields = {key: value[key] for key in (*_SCAN_FIELDS, "bottom_confirmed") if key in value}
    _finite_json(fields)
    return deepcopy(fields)


__all__ = ["validated_scan_completion"]

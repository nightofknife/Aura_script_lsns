"""Shared completion evidence for current and historical wishlist scans.

Only old records without any new scan fields may use the historical
``bottom_confirmed`` contract. A partial or explicitly incomplete new proof
must never fall back to that legacy contract.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


_SCAN_FIELDS = frozenset({
    "scan_complete", "end_reason", "top_confirmed", "boundary_evidence",
    "scroll_count", "unchanged_drag_count",
})


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
    # Historical records may carry this observation, but scans now begin at the
    # current list position and do not require a confirmed top.
    if "top_confirmed" in value and type(value["top_confirmed"]) is not bool:
        raise ValueError("top_confirmed must be a boolean")
    reason = value.get("end_reason")
    boundary = value.get("boundary_evidence")
    # Historical records have no counters and use upper_region_processed.
    # Current records require both counters, including a first-page sold-out stop.
    if (reason in ("unchanged_view", "drag_limit")
            or any(key in value for key in ("scroll_count", "unchanged_drag_count"))
            or (isinstance(boundary, dict) and "current_page_processed" in boundary)):
        scroll_count = value.get("scroll_count")
        if type(scroll_count) is not int or not 0 <= scroll_count <= 11:
            raise ValueError("scroll_count must be an integer between zero and eleven")
        unchanged_drag_count = value.get("unchanged_drag_count")
        if (type(unchanged_drag_count) is not int
                or not 0 <= unchanged_drag_count <= scroll_count):
            raise ValueError("unchanged_drag_count must be an integer between zero and scroll_count")
    if reason == "list_bottom":
        if value.get("bottom_confirmed") is not True:
            raise ValueError("Physical-bottom completion requires bottom confirmation")
    elif reason == "sold_out_boundary":
        if value.get("bottom_confirmed") is not False:
            raise ValueError("Sold-out completion must not claim physical-bottom proof")
        if not isinstance(boundary, dict) or boundary.get("recognized") is not True:
            raise ValueError("Sold-out completion requires a recognized boundary")
        processed = boundary.get("current_page_processed", boundary.get("upper_region_processed"))
        if processed is not True:
            raise ValueError("Sold-out completion requires the current page to be processed")
        if ("upper_region_processed" in boundary
                and type(boundary["upper_region_processed"]) is not bool):
            raise ValueError("Historical upper_region_processed must be a boolean")
        frames = boundary.get("stable_frames")
        if type(frames) is not int or frames < 2:
            raise ValueError("Sold-out boundary requires at least two stable frames")
        if type(boundary.get("row_top")) is not int:
            raise ValueError("Sold-out boundary row_top must be an integer")
        if type(boundary.get("column")) is not int or boundary["column"] < 0:
            raise ValueError("Sold-out boundary column must be a nonnegative integer")
        score = boundary.get("score")
        if type(score) not in (int, float) or not 0 <= score <= 1 or not math.isfinite(score):
            raise ValueError("Sold-out boundary score must be finite and between zero and one")
    elif reason in ("unchanged_view", "drag_limit"):
        if value.get("bottom_confirmed") is not False:
            raise ValueError("View-based completion must not claim physical-bottom proof")
        if reason == "unchanged_view" and value["unchanged_drag_count"] < 2:
            raise ValueError("Unchanged-view completion requires two consecutive unchanged drags")
        if reason == "drag_limit" and value["scroll_count"] <= 10:
            raise ValueError("Drag-limit completion requires more than ten drags")
    else:
        raise ValueError(
            "Scan end_reason must be sold_out_boundary, unchanged_view, drag_limit, "
            "or historical list_bottom"
        )

    fields = {key: value[key] for key in (*_SCAN_FIELDS, "bottom_confirmed") if key in value}
    _finite_json(fields)
    return deepcopy(fields)


def validated_selection_presence(value: Any, allowed_item_ids=None) -> dict:
    """Validate selected target presence without counting shop entries."""
    if not isinstance(value, dict) or type(value.get("has_selected_goods")) is not bool:
        raise ValueError("has_selected_goods must be a boolean")
    matched = value.get("matched_item_ids")
    if (not isinstance(matched, list)
            or any(not isinstance(item_id, str) or not item_id or item_id != item_id.strip()
                   for item_id in matched)
            or len(set(matched)) != len(matched)):
        raise ValueError("matched_item_ids must contain unique nonempty product IDs")
    if value["has_selected_goods"] != bool(matched):
        raise ValueError("Selected-target presence must agree with matched_item_ids")
    if allowed_item_ids is not None and not set(matched) <= set(allowed_item_ids):
        raise ValueError("Selected targets must belong to the frozen purchase table")
    return {"has_selected_goods": value["has_selected_goods"], "matched_item_ids": list(matched)}


__all__ = ["validated_scan_completion", "validated_selection_presence"]

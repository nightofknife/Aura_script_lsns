"""Independent wishlist selection from LOCAL shop; submit is a separate action."""
from __future__ import annotations

import asyncio
import time

import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ._black_moon_local_shop_policy import load_black_moon_catalog
from ._black_moon_local_select_vision import BlackMoonLocalSelectVision
from ._black_moon_local_scan_contract import validated_scan_completion, validated_selection_presence
from ._bounded_observation import ObservationBudget, async_wait_for_observation
from .black_moon_local_prepare_pc_actions import _capture, _cancel_check, _validated_center


_POLL_SECONDS = .15
_SETTLE_SECONDS = .35
_WAIT_SECONDS = 5.
_MAX_DRAGS = 10
_DRAG_SPAN = 262
_DRAG_HOLD_SECONDS = .5
_MAX_SUPPLEMENTAL_CLICKS = 3


class BlackMoonLocalSelectError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.detail = code, dict(detail or {})

    def to_dict(self):
        return {"code": self.code, "message": str(self), "detail": self.detail}


def normalize_purchase_table(table, known_ids=None):
    known = set(known_ids) if known_ids is not None else {row["item_id"] for row in load_black_moon_catalog()["products"]}
    if not isinstance(table, dict) or not table:
        raise ValueError("Purchase table must be a nonempty item-id to bool object")
    if any(not isinstance(key, str) or key not in known or type(value) is not bool for key, value in table.items()):
        raise ValueError("Purchase table requires known item IDs and strict boolean values")
    return dict(table)


def _same_view(first, second, viewport):
    """Compare the entire goods list, independently of target recognition."""
    x, y, w, h = viewport
    delta = np.abs(first[y:y+h, x:x+w].astype(np.float32) - second[y:y+h, x:x+w].astype(np.float32))
    return bool(float(delta.mean()) < 1.2)


async def execute_black_moon_select(purchase_table, app, reader=None):
    table = normalize_purchase_table(purchase_table, reader.known_ids if reader is not None else None)
    wanted = [key for key, buy in table.items() if buy]
    result = {"success": True, "status": "selected", "has_selected_goods": False,
              "matched_item_ids": [], "scroll_count": 0, "unchanged_drag_count": 0,
              "bottom_confirmed": False, "scan_complete": False, "end_reason": None}
    _cancel_check()
    if not wanted:
        return {**result, "status": "skipped", "reason": "no_purchase_targets"}
    reader = reader if reader is not None else BlackMoonLocalSelectVision(wanted)
    vx, vy, _, vh = reader.VIEWPORT
    matched_item_ids = set()

    def require_active(budget):
        if not budget.active():
            raise BlackMoonLocalSelectError("black_moon_select_not_ready", "LOCAL batch list did not stabilize")

    async def stable_frame():
        budget = ObservationBudget(_WAIT_SECONDS, monotonic=time.monotonic,
                                   check_cancelled=_cancel_check)
        previous = None

        def stable(frame):
            nonlocal previous
            if reader.ready(frame):
                same = previous is not None and _same_view(previous, frame, reader.VIEWPORT)
                previous = frame
                return same
            else:
                previous = None
                return False

        observed = await async_wait_for_observation(
            observe=lambda: _capture(app), accept=stable, budget=budget,
            poll_seconds=_POLL_SECONDS, sleep=asyncio.sleep,
        )
        if observed.matched:
            return observed.value, budget
        raise BlackMoonLocalSelectError("black_moon_select_not_ready", "LOCAL batch list did not stabilize")

    def targets(frame, budget):
        matches = []
        cards = reader.cards(frame)
        require_active(budget)
        for card in cards:
            if card.get("partial") or card["state"] not in {"unselected", "selected"}:
                continue
            hit = reader.identify(frame, card)
            require_active(budget)
            if hit is not None:
                matches.append((card, hit))
        return matches

    async def select_one(card, hit):
        x, y, w, h = card["rect"]
        for attempt in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
            fresh, budget = await stable_frame()
            cards = reader.cards(fresh)
            require_active(budget)
            rows = [row for row in cards if not row.get("partial")
                    and abs(row["rect"][0] - x) <= 3 and abs(row["rect"][1] - y) <= 3]
            if len(rows) != 1:
                raise BlackMoonLocalSelectError("black_moon_select_card_changed",
                                               "Target card moved before selection", {"rect": list(card["rect"])})
            current = rows[0]
            confirmed = reader.identify(fresh, current, candidates=[hit["item_id"]])
            require_active(budget)
            if confirmed is None or confirmed["item_id"] != hit["item_id"]:
                raise BlackMoonLocalSelectError("black_moon_select_identity_changed",
                                               "Target identity changed before selection", hit)
            # The badge ROI is relative to this recognized card, never a screen-wide search.
            mark = reader.selected_mark(fresh, current)
            require_active(budget)
            selected = mark["found"]
            if selected:
                return
            require_active(budget)
            app.click(x=int(x + w * .58), y=int(y + h * .53))
            logger.info("[BlackMoonSelect] item=%s click=%s rect=%s", hit["item_id"], attempt + 1, card["rect"])
            await asyncio.sleep(_SETTLE_SECONDS)
            budget = ObservationBudget(_WAIT_SECONDS, monotonic=time.monotonic,
                                       check_cancelled=_cancel_check)
            stable = 0

            def selected_stably(fresh):
                nonlocal stable
                selected = reader.ready(fresh) and reader.selected_mark(fresh, current)["found"]
                stable = stable + 1 if selected else 0
                return stable >= 2

            observed = await async_wait_for_observation(
                observe=lambda: _capture(app), accept=selected_stably, budget=budget,
                poll_seconds=_POLL_SECONDS, sleep=asyncio.sleep,
            )
            if observed.matched:
                return
        raise BlackMoonLocalSelectError("black_moon_select_click_exhausted",
                                       "Selection exhausted initial click and three supplements", hit)

    def finish(reason, boundary=None):
        require_active(budget)
        result.update(scan_complete=True, end_reason=reason, matched_item_ids=sorted(matched_item_ids))
        if boundary is not None:
            result["boundary_evidence"] = boundary
        logger.info("[BlackMoonSelect] completed=%s", result)
        return result

    frame, budget = await stable_frame()
    while True:
        expected_selected = []
        for card, hit in targets(frame, budget):
            await select_one(card, hit)
            expected_selected.append(card)
            result["has_selected_goods"] = True
            matched_item_ids.add(hit["item_id"])

        # This is the comparison baseline: all clicks and their badge feedback
        # have completed, so changes caused by selection cannot look like scrolling.
        frame, budget = await stable_frame()
        for card in expected_selected:
            mark = reader.selected_mark(frame, card)
            require_active(budget)
            if not mark["found"]:
                raise BlackMoonLocalSelectError("black_moon_select_feedback_changed",
                                               "Target selection feedback disappeared before scrolling",
                                               {"rect": list(card["rect"])})

        markers = reader.sold_out_markers(frame)
        require_active(budget)
        if markers:
            # Confirm the marker in a fresh stable frame; no row registration or
            # proof about clipped cards from previous pages is needed.
            following_frame, budget = await stable_frame()
            following = reader.sold_out_markers(following_frame)
            require_active(budget)
            if following:
                marker = following[0]
                return finish("sold_out_boundary", {
                    **marker, "recognized": True, "stable_frames": 2,
                    "current_page_processed": True,
                })
            frame = following_frame

        # The page following the 11th drag is processed before the limit is used.
        if result["unchanged_drag_count"] >= 2:
            return finish("unchanged_view")
        if result["scroll_count"] > _MAX_DRAGS:
            return finish("drag_limit")

        require_active(budget)
        start = vy + vh - 35
        app.drag(start_x=vx + 180, start_y=start,
                 end_x=vx + 180, end_y=start - _DRAG_SPAN,
                 duration=.45, hold_before_release_sec=_DRAG_HOLD_SECONDS)
        result["scroll_count"] += 1
        await asyncio.sleep(_SETTLE_SECONDS)
        following_frame, budget = await stable_frame()
        unchanged = _same_view(frame, following_frame, reader.VIEWPORT)
        require_active(budget)
        result["unchanged_drag_count"] = result["unchanged_drag_count"] + 1 if unchanged else 0
        logger.info("[BlackMoonSelect] drag=%s list_unchanged=%s consecutive_unchanged=%s",
                    result["scroll_count"], unchanged, result["unchanged_drag_count"])
        frame = following_frame


async def execute_black_moon_submit(selection, app, reader=None, before_submit=None):
    _cancel_check()
    if not isinstance(selection, dict) or not selection.get("success"):
        raise ValueError("Successful selection result is required")
    presence = validated_selection_presence(selection)
    if (not presence["has_selected_goods"] and selection.get("status") == "skipped"
            and selection.get("reason") == "no_purchase_targets"
            and selection.get("scan_complete") is False):
        # Explicit no-work is not a completed inventory scan and cannot authorize
        # an exit/weekly completion. Keep the standalone all-false path inert.
        return {"success": True, "status": "skipped", "reason": "no_purchase_targets",
                "purchase_submitted": False, "purchase_verified": False, **presence}
    try:
        validated_scan_completion(selection)
    except ValueError as exc:
        raise BlackMoonLocalSelectError("black_moon_submit_unsafe", str(exc)) from exc
    if not presence["has_selected_goods"]:
        return {"success": True, "status": "skipped", "reason": "no_selected_goods",
                "purchase_submitted": False, "purchase_verified": False, **presence,
                "scan": {**validated_scan_completion(selection), **presence}}
    reader = reader if reader is not None else BlackMoonLocalSelectVision()
    budget = ObservationBudget(_WAIT_SECONDS, monotonic=time.monotonic,
                               check_cancelled=_cancel_check)
    stable, point = 0, None

    def observe_button():
        frame = _capture(app)
        return reader.button(frame) if reader.ready(frame) and not reader.total_zero(frame) else {"found": False}

    def button_stable(hit):
        nonlocal stable, point
        if hit["found"]:
            current = _validated_center(hit)
            stable = stable + 1 if current == point else 1
            point = current
        else:
            stable, point = 0, None
        return stable >= 2

    observed = await async_wait_for_observation(
        observe=observe_button, accept=button_stable, budget=budget,
        poll_seconds=_POLL_SECONDS, sleep=asyncio.sleep,
    )
    if observed.matched and budget.active():
        if before_submit is not None:
            before_submit()
        if budget.active():
            app.click(x=int(point[0]), y=int(point[1]))
            await asyncio.sleep(_SETTLE_SECONDS)
            return {"success": True, "status": "purchase_submitted", "purchase_submitted": True,
                    "purchase_verified": False, **presence}
    raise BlackMoonLocalSelectError("black_moon_submit_unconfirmed", "Purchase button or nonempty selection is not confirmed")


@action_info(name="resonance_pc.validate_black_moon_purchase_table", public=True, read_only=True,
             description="Validate the item-id to boolean purchase table before any input.")
def resonance_pc_validate_black_moon_purchase_table(purchase_table: dict, exit_target: str = "rest_menu") -> dict:
    if not isinstance(exit_target, str) or exit_target not in {"rest_menu", "main"}:
        raise ValueError("Exit target must be rest_menu or main")
    table = normalize_purchase_table(purchase_table)
    return {"candidate_count": sum(table.values())}


@action_info(name="resonance_pc.select_black_moon_local_goods", public=True, read_only=False,
             description="Select wishlist stock from LOCAL batch shop without purchasing or refreshing.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_select_black_moon_local_goods(purchase_table: dict, app=None) -> dict:
    return await execute_black_moon_select(purchase_table, app)


@action_info(name="resonance_pc.submit_black_moon_local_selection", public=True, read_only=False,
             description="Submit a verified wishlist selection once; do not handle dialogs or exit.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_submit_black_moon_local_selection(selection: dict, app=None) -> dict:
    return await execute_black_moon_submit(selection, app)

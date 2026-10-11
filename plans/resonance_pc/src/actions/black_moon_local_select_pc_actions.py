"""Independent wishlist selection from LOCAL shop; submit is a separate action."""
from __future__ import annotations

import asyncio
import time

import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ._black_moon_local_shop_policy import load_black_moon_catalog
from ._black_moon_local_select_vision import BlackMoonLocalSelectVision
from .black_moon_local_prepare_pc_actions import _capture, _cancel_check, _validated_center


_POLL_SECONDS = .15
_SETTLE_SECONDS = .35
_WAIT_SECONDS = 5.
_MAX_DRAGS = 24
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
    x, y, w, h = viewport
    delta = np.abs(first[y:y+h, x:x+w].astype(np.float32) - second[y:y+h, x:x+w].astype(np.float32))
    return bool(float(delta.mean()) < 1.2)


async def execute_black_moon_select(purchase_table, app, reader=None, require_empty_selection=False):
    table = normalize_purchase_table(purchase_table, reader.known_ids if reader is not None else None)
    wanted = [key for key, buy in table.items() if buy]
    base = {"success": True, "status": "selected", "selected_new_count": 0,
            "selected_items": [], "scroll_count": 0, "bottom_confirmed": False,
            "started_empty": False}
    _cancel_check()
    if not wanted:
        return {**base, "reason": "no_purchase_targets"}
    reader = reader if reader is not None else BlackMoonLocalSelectVision(wanted)
    vx, vy, vw, vh = reader.VIEWPORT
    result = dict(base)

    async def stable_frame():
        deadline = time.monotonic() + _WAIT_SECONDS
        previous = None
        while time.monotonic() < deadline:
            frame = _capture(app)
            if reader.ready(frame):
                if previous is not None and _same_view(previous, frame, reader.VIEWPORT):
                    return frame
                previous = frame
            else:
                previous = None
            await asyncio.sleep(_POLL_SECONDS)
        raise BlackMoonLocalSelectError("black_moon_select_not_ready", "LOCAL batch list did not stabilize")

    async def drag(frame, down, span):
        _cancel_check()
        start = vy + vh - 35 if down else vy + 35
        app.drag(start_x=vx + 180, start_y=start, end_x=vx + 180,
                 end_y=start - span if down else start + span,
                 duration=.45, hold_before_release_sec=.15)
        result["scroll_count"] += 1
        await asyncio.sleep(_SETTLE_SECONDS)
        return await stable_frame()

    frame = await stable_frame()
    result["started_empty"] = reader.total_zero(frame)
    if require_empty_selection and not result["started_empty"]:
        raise BlackMoonLocalSelectError("black_moon_select_preselected", "Submission test requires no preselected goods")

    # LOCAL entry may retain an old scroll position. Reset once; never reset per page/click.
    for _ in range(_MAX_DRAGS):
        after = await drag(frame, False, vh - 70)
        if _same_view(frame, after, reader.VIEWPORT):
            probe = await drag(after, False, 43)
            if _same_view(after, probe, reader.VIEWPORT):
                frame = probe
                break
            after = probe
        frame = after
    else:
        raise BlackMoonLocalSelectError("black_moon_select_top_timeout", "List top could not be confirmed")

    async def select_one(card, hit):
        x, y, w, h = card["rect"]
        for attempt in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
            fresh = await stable_frame()
            rows = [row for row in reader.cards(fresh) if abs(row["rect"][0] - x) <= 3 and abs(row["rect"][1] - y) <= 3]
            if len(rows) != 1 or rows[0].get("partial"):
                return None
            current = rows[0]
            if current["state"] == "selected":
                return {**hit, "click_count": attempt} if attempt else None
            if current["state"] != "unselected":
                raise BlackMoonLocalSelectError("black_moon_select_source_unknown", "Card state is uncertain; no supplemental click")
            confirmed = reader.identify(fresh, current, candidates=[hit["item_id"]])
            if confirmed is None or confirmed["item_id"] != hit["item_id"]:
                return None
            _cancel_check()
            app.click(x=int(x + w * .58), y=int(y + h * .53))
            logger.info("[BlackMoonSelect] item=%s click=%s rect=%s", hit["item_id"], attempt + 1, card["rect"])
            await asyncio.sleep(_SETTLE_SECONDS)
            deadline, stable = time.monotonic() + _WAIT_SECONDS, 0
            while time.monotonic() < deadline:
                fresh = _capture(app)
                state = reader.state(fresh, card) if reader.ready(fresh) else "unknown"
                stable = stable + 1 if state == "selected" else 0
                if stable >= 2:
                    return {**hit, "click_count": attempt + 1}
                await asyncio.sleep(_POLL_SECONDS)
        raise BlackMoonLocalSelectError("black_moon_select_click_exhausted", "Selection exhausted initial click and three supplements", hit)

    for page in range(_MAX_DRAGS):
        cards = reader.cards(frame)
        if not cards:
            raise BlackMoonLocalSelectError("black_moon_select_layout_unknown", "No complete list geometry; not proof of empty shop")
        for card in cards:
            if card.get("partial") or card["state"] != "unselected":
                continue
            hit = reader.identify(frame, card)
            if hit is None:
                continue
            selected = await select_one(card, hit)
            if selected is not None:
                result["selected_items"].append(selected)
                result["selected_new_count"] += 1
        frame = await stable_frame()
        after = await drag(frame, True, 262)
        if _same_view(frame, after, reader.VIEWPORT):
            probe = await drag(after, True, 43)
            if _same_view(after, probe, reader.VIEWPORT):
                if any(row.get("partial") and row["rect"][1] >= vy
                       and row["rect"][1] + row["rect"][3] > vy + vh for row in reader.cards(probe)):
                    raise BlackMoonLocalSelectError("black_moon_select_bottom_partial", "Stopped list still contains a clipped final card")
                result["bottom_confirmed"] = True
                _cancel_check()
                logger.info("[BlackMoonSelect] completed=%s", result)
                return result
            after = probe
        frame = after
    raise BlackMoonLocalSelectError("black_moon_select_scroll_limit", "LOCAL list scan exceeded bounded drag count")


async def execute_black_moon_submit(selection, app, reader=None):
    _cancel_check()
    if not isinstance(selection, dict) or not selection.get("success"):
        raise ValueError("Successful selection result is required")
    count = selection.get("selected_new_count")
    if type(count) is not int or count < 0:
        raise ValueError("Selection count must be a nonnegative integer")
    if count == 0:
        return {"success": True, "status": "skipped", "reason": "no_new_selected_goods", "purchase_submitted": False, "purchase_verified": False}
    if not selection.get("bottom_confirmed") or not selection.get("started_empty"):
        raise BlackMoonLocalSelectError("black_moon_submit_unsafe", "Complete scan and empty initial selection are required")
    reader = reader if reader is not None else BlackMoonLocalSelectVision()
    deadline, stable, point = time.monotonic() + _WAIT_SECONDS, 0, None
    while time.monotonic() < deadline:
        frame = _capture(app)
        hit = reader.button(frame) if reader.ready(frame) and not reader.total_zero(frame) else {"found": False}
        if hit["found"]:
            current = _validated_center(hit)
            stable = stable + 1 if current == point else 1
            point = current
            if stable >= 2:
                _cancel_check()
                app.click(x=int(point[0]), y=int(point[1]))
                await asyncio.sleep(_SETTLE_SECONDS)
                return {"success": True, "status": "purchase_submitted", "purchase_submitted": True,
                        "purchase_verified": False, "selected_items": selection["selected_items"]}
        else:
            stable, point = 0, None
        await asyncio.sleep(_POLL_SECONDS)
    raise BlackMoonLocalSelectError("black_moon_submit_unconfirmed", "Purchase button or nonempty selection is not confirmed")


@action_info(name="resonance_pc.validate_black_moon_purchase_table", public=True, read_only=True,
             description="Validate the item-id to boolean purchase table before any input.")
def resonance_pc_validate_black_moon_purchase_table(purchase_table: dict) -> dict:
    table = normalize_purchase_table(purchase_table)
    return {"candidate_count": sum(table.values())}


@action_info(name="resonance_pc.select_black_moon_local_goods", public=True, read_only=False,
             description="Select wishlist stock from LOCAL batch shop without purchasing or refreshing.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_select_black_moon_local_goods(purchase_table: dict, require_empty_selection: bool = False, app=None) -> dict:
    return await execute_black_moon_select(purchase_table, app, require_empty_selection=require_empty_selection)


@action_info(name="resonance_pc.submit_black_moon_local_selection", public=True, read_only=False,
             description="Submit a verified wishlist selection once; do not handle dialogs or exit.")
@requires_services(app="plans/aura_base/app")
async def resonance_pc_submit_black_moon_local_selection(selection: dict, app=None) -> dict:
    return await execute_black_moon_submit(selection, app)

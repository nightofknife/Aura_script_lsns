"""Independent wishlist selection from LOCAL shop; submit is a separate action."""
from __future__ import annotations

import asyncio
import time

import numpy as np
import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger

from ._black_moon_local_shop_policy import load_black_moon_catalog
from ._black_moon_local_select_vision import BlackMoonLocalSelectVision
from ._black_moon_local_scan_contract import validated_scan_completion
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


def _same_boundary(first, second):
    return (first["column"] == second["column"]
            and abs(first["row_top"] - second["row_top"]) <= 3
            and abs(first["marker_rect"][0] - second["marker_rect"][0]) <= 3)


def _project_processed_cards(source, following, reader, processed):
    """Register an overlapping complete row before crediting a clipped card.

    Matching an arbitrary old fragment alone could confuse identical product
    duplicates whose selected badge has scrolled away. Require one unambiguous
    whole-row scroll displacement and the corresponding original card instance.
    """
    _, vy, _, vh = reader.VIEWPORT
    width, height = reader.meta["card_size"]
    columns = reader.meta["card_columns"]
    offsets = []
    joined = np.concatenate([following[vy:vy + vh, column:column + width] for column in columns], axis=1)
    for left in processed:
        x, y, _, _ = left["rect"]
        if x != columns[0]:
            continue
        right = next((card for card in processed if card["rect"][0] == columns[1]
                      and abs(card["rect"][1] - y) <= 3), None)
        if right is None or y < vy or y + height > vy + vh:
            continue
        # Use one common pixel baseline for the whole row. Independent corner
        # hits may differ by a pixel, which must not vertically offset one half
        # of the registration template.
        template = np.concatenate([source[y:y + height, column:column + width] for column in columns], axis=1)
        response = 1. - cv2.matchTemplate(np.ascontiguousarray(joined), np.ascontiguousarray(template), cv2.TM_SQDIFF_NORMED)
        response = np.nan_to_num(response, nan=-1., posinf=-1., neginf=-1.)
        while response.size:
            _, score, _, (_, py) = cv2.minMaxLoc(response)
            if score < .995:
                break
            displacement = y - (vy + py)
            if 0 <= displacement <= vh - height:
                offsets.append(displacement)
            response[max(0, py - 12):min(response.shape[0], py + 13), :] = -1.
    if not offsets or max(offsets) - min(offsets) > 3:
        raise BlackMoonLocalSelectError("black_moon_select_scroll_overlap_unknown",
                                       "Processed row overlap is missing or ambiguous; cannot skip clipped stock")
    displacement = round(float(np.median(offsets)))
    return [{**card, "rect": (card["rect"][0], card["rect"][1] - displacement, width, height)}
            for card in processed]


def _covered_upper_fragment(frame, reader, column, top, processed):
    _, vy, _, vh = reader.VIEWPORT
    width, height = reader.meta["card_size"]
    for previous in processed:
        px, py, _, _ = previous["rect"]
        if abs(px - column) > 3 or abs(py - top) > 6:
            continue
        # Refine only the already-linked physical row, not an arbitrary old
        # duplicate product. Badge/grid rounding can differ by a few pixels.
        for adjustment in range(-3, 4):
            actual_top = py + adjustment
            bottom = min(actual_top + height, vy + vh)
            if actual_top >= vy or bottom <= vy:
                continue
            visible = frame[vy:bottom, px:px + width]
            old = previous["pixels"][vy - actual_top:bottom - actual_top]
            if old.shape == visible.shape and float(np.abs(old.astype(np.float32) - visible.astype(np.float32)).mean()) < 1.2:
                return True
    return False


def _complete_page_cards(reader, cards, physical_bottom=False):
    """Do not scroll an unresolved full row out of the viewport."""
    if not cards:
        raise BlackMoonLocalSelectError("black_moon_select_layout_unknown", "No complete list geometry; not proof of empty shop")
    _, vy, _, vh = reader.VIEWPORT
    width, height = reader.meta["card_size"]
    pitch = reader.meta["sold_out_boundary"]["row_pitch"]
    anchor = max(cards, key=lambda card: card["geometry_score"])["rect"][1]
    first = anchor
    while first - pitch >= vy - 3:
        first -= pitch
    while first < vy - 3:
        first += pitch
    top = first
    last_row = max(card["rect"][1] for card in cards)
    while round(top) + height <= vy + vh and (not physical_bottom or top <= last_row + 6):
        row_top = round(top)
        for column in reader.meta["card_columns"]:
            matches = [card for card in cards if abs(card["rect"][0] - column) <= 3
                       and abs(card["rect"][1] - row_top) <= 6 and not card.get("partial")]
            # At a positively confirmed physical end, a densely packed grid can
            # have a last left card and no right companion. Never allow an
            # interior gap, an unknown existing card, or a missing left slot.
            trailing_right = (physical_bottom and not matches and column == reader.meta["card_columns"][-1]
                              and abs(row_top - last_row) <= 6
                              and any(abs(card["rect"][0] - reader.meta["card_columns"][0]) <= 3
                                      and abs(card["rect"][1] - row_top) <= 6 and not card.get("partial")
                                      for card in cards)
                              and not any(abs(card["rect"][0] - column) <= 3
                                          and abs(card["rect"][1] - row_top) <= 6 for card in cards))
            if trailing_right:
                continue
            if len(matches) != 1 or matches[0]["state"] not in {"selected", "unselected", "sold_out"}:
                raise BlackMoonLocalSelectError("black_moon_select_page_incomplete",
                                               "A full row must be recognized before scrolling it away",
                                               {"column": column, "row_top": row_top, "matches": len(matches)})
        top += pitch
    return cards


def _upper_boundary_cards(frame, reader, boundary, markers, cards, processed, physical_bottom=False):
    """Require both columns of every row through the first sold-out row.

    A clipped upper row is accepted only if its visible pixels match a full
    card already processed on a previous overlapping page. Empty geometry is
    never evidence that the still-available area has been handled.
    """
    vx, vy, vw, vh = reader.VIEWPORT
    width, height = reader.meta["card_size"]
    pitch = reader.meta["sold_out_boundary"]["row_pitch"]
    row_tops, step = [], 0
    while True:
        top = round(boundary["row_top"] - pitch * step)
        if top + height <= vy:
            break
        row_tops.append(top)
        step += 1
    required, lower_partial = [], False
    for top in reversed(row_tops):
        for column in reader.meta["card_columns"]:
            sold = any(hit["column"] == column and abs(hit["row_top"] - top) <= 6 for hit in markers)
            if sold:
                # Positive marker recognition does not depend on a dark card's
                # ordinary corner, and never authorizes an item click.
                continue
            # A positively confirmed dense-grid tail may contain only a left
            # sold-out card. This exception is limited to that boundary row's
            # empty right slot, including when its upper edge is clipped.
            slot_cards = [card for card in cards if abs(card["rect"][0] - column) <= 3
                          and abs(card["rect"][1] - top) <= 6]
            left_sold_tail = (column == reader.meta["card_columns"][-1]
                              and abs(top - boundary["row_top"]) <= 6
                              and not slot_cards
                              and any(hit["column"] == reader.meta["card_columns"][0]
                                      and abs(hit["row_top"] - top) <= 6 for hit in markers))
            if left_sold_tail:
                if physical_bottom:
                    continue
                raise BlackMoonLocalSelectError("black_moon_select_boundary_tail_unproven",
                                               "A possible empty right tail requires physical bottom confirmation",
                                               {"column": column, "row_top": top})
            if top < vy:
                if not _covered_upper_fragment(frame, reader, column, top, processed):
                    raise BlackMoonLocalSelectError("black_moon_select_upper_partial_unproven",
                                                   "Clipped goods above the sold-out boundary were not confirmed processed",
                                                   {"column": column, "row_top": top})
                continue
            if top + height > vy + vh:
                lower_partial = True
                continue
            matches = [card for card in cards if abs(card["rect"][0] - column) <= 3
                       and abs(card["rect"][1] - top) <= 6 and not card.get("partial")]
            if len(matches) != 1 or matches[0]["state"] not in {"unselected", "selected", "sold_out"}:
                raise BlackMoonLocalSelectError("black_moon_select_upper_region_unknown",
                                               "Every card above/in the sold-out row must have known geometry and state",
                                               {"column": column, "row_top": top, "matches": len(matches)})
            if matches[0]["state"] != "sold_out":
                required.append(matches[0])
    return required, lower_partial


async def execute_black_moon_select(purchase_table, app, reader=None, require_empty_selection=False):
    table = normalize_purchase_table(purchase_table, reader.known_ids if reader is not None else None)
    wanted = [key for key, buy in table.items() if buy]
    base = {"success": True, "status": "selected", "selected_new_count": 0,
            "selected_items": [], "scroll_count": 0, "bottom_confirmed": False,
            "started_empty": False, "scan_complete": False, "end_reason": None,
            "top_confirmed": False}
    _cancel_check()
    if not wanted:
        return {**base, "status": "skipped", "reason": "no_purchase_targets"}
    reader = reader if reader is not None else BlackMoonLocalSelectVision(wanted)
    vx, vy, vw, vh = reader.VIEWPORT
    result = dict(base)
    processed = []

    async def stable_frame():
        deadline = time.monotonic() + _WAIT_SECONDS
        previous = None
        while time.monotonic() < deadline:
            frame = _capture(app)
            _cancel_check()
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
                result["top_confirmed"] = True
                break
            after = probe
        frame = after
    else:
        raise BlackMoonLocalSelectError("black_moon_select_top_timeout", "List top could not be confirmed")

    async def boundary_probe(frame):
        observe = getattr(reader, "sold_out_markers", None)
        markers = observe(frame) if callable(observe) else []
        if not markers:
            return None, markers, frame
        first = markers[0]
        await asyncio.sleep(_POLL_SECONDS)
        fresh = await stable_frame()
        following = observe(fresh)
        if not following or not _same_boundary(first, following[0]):
            raise BlackMoonLocalSelectError("black_moon_select_boundary_unstable",
                                           "Sold-out boundary was not stable in two fresh frames")
        boundary = {**following[0], "recognized": True, "stable_frames": 2,
                    "score": min(first["score"], following[0]["score"])}
        logger.info("[BlackMoonSelect] boundary_seen=%s upper_region_processed=false", boundary)
        return boundary, following, fresh

    async def select_one(card, hit, boundary=None):
        x, y, w, h = card["rect"]
        for attempt in range(_MAX_SUPPLEMENTAL_CLICKS + 1):
            fresh = await stable_frame()
            fresh_cards = reader.cards(fresh, row_limit=boundary["row_top"]) if boundary else reader.cards(fresh)
            rows = [row for row in fresh_cards if abs(row["rect"][0] - x) <= 3 and abs(row["rect"][1] - y) <= 3]
            if len(rows) != 1 or rows[0].get("partial"):
                raise BlackMoonLocalSelectError("black_moon_select_upper_card_changed", "Required card geometry changed before selection")
            current = rows[0]
            if current["state"] == "selected":
                return {**hit, "click_count": attempt} if attempt else None
            if current["state"] != "unselected":
                raise BlackMoonLocalSelectError("black_moon_select_source_unknown", "Card state is uncertain; no supplemental click")
            confirmed = reader.identify(fresh, current, candidates=[hit["item_id"]])
            if confirmed is None or confirmed["item_id"] != hit["item_id"]:
                raise BlackMoonLocalSelectError("black_moon_select_upper_identity_changed", "Required target identity changed before selection")
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
        boundary, markers, frame = await boundary_probe(frame)
        cards = reader.cards(frame, row_limit=boundary["row_top"]) if boundary else reader.cards(frame)
        lower_partial = False
        physical_bottom = False
        if boundary:
            try:
                cards, lower_partial = _upper_boundary_cards(frame, reader, boundary, markers, cards, processed)
            except BlackMoonLocalSelectError as exc:
                if exc.code != "black_moon_select_boundary_tail_unproven":
                    raise
                after = await drag(frame, True, 262)
                if not _same_view(frame, after, reader.VIEWPORT):
                    raise exc
                probe = await drag(after, True, 43)
                if not _same_view(after, probe, reader.VIEWPORT):
                    raise exc
                following, markers, frame = await boundary_probe(probe)
                if following is None or not _same_boundary(boundary, following):
                    raise BlackMoonLocalSelectError("black_moon_select_boundary_changed",
                                                   "Sold-out tail changed during bottom confirmation")
                boundary, physical_bottom = following, True
                cards = reader.cards(frame, row_limit=boundary["row_top"])
                cards, lower_partial = _upper_boundary_cards(frame, reader, boundary, markers, cards, processed,
                                                             physical_bottom=True)
        else:
            try:
                cards = _complete_page_cards(reader, cards)
            except BlackMoonLocalSelectError as exc:
                # A possible short-list tail is not an end by absence. Prove
                # no movement with the normal drag and a non-row-sized probe.
                detail = exc.detail
                last_row = max((card["rect"][1] for card in cards), default=-1)
                tail = (exc.code == "black_moon_select_page_incomplete" and detail.get("matches") == 0
                        and (detail.get("row_top", -1) > last_row + 6
                             or (abs(detail.get("row_top", -999) - last_row) <= 6
                                 and detail.get("column") == reader.meta["card_columns"][-1])))
                if not tail:
                    raise
                after = await drag(frame, True, 262)
                if not _same_view(frame, after, reader.VIEWPORT):
                    raise exc
                probe = await drag(after, True, 43)
                if not _same_view(after, probe, reader.VIEWPORT):
                    raise exc
                frame, physical_bottom = probe, True
                cards = _complete_page_cards(reader, reader.cards(frame), physical_bottom=True)
                if any(card.get("partial") and card["rect"][1] + card["rect"][3] > vy + vh for card in cards):
                    raise BlackMoonLocalSelectError("black_moon_select_bottom_partial", "Physical end still has a clipped final card")
        expected_selected = [card for card in cards if card["state"] == "selected" and not card.get("partial")]
        for card in cards:
            if card.get("partial") or card["state"] in {"selected", "sold_out"}:
                continue
            if card["state"] != "unselected":
                raise BlackMoonLocalSelectError("black_moon_select_page_state_unknown", "An unknown card cannot be skipped before scan completion")
            hit = reader.identify(frame, card)
            if hit is None:
                continue
            selected = await select_one(card, hit, boundary)
            expected_selected.append(card)
            if selected is not None:
                result["selected_items"].append(selected)
                result["selected_new_count"] += 1
        frame = await stable_frame()
        def verify_selection_feedback(current):
            for expected in expected_selected:
                if reader.state(current, expected) != "selected":
                    raise BlackMoonLocalSelectError("black_moon_select_feedback_changed",
                                                   "A counted target is no longer selected; no completion or payment",
                                                   {"rect": list(expected["rect"])})

        verify_selection_feedback(frame)
        if physical_bottom and boundary is None:
            _complete_page_cards(reader, reader.cards(frame), physical_bottom=True)
            result.update(scan_complete=True, end_reason="list_bottom", bottom_confirmed=True)
            _cancel_check()
            logger.info("[BlackMoonSelect] completed=%s", result)
            return result
        page_processed = []
        for card in cards:
            if card.get("partial"):
                continue
            x, y, width, height = card["rect"]
            if reader.state(frame, card) in {"selected", "unselected"}:
                page_processed.append({"rect": card["rect"], "pixels": frame[y:y + height, x:x + width].copy()})
        if boundary:
            final_boundary, final_markers, frame = await boundary_probe(frame)
            verify_selection_feedback(frame)
            if final_boundary is None or not _same_boundary(boundary, final_boundary):
                raise BlackMoonLocalSelectError("black_moon_select_boundary_changed", "Sold-out boundary changed before scan completion")
            final_cards = reader.cards(frame, row_limit=final_boundary["row_top"])
            _, final_partial = _upper_boundary_cards(frame, reader, final_boundary, final_markers, final_cards, processed,
                                                     physical_bottom=physical_bottom)
            if not lower_partial and not final_partial:
                result.update(scan_complete=True, end_reason="sold_out_boundary",
                              boundary_evidence={**final_boundary, "upper_region_processed": True,
                                                 "processed_page_count": page + 1})
                _cancel_check()
                logger.info("[BlackMoonSelect] completed=%s", result)
                return result
            # Finish the other column of a bottom-clipped boundary row on the
            # next overlapping page; seeing the marker alone cannot finish.
        for card in page_processed:
            x, y, width, height = card["rect"]
            card["pixels"] = frame[y:y + height, x:x + width].copy()
        after = await drag(frame, True, 262)
        if _same_view(frame, after, reader.VIEWPORT):
            probe = await drag(after, True, 43)
            if _same_view(after, probe, reader.VIEWPORT):
                if boundary and (lower_partial or final_partial):
                    raise BlackMoonLocalSelectError("black_moon_select_boundary_row_partial",
                                                   "The other column of the sold-out row is still clipped; scan is incomplete")
                if any(row.get("partial") and row["rect"][1] >= vy
                       and row["rect"][1] + row["rect"][3] > vy + vh for row in reader.cards(probe)):
                    raise BlackMoonLocalSelectError("black_moon_select_bottom_partial", "Stopped list still contains a clipped final card")
                verify_selection_feedback(probe)
                result["bottom_confirmed"] = True
                result.update(scan_complete=True, end_reason="list_bottom")
                _cancel_check()
                logger.info("[BlackMoonSelect] completed=%s", result)
                return result
            after = probe
        processed = _project_processed_cards(frame, after, reader, page_processed)
        frame = after
    raise BlackMoonLocalSelectError("black_moon_select_scroll_limit", "LOCAL list scan exceeded bounded drag count")


async def execute_black_moon_submit(selection, app, reader=None, before_submit=None):
    _cancel_check()
    if not isinstance(selection, dict) or not selection.get("success"):
        raise ValueError("Successful selection result is required")
    count = selection.get("selected_new_count")
    if type(count) is not int or count < 0:
        raise ValueError("Selection count must be a nonnegative integer")
    if (count == 0 and selection.get("status") == "skipped"
            and selection.get("reason") == "no_purchase_targets"
            and selection.get("selected_items") == [] and selection.get("scan_complete") is False):
        # Explicit no-work is not a completed inventory scan and cannot authorize
        # an exit/weekly completion. Keep the standalone all-false path inert.
        return {"success": True, "status": "skipped", "reason": "no_purchase_targets",
                "purchase_submitted": False, "purchase_verified": False}
    try:
        validated_scan_completion(selection)
    except ValueError as exc:
        raise BlackMoonLocalSelectError("black_moon_submit_unsafe", str(exc)) from exc
    if not selection.get("started_empty"):
        raise BlackMoonLocalSelectError("black_moon_submit_unsafe", "Complete scan and empty initial selection are required")
    if count == 0:
        return {"success": True, "status": "skipped", "reason": "no_new_selected_goods",
                "purchase_submitted": False, "purchase_verified": False,
                "scan": {**validated_scan_completion(selection), "started_empty": True,
                         "selected_card_count": 0}}
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
                if before_submit is not None:
                    before_submit()
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
def resonance_pc_validate_black_moon_purchase_table(purchase_table: dict, exit_target: str = "rest_menu") -> dict:
    if not isinstance(exit_target, str) or exit_target not in {"rest_menu", "main"}:
        raise ValueError("Exit target must be rest_menu or main")
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

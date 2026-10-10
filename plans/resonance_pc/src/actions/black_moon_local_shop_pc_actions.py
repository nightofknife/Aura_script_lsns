"""Batch LOCAL purchases with weekly gates and a durable single-submit journal."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
import time
from typing import Any, Callable
import uuid

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._black_moon_local_shop_policy import (
    load_black_moon_catalog, normalize_record_scope, normalize_selected_items,
)
from ._black_moon_local_shop_state import (
    begin_visit, complete_visit, confirm_batch, mark_pending_batch,
    purchase_session_lock, read_city_record, resolve_pending_batch, resolve_purchase_week,
)
from ._black_moon_local_shop_vision import (
    BlackMoonLocalShopRecognitionError, BlackMoonLocalShopVision,
)


_BLACK_MOON_PROGRESS_CALLBACK: ContextVar[Callable | None] = ContextVar(
    "black_moon_local_purchase_progress_callback", default=None,
)
_MAX_SCROLLS = 40
_TIMEOUT = 7.0


class BlackMoonLocalPurchaseError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.detail = code, dict(detail or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "detail": self.detail}


def _cancel_check():
    if is_current_task_cancel_requested():
        raise BlackMoonLocalPurchaseError("trade_cancelled", "Black Moon purchase cancelled")


def _key(value):
    if isinstance(value, list):
        return tuple(_key(row) for row in value)
    if isinstance(value, dict):
        return tuple((name, _key(value.get(name))) for name in
                     ("found", "rect", "item_id", "partial", "selected", "sold_out"))
    if isinstance(value, tuple):
        return tuple(value)
    return value


def _ledger_batches(record: dict):
    return [batch for visit in [*record["previous_visits"], record]
            for batch in visit["batches"].values()]


def _stock(rows: list[dict]) -> dict:
    return {"total_count": len(rows), "sold_out_count": sum(row["sold_out"] for row in rows),
            "available_counts": dict(Counter(row["item_id"] for row in rows if not row["sold_out"]))}


def _reconciles(before: dict, after: dict, items: list[dict]) -> bool:
    counts = Counter(before["available_counts"])
    counts.subtract(Counter(item["item_id"] for item in items))
    if any(count < 0 for count in counts.values()):
        return False
    expected = {key: count for key, count in counts.items() if count}
    return (before["total_count"] == after["total_count"]
            and after["sold_out_count"] == before["sold_out_count"] + len(items)
            and after["available_counts"] == expected)


class _PurchaseSession:
    def __init__(self, *, app, reader, persistent_data, catalog, city, scope, week):
        self.app, self.reader, self.persistent_data = app, reader, persistent_data
        self.catalog, self.city, self.scope, self.week = catalog, city, scope, week
        self.identity = {"scope": scope, "week_key": week, "city_key": city["city_key"]}
        self.page = "rest_menu"
        self.scroll_count = 0

    def fail(self, code, message, **detail):
        raise BlackMoonLocalPurchaseError(code, message, {"page_state": self.page, **detail})

    def report(self, state, **detail):
        fields = {"city_name": self.city["city_name"], "city_key": self.city["city_key"],
                  "week_key": self.week, "page_state": self.page, **detail}
        logger.info("[BlackMoonLocalPurchase] state=%s detail=%s", state, fields)
        callback = _BLACK_MOON_PROGRESS_CALLBACK.get()
        if callback:
            callback(state, fields)

    def current_week(self):
        if resolve_purchase_week(self.catalog["refresh_policy"]) != self.week:
            self.fail("black_moon_week_changed", "Shop refresh crossed the frozen purchase week")

    def capture(self, *, cancellation=True):
        if cancellation:
            _cancel_check()
        result = self.app.capture()
        if not result.success or result.image is None:
            self.fail("black_moon_capture_failed", "Could not capture the shop")
        frame = np.asarray(result.image)
        if frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
            self.fail("black_moon_resolution_mismatch", "Shop requires uint8 RGB 1280x720")
        return frame

    async def wait(self, probe, *, timeout=_TIMEOUT):
        deadline, last_key, stable = time.monotonic() + timeout, None, 0
        last_error = None
        while time.monotonic() < deadline:
            frame = self.capture()
            try:
                value = probe(frame)
            except BlackMoonLocalShopRecognitionError as exc:
                value, last_error = None, exc.code
            if value is not None and value is not False:
                observed = _key(value)
                stable = stable + 1 if observed == last_key else 1
                last_key = observed
                if stable >= 2:
                    return frame, value
            else:
                stable, last_key = 0, None
            await asyncio.sleep(.2)
        self.fail("black_moon_state_timeout", "Shop state did not stabilize", recognition_error=last_error)

    def click(self, point):
        _cancel_check()
        if point is None or len(point) != 2:
            self.fail("black_moon_click_missing", "Required native click point is missing")
        self.app.click(x=int(point[0]), y=int(point[1]))

    async def button(self, name):
        _, hit = await self.wait(lambda frame: self.reader.match(frame, name)
                                 if self.reader.match(frame, name)["found"] else None)
        self.click(hit["center"])
        self.page = "transition"
        await asyncio.sleep(.35)

    async def open_local(self):
        await self.wait(lambda frame: True if self.reader.match(frame, "rest_menu")["found"] else None)
        await self.button("shop_entry")
        await self.wait(lambda frame: True if self.reader.match(frame, "shop_page")["found"]
                        and not self.reader.match(frame, "reward")["found"] else None)
        self.page = "shop_page"
        frame = self.capture()
        if not self.reader.is_local(frame):
            await self.button("local_tab_unselected")
        await self.wait(lambda frame: True if self.reader.is_local(frame) else None)
        self.page = "local_shop"
        _, mode = await self.wait(lambda frame: self.reader.mode(frame)
                                 if self.reader.mode(frame) != "unknown" else None)
        if mode == "single":
            await self.button("batch_off")
        await self.wait(lambda frame: True if self.reader.mode(frame) == "batch" else None)
        self.page = "local_shop"

    async def cards(self):
        def read(frame):
            if self.reader.mode(frame) != "batch":
                return None
            return self.reader.detect_cards(frame)
        return await self.wait(read)

    def tile(self, frame, card):
        x, y, w, h = card["rect"]
        if card["partial"]:
            return None
        # Scroll overlap includes sold-out signs; it never proves an item identity.
        return cv2.resize(frame[y + 5:y + h - 5, x + 5:x + w - 5], (64, 24),
                          interpolation=cv2.INTER_AREA).astype(np.float32)

    @staticmethod
    def same_tile(first, second):
        return first is not None and second is not None and float(np.abs(first - second).mean()) < 1.8

    def same_view(self, before, after):
        x, y, w, h = self.reader.VIEWPORT
        return float(np.abs(before[y:y + h, x:x + w].astype(np.float32)
                            - after[y:y + h, x:x + w].astype(np.float32)).mean()) < 1.2

    async def drag(self, *, down: bool, span: int):
        _cancel_check()
        x, y, w, h = self.reader.VIEWPORT
        start = y + h - 30 if down else y + 30
        self.app.drag(start_x=x + w // 2, start_y=start, end_x=x + w // 2,
                      end_y=start - span if down else start + span,
                      duration=.6, hold_before_release_sec=.35)
        await asyncio.sleep(.45)
        self.scroll_count += 1

    async def top(self):
        frame, cards = await self.cards()
        stationary = 0
        for _ in range(_MAX_SCROLLS):
            await self.drag(down=False, span=self.reader.VIEWPORT[3] - 70)
            after, new = await self.cards()
            stationary = stationary + 1 if self.same_view(frame, after) else 0
            frame, cards = after, new
            if stationary >= 2:
                vy = self.reader.VIEWPORT[1]
                if any(card["rect"][1] < vy for card in cards) or min(card["rect"][1] for card in cards) > vy + 16:
                    self.fail("black_moon_top_unconfirmed", "Shop start boundary is not confirmed")
                return frame, cards
        self.fail("black_moon_top_scroll_limit", "Could not reach the shop start boundary")

    async def selection(self, card, desired):
        x, y, w, h = card["rect"]
        self.click((x + w // 2, y + h // 2))
        def selected(frame):
            rows = [row for row in self.reader.detect_cards(frame)
                    if abs(row["rect"][0] - x) <= 3 and abs(row["rect"][1] - y) <= 3]
            if len(rows) != 1:
                return None
            row = rows[0]
            if row["partial"] or row["sold_out"] or row["item_id"] != card["item_id"]:
                self.fail("black_moon_card_changed", "Card identity changed while selecting")
            return row if row["selected"] is desired else None
        await self.wait(selected)

    def layout(self, cards):
        columns = self.reader.metadata["cards"]["columns"]
        positions = []
        for card in sorted(cards, key=lambda row: row["rect"][1]):
            if not positions or card["rect"][1] - positions[-1] > 3:
                positions.append(card["rect"][1])
        if len(positions) < 2:
            self.fail("black_moon_layout_incomplete", "Not enough card rows to establish list geometry")
        stride = int(round(float(np.median(np.diff(positions)))))
        if stride < self.reader.metadata["cards"]["size"][1] or stride > 180:
            self.fail("black_moon_layout_incomplete", "Shop row spacing is invalid")
        for position in positions:
            rows = [row for row in cards if abs(row["rect"][1] - position) <= 3]
            if len(rows) != 2 or any(min(abs(row["rect"][0] - col) for col in columns) > 4 for row in rows):
                self.fail("black_moon_layout_incomplete", "A two-column stock row was not fully observed")
        if any(abs(position - positions[0] - round((position - positions[0]) / stride) * stride) > 3
               for position in positions):
            self.fail("black_moon_layout_incomplete", "A stock row is missing from the viewport")
        return stride, columns

    async def scan(self, *, desired: set[str], select: bool):
        frame, cards = await self.top()
        stride, columns = self.layout(cards)
        first_y = min(card["rect"][1] for card in cards)
        offset, observed, stationary = 0, {}, 0
        for _ in range(_MAX_SCROLLS):
            self.current_week()
            self.report("running", phase="scanning", observed_count=len(observed))
            self.layout(cards)
            for card in cards:
                if card["partial"]:
                    continue
                if not card["sold_out"] and card["item_id"] is None:
                    self.fail("black_moon_unknown_stock", "Unrecognized available stock blocks completion",
                              rect=list(card["rect"]))
                if select and not card["sold_out"]:
                    wanted = card["item_id"] in desired
                    if bool(card["selected"]) != wanted:
                        await self.selection(card, wanted)
            frame, cards = await self.cards()
            for card in cards:
                if card["partial"]:
                    continue
                global_y = card["rect"][1] + offset - first_y
                row_index = round(global_y / stride)
                if abs(global_y - row_index * stride) > 5:
                    self.fail("black_moon_scroll_grid_changed", "Scroll overlap changed stock row geometry")
                column = min(range(2), key=lambda i: abs(columns[i] - card["rect"][0]))
                index = row_index * 2 + column
                snapshot = {"item_id": card["item_id"], "sold_out": bool(card["sold_out"]),
                            "selected": bool(card["selected"]), "stock_index": index}
                if index in observed and observed[index] != snapshot:
                    self.fail("black_moon_stock_changed", "Stock changed during the full scan")
                observed[index] = snapshot
            old_tiles = [(card, self.tile(frame, card)) for card in cards if not card["partial"]]
            # A non-row-sized boundary probe avoids mistaking repeated packs for a stopped list.
            await self.drag(down=True, span=43 if stationary else stride * 2)
            after, new_cards = await self.cards()
            if self.same_view(frame, after):
                stationary += 1
                if stationary >= 2:
                    if any(card["partial"] and card["rect"][1] > self.reader.VIEWPORT[1] for card in new_cards):
                        self.fail("black_moon_bottom_unconfirmed", "List stopped with unobserved clipped stock")
                    if sorted(observed) != list(range(len(observed))) or len(observed) % 2:
                        self.fail("black_moon_scan_gap", "Full stock scan has missing rows")
                    return [observed[index] for index in sorted(observed)]
                frame, cards = after, new_cards
                continue
            if stationary:
                self.fail("black_moon_repeating_scroll", "A repeating viewport cannot establish the stock boundary")
            stationary = 0
            new_tiles = [(card, self.tile(after, card)) for card in new_cards if not card["partial"]]
            deltas = []
            for old, old_tile in old_tiles:
                for new, new_tile in new_tiles:
                    delta = old["rect"][1] - new["rect"][1]
                    if (abs(old["rect"][0] - new["rect"][0]) <= 4 and 4 < delta <= stride * 2 + 45
                            and self.same_tile(old_tile, new_tile)):
                        deltas.append(delta)
            if len(deltas) < 2 or max(deltas) - min(deltas) > 4:
                self.fail("black_moon_scroll_overlap_missing", "Scroll displacement cannot be uniquely confirmed")
            offset += round(float(np.median(deltas)))
            frame, cards = after, new_cards
        self.fail("black_moon_scan_limit", "Stock scan exceeded its scroll guard")

    async def receipt(self, record, batch_id):
        # Once submitted, retain a recognized receipt even if cancellation arrives.
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            frame = self.capture(cancellation=False)
            if self.reader.match(frame, "reward")["found"]:
                self.page = "reward"
                confirm_batch(self.persistent_data, **self.identity, visit_id=record["visit_id"],
                              batch_id=batch_id, evidence={"confirmed": True, "recognized": True,
                                                          "source": "reward_overlay"})
                return True
            await asyncio.sleep(.15)
        return False

    async def close_reward(self):
        frame = self.capture()
        if self.reader.match(frame, "reward")["found"]:
            # Blank client space outside the reward strip and all purchase controls.
            self.click((480, 650))
        await self.wait(lambda frame: True if self.reader.mode(frame) == "batch" else None)
        self.page = "local_shop"

    async def return_rest(self):
        await self.wait(lambda frame: True if self.reader.is_local(frame) else None)
        await self.button("page_back")
        await self.wait(lambda frame: True if self.reader.match(frame, "rest_menu")["found"] else None)
        self.page = "rest_menu"


async def execute_black_moon_local_purchase_from_rest_menu(
    *, city_name: str, selected_item_ids=None, record_scope="default",
    app=None, resonance_pc_city_shop_data=None, persistent_data=None,
) -> dict:
    if any(service is None for service in (app, resonance_pc_city_shop_data, persistent_data)):
        raise RuntimeError("app, city shop data, and persistent data services are required")
    selected = normalize_selected_items(selected_item_ids)
    scope = normalize_record_scope(record_scope)
    catalog = load_black_moon_catalog()
    city = resonance_pc_city_shop_data.resolve_city(city_name)
    week = resolve_purchase_week(catalog["refresh_policy"])
    session = _PurchaseSession(app=app, reader=BlackMoonLocalShopVision(), persistent_data=persistent_data,
                               catalog=catalog, city=city, scope=scope, week=week)
    def result(status, reason, completed=False, items=None):
        purchases = items or []
        return {"success": True, "status": status, "reason": reason, "city_key": city["city_key"],
                "week_key": week, "completed_this_week": completed, "purchased_count": len(purchases),
                "purchased_items": purchases, "page_state": "rest_menu"}
    if not selected or city["city_key"] not in catalog["eligible_cities"]:
        await session.wait(lambda frame: True if session.reader.match(frame, "rest_menu")["found"] else None)
        return result("skipped", "empty_selection" if not selected else "city_not_eligible")
    async with purchase_session_lock(persistent_data):
        _cancel_check()
        session.current_week()
        existing = read_city_record(persistent_data, **session.identity)
        if existing and existing["completed"]:
            await session.wait(lambda frame: True if session.reader.match(frame, "rest_menu")["found"] else None)
            session.current_week()
            return result("skipped", "already_completed_this_week", True)
        record = begin_visit(persistent_data, **session.identity, selected_item_ids=selected)
        desired = set(normalize_selected_items(record["selected_item_ids"]))
        session.report("started", selected_item_ids=list(record["selected_item_ids"]))
        try:
            await session.open_local()
            prior_batches = [batch for batch in _ledger_batches(record)
                             if batch["status"] in {"pending", "confirmed"}]
            unresolved = [batch for batch in prior_batches if batch["status"] == "pending"]
            rows = await session.scan(desired=desired, select=not prior_batches)
            # A recorded reward is durable, but it alone never permits another submission.
            for prior in prior_batches:
                before = prior["items"][0].get("stock_before")
                if not isinstance(before, dict) or not _reconciles(before, _stock(rows), prior["items"]):
                    session.fail("black_moon_pending_unresolved", "Previous batch has no confirmed sold-out result; confirmation will not be replayed")
            if unresolved:
                pending = unresolved[0]
                record = confirm_batch(persistent_data, **session.identity, batch_id=pending["batch_id"],
                                       evidence={"confirmed": True, "recognized": True,
                                                 "source": "sold_out_reconciliation", "stock_after": _stock(rows)})
            wanted = [row for row in rows if not row["sold_out"] and row["item_id"] in desired]
            if wanted:
                if prior_batches:
                    session.fail("black_moon_pending_stock_remaining", "Reconciled stock still has desired items")
                if any(not row["selected"] for row in wanted) or any(
                        row["selected"] and row["item_id"] not in desired for row in rows):
                    session.fail("black_moon_selection_incomplete", "Batch selection differs from the frozen policy")
                session.current_week()
                frame = session.capture()
                if not session.reader.is_local(frame) or session.reader.mode(frame) != "batch":
                    session.fail("black_moon_confirmation_page_changed", "LOCAL batch page is no longer confirmed")
                hit = session.reader.match(frame, "confirm_enabled")
                if not hit["found"]:
                    session.fail("black_moon_confirmation_missing", "Batch confirmation is not enabled")
                items = [{"item_id": row["item_id"], "stock_index": row["stock_index"]} for row in wanted]
                items[0]["stock_before"] = _stock(rows)
                batch_id = uuid.uuid4().hex
                record = mark_pending_batch(persistent_data, **session.identity, visit_id=record["visit_id"],
                                            batch_id=batch_id, items=items)
                try:
                    session.current_week()
                    _cancel_check()
                except BaseException:
                    resolve_pending_batch(persistent_data, **session.identity, visit_id=record["visit_id"],
                                          batch_id=batch_id, evidence={"recognized": True,
                                                                     "purchase_not_applied": True,
                                                                     "source": "confirmation_not_invoked"})
                    raise
                # No await/check between this boundary and app.click: failures in the call are ambiguous.
                app.click(x=int(hit["center"][0]), y=int(hit["center"][1]))
                session.page = "transition"
                receipt_task = asyncio.create_task(session.receipt(record, batch_id))
                try:
                    seen_reward = await asyncio.shield(receipt_task)
                except asyncio.CancelledError:
                    await receipt_task
                    raise
                _cancel_check()
                if seen_reward:
                    await session.close_reward()
                else:
                    await session.wait(lambda frame: True if session.reader.mode(frame) == "batch" else None)
                    session.page = "local_shop"
                after_rows = await session.scan(desired=desired, select=False)
                if not _reconciles(_stock(rows), _stock(after_rows), items):
                    session.fail("black_moon_purchase_not_verified", "Complete post-purchase stock does not confirm the selected batch")
                if not seen_reward:
                    confirm_batch(persistent_data, **session.identity, visit_id=record["visit_id"], batch_id=batch_id,
                                  evidence={"confirmed": True, "recognized": True,
                                            "source": "sold_out_reconciliation", "stock_after": _stock(after_rows)})
                rows = after_rows
            session.current_week()
            if any(not row["sold_out"] and row["item_id"] in desired for row in rows):
                session.fail("black_moon_purchase_remaining", "Desired available stock remains after purchase")
            record = read_city_record(persistent_data, **session.identity)
            purchased = [item for batch in _ledger_batches(record) if batch["status"] == "confirmed"
                         for item in batch["items"]]
            purchased_ids = {item["item_id"] for item in purchased}
            reason = "purchased" if purchased else "no_available_matching_stock"
            complete_visit(persistent_data, **session.identity, visit_id=record["visit_id"], reason=reason,
                           evidence={"scan_status": "COMPLETE", "recognized": True, "stock_after": _stock(rows),
                                     "outcomes": {item: "purchased" if item in purchased_ids else "missing"
                                                  for item in record["selected_item_ids"]}})
            # Completion is durable before return navigation, which may fail independently.
            await session.return_rest()
            clean_items = [{"item_id": item["item_id"], "stock_index": item["stock_index"]} for item in purchased]
            session.report("completed", purchased_count=len(clean_items))
            return result("completed", reason, True, clean_items)
        except BaseException as exc:
            session.report("cancelled" if isinstance(exc, asyncio.CancelledError) or getattr(exc, "code", "") == "trade_cancelled"
                           else "failed", reason=getattr(exc, "code", type(exc).__name__))
            if not isinstance(exc, Exception):
                raise
            receipt = read_city_record(persistent_data, **session.identity)
            confirmed = [item for batch in _ledger_batches(receipt) if batch["status"] == "confirmed"
                         for item in batch["items"]]
            failure = result("cancelled" if getattr(exc, "code", "") == "trade_cancelled" else "failed",
                             getattr(exc, "code", type(exc).__name__), receipt["completed"],
                             [{"item_id": item["item_id"], "stock_index": item["stock_index"]} for item in confirmed])
            failure.update(success=False, page_state=session.page,
                           error=exc.to_dict() if isinstance(exc, BlackMoonLocalPurchaseError)
                           else {"code": failure["reason"], "message": str(exc)})
            return failure


@action_info(name="resonance_pc.purchase_black_moon_local_from_rest_menu", public=True, read_only=False,
             description="Purchase selected LOCAL stock once per city/week from an open rest menu and return there.")
@requires_services(app="plans/aura_base/app", resonance_pc_city_shop_data="resonance_pc_city_shop_data",
                   persistent_data="core/persistent_data")
async def resonance_pc_purchase_black_moon_local_from_rest_menu(
    city_name: str, selected_item_ids: list[str] | None = None, record_scope: str = "default",
    app: Any = None, resonance_pc_city_shop_data: Any = None, persistent_data: Any = None,
) -> dict:
    return await execute_black_moon_local_purchase_from_rest_menu(
        city_name=city_name, selected_item_ids=selected_item_ids, record_scope=record_scope,
        app=app, resonance_pc_city_shop_data=resonance_pc_city_shop_data, persistent_data=persistent_data,
    )

"""Texture-backed rectangle matching and confirmed purchase selection."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import time
from typing import Any, Callable

import cv2
import numpy as np


MATCH_THRESHOLD = .92
ICON_COLUMN = (500, 140, 130, 545)
CROP = (8, 24, 80, 56)
ICON_SIZE = (96, 96)
_PLAN_ROOT = Path(__file__).resolve().parents[2]


class TradeBuySelectionError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


@dataclass(frozen=True)
class ProductTemplate:
    product_id: str
    name: str
    available: np.ndarray | None
    selected: np.ndarray | None
    reason: str = ""


@dataclass(frozen=True)
class ProductCatalog:
    products: dict[str, ProductTemplate]
    names: dict[str, str]


@lru_cache(maxsize=4)
def load_product_templates(catalog_path: str | None = None) -> ProductCatalog:
    path = Path(catalog_path) if catalog_path else _PLAN_ROOT / "data/meta/trade_product_templates.json"
    root = path.resolve().parents[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema_version"] != 1 or data["icon_size"] != list(ICON_SIZE)
                or data["crop"] != list(CROP) or data["match"] != {
                    "method": "TM_SQDIFF_NORMED", "threshold": MATCH_THRESHOLD,
                    "use_grayscale": False, "preprocess": "none"}):
            raise ValueError("unsupported product-template configuration")
        products, names = {}, {}
        for pid, row in data["products"].items():
            name = row["name"]
            if not isinstance(pid, str) or not isinstance(name, str) or not name or name in names:
                raise ValueError("invalid or duplicate product identity")
            images = []
            if row["supported"] is True:
                for state in ("available", "selected"):
                    image_path = (root / row[state]).resolve()
                    if not image_path.is_relative_to(root / "templates/trade_products"):
                        raise ValueError("template path is outside product assets")
                    # Decode only opaque RGB PNGs; there is no mask or alpha fallback.
                    image = cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
                    if image is None or image.shape != (CROP[3], CROP[2], 3) or image.dtype != np.uint8:
                        raise ValueError("product template must be an opaque 80x56 RGB image")
                    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    image.setflags(write=False)
                    images.append(image)
            elif row["supported"] is False and isinstance(row.get("reason"), str) and row["reason"]:
                images = [None, None]
            else:
                raise ValueError("invalid product-template support metadata")
            products[pid] = ProductTemplate(pid, name, *images, row.get("reason", ""))
            names[name] = pid
        if not products:
            raise ValueError("empty product-template catalog")
        expected = json.loads((root / "data/meta/products.json").read_text(encoding="utf-8"))
        if {pid: item.name for pid, item in products.items()} != expected:
            raise ValueError("template catalog does not cover the product directory")
        return ProductCatalog(products, names)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, cv2.error) as exc:
        raise TradeBuySelectionError("trade_product_templates_invalid", str(exc)) from exc


def _match_ok(hit: Any) -> bool:
    try:
        confidence = float(hit.confidence)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TradeBuySelectionError("buy_template_match_failed", "Invalid matching result") from exc
    if (getattr(hit, "debug_info", None) or {}).get("error") or not math.isfinite(confidence):
        raise TradeBuySelectionError("buy_template_match_failed", "Invalid matching score")
    return bool(getattr(hit, "found", False)) and confidence >= MATCH_THRESHOLD


def _match_args() -> dict:
    return {"threshold": MATCH_THRESHOLD, "use_grayscale": False,
            "match_method": cv2.TM_SQDIFF_NORMED, "preprocess": "none"}


def _same_frame(first: np.ndarray, second: np.ndarray) -> bool:
    return first.shape == second.shape and float(cv2.absdiff(first, second).mean()) <= 1.2


class _Selection:
    def __init__(self, app, vision, check_cancelled, trace_callback):
        self.app, self.vision = app, vision
        self.check_cancelled = check_cancelled
        self.trace_callback = trace_callback
        self.trace: list[dict] = []

    def event(self, phase, **data):
        entry = {"phase": phase, **data}
        self.trace.append(entry)
        if self.trace_callback:
            self.trace_callback(entry)

    def pause(self, seconds):
        self.check_cancelled()
        time.sleep(seconds)
        self.check_cancelled()

    def capture(self):
        self.check_cancelled()
        result = self.app.capture(rect=ICON_COLUMN)
        frame = getattr(result, "image", None)
        if (not getattr(result, "success", False) or not isinstance(frame, np.ndarray)
                or frame.shape != (ICON_COLUMN[3], ICON_COLUMN[2], 3) or frame.dtype != np.uint8):
            raise TradeBuySelectionError("buy_capture_failed", "Unable to capture the product-icon column")
        return frame

    def settled(self):
        self.pause(.15)
        first = self.capture()
        for _ in range(12):
            self.pause(.15)
            second = self.capture()
            if _same_frame(first, second):
                return second
            first = second
        raise TradeBuySelectionError("buy_view_not_stable", "Product list did not settle")

    def candidates(self, frame, items):
        self.check_cancelled()
        hits = self.vision.find_templates_batch(source_image=frame,
            template_images=[item.available for item in items], **_match_args())
        if len(hits) != len(items):
            raise TradeBuySelectionError("buy_template_match_failed", "Incomplete matching results")
        found = []
        for item, hit in zip(items, hits):
            if not _match_ok(hit):
                continue
            rect = getattr(hit, "rect", None)
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise TradeBuySelectionError("buy_template_match_failed", "Missing match coordinates")
            x, y, width, height = (int(value) for value in rect)
            if (width, height) != (CROP[2], CROP[3]):
                raise TradeBuySelectionError("buy_template_match_failed", "Unexpected template dimensions")
            # A rectangle match near the viewport edge is not a fully visible icon.
            left, top = x - CROP[0], y - CROP[1]
            if left < 0 or top < 0 or left + ICON_SIZE[0] > frame.shape[1] or top + ICON_SIZE[1] > frame.shape[0]:
                self.event("partial_icon", product_id=item.product_id, score=hit.confidence)
                continue
            found.append((item, (x, y, width, height), float(hit.confidence)))
        winners = []
        for item, rect, score in found:
            competitors = [(other, confidence) for other, box, confidence in found
                           if other.product_id != item.product_id
                           and abs(rect[0]-box[0]) < CROP[2]//2
                           and abs(rect[1]-box[1]) < CROP[3]//2]
            if any(confidence >= score-1e-6 for _, confidence in competitors):
                self.event("competing_match_rejected", product_id=item.product_id, score=score)
                continue
            winners.append((item, rect, score))
        return sorted(winners, key=lambda row: row[1][1])

    def in_row(self, frame, template, rect):
        x, y, width, height = rect
        left, top = max(0, x-2), max(0, y-2)
        right, bottom = min(frame.shape[1], x+width+2), min(frame.shape[0], y+height+2)
        return self.vision.find_template(source_image=frame[top:bottom, left:right],
                                         template_image=template, **_match_args())

    def confirm(self, item, rect):
        deadline = time.monotonic() + 1.6
        frame = None
        for _ in range(8):
            self.pause(.2)
            frame = self.capture()
            hit = self.in_row(frame, item.selected, rect)
            matched = _match_ok(hit)
            self.event("selection_probe", product_id=item.product_id, score=float(hit.confidence))
            if matched:
                return True, frame
            if time.monotonic() >= deadline:
                break
        return False, frame

    def select(self, item, rect):
        for attempt in (1, 2):
            self.check_cancelled()
            x = ICON_COLUMN[0] + rect[0] + rect[2]//2
            y = ICON_COLUMN[1] + rect[1] + rect[3]//2
            self.app.click(x=x, y=y)
            self.event("product_clicked", product_id=item.product_id, attempt=attempt, x=x, y=y)
            confirmed, frame = self.confirm(item, rect)
            if confirmed:
                self.event("selection_confirmed", product_id=item.product_id, attempt=attempt)
                return True, frame
            if attempt == 2 or not _match_ok(self.in_row(frame, item.available, rect)):
                break
            self.event("retry_click", product_id=item.product_id)
        self.event("selection_unconfirmed", product_id=item.product_id)
        return False, frame

    def scroll(self):
        self.check_cancelled()
        self.app.move_to(x=670, y=450, duration=.1)
        self.check_cancelled()
        self.app.drag(start_x=670, start_y=450, end_x=670, end_y=200,
                      duration=.5, hold_before_release_sec=.5)
        self.event("scroll_down")
        return self.settled()


def select_buy_products(*, product_list, app, vision, max_scan_rounds=6,
                        check_cancelled: Callable[[], None], trace_callback=None,
                        catalog: ProductCatalog | None = None) -> dict:
    """Select requested identities from the current position, never rewind or OCR."""
    catalog = catalog if catalog is not None else load_product_templates()
    requested = list(dict.fromkeys(str(name).strip() for name in product_list if str(name).strip()))
    session = _Selection(app, vision, check_cancelled, trace_callback)
    supported, misses = {}, {}
    for name in requested:
        pid = catalog.names.get(name)
        item = catalog.products.get(pid)
        if item is None or item.available is None:
            misses[name] = item.reason if item else "unknown_product"
        else:
            supported[pid] = item
    pending = dict(supported)
    # Compare identities even outside the request, so a similar unwanted item
    # cannot win just because its stronger, correct template was not requested.
    all_templates = [item for item in catalog.products.values() if item.available is not None]
    selected = []
    unchanged = 0
    stop_reason = "no_supported_products" if not pending else "scan_limit"
    frame = session.settled() if pending else None
    for page in range(max(1, int(max_scan_rounds))):
        if not pending:
            if supported:
                stop_reason = "all_products_processed"
            break
        session.event("scan_started", page=page+1, pending_product_ids=list(pending))
        while pending:
            candidates = [row for row in session.candidates(frame, all_templates)
                          if row[0].product_id in pending]
            if not candidates:
                break
            item, rect, score = candidates[0]
            session.event("product_match", product_id=item.product_id, score=score, rect=list(rect))
            confirmed, frame = session.select(item, rect)
            pending.pop(item.product_id)
            if confirmed:
                selected.append(item)
            else:
                misses[item.name] = "selection_not_confirmed"
            # Refresh all candidate coordinates after a click, not just the old OCR list.
            if pending:
                frame = session.settled()
        if not pending:
            stop_reason = "all_products_processed"
            break
        if page+1 >= max(1, int(max_scan_rounds)):
            break
        previous = frame
        frame = session.scroll()
        unchanged = unchanged+1 if _same_frame(previous, frame) else 0
        if unchanged >= 2:
            stop_reason = "list_end"
            break
    for item in pending.values():
        misses[item.name] = "not_found_or_not_purchasable"
    missing = [name for name in requested if name in misses]
    warnings = [{"code": "trade_product_not_selected", "product": name,
                 "product_id": catalog.names.get(name), "reason": misses[name]} for name in missing]
    return {"selected_products": [item.name for item in selected],
            "selected_product_ids": [item.product_id for item in selected],
            "missing_products": missing, "warnings": warnings,
            "scan_trace": session.trace, "stop_reason": stop_reason}

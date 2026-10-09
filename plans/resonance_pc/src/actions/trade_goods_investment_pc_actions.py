"""Ordered template-only goods investment from the current exchange menu."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._trade_goods_investment_vision import InvestmentRecognitionError, InvestmentVision


MODE_TARGETS = {"unlock": 10, "balanced": 14, "full": 20}
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_POLL_SECONDS = 0.2
_STATE_TIMEOUT = 5.0
_COMMIT_TIMEOUT = 8.0
_MAX_SCROLLS = 40
_MAX_TRANSACTIONS = 500


class TradeGoodsInvestmentError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.detail = dict(detail or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


def normalize_investment_mode(mode: str) -> str:
    if not isinstance(mode, str) or mode not in MODE_TARGETS:
        raise ValueError("trade_goods_investment_mode must be unlock, balanced, or full")
    return mode


def _cancel_check() -> None:
    if is_current_task_cancel_requested():
        raise TradeGoodsInvestmentError("trade_cancelled", "Goods investment was cancelled")


def _pause(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _cancel_check()
        time.sleep(min(.1, max(0., deadline - time.monotonic())))


def _capture(app: Any) -> np.ndarray:
    _cancel_check()
    result = app.capture()
    if not result.success or result.image is None:
        raise TradeGoodsInvestmentError("investment_capture_failed", "Could not capture the investment page")
    frame = np.asarray(result.image)
    if frame.shape != (720, 1280, 3):
        raise TradeGoodsInvestmentError("investment_resolution_mismatch", "Investment requires a 1280x720 RGB client", {"shape": list(frame.shape)})
    return frame


def _wait(app: Any, predicate: Callable, *, timeout: float = _STATE_TIMEOUT, stable: int = 2):
    deadline, consecutive, previous, last = time.monotonic() + timeout, 0, None, None
    while time.monotonic() < deadline:
        frame = _capture(app)
        try:
            value = predicate(frame)
        except InvestmentRecognitionError:
            value = None
        if value is not None and value is not False:
            key = _observation_key(value)
            consecutive = consecutive + 1 if key == previous else 1
            previous, last = key, value
            if consecutive >= stable:
                return frame, value
        else:
            consecutive, previous = 0, None
        _pause(_POLL_SECONDS)
    raise TradeGoodsInvestmentError("investment_state_timeout", "Investment page state did not stabilize", {"last": last})


def _observation_key(value):
    if isinstance(value, list):
        return tuple(_observation_key(item) for item in value)
    if isinstance(value, dict):
        if "current" in value:
            return value["current"], value.get("preview"), value.get("max_level")
        rect = value.get("rect")
        position = tuple(round(int(point) / 4) for point in rect[:2]) if rect is not None else None
        return position, tuple(value.get(key) for key in ("found", "partial", "occluded", "selected", "locked", "maximum"))
    return value


def _click(app: Any, center) -> None:
    _cancel_check()
    if center is None or len(center) != 2:
        raise TradeGoodsInvestmentError("investment_click_unconfirmed", "No confirmed template click point")
    app.click(x=int(center[0]), y=int(center[1]))


def _idle(app: Any, reader: InvestmentVision):
    def idle(frame):
        if reader.match(frame, "success")["found"]:
            return None
        return True if reader.match(frame, "page_anchor")["found"] else None
    return _wait(app, idle)


def _levels(app: Any, reader: InvestmentVision, current: Optional[int] = None):
    def read(frame):
        if reader.match(frame, "success")["found"]:
            return None
        value = reader.read_levels(frame)
        if current is not None and value["current"] != current:
            raise TradeGoodsInvestmentError("investment_actual_level_changed", "Actual level changed before confirmation", value)
        return value
    return _wait(app, read)


def _cards(app: Any, reader: InvestmentVision):
    _idle(app, reader)
    def read(frame):
        if reader.match(frame, "success")["found"]:
            return None
        cards = reader.detect_cards(frame)
        if not cards or any(card.get("occluded") for card in cards):
            return None
        return cards
    return _wait(app, read)


def _same_position(first: dict, second: dict) -> bool:
    return abs(first["rect"][0] - second["rect"][0]) <= 5 and abs(first["rect"][1] - second["rect"][1]) <= 5


def _select(app: Any, reader: InvestmentVision, card: dict):
    x, y, width, height = card["rect"]
    _click(app, (x + width // 2, y + height // 2))
    _pause(.4)
    def selected(frame):
        candidates = [row for row in reader.detect_cards(frame) if _same_position(row, card)]
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        # The native maximum overlay can obscure the selection marker.
        if candidate.get("maximum"):
            return candidate if reader.read_levels(frame).get("max_level") else None
        return candidate if candidate.get("selected") else None
    frame, selected_card = _wait(app, selected)
    frame, levels = _levels(app, reader)
    return frame, selected_card, levels


def _adjust(app: Any, reader: InvestmentVision, name: str, current: int, previous: int):
    frame = _capture(app)
    if reader.read_levels(frame)["current"] != current:
        raise TradeGoodsInvestmentError("investment_actual_level_changed", "Selected product changed during level adjustment")
    button = reader.match(frame, name)
    if not button["found"]:
        raise TradeGoodsInvestmentError("investment_adjust_button_missing", "Required level adjustment button was not matched", {"button": name})
    _click(app, button["center"])
    _pause(.35)
    frame, levels = _levels(app, reader, current)
    if levels["preview"] != previous:
        return frame, levels
    # Recheck before one retry so a delayed first update does not get a second click.
    _pause(.35)
    frame, levels = _levels(app, reader, current)
    if levels["preview"] != previous:
        return frame, levels
    button = reader.match(frame, name)
    if not button["found"]:
        return frame, levels
    _click(app, button["center"])
    _pause(.35)
    return _levels(app, reader, current)


def _prepare_upgrade(app: Any, reader: InvestmentVision, levels: dict, target: int, update: Callable):
    current, preview = int(levels["current"]), levels.get("preview")
    if preview is None:
        raise TradeGoodsInvestmentError("investment_preview_missing", "No upgrade preview for an unfinished product", levels)
    frame = _capture(app)
    for _ in range(45):
        update(current_level=current, preview_level=preview)
        if preview > target:
            frame, next_levels = _adjust(app, reader, "minus", current, preview)
            next_preview = next_levels.get("preview")
            if next_preview is None or next_preview >= preview:
                raise TradeGoodsInvestmentError("investment_preview_cannot_reduce", "Could not reduce an oversized upgrade preview", next_levels)
            preview = next_preview
            continue
        if preview == target:
            break
        frame, next_levels = _adjust(app, reader, "plus", current, preview)
        next_preview = next_levels.get("preview")
        if next_preview is None or next_preview < preview:
            raise TradeGoodsInvestmentError("investment_preview_invalid", "Unexpected preview after plus", next_levels)
        if next_preview == preview:
            break
        preview = next_preview
    else:
        raise TradeGoodsInvestmentError("investment_adjustment_limit", "Level adjustment made no bounded progress")
    # No money/allowance is read. The UI supplies the highest selectable preview.
    frame, fresh = _levels(app, reader, current)
    if fresh.get("preview") != preview or preview > target:
        raise TradeGoodsInvestmentError("investment_preview_changed", "Upgrade preview changed before submission", fresh)
    enabled = reader.match(frame, "confirm_enabled")
    if preview <= current or not enabled["found"]:
        return None
    return {"current_level": current, "upgrade_level": int(preview), "center": enabled["center"]}


def _commit(app: Any, reader: InvestmentVision, operation: dict):
    frame, levels = _levels(app, reader, operation["current_level"])
    if levels.get("preview") != operation["upgrade_level"]:
        raise TradeGoodsInvestmentError("investment_commit_preview_changed", "The pending upgrade no longer matches the requested submission", levels)
    button = reader.match(frame, "confirm_enabled")
    if not button["found"]:
        return None
    _click(app, button["center"])
    deadline, seen_success, stable = time.monotonic() + _COMMIT_TIMEOUT, False, 0
    last = None
    while time.monotonic() < deadline:
        frame = _capture(app)
        if reader.match(frame, "success")["found"]:
            seen_success, stable = True, 0
        else:
            try:
                last = reader.read_levels(frame)
            except InvestmentRecognitionError:
                last, stable = None, 0
            if last is not None and last["current"] == operation["upgrade_level"]:
                stable += 1
                if stable >= 2:
                    return {"confirmed": True, "success_toast_seen": seen_success,
                            "current_level": last["current"], "confirmation": "level_updated"}
            else:
                stable = 0
        _pause(_POLL_SECONDS)
    raise TradeGoodsInvestmentError("investment_commit_unconfirmed", "No confirmed actual level after the single submit click", {"operation": operation, "last_levels": last, "success_toast_seen": seen_success})


def _scroll(app: Any, reader: InvestmentVision, before: np.ndarray, previous_cards: list[dict]):
    viewport = reader.metadata["cards"]["viewport"]
    x, y, width, height = viewport
    complete = [card for card in previous_cards if not card["partial"]]
    old = [(card, None if card.get("maximum") else reader.fingerprint(before, card)) for card in complete]
    row_positions = sorted(set(round(card["rect"][1] / 3) * 3 for card in complete))
    spacing = int(np.median(np.diff(row_positions))) if len(row_positions) > 1 else reader.metadata["cards"]["size"][1] + 3
    span = min(spacing * 2, height - 80)
    for _ in range(2):
        _cancel_check()
        app.drag(start_x=x + width // 2, start_y=y + height - 35,
                 end_x=x + width // 2, end_y=y + height - 35 - span,
                 duration=.6, hold_before_release_sec=.15, stop_inertia=True)
        _pause(.4)
        after, cards = _cards(app, reader)
        difference = float(np.abs(before[y:y + height, x:x + width].astype(float) - after[y:y + height, x:x + width].astype(float)).mean())
        if difference <= 1.5:
            continue
        new = [card for card in cards if not card["partial"]]
        new_images = [None if card.get("maximum") else reader.fingerprint(after, card) for card in new]
        def overlaps(first, second):
            if abs(first[0]["rect"][0] - second[0]["rect"][0]) > 5:
                return False
            # Covered maximum cards are equivalent only for completed-card bookkeeping.
            if first[0].get("maximum") or second[0].get("maximum"):
                return bool(first[0].get("maximum") and second[0].get("maximum"))
            return reader.same_fingerprint(first[1], second[1])
        for count in range(min(len(old), len(new)), 0, -1):
            pairs = zip(old[-count:], zip(new[:count], new_images[:count]))
            if all(overlaps(first, second) for first, second in pairs):
                if count < 2 and len(old) > 1 and len(new) > 1:
                    continue
                return after, cards, [dict(card) for card in new[:count]]
        raise TradeGoodsInvestmentError("investment_scroll_overlap_unconfirmed", "Could not verify the ordered overlap after scrolling")
    if any(card["partial"] for card in previous_cards):
        raise TradeGoodsInvestmentError("investment_scroll_stalled", "A partial product remains but the list would not scroll")
    return None


def _return_shop(app: Any, vision: Any, reader: InvestmentVision) -> None:
    image = _capture(app)
    match = vision.find_template(source_image=image[:80, :170],
                                 template_image=str(_PLAN_ROOT / "templates/nav_back_button.png"),
                                 threshold=.86, use_grayscale=True)
    if not match.found or match.center_point is None:
        raise TradeGoodsInvestmentError("investment_back_button_missing", "Could not confirm the back button")
    _click(app, match.center_point)
    _pause(.4)
    _wait(app, lambda frame: True if reader.match(frame, "entry")["found"] else None)


def execute_trade_goods_investment_from_shop(*, mode: str, city_name: str, app: Any, vision: Any,
                                           progress: Optional[Callable[[str, dict], None]] = None) -> Dict[str, Any]:
    mode = normalize_investment_mode(mode)
    if app is None or vision is None:
        raise RuntimeError("app and vision services are required")
    reader, target = InvestmentVision(), MODE_TARGETS[mode]
    started_at, transactions, product_index, scroll_count = time.monotonic(), [], 0, 0
    done, reason = [], "all_products_at_target"
    def report(state, **data):
        fields = {"city_name": city_name, "mode": mode, "target_level": target, "product_index": product_index, **data}
        logger.info("[TradeGoodsInvestment] state=%s detail=%s", state, fields)
        if progress is not None:
            progress(state, fields)
    report("started")
    try:
        def entry_match(frame):
            hit = reader.match(frame, "entry")
            return hit if hit["found"] else None
        frame, entry = _wait(app, entry_match)
        _click(app, entry["center"])
        _pause(.4)
        _idle(app, reader)
        while True:
            frame, cards = _cards(app, reader)
            viewport_top = reader.VIEWPORT[1]
            if scroll_count == 0 and any(card["partial"] and card["rect"][1] < viewport_top for card in cards):
                raise TradeGoodsInvestmentError(
                    "investment_initial_order_unconfirmed",
                    "The preserved list starts with a partial product whose completion is unconfirmed",
                )
            pending = [card for card in cards if not card["partial"] and not any(_same_position(card, old) for old in done)]
            if not pending:
                if scroll_count >= _MAX_SCROLLS:
                    raise TradeGoodsInvestmentError("investment_scroll_limit", "Investment list exceeded its scroll guard")
                scrolled = _scroll(app, reader, frame, cards)
                if scrolled is None:
                    break
                frame, cards, done = scrolled
                scroll_count += 1
                continue
            card = pending[0]
            if card.get("locked") is None:
                raise TradeGoodsInvestmentError("investment_lock_state_unknown", "Product lock state is not confirmed", card)
            if card["locked"]:
                _pause(.5)
                _, fresh_cards = _cards(app, reader)
                candidates = [row for row in fresh_cards if _same_position(row, card)]
                if not candidates or candidates[0].get("locked") is not False:
                    reason = "next_product_locked"
                    break
                card = candidates[0]
            product_index += 1
            while True:
                frame, selected, levels = _select(app, reader, card)
                report("running", current_level=levels["current"], preview_level=levels.get("preview"))
                if levels["current"] >= target:
                    done.append(dict(selected))
                    break
                operation = _prepare_upgrade(app, reader, levels, target, lambda **data: report("running", **data))
                if operation is None:
                    reason = "no_further_upgrade"
                    break
                if len(transactions) >= _MAX_TRANSACTIONS:
                    raise TradeGoodsInvestmentError("investment_transaction_limit", "Investment exceeded its progress guard")
                commit = _commit(app, reader, operation)
                if commit is None:
                    reason = "no_further_upgrade"
                    break
                transactions.append({"product_index": product_index, "from_level": operation["current_level"],
                                     "to_level": operation["upgrade_level"], **commit})
                report("running", current_level=commit["current_level"], transaction_count=len(transactions))
                card = selected
            if reason == "no_further_upgrade":
                break
        _return_shop(app, vision, reader)
    except Exception as exc:
        report("failed", reason=getattr(exc, "code", type(exc).__name__), message=str(exc), transaction_count=len(transactions))
        raise
    result = {"success": True, "triggered": True, "status": "invested" if transactions else "skipped",
              "reason": reason, "city_name": city_name, "mode": mode, "target_level": target,
              "transaction_count": len(transactions), "transactions": transactions,
              "upgraded_levels": sum(row["to_level"] - row["from_level"] for row in transactions),
              "product_count": product_index, "scroll_count": scroll_count,
              "page_state": "shop_page", "elapsed_ms": round((time.monotonic() - started_at) * 1000)}
    report("completed" if transactions else "skipped", **{key: value for key, value in result.items() if key not in {"city_name", "mode", "target_level"}})
    return result


@action_info(name="resonance_pc.invest_trade_goods_from_shop", public=True, read_only=False,
             description="Invest goods in original unlock order to level 10, 14, or 20 and return to the exchange.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision")
def resonance_pc_invest_trade_goods_from_shop(mode: str = "unlock", city_name: str = "",
                                            app: Any = None, vision: Any = None) -> Dict[str, Any]:
    return execute_trade_goods_investment_from_shop(mode=mode, city_name=city_name, app=app, vision=vision)

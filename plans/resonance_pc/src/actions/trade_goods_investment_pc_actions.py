"""Ordered template-only goods investment from the current exchange menu."""

from __future__ import annotations

from contextvars import ContextVar
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested

from ._trade_goods_investment_vision import InvestmentRecognitionError, InvestmentVision
from ._trade_goods_investment_records import InvestmentLevelRecords


_INVESTMENT_PROGRESS_CALLBACK: ContextVar[Optional[Callable]] = ContextVar(
    "trade_goods_investment_progress_callback", default=None,
)
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_POLL_SECONDS = 0.2
_ADJUST_CLICK_INTERVAL = 0.08
_UNCHANGED_ADJUST_REREAD_DELAY = 0.9
_STATE_TIMEOUT = 5.0
_COMMIT_TIMEOUT = 8.0
_MAX_SCROLLS = 40
_MAX_TRANSACTIONS = 500
_RETURN_TIMEOUT = 20.0
_RETURN_RETRY_INTERVAL = 3.0
_RETURN_POLL_SECONDS = 0.25
_RETURN_MAX_CLICKS = 3


class TradeGoodsInvestmentError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.detail = dict(detail or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


def normalize_investment_mode(mode: int) -> int:
    if type(mode) is not int or not 1 <= mode <= 20:
        raise ValueError("trade_goods_investment_mode must be an integer from 1 to 20")
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
    if isinstance(value, (list, tuple)):
        return tuple(_observation_key(item) for item in value)
    if isinstance(value, dict):
        if "current" in value:
            return value["current"], value.get("preview"), value.get("max_level")
        rect = value.get("rect")
        position = tuple(round(int(point) / 4) for point in rect[:2]) if rect is not None else None
        return position, tuple(value.get(key) for key in ("found", "partial", "occluded", "selected", "locked", "maximum", "availability"))
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
    started = time.monotonic()
    def read(frame):
        if reader.match(frame, "success")["found"]:
            return None
        if not reader.match(frame, "page_anchor")["found"]:
            return None
        cards = reader.locate_cards(frame, success_visible=False)
        if not cards or any(card.get("occluded") for card in cards):
            return None
        return cards
    result = _wait(app, read)
    logger.info("[TradeGoodsInvestment] phase=list_located cards=%s elapsed_ms=%s",
                len(result[1]), round((time.monotonic() - started) * 1000))
    return result


def _current_card(app: Any, reader: InvestmentVision, card: dict):
    def read(frame):
        if reader.match(frame, "success")["found"] or not reader.match(frame, "page_anchor")["found"]:
            return None
        return reader.read_card_state(frame, card)
    return _wait(app, read)


def _initial_card_indices(app: Any, reader: InvestmentVision, frame, cards):
    viewport_top = reader.VIEWPORT[1]
    if not cards or any(card["partial"] and card["rect"][1] < viewport_top for card in cards):
        raise TradeGoodsInvestmentError("investment_initial_order_unconfirmed",
                                       "The investment list origin could not be confirmed")
    if getattr(reader, "city_id", ""):
        validate_grid = getattr(reader, "validate_card_grid", None)
        if validate_grid is not None:
            try:
                validate_grid(cards)
            except InvestmentRecognitionError as exc:
                raise TradeGoodsInvestmentError("investment_initial_order_unconfirmed", exc.detail) from exc
        first = reader.read_card_selection(frame, cards[0])
        if first.get("maximum") and not first.get("selected"):
            frame, cards = _reset_list_top(app, reader, frame, cards)
        if validate_grid is not None:
            try:
                validate_grid(cards, first_index=1)
            except InvestmentRecognitionError as exc:
                raise TradeGoodsInvestmentError("investment_initial_order_unconfirmed", exc.detail) from exc
        # The native investment controller selects absolute item 1 on entry.
        # A preserved scrolled viewport must not be silently numbered from 1.
        def origin(image):
            if reader.match(image, "success")["found"] or not reader.match(image, "page_anchor")["found"]:
                return None
            return True if reader.initial_order_confirmed(image, cards) else None
        try:
            frame, _ = _wait(app, origin)
        except TradeGoodsInvestmentError as exc:
            if exc.code != "investment_state_timeout":
                raise
            raise TradeGoodsInvestmentError("investment_initial_order_unconfirmed",
                                           "The first investment item is not confirmed at the list origin") from exc
    return frame, [{**card, "investment_index": index} for index, card in enumerate(cards, 1)]


def _reset_list_top(app: Any, reader: InvestmentVision, frame, cards):
    """Establish the upper boundary before accepting a maximum-covered origin."""
    x, y, width, height = reader.VIEWPORT
    stationary = 0
    for attempt in range(_MAX_SCROLLS):
        _cancel_check()
        # Require two different drag spans to avoid one ineffective gesture
        # being mistaken for a scroll boundary.
        span = min((200, 320)[attempt % 2], height - 70)
        _wait(app, lambda image: (not reader.match(image, "success")["found"]
                                  and reader.match(image, "page_anchor")["found"]))
        _cancel_check()
        app.drag(start_x=x + width // 2, start_y=y + 35,
                 end_x=x + width // 2, end_y=y + 35 + span,
                 duration=.6, hold_before_release_sec=.15, stop_inertia=True)
        _pause(.4)
        after, following = _cards(app, reader)
        difference = float(np.abs(frame[y:y + height, x:x + width].astype(float)
                                  - after[y:y + height, x:x + width].astype(float)).mean())
        stationary = stationary + 1 if difference <= 1.5 else 0
        frame, cards = after, following
        if stationary >= 2:
            if (cards and not cards[0].get("partial")
                    and y <= cards[0]["rect"][1] <= y + 24):
                reader.validate_card_grid(cards, first_index=1)
                return frame, cards
            break
    raise TradeGoodsInvestmentError("investment_initial_order_unconfirmed",
                                   "Could not establish the top of the investment list")


def _same_position(first: dict, second: dict) -> bool:
    return abs(first["rect"][0] - second["rect"][0]) <= 5 and abs(first["rect"][1] - second["rect"][1]) <= 5


def _select(app: Any, reader: InvestmentVision, card: dict):
    x, y, width, height = card["rect"]
    if not card.get("selected"):
        _click(app, (x + width // 2, y + height // 2))
        _pause(.4)
    def selected(frame):
        if reader.match(frame, "success")["found"]:
            return None
        candidate = reader.read_card_selection(frame, card)
        if not candidate.get("selected") and not candidate.get("maximum"):
            return None
        levels = reader.read_levels(frame)
        # The native maximum overlay can obscure the selection marker.
        if candidate.get("maximum") and not levels.get("max_level"):
            return None
        return candidate, levels
    frame, (selected_card, levels) = _wait(app, selected)
    return frame, selected_card, levels


def _adjust(app: Any, reader: InvestmentVision, name: str, current: int, previous: int,
            *, clicks: int = 1):
    if type(clicks) is not int or not 1 <= clicks <= 20:
        raise TradeGoodsInvestmentError("investment_adjustment_count_invalid", "Adjustment batch requires 1-20 clicks")
    frame = _capture(app)
    fresh = reader.read_levels(frame)
    if fresh["current"] != current:
        raise TradeGoodsInvestmentError("investment_actual_level_changed", "Selected product changed during level adjustment")
    if fresh.get("preview") != previous:
        # Late feedback from an earlier batch changes the remaining click count.
        frame, levels = _levels(app, reader, current)
        return frame, {**levels, "adjustment_skipped": True}
    button = reader.match(frame, name)
    if not button["found"]:
        raise TradeGoodsInvestmentError("investment_adjust_button_missing", "Required level adjustment button was not matched", {"button": name})
    for index in range(clicks):
        _click(app, button["center"])
        if index + 1 < clicks:
            _pause(_ADJUST_CLICK_INTERVAL)
    clicked_at = time.monotonic()
    frame, levels = _levels(app, reader, current)
    if levels["preview"] != previous:
        return frame, levels
    # Recheck delayed feedback before declaring the plus limit or retrying minus.
    # Keep the old no-progress window: the delayed read began at ~0.9 seconds
    # and required two samples. Only successful feedback takes the fast path.
    _pause(max(.35, _UNCHANGED_ADJUST_REREAD_DELAY - (time.monotonic() - clicked_at)))
    frame, levels = _levels(app, reader, current)
    if levels["preview"] != previous:
        return frame, levels
    if name == "plus":
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
    frame, fresh = None, None
    stop_after_commit = False
    for _ in range(45):
        update(current_level=current, preview_level=preview)
        if preview > target:
            frame, next_levels = _adjust(app, reader, "minus", current, preview,
                                         clicks=preview - target)
            fresh = next_levels
            next_preview = next_levels.get("preview")
            if next_levels.get("adjustment_skipped") and next_preview is not None:
                preview = next_preview
                continue
            if next_preview is None or next_preview >= preview:
                raise TradeGoodsInvestmentError("investment_preview_cannot_reduce", "Could not reduce an oversized upgrade preview", next_levels)
            preview = next_preview
            continue
        if preview == target:
            break
        frame, next_levels = _adjust(app, reader, "plus", current, preview,
                                     clicks=target - preview)
        fresh = next_levels
        next_preview = next_levels.get("preview")
        if next_levels.get("adjustment_skipped") and next_preview is not None:
            preview = next_preview
            continue
        if next_preview is None or next_preview < preview:
            raise TradeGoodsInvestmentError("investment_preview_invalid", "Unexpected preview after plus", next_levels)
        if next_preview == preview:
            stop_after_commit = True
            break
        preview = next_preview
    else:
        raise TradeGoodsInvestmentError("investment_adjustment_limit", "Level adjustment made no bounded progress")
    # No money/allowance is read. The UI supplies the highest selectable preview.
    # A batch already supplied stable feedback; use that same frame to confirm
    # the button. With no adjustment, obtain the single pre-submit observation.
    if frame is None:
        frame, fresh = _levels(app, reader, current)
    if fresh.get("preview") != preview or preview > target:
        raise TradeGoodsInvestmentError("investment_preview_changed", "Upgrade preview changed before submission", fresh)
    enabled = reader.match(frame, "confirm_enabled")
    if preview <= current or not enabled["found"]:
        return None
    return {"current_level": current, "upgrade_level": int(preview), "center": enabled["center"],
            "stop_after_commit": stop_after_commit}


def _commit(app: Any, reader: InvestmentVision, operation: dict):
    # Preparation already checked stable levels and the enabled button together.
    # No input occurs between preparation and this single submission.
    _click(app, operation.get("center"))
    deadline, seen_success, stable = time.monotonic() + _COMMIT_TIMEOUT, False, 0
    unchanged = 0
    last = None
    while time.monotonic() < deadline:
        frame = _capture(app)
        if reader.match(frame, "success")["found"]:
            seen_success, stable, unchanged = True, 0, 0
        else:
            try:
                last = reader.read_levels(frame)
            except InvestmentRecognitionError:
                last, stable = None, 0
            if last is not None and last["current"] == operation["upgrade_level"]:
                unchanged = 0
                stable += 1
                if stable >= 2:
                    return {"confirmed": True, "success_toast_seen": seen_success,
                            "current_level": last["current"], "confirmation": "level_updated"}
            else:
                stable = 0
                unchanged = (unchanged + 1 if last is not None
                             and last["current"] == operation["current_level"]
                             and last.get("preview") == operation["upgrade_level"] else 0)
        _pause(_POLL_SECONDS)
    if unchanged >= 2 and not seen_success:
        logger.info("[TradeGoodsInvestment] state=submit_no_effect current_level=%s preview_level=%s",
                    operation["current_level"], operation["upgrade_level"])
        return None
    raise TradeGoodsInvestmentError("investment_commit_unconfirmed", "No confirmed actual level after the single submit click", {"operation": operation, "last_levels": last, "success_toast_seen": seen_success})


def _scroll(app: Any, reader: InvestmentVision, before: np.ndarray, previous_cards: list[dict]):
    viewport = reader.metadata["cards"]["viewport"]
    x, y, width, height = viewport
    complete = [card for card in previous_cards if not card["partial"]]
    old = [(card, reader.fingerprint(before, card)) for card in complete]
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
        new_images = [reader.fingerprint(after, card) for card in new]
        def overlaps(first, second):
            if abs(first[0]["rect"][0] - second[0]["rect"][0]) > 5:
                return False
            return reader.same_fingerprint(first[1], second[1])
        overlap_counts = []
        for count in range(min(len(old), len(new)), 0, -1):
            pairs = zip(old[-count:], zip(new[:count], new_images[:count]))
            if all(overlaps(first, second) for first, second in pairs):
                if count < 2 and len(old) > 1 and len(new) > 1:
                    continue
                overlap_counts.append(count)
        if len(overlap_counts) == 1:
            count = overlap_counts[0]
            cards = _scrolled_card_indices(complete, cards, count)
            validate_grid = getattr(reader, "validate_card_grid", None)
            if validate_grid is not None:
                validate_grid(cards, first_index=cards[0]["investment_index"])
            return after, cards, [dict(card) for card in cards if not card["partial"]][:count]
        raise TradeGoodsInvestmentError("investment_scroll_overlap_unconfirmed", "Could not verify the ordered overlap after scrolling")
    if any(card["partial"] for card in previous_cards):
        raise TradeGoodsInvestmentError("investment_scroll_stalled", "A partial product remains but the list would not scroll")
    return None


def _scrolled_card_indices(previous_cards: list[dict], cards: list[dict], overlap_count: int) -> list[dict]:
    complete = [card for card in previous_cards if not card["partial"]]
    first_index = complete[-overlap_count]["investment_index"]
    top_partials = 0
    for card in cards:
        if not card["partial"]:
            break
        top_partials += 1
    first_index -= top_partials
    if first_index < 1:
        raise TradeGoodsInvestmentError("investment_scroll_overlap_unconfirmed", "Invalid absolute investment indices")
    return [{**card, "investment_index": first_index + offset} for offset, card in enumerate(cards)]


def _observe_shop_return(app: Any, vision: Any, reader: InvestmentVision) -> dict:
    """Observe both pages from one frame; only a known investment page permits input."""
    image = _capture(app)
    menu = vision.find_template(
        source_image=image[350:470, 720:940],
        template_image=str(_PLAN_ROOT / "templates/commerce/trade/shop/menu_ready.png"),
        threshold=.86, use_grayscale=True,
    )
    score = getattr(menu, "confidence", None)
    valid = (isinstance(score, (int, float)) and math.isfinite(score)
             and not (getattr(menu, "debug_info", None) or {}).get("error"))
    result = {"page": "unknown", "shop_seen": bool(valid and menu.found),
              "menu_score": score if valid else None, "back_point": None}
    if not valid:
        return result
    try:
        investment = reader.match(image, "page_anchor")["found"]
        success = reader.match(image, "success")["found"]
        if result["shop_seen"]:
            result["page"] = "shop" if not investment and not success else "ambiguous"
        elif investment and not success:
            reader.read_levels(image)
            back = vision.find_template(
                source_image=image[:80, :170],
                template_image=str(_PLAN_ROOT / "templates/navigation/global/back.png"),
                threshold=.86, use_grayscale=True,
            )
            if (back.found and back.center_point is not None
                    and math.isfinite(back.confidence)
                    and not (getattr(back, "debug_info", None) or {}).get("error")):
                result.update(page="investment", back_point=list(back.center_point))
    except InvestmentRecognitionError as exc:
        result["recognition_error"] = exc.code
    return result



def _return_shop(app: Any, vision: Any, reader: InvestmentVision) -> None:
    started = time.monotonic()
    deadline = started + _RETURN_TIMEOUT
    last_click = None
    clicks = shop_streak = investment_streak = 0
    shop_seen = False
    observation = {}

    def observe():
        nonlocal observation, shop_seen, shop_streak, investment_streak
        observation = _observe_shop_return(app, vision, reader)
        _cancel_check()
        # Latch even a single destination observation: a late return must never
        # turn a retry into another back click from the exchange.
        shop_seen = shop_seen or observation["shop_seen"]
        shop_streak = shop_streak + 1 if observation["page"] == "shop" else 0
        investment_streak = investment_streak + 1 if observation["page"] == "investment" else 0

    def details():
        now = time.monotonic()
        return {"click_attempts": clicks, "elapsed_sec": round(now - started, 3),
                "since_last_click_sec": None if last_click is None else round(now - last_click, 3),
                "shop_seen": shop_seen, "last_observation": observation}

    while time.monotonic() < deadline:
        observe()
        if time.monotonic() >= deadline:
            break
        if shop_streak >= 2:
            logger.info("[TradeGoodsInvestmentReturn] state=completed detail=%s", details())
            return
        if (not shop_seen and investment_streak >= 2 and clicks < _RETURN_MAX_CLICKS
                and (last_click is None or time.monotonic() - last_click >= _RETURN_RETRY_INTERVAL)):
            # Recognition takes time. Refresh both page and button immediately
            # before every click, including retries; never reuse a saved point.
            observe()
            if not shop_seen and observation["page"] == "investment" and time.monotonic() < deadline:
                _click(app, observation["back_point"])
                clicks += 1
                last_click = time.monotonic()
                investment_streak = 0
                logger.info("[TradeGoodsInvestmentReturn] state=clicked detail=%s", details())
        logger.debug("[TradeGoodsInvestmentReturn] state=waiting detail=%s", details())
        _pause(min(_RETURN_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    detail = details()
    logger.warning("[TradeGoodsInvestmentReturn] state=timeout detail=%s", detail)
    raise TradeGoodsInvestmentError(
        "investment_return_shop_timeout", "投资处理完成后返回交易所超时", detail,
    )


def execute_trade_goods_investment_from_shop(*, mode: int, city_name: str, app: Any, vision: Any,
                                           progress: Optional[Callable[[str, dict], None]] = None,
                                           persistent_data: Any = None) -> Dict[str, Any]:
    mode = normalize_investment_mode(mode)
    if app is None or vision is None:
        raise RuntimeError("app and vision services are required")
    reader, target = InvestmentVision(city_name=city_name), mode
    product_limit = reader.city_product_count
    started_at, transactions, product_index, scroll_count = time.monotonic(), [], 0, 0
    reason = "all_products_at_target"
    records = (InvestmentLevelRecords(persistent_data, reader.city_id, product_limit,
                                     catalog_signature=getattr(reader, "city_catalog_signature", None))
               if getattr(reader, "city_id", None) and type(product_limit) is int and product_limit > 0 else None)
    single_next_attempt = False
    def report(state, **data):
        fields = {"city_name": city_name, "mode": mode, "target_level": target, "product_index": product_index, **data}
        logger.info("[TradeGoodsInvestment] state=%s detail=%s", state, fields)
        if progress is not None:
            progress(state, fields)
    report("started")
    try:
        try:
            frame, entry = _wait(app, reader.read_entry_state)
        except TradeGoodsInvestmentError as exc:
            if exc.code != "investment_state_timeout":
                raise
            raise TradeGoodsInvestmentError(
                "investment_entry_not_found", "Could not locate the investment entry; entry was not clicked",
                exc.detail,
            ) from exc
        logger.info("[TradeGoodsInvestment] entry_state=%s white_score=%.6f gray_score=%.6f",
                    entry["availability"], entry["white_score"], entry["gray_score"])
        if entry["availability"] == "unavailable":
            result = {"success": True, "triggered": False, "status": "skipped",
                      "reason": "investment_not_available", "city_name": city_name,
                      "mode": mode, "target_level": target, "transaction_count": 0,
                      "transactions": [], "upgraded_levels": 0, "product_count": 0,
                      "scroll_count": 0, "page_state": "shop_page", "entry_state": entry,
                      "elapsed_ms": round((time.monotonic() - started_at) * 1000)}
            report("skipped", reason=result["reason"], transaction_count=0,
                   white_score=entry["white_score"], gray_score=entry["gray_score"])
            return result
        if product_limit != 0:
            _click(app, entry["center"])
            _pause(.4)
            frame, cards = _cards(app, reader)
            frame, cards = _initial_card_indices(app, reader, frame, cards)
        while product_limit != 0:
            _cancel_check()
            pending = [card for card in cards if not card["partial"] and card["investment_index"] > product_index]
            if not pending:
                if scroll_count >= _MAX_SCROLLS:
                    raise TradeGoodsInvestmentError("investment_scroll_limit", "Investment list exceeded its scroll guard")
                def page_ready(image):
                    return (not reader.match(image, "success")["found"]
                            and reader.match(image, "page_anchor")["found"])
                frame, _ = _wait(app, page_ready)
                scrolled = _scroll(app, reader, frame, cards)
                if scrolled is None:
                    if product_limit is not None and product_index < product_limit:
                        raise TradeGoodsInvestmentError(
                            "investment_scroll_stalled", "Known investment products remain outside the confirmed list",
                            {"product_index": product_index, "product_count": product_limit},
                        )
                    break
                frame, cards, _ = scrolled
                scroll_count += 1
                continue
            card = pending[0]
            index = card["investment_index"]
            if index != product_index + 1:
                raise TradeGoodsInvestmentError("investment_order_gap", "The next investment slot is not visible",
                                               {"expected": product_index + 1, "observed": index})
            known_level = records.confirmed_level(index) if records is not None else None
            if known_level is not None and known_level >= target:
                product_index = index
                report("running", current_level=known_level, cached=True)
                if known_level < 10:
                    reason = "next_product_locked"
                    break
                if product_limit is not None and product_index >= product_limit:
                    break
                continue
            frame, card = _current_card(app, reader, card)
            if card.get("locked") is None:
                raise TradeGoodsInvestmentError("investment_lock_state_unknown", "Product lock state is not confirmed", card)
            if card["locked"]:
                _pause(.5)
                frame, fresh_card = _current_card(app, reader, card)
                if fresh_card.get("locked") is not False:
                    reason = "next_product_locked"
                    break
                card = fresh_card
            product_index = index
            attempted, blocked = False, False
            while True:
                frame, selected, levels = _select(app, reader, card)
                actual_level = levels["current"]
                if records is not None:
                    records.record(product_index, actual_level)
                report("running", current_level=levels["current"], preview_level=levels.get("preview"))
                if levels["current"] >= target:
                    break
                attempted = True
                operation = _prepare_upgrade(app, reader, levels, target, lambda **data: report("running", **data))
                if operation is None:
                    blocked = True
                    break
                if len(transactions) >= _MAX_TRANSACTIONS:
                    raise TradeGoodsInvestmentError("investment_transaction_limit", "Investment exceeded its progress guard")
                commit = _commit(app, reader, operation)
                if commit is None:
                    blocked = True
                    break
                transactions.append({"product_index": product_index, "from_level": operation["current_level"],
                                     "to_level": operation["upgrade_level"], **commit})
                if records is not None:
                    records.record(product_index, commit["current_level"])
                report("running", current_level=commit["current_level"], transaction_count=len(transactions))
                actual_level = commit["current_level"]
                if actual_level >= target or operation["stop_after_commit"] or single_next_attempt:
                    blocked = actual_level < target
                    break
                card = selected
            # Reading an already-complete product does not spend the final attempt.
            if single_next_attempt and attempted:
                reason = "next_product_attempted"
                break
            if actual_level < 10:
                reason = "no_further_upgrade" if blocked else "next_product_locked"
                break
            cards = [dict(selected) if _same_position(row, selected) else row for row in cards]
            if blocked:
                reason = "no_further_upgrade"
                single_next_attempt = True
            if product_limit is not None and product_index >= product_limit:
                break
        if product_limit != 0:
            _return_shop(app, vision, reader)
    except Exception as exc:
        if isinstance(exc, TradeGoodsInvestmentError):
            exc.detail.update(transaction_count=len(transactions), transactions=list(transactions))
        report("failed", reason=getattr(exc, "code", type(exc).__name__), message=str(exc),
               transaction_count=len(transactions), transactions=list(transactions),
               detail=getattr(exc, "detail", {}))
        raise
    result = {"success": True, "triggered": True, "status": "invested" if transactions else "skipped",
              "reason": reason, "city_name": city_name, "mode": mode, "target_level": target,
              "transaction_count": len(transactions), "transactions": transactions,
              "upgraded_levels": sum(row["to_level"] - row["from_level"] for row in transactions),
              "product_count": product_index, "scroll_count": scroll_count,
              "page_state": "shop_page", "entry_state": entry,
              "elapsed_ms": round((time.monotonic() - started_at) * 1000)}
    report("completed" if transactions else "skipped", **{key: value for key, value in result.items() if key not in {"city_name", "mode", "target_level"}})
    return result


@action_info(name="resonance_pc.invest_trade_goods_from_shop", public=True, read_only=False,
             description="Invest goods in original unlock order to a target level from 1 to 20 and return to the exchange.")
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", persistent_data="core/persistent_data")
def resonance_pc_invest_trade_goods_from_shop(mode: int = 10, city_name: str = "",
                                            app: Any = None, vision: Any = None,
                                            persistent_data: Any = None) -> Dict[str, Any]:
    return execute_trade_goods_investment_from_shop(
        mode=mode, city_name=city_name, app=app, vision=vision,
        progress=_INVESTMENT_PROGRESS_CALLBACK.get(),
        persistent_data=persistent_data,
    )

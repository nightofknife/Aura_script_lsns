"""Actions for using purchase quantity items in ResonancePc trade flow."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger


class PurchaseBookUseError(RuntimeError):
    """Structured error for purchase-book usage failures."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"{self.code}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


_USE_ITEM_BUTTON_REGION = [1010, 80, 145, 55]
_ITEM_MODAL_HEADER_REGION = [580, 60, 260, 80]
_ITEM_IDENTITY_REGION = [640, 115, 250, 110]
_FIRST_ITEM_USE_BUTTON_REGION = [840, 135, 175, 75]
_QUANTITY_PROMPT_REGION = [420, 400, 430, 90]
_CANCEL_BUTTON_REGION = [0, 485, 640, 110]
_CONFIRM_BUTTON_REGION = [650, 500, 620, 85]
_BUY_PAGE_READY_REGION = [850, 590, 380, 110]

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_USE_ITEM_BUTTON_TEMPLATE = "templates/purchase_book_use_items_button.png"
_ITEM_MODAL_HEADER_TEMPLATE = "templates/purchase_book_item_modal_header.png"
_ITEM_IDENTITY_TEMPLATE = "templates/purchase_book_item_identity.png"
_FIRST_ITEM_USE_BUTTON_TEMPLATE = "templates/purchase_book_first_use_button.png"
_QUANTITY_PROMPT_TEMPLATE = "templates/purchase_book_quantity_prompt.png"
_CANCEL_BUTTON_TEMPLATE = "templates/purchase_book_cancel_button.png"
_CONFIRM_BUTTON_TEMPLATE = "templates/purchase_book_confirm_button.png"
_BUY_PAGE_READY_TEMPLATE = "templates/purchase_book_buy_page_ready.png"

_USE_ITEM_BUTTON_POINT = (1080, 105)
_FIRST_ITEM_USE_BUTTON_POINT = (922, 170)
_PLUS_ONE_POINT = (828, 407)
_CONFIRM_POINT = (960, 538)

_DEFAULT_TEMPLATE_THRESHOLD = 0.8
_STATE_STABLE_COUNT = 2
_ITEM_MODAL_OPEN_ATTEMPTS = 3


def _raise_error(code: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    raise PurchaseBookUseError(code=code, message=message, detail=detail)


def _coerce_region(region: List[int] | Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        _raise_error("invalid_region", "region must be [x, y, w, h]", {"region": region})
    return (int(region[0]), int(region[1]), int(region[2]), int(region[3]))


def _coerce_book_count(books_used: Any, max_books_per_purchase: Any) -> int:
    try:
        requested = int(books_used or 0)
    except (TypeError, ValueError):
        _raise_error("invalid_books_used", "books_used must be an integer", {"books_used": books_used})

    try:
        max_books = int(max_books_per_purchase)
    except (TypeError, ValueError):
        _raise_error(
            "invalid_max_books_per_purchase",
            "max_books_per_purchase must be an integer",
            {"max_books_per_purchase": max_books_per_purchase},
        )

    if requested < 0:
        _raise_error("invalid_books_used", "books_used must be >= 0", {"books_used": books_used})
    if max_books <= 0:
        _raise_error(
            "invalid_max_books_per_purchase",
            "max_books_per_purchase must be > 0",
            {"max_books_per_purchase": max_books_per_purchase},
        )
    return requested


def _split_book_batches(books_used: Any, max_books_per_purchase: Any) -> List[int]:
    requested = _coerce_book_count(books_used, max_books_per_purchase)
    max_books = int(max_books_per_purchase)
    if requested <= 0:
        return []

    full_batches, remainder = divmod(requested, max_books)
    batches = [max_books] * full_batches
    if remainder:
        batches.append(remainder)
    return batches


def _offset_center(center: Tuple[int, int] | None, region: Tuple[int, int, int, int]) -> Tuple[int, int] | None:
    if center is None:
        return None
    return (int(center[0]) + int(region[0]), int(center[1]) + int(region[1]))


def _resolve_template_path(template: str) -> Path:
    template_path = Path(str(template))
    if template_path.is_absolute():
        return template_path
    return _PLAN_ROOT / template_path


def _offset_template_result(result: Any, region: Tuple[int, int, int, int]) -> Any:
    if not getattr(result, "found", False):
        return result

    center = getattr(result, "center_point", None)
    if center is not None:
        result.center_point = _offset_center(center, region)

    top_left = getattr(result, "top_left", None)
    if top_left is not None:
        result.top_left = (int(top_left[0]) + region[0], int(top_left[1]) + region[1])

    rect = getattr(result, "rect", None)
    if rect is not None:
        result.rect = (int(rect[0]) + region[0], int(rect[1]) + region[1], int(rect[2]), int(rect[3]))
    return result


def _find_template(
    app: Any,
    vision: Any,
    template: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    threshold: float,
    use_grayscale: bool = True,
    preprocess: str = "none",
) -> Any:
    region_tuple = _coerce_region(region)
    template_path = _resolve_template_path(template)
    if not template_path.is_file():
        _raise_error("template_not_found", "purchase book button template not found", {"template": str(template_path)})

    capture = app.capture(rect=region_tuple)
    if not capture.success:
        _raise_error("capture_failed", "failed to capture screen region", {"region": list(region_tuple)})

    result = vision.find_template(
        source_image=capture.image,
        template_image=str(template_path),
        threshold=float(threshold),
        use_grayscale=use_grayscale,
        preprocess=preprocess,
    )
    return _offset_template_result(result, region_tuple)


def _template_spec(
    name: str,
    template: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    threshold: float = _DEFAULT_TEMPLATE_THRESHOLD,
) -> Dict[str, Any]:
    return {
        "name": str(name),
        "template": str(template),
        "region": list(_coerce_region(region)),
        "threshold": float(threshold),
    }


def _observe_template(app: Any, vision: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    result = _find_template(
        app,
        vision,
        str(spec["template"]),
        spec["region"],
        threshold=float(spec["threshold"]),
    )
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    center = getattr(result, "center_point", None)
    return {
        "name": str(spec["name"]),
        "template": str(spec["template"]),
        "region": list(spec["region"]),
        "threshold": float(spec["threshold"]),
        "found": bool(getattr(result, "found", False)),
        "confidence": confidence,
        "center_point": [int(center[0]), int(center[1])] if center is not None else None,
    }


def _wait_for_template_state(
    app: Any,
    vision: Any,
    *,
    state_name: str,
    all_of: List[Dict[str, Any]],
    none_of: Optional[List[Dict[str, Any]]] = None,
    timeout_sec: float,
    interval_sec: float,
    stable_count: int = _STATE_STABLE_COUNT,
) -> Dict[str, Any]:
    required_absent = list(none_of or [])
    required_stable = max(int(stable_count), 1)
    timeout = max(float(timeout_sec), 0.0)
    interval = max(float(interval_sec), 0.05)
    started_at = time.monotonic()
    deadline = started_at + timeout
    attempts = 0
    consecutive = 0
    best_confidences: Dict[str, float] = {
        str(spec["name"]): 0.0 for spec in [*all_of, *required_absent]
    }
    observations: Dict[str, Dict[str, Any]] = {}

    while True:
        attempts += 1
        observations = {}
        for spec in [*all_of, *required_absent]:
            observation = _observe_template(app, vision, spec)
            name = str(observation["name"])
            observations[name] = observation
            best_confidences[name] = max(best_confidences.get(name, 0.0), float(observation["confidence"]))

        present_ok = all(bool(observations[str(spec["name"])]["found"]) for spec in all_of)
        absent_ok = all(not bool(observations[str(spec["name"])]["found"]) for spec in required_absent)
        if present_ok and absent_ok:
            consecutive += 1
            if consecutive >= required_stable:
                elapsed = time.monotonic() - started_at
                logger.info(
                    "模板状态已稳定: %s (attempts=%d, stable=%d, elapsed=%.2fs)",
                    state_name,
                    attempts,
                    consecutive,
                    elapsed,
                )
                return {
                    "matched": True,
                    "state": state_name,
                    "attempts": attempts,
                    "stable_count": consecutive,
                    "required_stable_count": required_stable,
                    "elapsed_sec": elapsed,
                    "observations": observations,
                    "best_confidences": best_confidences,
                }
        else:
            consecutive = 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            elapsed = time.monotonic() - started_at
            logger.warning(
                "等待模板状态超时: %s (attempts=%d, elapsed=%.2fs, observations=%s)",
                state_name,
                attempts,
                elapsed,
                observations,
            )
            return {
                "matched": False,
                "state": state_name,
                "attempts": attempts,
                "stable_count": consecutive,
                "required_stable_count": required_stable,
                "elapsed_sec": elapsed,
                "observations": observations,
                "best_confidences": best_confidences,
            }
        time.sleep(min(interval, remaining))


def _click_template_or_point(
    app: Any,
    vision: Any,
    template: str,
    region: List[int] | Tuple[int, int, int, int],
    fallback_point: Tuple[int, int],
    *,
    threshold: float = 0.8,
    timeout_sec: float = 0.0,
    retry_interval_sec: float = 0.2,
    error_code: str = "purchase_book_template_not_found",
    error_message: str = "failed to find required purchase-book UI template",
) -> Dict[str, Any]:
    timeout = max(float(timeout_sec or 0.0), 0.0)
    retry_interval = max(float(retry_interval_sec or 0.0), 0.05)
    deadline = time.monotonic() + timeout
    attempts = 0
    confidence = 0.0
    best_confidence = 0.0

    while True:
        attempts += 1
        result = _find_template(app, vision, template, region, threshold=threshold)
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        best_confidence = max(best_confidence, confidence)
        if getattr(result, "found", False) and getattr(result, "center_point", None):
            x, y = result.center_point
            logger.info(
                "模板找到: '%s'，位于窗口坐标 (%s, %s)，置信度: %.2f，尝试次数: %d",
                template,
                x,
                y,
                confidence,
                attempts,
            )
            app.move_to(int(x), int(y), duration=0.1)
            app.click(x=int(x), y=int(y))
            return {
                "clicked": True,
                "method": "template",
                "template": template,
                "confidence": confidence,
                "attempts": attempts,
                "x": int(x),
                "y": int(y),
            }

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(retry_interval, remaining))

    logger.warning(
        "限定时间内未能在区域 %s 找到模板 '%s' "
        "(attempts=%d, timeout_sec=%.2f, last_confidence=%.3f, best_confidence=%.3f); "
        "不使用固定坐标兜底。",
        region,
        template,
        attempts,
        timeout,
        confidence,
        best_confidence,
    )
    _raise_error(
        error_code,
        error_message,
        {
            "template": template,
            "region": list(region),
            "attempts": attempts,
            "timeout_sec": timeout,
            "last_confidence": confidence,
            "best_confidence": best_confidence,
            "fallback_point_disabled": {"x": int(fallback_point[0]), "y": int(fallback_point[1])},
        },
    )


def _attempt_quantity_dialog_cleanup(
    *,
    dialog_timeout_sec: float,
    click_interval_sec: float,
    app: Any,
    vision: Any,
) -> Dict[str, Any]:
    cleanup: Dict[str, Any] = {"attempted": True, "succeeded": False}
    try:
        cleanup["cancel_click"] = _click_template_or_point(
            app,
            vision,
            _CANCEL_BUTTON_TEMPLATE,
            _CANCEL_BUTTON_REGION,
            (322, 538),
            threshold=_DEFAULT_TEMPLATE_THRESHOLD,
            timeout_sec=min(max(float(dialog_timeout_sec), 0.5), 2.0),
            retry_interval_sec=click_interval_sec,
            error_code="purchase_book_cleanup_cancel_not_found",
            error_message="failed to find the purchase-book quantity-dialog cancel button",
        )
    except PurchaseBookUseError as exc:
        cleanup["error"] = exc.to_dict()
        return cleanup
    except Exception as exc:  # cleanup must never hide the original batch failure
        cleanup["error"] = {
            "code": "purchase_book_cleanup_unexpected_error",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
        return cleanup

    try:
        returned_state = _wait_for_template_state(
            app,
            vision,
            state_name="purchase_book_cleanup_buy_page_ready",
            all_of=[
                _template_spec("buy_page_ready", _BUY_PAGE_READY_TEMPLATE, _BUY_PAGE_READY_REGION),
            ],
            none_of=[
                _template_spec("quantity_prompt", _QUANTITY_PROMPT_TEMPLATE, _QUANTITY_PROMPT_REGION),
                _template_spec("confirm_button", _CONFIRM_BUTTON_TEMPLATE, _CONFIRM_BUTTON_REGION),
                _template_spec("cancel_button", _CANCEL_BUTTON_TEMPLATE, _CANCEL_BUTTON_REGION),
            ],
            timeout_sec=max(float(dialog_timeout_sec), 2.0),
            interval_sec=click_interval_sec,
        )
    except PurchaseBookUseError as exc:
        cleanup["error"] = exc.to_dict()
        return cleanup
    except Exception as exc:  # cleanup must never hide the original batch failure
        cleanup["error"] = {
            "code": "purchase_book_cleanup_unexpected_error",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
        return cleanup
    cleanup["returned_state"] = returned_state
    cleanup["succeeded"] = bool(returned_state["matched"])
    return cleanup


def _use_purchase_book_batch(
    *,
    batch_size: int,
    item_name: str,
    open_timeout_sec: float,
    dialog_timeout_sec: float,
    click_interval_sec: float,
    app: Any,
    vision: Any,
) -> Dict[str, Any]:
    logger.info("执行单批 %s x %d。", item_name, batch_size)
    state = "buy_page"
    state_trace: List[Dict[str, Any]] = []
    quantity_dialog_may_be_open = False

    try:
        state = "item_modal"
        open_attempts: List[Dict[str, Any]] = []
        item_modal_state: Dict[str, Any] = {"matched": False}
        open_click: Dict[str, Any] = {}
        for open_attempt in range(1, _ITEM_MODAL_OPEN_ATTEMPTS + 1):
            open_click = _click_template_or_point(
                app,
                vision,
                _USE_ITEM_BUTTON_TEMPLATE,
                _USE_ITEM_BUTTON_REGION,
                _USE_ITEM_BUTTON_POINT,
                threshold=0.82,
                timeout_sec=open_timeout_sec,
                retry_interval_sec=click_interval_sec,
                error_code="purchase_book_use_items_button_not_found",
                error_message="failed to find the use-items button on the buy page",
            )
            item_modal_state = _wait_for_template_state(
                app,
                vision,
                state_name=state,
                all_of=[
                    _template_spec(
                        "item_modal_header",
                        _ITEM_MODAL_HEADER_TEMPLATE,
                        _ITEM_MODAL_HEADER_REGION,
                        threshold=0.86,
                    ),
                ],
                timeout_sec=open_timeout_sec,
                interval_sec=click_interval_sec,
            )
            state_trace.append(item_modal_state)
            open_attempts.append(
                {
                    "attempt": open_attempt,
                    "click": open_click,
                    "item_modal_state": item_modal_state,
                }
            )
            if item_modal_state["matched"]:
                break
            if open_attempt < _ITEM_MODAL_OPEN_ATTEMPTS:
                logger.warning(
                    "使用道具窗口未打开，准备重试点击：attempt=%d/%d。",
                    open_attempt,
                    _ITEM_MODAL_OPEN_ATTEMPTS,
                )

        if not item_modal_state["matched"]:
            _raise_error(
                "purchase_item_modal_not_found",
                "the use-items modal header did not reach a stable template state",
                {
                    "item_name": item_name,
                    "batch_size": batch_size,
                    "open_attempts": open_attempts,
                    "state_result": item_modal_state,
                },
            )

        state = "purchase_book_available"
        item_available_state = _wait_for_template_state(
            app,
            vision,
            state_name=state,
            all_of=[
                _template_spec("item_identity", _ITEM_IDENTITY_TEMPLATE, _ITEM_IDENTITY_REGION),
            ],
            timeout_sec=open_timeout_sec,
            interval_sec=click_interval_sec,
        )
        state_trace.append(item_available_state)
        if not item_available_state["matched"]:
            _raise_error(
                "purchase_book_not_available",
                "the use-items modal opened, but no purchase book was available in the first item slot",
                {
                    "item_name": item_name,
                    "batch_size": batch_size,
                    "open_attempts": open_attempts,
                    "state_result": item_available_state,
                },
            )

        item_use_click = _click_template_or_point(
            app,
            vision,
            _FIRST_ITEM_USE_BUTTON_TEMPLATE,
            _FIRST_ITEM_USE_BUTTON_REGION,
            _FIRST_ITEM_USE_BUTTON_POINT,
            threshold=0.82,
            timeout_sec=dialog_timeout_sec,
            retry_interval_sec=click_interval_sec,
            error_code="purchase_book_first_use_button_not_found",
            error_message="failed to rematch the first purchase-book use button",
        )
        quantity_dialog_may_be_open = True
        state = "quantity_dialog"

        quantity_state = _wait_for_template_state(
            app,
            vision,
            state_name=state,
            all_of=[
                _template_spec("quantity_prompt", _QUANTITY_PROMPT_TEMPLATE, _QUANTITY_PROMPT_REGION),
                _template_spec("confirm_button", _CONFIRM_BUTTON_TEMPLATE, _CONFIRM_BUTTON_REGION),
            ],
            timeout_sec=dialog_timeout_sec,
            interval_sec=click_interval_sec,
        )
        state_trace.append(quantity_state)
        if not quantity_state["matched"]:
            _raise_error(
                "purchase_book_quantity_dialog_not_found",
                "purchase-book quantity dialog did not reach a stable template state",
                {"item_name": item_name, "books_used": batch_size, "state_result": quantity_state},
            )
        state = "set_quantity"
        plus_clicks = max(batch_size - 1, 0)
        for _ in range(plus_clicks):
            app.click(x=_PLUS_ONE_POINT[0], y=_PLUS_ONE_POINT[1])
            time.sleep(max(float(click_interval_sec), 0.1))

        quantity_stable_state = _wait_for_template_state(
            app,
            vision,
            state_name="quantity_dialog_after_increment",
            all_of=[
                _template_spec("quantity_prompt", _QUANTITY_PROMPT_TEMPLATE, _QUANTITY_PROMPT_REGION),
                _template_spec("confirm_button", _CONFIRM_BUTTON_TEMPLATE, _CONFIRM_BUTTON_REGION),
            ],
            timeout_sec=dialog_timeout_sec,
            interval_sec=click_interval_sec,
        )
        state_trace.append(quantity_stable_state)
        if not quantity_stable_state["matched"]:
            _raise_error(
                "purchase_book_quantity_dialog_unstable",
                "purchase-book quantity dialog became unstable while setting the batch size",
                {
                    "item_name": item_name,
                    "books_used": batch_size,
                    "plus_clicks": plus_clicks,
                    "state_result": quantity_stable_state,
                },
            )

        state = "confirm"
        confirm_click = _click_template_or_point(
            app,
            vision,
            _CONFIRM_BUTTON_TEMPLATE,
            _CONFIRM_BUTTON_REGION,
            _CONFIRM_POINT,
            threshold=_DEFAULT_TEMPLATE_THRESHOLD,
            timeout_sec=dialog_timeout_sec,
            retry_interval_sec=click_interval_sec,
            error_code="purchase_book_confirm_button_not_found",
            error_message="failed to rematch the purchase-book quantity-dialog confirm button",
        )

        state = "return_buy_page"
        returned_state = _wait_for_template_state(
            app,
            vision,
            state_name=state,
            all_of=[
                _template_spec("buy_page_ready", _BUY_PAGE_READY_TEMPLATE, _BUY_PAGE_READY_REGION),
            ],
            none_of=[
                _template_spec("quantity_prompt", _QUANTITY_PROMPT_TEMPLATE, _QUANTITY_PROMPT_REGION),
                _template_spec("confirm_button", _CONFIRM_BUTTON_TEMPLATE, _CONFIRM_BUTTON_REGION),
                _template_spec("cancel_button", _CANCEL_BUTTON_TEMPLATE, _CANCEL_BUTTON_REGION),
            ],
            timeout_sec=max(float(dialog_timeout_sec), 2.0),
            interval_sec=click_interval_sec,
        )
        state_trace.append(returned_state)
        if not returned_state["matched"]:
            observations = returned_state.get("observations") or {}
            dialog_remains = any(
                bool((observations.get(name) or {}).get("found"))
                for name in ("quantity_prompt", "confirm_button", "cancel_button")
            )
            if dialog_remains:
                _raise_error(
                    "purchase_book_confirm_not_applied",
                    "purchase-book quantity dialog remained after clicking confirm",
                    {
                        "item_name": item_name,
                        "books_used": batch_size,
                        "confirm_click": confirm_click,
                        "state_result": returned_state,
                    },
                )
            _raise_error(
                "purchase_book_buy_page_not_restored",
                "purchase-book dialog closed but the buy page did not reach a stable template state",
                {
                    "item_name": item_name,
                    "books_used": batch_size,
                    "confirm_click": confirm_click,
                    "state_result": returned_state,
                },
            )

        return {
            "ok": True,
            "used": batch_size,
            "item_name": item_name,
            "plus_clicks": plus_clicks,
            "open_click": open_click,
            "open_attempts": open_attempts,
            "item_use_click": item_use_click,
            "confirm_click": confirm_click,
            "buy_page_ready": True,
            "state_trace": state_trace,
        }
    except PurchaseBookUseError as exc:
        cleanup = {"attempted": False, "succeeded": False}
        if quantity_dialog_may_be_open:
            cleanup = _attempt_quantity_dialog_cleanup(
                dialog_timeout_sec=dialog_timeout_sec,
                click_interval_sec=click_interval_sec,
                app=app,
                vision=vision,
            )
        exc.detail = {
            **exc.detail,
            "failed_state": state,
            "state_trace": state_trace,
            "cleanup": cleanup,
        }
        raise


@action_info(
    name="resonance_pc.use_purchase_books",
    public=True,
    description="Use 进货采买书 before selecting products on the buy-goods page.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
)
def resonance_pc_use_purchase_books(
    books_used: int,
    item_name: str = "进货采买书",
    max_books_per_purchase: int = 10,
    open_timeout_sec: float = 3.0,
    dialog_timeout_sec: float = 3.0,
    click_interval_sec: float = 0.2,
    app: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or vision is None:
        _raise_error("missing_service", "app and vision services are required")

    requested = _coerce_book_count(books_used, max_books_per_purchase)
    max_books = int(max_books_per_purchase)
    batch_sizes = _split_book_batches(requested, max_books)
    if requested <= 0:
        return {
            "ok": True,
            "requested": 0,
            "used": 0,
            "skipped": True,
            "reason": "books_used_zero",
            "max_books_per_purchase": max_books,
            "batch_count": 0,
            "batch_sizes": [],
            "batches": [],
        }

    logger.info(
        "准备分批使用 %s：总数=%d，单批上限=%d，批次=%s。",
        item_name,
        requested,
        max_books,
        batch_sizes,
    )

    completed_batches: List[Dict[str, Any]] = []
    used = 0
    for batch_index, batch_size in enumerate(batch_sizes, start=1):
        logger.info(
            "开始使用 %s 批次 %d/%d：数量=%d。",
            item_name,
            batch_index,
            len(batch_sizes),
            batch_size,
        )
        try:
            batch_result = _use_purchase_book_batch(
                batch_size=batch_size,
                item_name=item_name,
                open_timeout_sec=open_timeout_sec,
                dialog_timeout_sec=dialog_timeout_sec,
                click_interval_sec=click_interval_sec,
                app=app,
                vision=vision,
            )
        except PurchaseBookUseError as exc:
            _raise_error(
                "purchase_book_batch_failed",
                "failed while applying a purchase-book batch",
                {
                    "item_name": item_name,
                    "requested": requested,
                    "used_before_failure": used,
                    "failed_batch_index": batch_index,
                    "failed_batch_size": batch_size,
                    "batch_sizes": batch_sizes,
                    "completed_batches": completed_batches,
                    "cause": exc.to_dict(),
                },
            )

        batch_record = {
            **batch_result,
            "batch_index": batch_index,
            "batch_size": batch_size,
        }
        completed_batches.append(batch_record)
        used += batch_size

        if batch_index < len(batch_sizes) and not batch_result["buy_page_ready"]:
            _raise_error(
                "purchase_book_batch_return_not_ready",
                "buy page was not confirmed before the next purchase-book batch",
                {
                    "item_name": item_name,
                    "requested": requested,
                    "used_before_failure": used,
                    "failed_batch_index": batch_index + 1,
                    "failed_batch_size": batch_sizes[batch_index],
                    "batch_sizes": batch_sizes,
                    "completed_batches": completed_batches,
                },
            )

        logger.info(
            "完成使用 %s 批次 %d/%d：本批=%d，累计=%d/%d。",
            item_name,
            batch_index,
            len(batch_sizes),
            batch_size,
            used,
            requested,
        )

    last_batch = completed_batches[-1]
    return {
        "ok": True,
        "requested": requested,
        "used": used,
        "item_name": item_name,
        "max_books_per_purchase": max_books,
        "batch_count": len(completed_batches),
        "batch_sizes": batch_sizes,
        "batches": completed_batches,
        "plus_clicks": sum(int(batch["plus_clicks"]) for batch in completed_batches),
        "open_click": last_batch["open_click"],
        "item_use_click": last_batch["item_use_click"],
        "confirm_click": last_batch["confirm_click"],
        "buy_page_ready": all(bool(batch["buy_page_ready"]) for batch in completed_batches),
    }

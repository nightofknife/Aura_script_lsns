"""Actions for using purchase quantity items in Resonance trade flow."""

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
_ITEM_NAME_REGION = [585, 125, 260, 90]
_FIRST_ITEM_USE_BUTTON_REGION = [840, 135, 175, 75]
_QUANTITY_DIALOG_REGION = [430, 270, 430, 235]
_CONFIRM_BUTTON_REGION = [650, 500, 620, 85]
_BUY_PAGE_READY_REGION = [850, 80, 190, 70]

_PLAN_ROOT = Path(__file__).resolve().parents[2]
_USE_ITEM_BUTTON_TEMPLATE = "templates/purchase_book_use_items_button.png"
_FIRST_ITEM_USE_BUTTON_TEMPLATE = "templates/purchase_book_first_use_button.png"
_CONFIRM_BUTTON_TEMPLATE = "templates/purchase_book_confirm_button.png"

_USE_ITEM_BUTTON_POINT = (1080, 105)
_FIRST_ITEM_USE_BUTTON_POINT = (922, 170)
_PLUS_ONE_POINT = (828, 407)
_CONFIRM_POINT = (960, 538)


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
    if requested > max_books:
        _raise_error(
            "books_used_exceeds_ui_limit",
            "requested books_used exceeds the purchase item dialog limit",
            {"books_used": requested, "max_books_per_purchase": max_books},
        )
    return requested


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


def _find_text(
    app: Any,
    ocr: Any,
    text_to_find: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    match_mode: str = "contains",
) -> Any:
    region_tuple = _coerce_region(region)
    capture = app.capture(rect=region_tuple)
    if not capture.success:
        _raise_error("capture_failed", "failed to capture screen region", {"region": list(region_tuple)})
    result = ocr.find_text(source_image=capture.image, text_to_find=text_to_find, match_mode=match_mode)
    if getattr(result, "found", False):
        result.center_point = _offset_center(getattr(result, "center_point", None), region_tuple)
        rect = getattr(result, "rect", None)
        if rect is not None:
            result.rect = (int(rect[0]) + region_tuple[0], int(rect[1]) + region_tuple[1], int(rect[2]), int(rect[3]))
    return result


def _wait_for_text(
    app: Any,
    ocr: Any,
    text_to_find: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    timeout_sec: float,
    interval_sec: float,
    match_mode: str = "contains",
) -> Any:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last_result = None
    while True:
        last_result = _find_text(app, ocr, text_to_find, region, match_mode=match_mode)
        if getattr(last_result, "found", False):
            return last_result
        if time.monotonic() >= deadline:
            return last_result
        time.sleep(max(float(interval_sec), 0.05))


def _wait_for_text_absent(
    app: Any,
    ocr: Any,
    text_to_find: str,
    region: List[int] | Tuple[int, int, int, int],
    *,
    timeout_sec: float,
    interval_sec: float,
    match_mode: str = "contains",
) -> bool:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    while True:
        result = _find_text(app, ocr, text_to_find, region, match_mode=match_mode)
        if not getattr(result, "found", False):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(float(interval_sec), 0.05))


def _click_template_or_point(
    app: Any,
    vision: Any,
    template: str,
    region: List[int] | Tuple[int, int, int, int],
    fallback_point: Tuple[int, int],
    *,
    threshold: float = 0.8,
) -> Dict[str, Any]:
    result = _find_template(app, vision, template, region, threshold=threshold)
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    if getattr(result, "found", False) and getattr(result, "center_point", None):
        x, y = result.center_point
        logger.info("模板找到: '%s'，位于窗口坐标 (%s, %s)，置信度: %.2f", template, x, y, confidence)
        app.move_to(int(x), int(y), duration=0.1)
        app.click(x=int(x), y=int(y))
        return {
            "clicked": True,
            "method": "template",
            "template": template,
            "confidence": confidence,
            "x": int(x),
            "y": int(y),
        }

    logger.warning(
        "未能在区域 %s 找到模板 '%s' (confidence=%.3f); 不使用固定坐标兜底。",
        region,
        template,
        confidence,
    )
    _raise_error(
        "purchase_book_template_not_found",
        "failed to find required purchase-book UI template",
        {
            "template": template,
            "region": list(region),
            "confidence": confidence,
            "fallback_point_disabled": {"x": int(fallback_point[0]), "y": int(fallback_point[1])},
        },
    )


@action_info(
    name="resonance.use_purchase_books",
    public=True,
    description="Use 进货采买书 before selecting products on the buy-goods page.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
    vision="plans/aura_base/vision",
)
def resonance_use_purchase_books(
    books_used: int,
    item_name: str = "进货采买书",
    max_books_per_purchase: int = 10,
    open_timeout_sec: float = 3.0,
    dialog_timeout_sec: float = 3.0,
    click_interval_sec: float = 0.2,
    app: Any = None,
    ocr: Any = None,
    vision: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None or vision is None:
        _raise_error("missing_service", "app, ocr and vision services are required")

    requested = _coerce_book_count(books_used, max_books_per_purchase)
    if requested <= 0:
        return {
            "ok": True,
            "used": 0,
            "skipped": True,
            "reason": "books_used_zero",
        }

    logger.info("准备使用 %s x %d。", item_name, requested)

    open_click = _click_template_or_point(
        app,
        vision,
        _USE_ITEM_BUTTON_TEMPLATE,
        _USE_ITEM_BUTTON_REGION,
        _USE_ITEM_BUTTON_POINT,
        threshold=0.82,
    )
    time.sleep(max(float(click_interval_sec), 0.1))

    item_result = _wait_for_text(
        app,
        ocr,
        item_name,
        _ITEM_NAME_REGION,
        timeout_sec=open_timeout_sec,
        interval_sec=0.3,
    )
    if not getattr(item_result, "found", False):
        _raise_error(
            "purchase_item_modal_not_found",
            "failed to find purchase item row after opening item dialog",
            {"item_name": item_name},
        )

    item_use_click = _click_template_or_point(
        app,
        vision,
        _FIRST_ITEM_USE_BUTTON_TEMPLATE,
        _FIRST_ITEM_USE_BUTTON_REGION,
        _FIRST_ITEM_USE_BUTTON_POINT,
        threshold=0.82,
    )
    time.sleep(max(float(click_interval_sec), 0.1))

    quantity_result = _wait_for_text(
        app,
        ocr,
        "是否使用",
        _QUANTITY_DIALOG_REGION,
        timeout_sec=dialog_timeout_sec,
        interval_sec=0.3,
    )
    if not getattr(quantity_result, "found", False):
        _raise_error(
            "purchase_book_quantity_dialog_not_found",
            "failed to find purchase book quantity dialog",
            {"item_name": item_name, "books_used": requested},
        )

    plus_clicks = max(requested - 1, 0)
    for _ in range(plus_clicks):
        app.click(x=_PLUS_ONE_POINT[0], y=_PLUS_ONE_POINT[1])
        time.sleep(max(float(click_interval_sec), 0.1))

    time.sleep(max(float(click_interval_sec), 0.1))
    confirm_click = _click_template_or_point(
        app,
        vision,
        _CONFIRM_BUTTON_TEMPLATE,
        _CONFIRM_BUTTON_REGION,
        _CONFIRM_POINT,
        threshold=0.8,
    )
    time.sleep(0.8)

    dialog_closed = _wait_for_text_absent(
        app,
        ocr,
        "是否使用",
        _QUANTITY_DIALOG_REGION,
        timeout_sec=2.0,
        interval_sec=0.3,
    )
    if not dialog_closed:
        _raise_error(
            "purchase_book_confirm_not_applied",
            "purchase book quantity dialog remained after clicking confirm",
            {"item_name": item_name, "books_used": requested, "confirm_click": confirm_click},
        )

    ready_result = _wait_for_text(
        app,
        ocr,
        "预计买入",
        _BUY_PAGE_READY_REGION,
        timeout_sec=2.0,
        interval_sec=0.3,
    )
    if not getattr(ready_result, "found", False):
        logger.warning("使用 %s 后未确认看到预计买入，但会继续后续买货步骤。", item_name)

    return {
        "ok": True,
        "used": requested,
        "item_name": item_name,
        "plus_clicks": plus_clicks,
        "open_click": open_click,
        "item_use_click": item_use_click,
        "confirm_click": confirm_click,
        "buy_page_ready": bool(getattr(ready_result, "found", False)),
    }

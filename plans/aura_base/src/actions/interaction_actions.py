from __future__ import annotations

import asyncio
import time

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.utils.exceptions import StopTaskException

from ..services.app_provider_service import AppProviderService
from ..services.ocr_service import OcrService
from ..services.vision_service import MatchResult, VisionService
from .ocr_actions import find_text
from .vision_actions import find_all_images, find_image


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError


def _sleep_sync_cancellable(seconds: float) -> None:
    deadline = time.monotonic() + max(float(seconds), 0.0)
    while True:
        _check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))
        _check_cancelled()


def _poll_sync(*, probe, predicate, timeout: float, interval: float):
    deadline = time.monotonic() + max(float(timeout), 0.0)
    poll_interval = max(float(interval), 0.01)
    while True:
        _check_cancelled()
        result = probe()
        _check_cancelled()
        matched = predicate(result)
        _check_cancelled()
        if matched:
            return True, result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, result
        _sleep_sync_cancellable(min(poll_interval, remaining))


def _required_failure(required: bool, message: str) -> None:
    if bool(required):
        raise StopTaskException(message, success=False)


@action_info(name="find_image_and_click", public=True)
@requires_services(vision="vision", app="app")
def find_image_and_click(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    button: str = "left",
    move_duration: float = 0.2,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
    timeout: float = 0.0,
    interval: float = 0.2,
    stable_scans: int = 1,
    stable_center_tolerance_px: int | None = None,
    after_click_sec: float = 0.0,
    required: bool = False,
    failure_message: str | None = None,
) -> bool:
    required_stable_scans = max(int(stable_scans), 1)
    tolerance = (
        None
        if stable_center_tolerance_px is None
        else max(int(stable_center_tolerance_px), 0)
    )
    consecutive_matches = 0
    last_center: tuple[int, int] | None = None

    def probe() -> MatchResult:
        return find_image(
            app,
            vision,
            engine,
            template,
            region,
            threshold,
            use_grayscale,
            match_method,
            preprocess,
        )

    def is_stable_match(value: MatchResult) -> bool:
        nonlocal consecutive_matches, last_center
        if not value.found:
            consecutive_matches = 0
            last_center = None
            return False
        center = getattr(value, "center_point", None)
        current_center = (
            (int(center[0]), int(center[1]))
            if isinstance(center, (list, tuple)) and len(center) == 2
            else None
        )
        position_is_stable = bool(
            tolerance is None
            or last_center is None
            or (
                current_center is not None
                and abs(current_center[0] - last_center[0]) <= tolerance
                and abs(current_center[1] - last_center[1]) <= tolerance
            )
        )
        consecutive_matches = consecutive_matches + 1 if position_is_stable else 1
        last_center = current_center
        return consecutive_matches >= required_stable_scans

    found, match_result = _poll_sync(
        probe=probe,
        predicate=is_stable_match,
        timeout=timeout,
        interval=interval,
    )
    if found and match_result.found and match_result.center_point is not None:
        found_x, found_y = match_result.center_point
        logger.info("图像找到，位于窗口坐标 (%s, %s)，置信度: %.2f", found_x, found_y, match_result.confidence)
        _check_cancelled()
        app.move_to(found_x, found_y, duration=move_duration)
        _check_cancelled()
        app.click(x=found_x, y=found_y, button=button)
        if float(after_click_sec) > 0:
            _sleep_sync_cancellable(float(after_click_sec))
        logger.info("点击操作完成。")
        return True
    resolved_failure_message = failure_message or f"未能在指定区域找到图像 '{template}'。"
    logger.warning("%s", resolved_failure_message)
    _required_failure(bool(required), resolved_failure_message)
    return False


@action_info(name="find_text_and_click", public=True)
@requires_services(ocr="ocr", app="app")
def find_text_and_click(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str | list[str],
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
    button: str = "left",
    move_duration: float = 0.2,
    timeout: float = 0.0,
    interval: float = 0.2,
    stable_scans: int = 1,
    normalize: bool = False,
    min_confidence: float = 0.0,
    after_click_sec: float = 0.0,
    required: bool = False,
    failure_message: str | None = None,
) -> bool:
    required_stable_scans = max(int(stable_scans), 1)
    consecutive_matches = 0
    last_target: str | None = None

    def probe():
        return find_text(
            app,
            ocr,
            engine,
            text_to_find,
            region,
            match_mode,
            normalize,
            min_confidence,
        )

    def is_stable_match(value) -> bool:
        nonlocal consecutive_matches, last_target
        if not value.found:
            consecutive_matches = 0
            last_target = None
            return False
        matched_target = str(value.debug_info.get("matched_target") or text_to_find)
        if matched_target == last_target:
            consecutive_matches += 1
        else:
            last_target = matched_target
            consecutive_matches = 1
        return consecutive_matches >= required_stable_scans

    found, ocr_result = _poll_sync(
        probe=probe,
        predicate=is_stable_match,
        timeout=timeout,
        interval=interval,
    )
    if found and ocr_result.found and ocr_result.center_point is not None:
        found_x, found_y = ocr_result.center_point
        logger.info("文本找到: '%s'，位于窗口坐标 (%s, %s)，置信度: %.2f", ocr_result.text, found_x, found_y, ocr_result.confidence)
        _check_cancelled()
        app.move_to(found_x, found_y, duration=move_duration)
        _check_cancelled()
        app.click(x=found_x, y=found_y, button=button)
        if float(after_click_sec) > 0:
            _sleep_sync_cancellable(float(after_click_sec))
        logger.info("点击操作完成。")
        return True

    all_recognized_results = ocr_result.debug_info.get("all_recognized_results", [])
    if all_recognized_results:
        recognized_items = [f"{idx}. '{result.text}' (conf={result.confidence:.3f})" for idx, result in enumerate(all_recognized_results, start=1)]
        logger.warning(
            "OCR recognized texts (count=%d): %s",
            len(all_recognized_results),
            " | ".join(recognized_items),
        )
    else:
        logger.warning("OCR recognized no text in current capture.")
    resolved_failure_message = failure_message or f"未能在指定区域找到文本 '{text_to_find}'。"
    logger.warning("%s", resolved_failure_message)
    _required_failure(bool(required), resolved_failure_message)
    return False


@action_info(name="drag_to_find", public=True)
@requires_services(vision="vision", app="app")
def drag_to_find(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    drag_from_template: str,
    drag_to_template: str,
    from_region: tuple[int, int, int, int] | None = None,
    to_region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    duration: float = 0.5,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> bool:
    source_match = find_image(
        app,
        vision,
        engine,
        drag_from_template,
        from_region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    if not source_match.found:
        logger.error("拖拽失败：找不到起点图像 '%s'。", drag_from_template)
        return False
    target_match = find_image(
        app,
        vision,
        engine,
        drag_to_template,
        to_region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    if not target_match.found:
        logger.error("拖拽失败：找不到终点图像 '%s'。", drag_to_template)
        return False
    start_x, start_y = source_match.center_point
    end_x, end_y = target_match.center_point
    logger.info("执行拖拽: 从 %s 到 %s", (start_x, start_y), (end_x, end_y))
    app.drag(start_x, start_y, end_x, end_y, duration=duration)
    return True


@action_info(name="scan_and_find_best_match", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def scan_and_find_best_match(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int],
    priority: str = "top",
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MatchResult:
    logger.info("扫描区域寻找最佳匹配项 '%s'，优先级: %s", template, priority)
    multi_match_result = find_all_images(
        app,
        vision,
        engine,
        template,
        region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    if not multi_match_result.matches:
        logger.warning("在扫描区域内未找到任何匹配项。")
        return MatchResult(found=False)

    matches = multi_match_result.matches
    priority_map = {
        "top": lambda m: m.center_point[1],
        "bottom": lambda m: -m.center_point[1],
        "left": lambda m: m.center_point[0],
        "right": lambda m: -m.center_point[0],
    }
    if priority not in priority_map:
        logger.error("无效的优先级规则: '%s'。", priority)
        return MatchResult(found=False)

    best_match = min(matches, key=priority_map[priority]) if priority in {"top", "left"} else max(matches, key=priority_map[priority])
    logger.info("找到最佳匹配项，位于 %s", best_match.center_point)
    return best_match

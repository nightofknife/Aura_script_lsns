from __future__ import annotations

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from packages.aura_core.utils.exceptions import StopTaskException

from ..services.app_provider_service import AppProviderService
from ..services.interaction_service import InteractionService
from ..services.observation_service import ObservationService
from ..services.ocr_service import OcrService
from ..services.vision_service import MatchResult, VisionService
from ._shared import resolve_template_path
from .vision_actions import find_all_images, find_image


def _click_scope(app, timeout):
    seconds = max(float(timeout), 0.0)
    return OperationScope(None if seconds == 0 else seconds, parent=current_operation_scope(),
                          target=getattr(app, "target_runtime", None))


def _inspect_click(engine, params, *, vision=None, template=None):
    if not engine.root_context.data.get("initial", {}).get("__is_inspect_mode__", False):
        return None

    def annotate(observation, frame):
        debug = getattr(observation.value, "debug_info", None)
        if debug is not None:
            debug.update(source_image=frame.image.copy(), params=params)
            if template is not None:
                try:
                    image = vision.load_template_image(template)
                    if image is None:
                        preview = None
                    elif image.ndim == 2:
                        preview = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                    elif image.ndim == 3 and image.shape[2] == 1:
                        preview = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
                    else:
                        preview = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    debug["template_image"] = preview
                except Exception as exc:
                    logger.warning("Cannot load click template preview: %s", exc)
        return observation

    return annotate


def _required_failure(required: bool, message: str) -> None:
    if bool(required):
        raise StopTaskException(message, success=False)


@action_info(name="find_image_and_click", public=True)
@requires_services(vision="vision", app="app", interaction="plans/aura_base/interaction")
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
    interaction: InteractionService | None = None,
) -> bool:
    scope = _click_scope(app, timeout)
    service = interaction if interaction is not None else InteractionService(app, ObservationService(app, vision=vision))
    try:
        with scope.activate():
            template_path = resolve_template_path(engine, vision, template)
            scope.checkpoint()
            result = service.find_template_and_click(
                template_path, scope=scope, roi=region, threshold=threshold,
                use_grayscale=use_grayscale, match_method=match_method, preprocess=preprocess,
                button=button, move_duration=move_duration, stable_scans=max(int(stable_scans), 1),
                center_tolerance=(None if stable_center_tolerance_px is None else max(int(stable_center_tolerance_px), 0)),
                interval=max(float(interval), .01), max_probes=1 if float(timeout) <= 0 else None,
                after_click_sec=max(float(after_click_sec), 0.0),
                inspect=_inspect_click(engine, {"template": template, "region": region, "threshold": threshold,
                                               "use_grayscale": use_grayscale, "match_method": match_method,
                                               "preprocess": preprocess}, vision=vision, template=template_path),
            )
            scope.checkpoint()
    except OperationTimeout:
        scope.check_cancelled()
    else:
        if result.sent:
            logger.info("图像找到并已发送点击，窗口坐标: %s。", result.observation.client_center)
            return True
    resolved_failure_message = failure_message or f"未能在指定区域找到图像 '{template}'。"
    logger.warning("%s", resolved_failure_message)
    _required_failure(bool(required), resolved_failure_message)
    return False


@action_info(name="find_text_and_click", public=True)
@requires_services(ocr="ocr", app="app", interaction="plans/aura_base/interaction")
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
    recognition_only: bool = False,
    det_resize_long: int | None = None,
    use_textline_orientation: bool = True,
    rotate_tall_crops: bool = False,
    interaction: InteractionService | None = None,
) -> bool:
    scope = _click_scope(app, timeout)
    service = interaction if interaction is not None else InteractionService(app, ObservationService(app, ocr=ocr))
    result = None
    try:
        with scope.activate():
            result = service.find_text_and_click(
                text_to_find, scope=scope, roi=region, match_mode=match_mode, normalize=normalize,
                min_confidence=min_confidence, recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation, rotate_tall_crops=rotate_tall_crops,
                button=button, move_duration=move_duration, stable_scans=max(int(stable_scans), 1),
                interval=max(float(interval), .01), max_probes=1 if float(timeout) <= 0 else None,
                after_click_sec=max(float(after_click_sec), 0.0),
                inspect=_inspect_click(engine, {"text_to_find": text_to_find, "region": region, "match_mode": match_mode,
                                               "normalize": bool(normalize), "min_confidence": float(min_confidence),
                                               "recognition_only": bool(recognition_only), "det_resize_long": det_resize_long,
                                               "use_textline_orientation": bool(use_textline_orientation),
                                               "rotate_tall_crops": bool(rotate_tall_crops)}),
            )
            scope.checkpoint()
    except OperationTimeout:
        scope.check_cancelled()
    else:
        if result.sent:
            logger.info("文本找到并已发送点击，窗口坐标: %s。", result.observation.client_center)
            return True

    ocr_result = result.observation.value if result is not None else None
    all_recognized_results = getattr(ocr_result, "debug_info", {}).get("all_recognized_results", [])
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

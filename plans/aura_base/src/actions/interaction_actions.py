from __future__ import annotations

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger

from ..services.app_provider_service import AppProviderService
from ..services.ocr_service import OcrService
from ..services.vision_service import MatchResult, VisionService
from .ocr_actions import find_text
from .vision_actions import find_all_images, find_image


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
) -> bool:
    match_result = find_image(
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
    if match_result.found:
        found_x, found_y = match_result.center_point
        logger.info("图像找到，位于窗口坐标 (%s, %s)，置信度: %.2f", found_x, found_y, match_result.confidence)
        app.move_to(found_x, found_y, duration=move_duration)
        app.click(x=found_x, y=found_y, button=button)
        logger.info("点击操作完成。")
        return True
    logger.warning("未能在指定区域找到图像 '%s'。", template)
    return False


@action_info(name="find_text_and_click", public=True)
@requires_services(ocr="ocr", app="app")
def find_text_and_click(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
    button: str = "left",
    move_duration: float = 0.2,
) -> bool:
    ocr_result = find_text(app, ocr, engine, text_to_find, region, match_mode)
    if ocr_result.found:
        found_x, found_y = ocr_result.center_point
        logger.info("文本找到: '%s'，位于窗口坐标 (%s, %s)，置信度: %.2f", ocr_result.text, found_x, found_y, ocr_result.confidence)
        app.move_to(found_x, found_y, duration=move_duration)
        app.click(x=found_x, y=found_y, button=button)
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
    logger.warning("未能在指定区域找到文本 '%s'。", text_to_find)
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

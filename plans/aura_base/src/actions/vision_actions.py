from __future__ import annotations

import time
from typing import Any

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException

from ..services.app_provider_service import AppProviderService
from ..services.vision_service import MatchResult, MultiMatchResult, VisionService
from ._shared import expand_template_paths, resolve_template_path


def _offset_match_result(
    match_result: MatchResult,
    *,
    region: tuple[int, int, int, int] | None,
) -> MatchResult:
    if not match_result.found:
        return match_result

    region_x_offset = region[0] if region else 0
    region_y_offset = region[1] if region else 0
    match_result.top_left = (
        match_result.top_left[0] + region_x_offset,
        match_result.top_left[1] + region_y_offset,
    )
    match_result.center_point = (
        match_result.center_point[0] + region_x_offset,
        match_result.center_point[1] + region_y_offset,
    )
    match_result.rect = (
        match_result.rect[0] + region_x_offset,
        match_result.rect[1] + region_y_offset,
        match_result.rect[2],
        match_result.rect[3],
    )
    return match_result


def _offset_multi_match_result(
    multi_match_result: MultiMatchResult,
    *,
    region: tuple[int, int, int, int] | None,
) -> MultiMatchResult:
    for match in multi_match_result.matches:
        _offset_match_result(match, region=region)
    return multi_match_result


@action_info(name="find_image", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_image(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
    mask: str | None = None,
) -> MatchResult:
    is_inspect_mode = engine.root_context.data.get("initial", {}).get("__is_inspect_mode__", False)
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'find_image' 失败：无法截图。")
        return MatchResult(found=False)

    source_image_for_debug = capture.image.copy()
    template_path = resolve_template_path(engine, vision, template)
    mask_path = resolve_template_path(engine, vision, mask) if mask else None

    match_result = vision.find_template(
        source_image=source_image_for_debug,
        template_image=template_path,
        mask_image=mask_path,
        threshold=threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )

    _offset_match_result(match_result, region=region)

    if is_inspect_mode:
        try:
            template_image_for_debug = cv2.imread(template_path)
            match_result.debug_info.update(
                {
                    "source_image": source_image_for_debug,
                    "template_image": template_image_for_debug,
                    "params": {
                        "template": template,
                        "region": region,
                        "threshold": threshold,
                        "use_grayscale": use_grayscale,
                        "match_method": match_method,
                        "preprocess": preprocess,
                    },
                }
            )
        except Exception as exc:
            logger.error("打包调试信息时出错: %s", exc)

    return match_result


@action_info(name="find_all_images", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_all_images(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MultiMatchResult:
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'find_all_images' 失败：无法截图。")
        return MultiMatchResult()

    template_path = resolve_template_path(engine, vision, template)
    multi_match_result = vision.find_all_templates(
        source_image=capture.image,
        template_image=template_path,
        threshold=threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )

    return _offset_multi_match_result(multi_match_result, region=region)


@action_info(name="find_best_image", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_best_image(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MatchResult:
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'find_best_image' 失败：无法截图。")
        return MatchResult(found=False)

    template_path = resolve_template_path(engine, vision, template)
    match_result = vision.find_template(
        source_image=capture.image,
        template_image=template_path,
        threshold=threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )

    return _offset_match_result(match_result, region=region)


@action_info(name="find_templates_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_templates_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> dict[str, Any]:
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'find_templates_in_set' 失败：无法截图。")
        return {"count": 0, "matches": []}

    template_paths = expand_template_paths(engine, vision, templates_ref)
    matches: list[dict[str, Any]] = []
    template_images = [str(path) for path in template_paths]
    match_results = vision.find_templates_batch(
        source_image=capture.image,
        template_images=template_images,
        threshold=threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )

    for template_path, match_result in zip(template_paths, match_results):
        if not match_result.found:
            continue
        _offset_match_result(match_result, region=region)
        matches.append({"template": str(template_path), "match": match_result})

    return {"count": len(matches), "matches": matches}


@action_info(name="find_all_templates_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_all_templates_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    nms_threshold: float = 0.5,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> dict[str, Any]:
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("Action 'find_all_templates_in_set' failed: capture failed.")
        return {"count": 0, "matches": []}

    template_paths = expand_template_paths(engine, vision, templates_ref)
    matches: list[dict[str, Any]] = []
    template_images = [str(path) for path in template_paths]
    batch_results = vision.find_all_templates_batch(
        source_image=capture.image,
        template_images=template_images,
        threshold=threshold,
        nms_threshold=nms_threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )

    for template_path, multi_match_result in zip(template_paths, batch_results):
        _offset_multi_match_result(multi_match_result, region=region)
        for match_result in multi_match_result.matches:
            matches.append({"template": str(template_path), "match": match_result})

    return {"count": len(matches), "matches": matches}


@action_info(name="find_unique_template_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_unique_template_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MatchResult:
    result = find_templates_in_set(
        app,
        vision,
        engine,
        templates_ref,
        region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    if result["count"] == 1:
        return result["matches"][0]["match"]
    if result["count"] > 1:
        logger.warning("Action 'find_unique_template_in_set' matched multiple templates in '%s'.", templates_ref)
    return MatchResult(found=False)


@action_info(name="find_best_template_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def find_best_template_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> dict[str, Any]:
    result = find_templates_in_set(
        app,
        vision,
        engine,
        templates_ref,
        region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    matches = result["matches"]
    if not matches:
        return {"template": None, "match": MatchResult(found=False)}
    return max(matches, key=lambda item: item["match"].confidence)


@action_info(name="list_templates_in_set", read_only=True, public=True)
@requires_services(vision="vision")
def list_templates_in_set(vision: VisionService, engine: ExecutionEngine, templates_ref: str) -> dict[str, Any]:
    template_paths = expand_template_paths(engine, vision, templates_ref)
    return {"count": len(template_paths), "templates": [str(path) for path in template_paths]}


@action_info(name="assert_any_template_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def assert_any_template_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    message: str | None = None,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
):
    result = find_templates_in_set(
        app,
        vision,
        engine,
        templates_ref,
        region,
        threshold,
        use_grayscale,
        match_method,
        preprocess,
    )
    if result["count"] == 0:
        error_message = message or f"Assertion failed: none of the templates in '{templates_ref}' were found."
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)
    logger.info("Assertion passed: found %s template(s) in '%s'.", result["count"], templates_ref)
    return True


@action_info(name="assert_all_templates_in_set", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def assert_all_templates_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    message: str | None = None,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
):
    template_paths = expand_template_paths(engine, vision, templates_ref)
    if not template_paths:
        error_message = message or f"Assertion failed: template set '{templates_ref}' resolved to no files."
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)

    capture = app.capture(rect=region)
    if not capture.success:
        error_message = message or "Assertion failed: capture failed while checking template set."
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)

    template_images = [str(path) for path in template_paths]
    match_results = vision.find_templates_batch(
        source_image=capture.image,
        template_images=template_images,
        threshold=threshold,
        use_grayscale=use_grayscale,
        match_method=match_method,
        preprocess=preprocess,
    )
    missing_templates = [
        str(template_path)
        for template_path, match_result in zip(template_paths, match_results)
        if not match_result.found
    ]

    if missing_templates:
        error_message = message or (
            f"Assertion failed: {len(missing_templates)} template(s) missing in '{templates_ref}'."
        )
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)

    logger.info("Assertion passed: all templates in '%s' were found.", templates_ref)
    return True


@action_info(name="check_image_exists", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def check_image_exists(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
    mask: str | None = None,
) -> bool:
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
        mask,
    ).found


@action_info(name="assert_image_exists", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def assert_image_exists(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    message: str | None = None,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
    mask: str | None = None,
):
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
        mask,
    )
    if not match_result.found:
        error_message = message or f"断言失败：期望的图像 '{template}' 不存在。"
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)
    logger.info("断言成功：图像 '%s' 已确认存在。", template)
    return True


@action_info(name="assert_image_not_exists", read_only=True, public=True)
@requires_services(vision="vision", app="app")
def assert_image_not_exists(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    message: str | None = None,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
):
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
        error_message = message or f"断言失败：不期望的图像 '{template}' 却存在了。"
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)
    logger.info("断言成功：图像 '%s' 已确认不存在。", template)
    return True


@action_info(name="find_image_in_scrolling_area", public=True)
@requires_services(vision="vision", app="app")
def find_image_in_scrolling_area(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    scroll_area: tuple[int, int, int, int],
    scroll_direction: str = "down",
    max_scrolls: int = 5,
    scroll_amount: int = 200,
    threshold: float = 0.8,
    delay_after_scroll: float = 0.5,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MatchResult:
    logger.info("在可滚动区域 %s 中查找 '%s'，最多滚动 %s 次。", scroll_area, template, max_scrolls)
    direction_map = {"up": 1, "down": -1}
    if scroll_direction.lower() not in direction_map:
        logger.error("无效的滚动方向: '%s'。", scroll_direction)
        return MatchResult(found=False)

    scroll_val = scroll_amount * direction_map[scroll_direction.lower()]
    scroll_center_x = scroll_area[0] + scroll_area[2] // 2
    scroll_center_y = scroll_area[1] + scroll_area[3] // 2
    app.move_to(scroll_center_x, scroll_center_y, duration=0.1)

    for i in range(max_scrolls + 1):
        if i > 0:
            logger.info("第 %s 次滚动...", i)
            app.scroll(scroll_val)
            time.sleep(delay_after_scroll)
        match_result = find_image(
            app,
            vision,
            engine,
            template,
            region=scroll_area,
            threshold=threshold,
            use_grayscale=use_grayscale,
            match_method=match_method,
            preprocess=preprocess,
        )
        if match_result.found:
            logger.info("在第 %s 次滚动后找到图像！", i)
            return match_result

    logger.warning("在滚动 %s 次后，仍未找到图像 '%s'。", max_scrolls, template)
    return MatchResult(found=False)

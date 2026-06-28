from __future__ import annotations

import asyncio
from asyncio import CancelledError
import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger

from ..services.app_provider_service import AppProviderService
from ..services.ocr_service import OcrResult, OcrService
from ..services.vision_service import MatchResult, VisionService
from ._shared import poll_until
from .ocr_actions import find_text
from .vision_actions import find_image, find_templates_in_set


@action_info(name="wait_for_any_template_in_set", public=True)
@requires_services(vision="vision", app="app")
async def wait_for_any_template_in_set(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> dict[str, object]:
    logger.info("Waiting for any template in '%s' (timeout=%s).", templates_ref, timeout)
    found, result = await poll_until(
        timeout=timeout,
        interval=interval,
        probe=lambda: find_templates_in_set(
            app,
            vision,
            engine,
            templates_ref,
            region,
            threshold,
            use_grayscale,
            match_method,
            preprocess,
        ),
        predicate=lambda value: value["count"] > 0,
    )
    if found:
        best_match = max(result["matches"], key=lambda item: item["match"].confidence)
        logger.info("Found template '%s' in '%s'.", best_match["template"], templates_ref)
        return best_match
    logger.warning("Timeout waiting for templates in '%s'.", templates_ref)
    return {"template": None, "match": MatchResult(found=False)}


@action_info(name="wait_for_templates_in_set_to_disappear", public=True)
@requires_services(vision="vision", app="app")
async def wait_for_templates_in_set_to_disappear(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    templates_ref: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> bool:
    logger.info("Waiting for templates in '%s' to disappear (timeout=%s).", templates_ref, timeout)
    disappeared, _ = await poll_until(
        timeout=timeout,
        interval=interval,
        probe=lambda: find_templates_in_set(
            app,
            vision,
            engine,
            templates_ref,
            region,
            threshold,
            use_grayscale,
            match_method,
            preprocess,
        ),
        predicate=lambda value: value["count"] == 0,
    )
    if disappeared:
        logger.info("Templates in '%s' disappeared.", templates_ref)
        return True
    logger.warning("Timeout waiting for templates in '%s' to disappear.", templates_ref)
    return False


@action_info(name="wait_for_text", public=True)
@requires_services(ocr="ocr", app="app")
async def wait_for_text(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
) -> OcrResult:
    logger.info("开始等待文本 '%s' 出现，最长等待 %s 秒...", text_to_find, timeout)
    found, ocr_result = await poll_until(
        timeout=timeout,
        interval=interval,
        probe=lambda: find_text(app, ocr, engine, text_to_find, region, match_mode),
        predicate=lambda value: value.found,
    )
    if found:
        logger.info("成功等到文本 '%s'！", ocr_result.text)
        return ocr_result
    logger.warning("超时 %s 秒，未能等到文本 '%s'。", timeout, text_to_find)
    return OcrResult(found=False)


@action_info(name="wait_for_text_to_disappear", public=True)
@requires_services(ocr="ocr", app="app")
async def wait_for_text_to_disappear(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_monitor: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
) -> bool:
    logger.info("开始等待文本 '%s' 消失，最长等待 %s 秒...", text_to_monitor, timeout)
    disappeared, _ = await poll_until(
        timeout=timeout,
        interval=interval,
        probe=lambda: find_text(app, ocr, engine, text_to_monitor, region, match_mode),
        predicate=lambda value: not value.found,
    )
    if disappeared:
        logger.info("文本 '%s' 已消失。等待成功！", text_to_monitor)
        return True
    logger.warning("超时 %s 秒，文本 '%s' 仍然存在。", timeout, text_to_monitor)
    return False


@action_info(name="wait_for_image", public=True)
@requires_services(vision="vision", app="app")
async def wait_for_image(
    app: AppProviderService,
    vision: VisionService,
    engine: ExecutionEngine,
    template: str,
    timeout: float = 10.0,
    interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None,
    threshold: float = 0.8,
    use_grayscale: bool = True,
    match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none",
) -> MatchResult:
    logger.info("开始等待图像 '%s' 出现，最长等待 %s 秒...", template, timeout)
    found, match_result = await poll_until(
        timeout=timeout,
        interval=interval,
        probe=lambda: find_image(
            app,
            vision,
            engine,
            template,
            region,
            threshold,
            use_grayscale,
            match_method,
            preprocess,
        ),
        predicate=lambda value: value.found,
    )
    if found:
        logger.info("成功等到图像 '%s'！", template)
        return match_result
    logger.warning("超时 %s 秒，未能等到图像 '%s'。", timeout, template)
    return MatchResult(found=False)


@action_info(name="sleep", read_only=True, public=True)
async def sleep(seconds: float):
    duration = max(float(seconds), 0.0)
    try:
        await asyncio.sleep(duration)
        return True
    except CancelledError:
        logger.info("sleep action cancelled seconds=%s", duration)
        raise

from __future__ import annotations

import re
from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException

from ..services.app_provider_service import AppProviderService
from ..services.ocr_service import MultiOcrResult, OcrResult, OcrService


def _offset_ocr_result(
    ocr_result: OcrResult,
    *,
    region: tuple[int, int, int, int] | None,
) -> OcrResult:
    if not ocr_result.found:
        return ocr_result

    region_x_offset = region[0] if region else 0
    region_y_offset = region[1] if region else 0
    ocr_result.center_point = (
        ocr_result.center_point[0] + region_x_offset,
        ocr_result.center_point[1] + region_y_offset,
    )
    ocr_result.rect = (
        ocr_result.rect[0] + region_x_offset,
        ocr_result.rect[1] + region_y_offset,
        ocr_result.rect[2],
        ocr_result.rect[3],
    )
    return ocr_result


def _offset_multi_ocr_result(
    multi_ocr_result: MultiOcrResult,
    *,
    region: tuple[int, int, int, int] | None,
) -> MultiOcrResult:
    region_x_offset = region[0] if region else 0
    region_y_offset = region[1] if region else 0
    for result in multi_ocr_result.results:
        result.center_point = (
            result.center_point[0] + region_x_offset,
            result.center_point[1] + region_y_offset,
        )
        result.rect = (
            result.rect[0] + region_x_offset,
            result.rect[1] + region_y_offset,
            result.rect[2],
            result.rect[3],
        )
    return multi_ocr_result


@action_info(name="preload_ocr", read_only=True, public=True)
@requires_services(ocr="ocr")
def preload_ocr(ocr: OcrService, warmup: bool = False) -> dict[str, Any]:
    device = ocr.preload_engine(warmup=warmup)
    return {
        "ok": True,
        "device": device,
        "warmed": bool(warmup),
        "backend": ocr.get_backend(),
        "provider": ocr.get_provider(),
        "model": ocr.get_model(),
    }


@action_info(name="find_text", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def find_text(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "exact",
) -> OcrResult:
    is_inspect_mode = engine.root_context.data.get("initial", {}).get("__is_inspect_mode__", False)
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'find_text' 失败：无法截图。")
        return OcrResult(found=False)

    source_image_for_debug = capture.image.copy()
    ocr_result = ocr.find_text(source_image=source_image_for_debug, text_to_find=text_to_find, match_mode=match_mode)

    _offset_ocr_result(ocr_result, region=region)

    if is_inspect_mode:
        ocr_result.debug_info.update(
            {
                "source_image": source_image_for_debug,
                "params": {
                    "text_to_find": text_to_find,
                    "region": region,
                    "match_mode": match_mode,
                },
            }
        )

    return ocr_result


@action_info(name="recognize_all_text", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def recognize_all_text(
    app: AppProviderService,
    ocr: OcrService,
    region: tuple[int, int, int, int] | None = None,
) -> MultiOcrResult:
    capture = app.capture(rect=region)
    if not capture.success:
        logger.error("行为 'recognize_all_text' 失败：无法截图。")
        return MultiOcrResult()
    multi_ocr_result = ocr.recognize_all(source_image=capture.image)
    return _offset_multi_ocr_result(multi_ocr_result, region=region)


@action_info(name="get_text_in_region", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def get_text_in_region(
    app: AppProviderService,
    ocr: OcrService,
    region: tuple[int, int, int, int],
    whitelist: str | None = None,
    join_with: str = " ",
) -> str:
    logger.info("正在读取区域 %s 内的文本...", region)
    multi_ocr_result = recognize_all_text(app, ocr, region)
    if not multi_ocr_result.results:
        return ""
    detected_texts = [res.text for res in multi_ocr_result.results]
    if whitelist:
        pattern = f"[^{re.escape(whitelist)}]"
        cleaned_texts = [re.sub(r"[\n\r]", "", txt) for txt in detected_texts]
        filtered_texts = [re.sub(pattern, "", txt) for txt in cleaned_texts]
    else:
        filtered_texts = detected_texts
    result = join_with.join(filtered_texts)
    logger.info("识别并处理后的文本: '%s'", result)
    return result


@action_info(name="check_text_exists", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def check_text_exists(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "exact",
) -> bool:
    return find_text(app, ocr, engine, text_to_find, region, match_mode).found


@action_info(name="assert_text_exists", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def assert_text_exists(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
    message: str | None = None,
):
    ocr_result = find_text(app, ocr, engine, text_to_find, region, match_mode)
    if not ocr_result.found:
        error_message = message or f"断言失败：期望的文本 '{text_to_find}' 不存在。"
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)
    logger.info("断言成功：文本 '%s' 已确认存在。", text_to_find)
    return True


@action_info(name="assert_text_not_exists", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def assert_text_not_exists(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    region: tuple[int, int, int, int] | None = None,
    match_mode: str = "contains",
    message: str | None = None,
):
    ocr_result = find_text(app, ocr, engine, text_to_find, region, match_mode)
    if ocr_result.found:
        error_message = message or f"断言失败：不期望的文本 '{ocr_result.text}' 却存在了。"
        logger.error(error_message)
        raise StopTaskException(error_message, success=False)
    logger.info("断言成功：文本 '%s' 已确认不存在。", text_to_find)
    return True


@action_info(name="assert_text_equals", read_only=True, public=True)
@requires_services(ocr="ocr", app="app")
def assert_text_equals(
    app: AppProviderService,
    ocr: OcrService,
    engine: ExecutionEngine,
    text_to_find: str,
    expected_value: str,
    region: tuple[int, int, int, int] | None = None,
    message: str | None = None,
):
    ocr_result = find_text(app, ocr, engine, text_to_find, region, match_mode="exact")
    if not ocr_result.found:
        error_message = message or f"断言失败：期望的文本 '{text_to_find}' 不存在。"
        raise StopTaskException(error_message, success=False)
    if ocr_result.text != expected_value:
        error_message = message or f"断言失败：文本内容不匹配。期望: '{expected_value}', 实际: '{ocr_result.text}'。"
        raise StopTaskException(error_message, success=False)
    logger.info("断言成功：文本 '%s' 内容符合预期。", ocr_result.text)
    return True

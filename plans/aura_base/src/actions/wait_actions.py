"""Compatibility wait actions backed by the public observation service."""
from __future__ import annotations

import asyncio
import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.engine import ExecutionEngine
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope
from ..services.app_provider_service import AppProviderService
from ..services.observation_service import Observation, ObservationService
from ..services.ocr_service import OcrResult, OcrService
from ..services.vision_service import MatchResult, VisionService
from ._shared import expand_template_paths, resolve_template_path


def _service(observation, app, vision=None, ocr=None):
    # Direct Python callers share their injected backends through this facade.
    return observation if observation is not None else ObservationService(app, vision, ocr)


def _inspect(result, frame, engine, params, template=None, vision=None):
    if not engine.root_context.data.get("initial", {}).get("__is_inspect_mode__", False):
        return result
    debug = getattr(result.value, "debug_info", None)
    if debug is not None:
        debug.update(source_image=frame.image.copy(), params=params)
        if template is not None and vision is not None:
            try:
                image = vision.load_template_image(template)
                debug["template_image"] = (cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                                           if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
            except Exception as exc:
                logger.warning("Cannot load wait template preview: %s", exc)
    return result


def _wait_scope(app, timeout):
    seconds = max(float(timeout), 0.0)
    return OperationScope(None if seconds == 0 else seconds, parent=current_operation_scope(),
                          target=getattr(app, "target_runtime", None))


async def _wait(app, service, probe, *, timeout, interval, stable_scans,
                state="present", center_tolerance=None, roi=None, scope=None):
    seconds = max(float(timeout), 0.0)
    scope = _wait_scope(app, timeout) if scope is None else scope
    return await service.wait_async(
        lambda: service.observe(lambda frame: probe(frame, scope), scope=scope, roi=roi),
        lambda result: result.state == state,
        scope=scope, interval=max(float(interval), 0.0),
        stable_scans=max(int(stable_scans), 1),
        center_tolerance=None if center_tolerance is None else max(int(center_tolerance), 0),
        max_probes=1 if seconds == 0 else None,
    )


def _template_set(service, frame, paths, *, scope, **options):
    observations = service.match_templates(frame, paths, scope=scope, **options)
    matches = [{"template": str(path), "match": item.value}
               for path, item in zip(paths, observations) if item.state == "present"]
    best = max(matches, key=lambda item: item["match"].confidence) if matches else None
    state = "present" if best else (
        "unknown" if any(item.state == "unknown" for item in observations) else "absent")
    return Observation(
        state=state, frame_id=frame.id, value={"count": len(matches), "matches": matches},
        semantic_key=best["template"] if best else state,
        client_center=best["match"].center_point if best else None,
        reason=next((item.reason for item in observations if item.state == "unknown"), None),
    )


@action_info(name="wait_for_any_template_in_set", public=True)
@requires_services(vision="vision", app="app", observation="plans/aura_base/observation")
async def wait_for_any_template_in_set(
    app: AppProviderService, vision: VisionService, engine: ExecutionEngine,
    templates_ref: str, timeout: float = 10.0, interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None, threshold: float = 0.8,
    use_grayscale: bool = True, match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none", stable_scans: int = 1,
    stable_center_tolerance_px: int | None = None,
    observation: ObservationService | None = None,
) -> dict[str, object]:
    scope = _wait_scope(app, timeout)
    service = _service(observation, app, vision=vision)
    paths = expand_template_paths(engine, vision, templates_ref)
    if not paths:
        raise ValueError(f"Template set is empty: {templates_ref}")
    result = await _wait(
        app, service, lambda frame, scope: _template_set(
            service, frame, paths, scope=scope, threshold=threshold,
            use_grayscale=use_grayscale, match_method=match_method, preprocess=preprocess),
        timeout=timeout, interval=interval, stable_scans=stable_scans,
        center_tolerance=stable_center_tolerance_px, roi=region, scope=scope,
    )
    if result.matched:
        return max(result.value["matches"], key=lambda item: item["match"].confidence)
    return {"template": None, "match": MatchResult(found=False)}


@action_info(name="wait_for_templates_in_set_to_disappear", public=True)
@requires_services(vision="vision", app="app", observation="plans/aura_base/observation")
async def wait_for_templates_in_set_to_disappear(
    app: AppProviderService, vision: VisionService, engine: ExecutionEngine,
    templates_ref: str, timeout: float = 10.0, interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None, threshold: float = 0.8,
    use_grayscale: bool = True, match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none", stable_scans: int = 1,
    observation: ObservationService | None = None,
) -> bool:
    scope = _wait_scope(app, timeout)
    service = _service(observation, app, vision=vision)
    paths = expand_template_paths(engine, vision, templates_ref)
    if not paths:
        raise ValueError(f"Template set is empty: {templates_ref}")
    result = await _wait(
        app, service, lambda frame, scope: _template_set(
            service, frame, paths, scope=scope, threshold=threshold,
            use_grayscale=use_grayscale, match_method=match_method, preprocess=preprocess),
        state="absent", timeout=timeout, interval=interval, stable_scans=stable_scans, roi=region, scope=scope,
    )
    return result.matched


@action_info(name="wait_for_text", public=True)
@requires_services(ocr="ocr", app="app", observation="plans/aura_base/observation")
async def wait_for_text(
    app: AppProviderService, ocr: OcrService, engine: ExecutionEngine,
    text_to_find: str | list[str], timeout: float = 10.0, interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None, match_mode: str = "contains",
    stable_scans: int = 1, normalize: bool = False, min_confidence: float = 0.0,
    recognition_only: bool = False, det_resize_long: int | None = None,
    use_textline_orientation: bool = True,
    observation: ObservationService | None = None,
    rotate_tall_crops: bool = False,
) -> OcrResult:
    service = _service(observation, app, ocr=ocr)
    options = dict(match_mode=match_mode, normalize=normalize, min_confidence=min_confidence,
                   recognition_only=recognition_only, det_resize_long=det_resize_long,
                   use_textline_orientation=use_textline_orientation,
                   rotate_tall_crops=rotate_tall_crops)
    result = await _wait(
        app, service, lambda frame, scope: _inspect(
            service.find_text(frame, text_to_find, scope=scope, **options),
            frame, engine, dict(options, text_to_find=text_to_find, region=region)),
        timeout=timeout, interval=interval, stable_scans=stable_scans, roi=region,
    )
    return result.value if result.matched else OcrResult(found=False)


@action_info(name="wait_for_text_to_disappear", public=True)
@requires_services(ocr="ocr", app="app", observation="plans/aura_base/observation")
async def wait_for_text_to_disappear(
    app: AppProviderService, ocr: OcrService, engine: ExecutionEngine,
    text_to_monitor: str | list[str], timeout: float = 10.0, interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None, match_mode: str = "contains",
    stable_scans: int = 1, normalize: bool = False, min_confidence: float = 0.0,
    recognition_only: bool = False, det_resize_long: int | None = None,
    use_textline_orientation: bool = True,
    observation: ObservationService | None = None,
    rotate_tall_crops: bool = False,
) -> bool:
    service = _service(observation, app, ocr=ocr)
    result = await _wait(
        app, service, lambda frame, scope: service.find_text(
            frame, text_to_monitor, scope=scope, match_mode=match_mode,
            normalize=normalize, min_confidence=min_confidence,
            recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops),
        state="absent", timeout=timeout, interval=interval, stable_scans=stable_scans, roi=region,
    )
    return result.matched


@action_info(name="wait_for_image", public=True)
@requires_services(vision="vision", app="app", observation="plans/aura_base/observation")
async def wait_for_image(
    app: AppProviderService, vision: VisionService, engine: ExecutionEngine,
    template: str, timeout: float = 10.0, interval: float = 1.0,
    region: tuple[int, int, int, int] | None = None, threshold: float = 0.8,
    use_grayscale: bool = True, match_method: int = cv2.TM_CCOEFF_NORMED,
    preprocess: str = "none", stable_scans: int = 1,
    stable_center_tolerance_px: int | None = None,
    observation: ObservationService | None = None,
) -> MatchResult:
    scope = _wait_scope(app, timeout)
    service = _service(observation, app, vision=vision)
    path = resolve_template_path(engine, vision, template)
    options = dict(threshold=threshold, use_grayscale=use_grayscale,
                   match_method=match_method, preprocess=preprocess)
    result = await _wait(
        app, service, lambda frame, scope: _inspect(
            service.match_template(frame, path, scope=scope, **options),
            frame, engine, dict(options, template=template, region=region), path, vision),
        timeout=timeout, interval=interval, stable_scans=stable_scans,
        center_tolerance=stable_center_tolerance_px, roi=region, scope=scope,
    )
    return result.value if result.matched else MatchResult(found=False)


@action_info(name="sleep", read_only=True, public=True)
async def sleep(seconds: float):
    try:
        await asyncio.sleep(max(float(seconds), 0.0))
        return True
    except asyncio.CancelledError:
        logger.info("sleep action cancelled seconds=%s", seconds)
        raise

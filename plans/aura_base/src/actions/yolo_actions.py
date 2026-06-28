# -*- coding: utf-8 -*-

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.services import YoloService
from ..services.app_provider_service import AppProviderService
from ..services.controller_service import ControllerService
from ._shared import poll_until


def _normalize_roi(roi: Optional[Sequence[int]]) -> Optional[Tuple[int, int, int, int]]:
    if roi is None:
        return None
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        raise ValueError("roi must be [x, y, w, h]")
    x, y, w, h = roi
    return int(x), int(y), int(w), int(h)


def _normalize_point(point: Optional[Sequence[int]]) -> Optional[Tuple[int, int]]:
    if point is None:
        return None
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError("point must be [x, y]")
    return int(point[0]), int(point[1])


def _normalize_offset(offset: Optional[Sequence[int]]) -> Tuple[int, int]:
    if offset is None:
        return 0, 0
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        raise ValueError("offset must be [dx, dy]")
    return int(offset[0]), int(offset[1])


def _filter_by_labels(
    detections: List[Dict[str, Any]],
    target_labels: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    if not target_labels:
        return detections
    wanted = {str(label).lower() for label in target_labels if label}
    return [det for det in detections if str(det.get("label", "")).lower() in wanted]


def _filter_by_min_confidence(
    detections: List[Dict[str, Any]],
    min_confidence: float,
) -> List[Dict[str, Any]]:
    threshold = float(min_confidence)
    return [det for det in detections if float(det.get("score", 0.0)) >= threshold]


def _get_bbox_center(det: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    bbox = det.get("bbox_global")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    x, y, w, h = [int(v) for v in bbox]
    return int(x + w / 2), int(y + h / 2)


def _sort_detections(
    detections: List[Dict[str, Any]],
    sort_mode: str,
    anchor_point: Optional[Tuple[int, int]] = None,
) -> List[Dict[str, Any]]:
    if not detections:
        return []

    mode = (sort_mode or "highest_confidence").lower()
    if mode == "highest_confidence":
        return sorted(detections, key=lambda det: float(det.get("score", 0.0)), reverse=True)
    if mode == "left_to_right":
        return sorted(detections, key=lambda det: (det.get("bbox_global", [0, 0, 0, 0])[0], -float(det.get("score", 0.0))))
    if mode == "right_to_left":
        return sorted(detections, key=lambda det: (-det.get("bbox_global", [0, 0, 0, 0])[0], -float(det.get("score", 0.0))))
    if mode == "top_to_bottom":
        return sorted(detections, key=lambda det: (det.get("bbox_global", [0, 0, 0, 0])[1], -float(det.get("score", 0.0))))
    if mode == "bottom_to_top":
        return sorted(detections, key=lambda det: (-det.get("bbox_global", [0, 0, 0, 0])[1], -float(det.get("score", 0.0))))
    if mode == "nearest_to_point":
        if anchor_point is None:
            raise ValueError("anchor_point is required when sort_mode is nearest_to_point")

        ax, ay = anchor_point

        def dist_sq(det: Dict[str, Any]) -> float:
            center = _get_bbox_center(det)
            if center is None:
                return float("inf")
            dx = center[0] - ax
            dy = center[1] - ay
            return float(dx * dx + dy * dy)

        return sorted(detections, key=lambda det: (dist_sq(det), -float(det.get("score", 0.0))))

    raise ValueError(f"Unsupported sort_mode: {sort_mode}")


def _find_target_detection(
    yolo: YoloService,
    app: AppProviderService,
    target_labels: Optional[Sequence[str]] = None,
    roi: Optional[Tuple[int, int, int, int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "highest_confidence",
    anchor_point: Optional[Tuple[int, int]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    result = yolo.detect_on_screen(
        app=app,
        roi=roi,
        model_name=model_name,
        options=options,
    )
    if not result.get("ok"):
        return result, None

    detections = result.get("detections", [])
    detections = _filter_by_labels(detections, target_labels)
    detections = _filter_by_min_confidence(detections, min_confidence)
    detections = _sort_detections(detections, sort_mode=sort_mode, anchor_point=anchor_point)

    return result, (detections[0] if detections else None)


@action_info(name="yolo_preload_model", public=True)
@requires_services(yolo="core/yolo")
def yolo_preload_model(
    yolo: YoloService,
    model_name: str,
) -> Dict[str, Any]:
    """Preload a YOLO model by name or path."""
    return yolo.preload_model(model_name)


@action_info(name="yolo_set_active_model", public=True)
@requires_services(yolo="core/yolo")
def yolo_set_active_model(
    yolo: YoloService,
    model_name: str,
) -> Dict[str, Any]:
    """Set current active YOLO model by name or path."""
    return yolo.set_active_model(model_name)


@action_info(name="yolo_unload_model", public=True)
@requires_services(yolo="core/yolo")
def yolo_unload_model(
    yolo: YoloService,
    model_name: str,
) -> Dict[str, Any]:
    """Unload a YOLO model from memory."""
    return yolo.unload_model(model_name)


@action_info(name="yolo_list_loaded_models", read_only=True, public=True)
@requires_services(yolo="core/yolo")
def yolo_list_loaded_models(
    yolo: YoloService,
) -> List[str]:
    """List currently loaded YOLO model keys."""
    return yolo.list_loaded_models()


@action_info(name="yolo_get_active_model", read_only=True, public=True)
@requires_services(yolo="core/yolo")
def yolo_get_active_model(
    yolo: YoloService,
) -> Optional[str]:
    """Get current active YOLO model key."""
    return yolo.get_active_model()


@action_info(name="yolo_get_class_names", read_only=True, public=True)
@requires_services(yolo="core/yolo")
def yolo_get_class_names(
    yolo: YoloService,
    model_name: Optional[str] = None,
) -> Dict[int, str]:
    """Get class id -> label mapping from YOLO model."""
    return yolo.get_class_names(model_name=model_name)


@action_info(name="yolo_resolve_class_ids", read_only=True, public=True)
@requires_services(yolo="core/yolo")
def yolo_resolve_class_ids(
    yolo: YoloService,
    labels: List[str],
    model_name: Optional[str] = None,
) -> List[int]:
    """Resolve label names to class ids."""
    return yolo.resolve_class_ids(labels=labels, model_name=model_name)


@action_info(name="yolo_detect_on_screen", read_only=True, public=True)
@requires_services(yolo="core/yolo", app="app")
def yolo_detect_on_screen(
    yolo: YoloService,
    app: AppProviderService,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run YOLO detection on current screen (or ROI)."""
    return yolo.detect_on_screen(
        app=app,
        roi=_normalize_roi(roi),
        model_name=model_name,
        options=options,
    )


@action_info(name="yolo_detect_image", read_only=True, public=True)
@requires_services(yolo="core/yolo")
def yolo_detect_image(
    yolo: YoloService,
    image_path: str,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run YOLO detection on a local image path."""
    return yolo.detect_image(
        image=image_path,
        model_name=model_name,
        options=options,
    )


@action_info(name="yolo_count_targets", read_only=True, public=True)
@requires_services(yolo="core/yolo", app="app")
def yolo_count_targets(
    yolo: YoloService,
    app: AppProviderService,
    target_labels: Optional[List[str]] = None,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
) -> int:
    """Count targets on screen, optionally filtered by labels and confidence."""
    result = yolo.detect_on_screen(
        app=app,
        roi=_normalize_roi(roi),
        model_name=model_name,
        options=options,
    )
    if not result.get("ok"):
        return 0
    detections = _filter_by_labels(result.get("detections", []), target_labels)
    detections = _filter_by_min_confidence(detections, min_confidence)
    return len(detections)


@action_info(name="yolo_find_target", read_only=True, public=True)
@requires_services(yolo="core/yolo", app="app")
def yolo_find_target(
    yolo: YoloService,
    app: AppProviderService,
    target_labels: Optional[List[str]] = None,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "highest_confidence",
    anchor_point: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Find a single target by composite strategy (filter + sort)."""
    anchor = _normalize_point(anchor_point)
    result, selected = _find_target_detection(
        yolo=yolo,
        app=app,
        target_labels=target_labels,
        roi=_normalize_roi(roi),
        model_name=model_name,
        options=options,
        min_confidence=min_confidence,
        sort_mode=sort_mode,
        anchor_point=anchor,
    )
    if not result.get("ok") or selected is None:
        return {
            "found": False,
            "model": result.get("model"),
            "sort_mode": sort_mode,
            "target_labels": target_labels or [],
        }

    center = _get_bbox_center(selected)
    return {
        "found": center is not None,
        "model": result.get("model"),
        "sort_mode": sort_mode,
        "target_labels": target_labels or [],
        "detection": selected,
        "center_point": list(center) if center else None,
    }


@action_info(name="yolo_wait_for_target", read_only=True, public=True)
@requires_services(yolo="core/yolo", app="app")
async def yolo_wait_for_target(
    yolo: YoloService,
    app: AppProviderService,
    target_labels: Optional[List[str]] = None,
    timeout_sec: float = 10.0,
    poll_interval_sec: float = 0.3,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "highest_confidence",
    anchor_point: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Wait until target appears and return the selected detection."""
    started = time.time()
    found_ok, found = await poll_until(
        timeout=max(float(timeout_sec), 0.0),
        interval=max(float(poll_interval_sec), 0.01),
        probe=lambda: yolo_find_target(
            yolo=yolo,
            app=app,
            target_labels=target_labels,
            roi=roi,
            model_name=model_name,
            options=options,
            min_confidence=min_confidence,
            sort_mode=sort_mode,
            anchor_point=anchor_point,
        ),
        predicate=lambda value: bool(value.get("found")),
    )
    if found_ok:
        found["elapsed_sec"] = float(time.time() - started)
        return found

    return {
        "found": False,
        "target_labels": target_labels or [],
        "sort_mode": sort_mode,
        "elapsed_sec": float(time.time() - started),
    }


@action_info(name="yolo_wait_for_target_disappear", read_only=True, public=True)
@requires_services(yolo="core/yolo", app="app")
async def yolo_wait_for_target_disappear(
    yolo: YoloService,
    app: AppProviderService,
    target_labels: Optional[List[str]] = None,
    timeout_sec: float = 10.0,
    poll_interval_sec: float = 0.3,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
) -> Dict[str, Any]:
    """Wait until target disappears from screen."""
    started = time.time()
    disappeared, _ = await poll_until(
        timeout=max(float(timeout_sec), 0.0),
        interval=max(float(poll_interval_sec), 0.01),
        probe=lambda: yolo_count_targets(
            yolo=yolo,
            app=app,
            target_labels=target_labels,
            roi=roi,
            model_name=model_name,
            options=options,
            min_confidence=min_confidence,
        ),
        predicate=lambda value: int(value) <= 0,
    )
    if disappeared:
        return {
            "disappeared": True,
            "target_labels": target_labels or [],
            "elapsed_sec": float(time.time() - started),
        }

    return {
        "disappeared": False,
        "target_labels": target_labels or [],
        "elapsed_sec": float(time.time() - started),
    }


@action_info(name="yolo_find_and_click_target", public=True)
@requires_services(yolo="core/yolo", app="app", controller="controller")
async def yolo_find_and_click_target(
    yolo: YoloService,
    app: AppProviderService,
    controller: ControllerService,
    target_labels: Optional[List[str]] = None,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "highest_confidence",
    anchor_point: Optional[Sequence[int]] = None,
    click_offset: Optional[Sequence[int]] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
    post_delay_sec: float = 0.0,
) -> Dict[str, Any]:
    """Find a target and click it using global screen coordinates."""
    found = yolo_find_target(
        yolo=yolo,
        app=app,
        target_labels=target_labels,
        roi=roi,
        model_name=model_name,
        options=options,
        min_confidence=min_confidence,
        sort_mode=sort_mode,
        anchor_point=anchor_point,
    )
    if not found.get("found"):
        return {
            "clicked": False,
            "reason": "target_not_found",
            "target_labels": target_labels or [],
        }

    center = found.get("center_point")
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        return {
            "clicked": False,
            "reason": "invalid_center_point",
            "target_labels": target_labels or [],
            "detection": found.get("detection"),
        }

    dx, dy = _normalize_offset(click_offset)
    click_x = int(center[0]) + dx
    click_y = int(center[1]) + dy

    await asyncio.to_thread(
        controller.click,
        x=click_x,
        y=click_y,
        button=button,
        clicks=int(clicks),
        interval=float(interval),
    )
    if post_delay_sec > 0:
        await asyncio.sleep(float(post_delay_sec))

    return {
        "clicked": True,
        "click_point": [click_x, click_y],
        "button": button,
        "clicks": int(clicks),
        "target_labels": target_labels or [],
        "sort_mode": sort_mode,
        "detection": found.get("detection"),
    }


@action_info(name="yolo_click_all_targets", public=True)
@requires_services(yolo="core/yolo", app="app", controller="controller")
async def yolo_click_all_targets(
    yolo: YoloService,
    app: AppProviderService,
    controller: ControllerService,
    target_labels: Optional[List[str]] = None,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "left_to_right",
    anchor_point: Optional[Sequence[int]] = None,
    click_offset: Optional[Sequence[int]] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.08,
    delay_between_clicks_sec: float = 0.1,
    max_clicks: int = 10,
) -> Dict[str, Any]:
    """Detect once and click all matched targets in sorted order."""
    result = yolo.detect_on_screen(
        app=app,
        roi=_normalize_roi(roi),
        model_name=model_name,
        options=options,
    )
    if not result.get("ok"):
        return {"clicked": 0, "matched": 0, "reason": "detect_failed"}

    detections = result.get("detections", [])
    detections = _filter_by_labels(detections, target_labels)
    detections = _filter_by_min_confidence(detections, min_confidence)
    detections = _sort_detections(
        detections,
        sort_mode=sort_mode,
        anchor_point=_normalize_point(anchor_point),
    )

    dx, dy = _normalize_offset(click_offset)
    limit = max(0, int(max_clicks))
    clicked_points: List[List[int]] = []

    for det in detections[:limit]:
        center = _get_bbox_center(det)
        if center is None:
            continue
        x = int(center[0]) + dx
        y = int(center[1]) + dy
        await asyncio.to_thread(
            controller.click,
            x=x,
            y=y,
            button=button,
            clicks=int(clicks),
            interval=float(interval),
        )
        clicked_points.append([x, y])
        if delay_between_clicks_sec > 0:
            await asyncio.sleep(float(delay_between_clicks_sec))

    return {
        "clicked": len(clicked_points),
        "matched": len(detections),
        "click_points": clicked_points,
        "target_labels": target_labels or [],
        "sort_mode": sort_mode,
    }


@action_info(name="yolo_find_target_and_press_key", public=True)
@requires_services(yolo="core/yolo", app="app", controller="controller")
def yolo_find_target_and_press_key(
    yolo: YoloService,
    app: AppProviderService,
    controller: ControllerService,
    key: str,
    target_labels: Optional[List[str]] = None,
    roi: Optional[Sequence[int]] = None,
    model_name: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.0,
    sort_mode: str = "highest_confidence",
    anchor_point: Optional[Sequence[int]] = None,
    presses: int = 1,
    interval: float = 0.1,
) -> Dict[str, Any]:
    """Find target first, then press key if found."""
    found = yolo_find_target(
        yolo=yolo,
        app=app,
        target_labels=target_labels,
        roi=roi,
        model_name=model_name,
        options=options,
        min_confidence=min_confidence,
        sort_mode=sort_mode,
        anchor_point=anchor_point,
    )
    if not found.get("found"):
        return {
            "pressed": False,
            "reason": "target_not_found",
            "target_labels": target_labels or [],
            "key": key,
        }

    controller.press_key(key=key, presses=int(presses), interval=float(interval))
    return {
        "pressed": True,
        "key": key,
        "presses": int(presses),
        "target_labels": target_labels or [],
        "detection": found.get("detection"),
    }

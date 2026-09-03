"""Material capture helpers for the Resonance PC Consciousness Deep Dive board."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException


_EXPECTED_CLIENT_SIZE = (1280, 720)
_CAPTURE_PROFILES = {
    "slow": (
        (560, (320, 240)),
        (2480, (320, 320, 320, 320, 320, 320)),
        (3680, (320, 320, 320, 240)),
        (5600, (320, 320, 320, 320, 320, 320)),
    ),
    "fast": (
        (280, (280,)),
        (1280, (320, 320, 320, 40)),
        (1920, (320, 320)),
        (2880, (320, 320, 320)),
    ),
}
_CAPTURE_DISPLACEMENTS = frozenset(
    {320}
    | {
        displacement
        for profile in _CAPTURE_PROFILES.values()
        for displacement, _chunks in profile
    }
)


class ConsciousnessDeepDiveCaptureError(RuntimeError):
    """Expected material-capture failure with a stable error code."""

    def __init__(self, code: str, message: str, detail: Dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _next_output_path(displacement_px: int) -> tuple[Path, str]:
    output_dir = resolve_base_path() / "logs" / "test" / str(displacement_px)
    output_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        timestamp = datetime.now().strftime("%H%M%S%f")[:9]
        output_path = output_dir / f"{displacement_px}_{timestamp}.png"
        if not output_path.exists():
            return output_path, timestamp
        time.sleep(0.001)
    raise ConsciousnessDeepDiveCaptureError(
        "deep_dive_capture_name_collision",
        "无法生成不重复的识海深潜素材文件名。",
        {"displacement_px": displacement_px, "output_dir": str(output_dir)},
    )


@action_info(
    name="resonance_pc.prepare_consciousness_deep_dive_capture_profile",
    public=False,
    read_only=True,
    timeout=5,
    description="Resolve one calibrated Deep Dive capture sensitivity profile.",
)
def resonance_pc_prepare_consciousness_deep_dive_capture_profile(
    sensitivity: str = "slow",
) -> Dict[str, Any]:
    mode = str(sensitivity or "slow").strip().lower()
    profile = _CAPTURE_PROFILES.get(mode)
    if profile is None:
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_sensitivity_invalid",
            "识海深潜素材采集灵敏度无效。",
            {
                "sensitivity": mode,
                "supported": sorted(_CAPTURE_PROFILES),
            },
        )

    views = [
        {
            "displacement_px": displacement,
            "drag_chunks": list(chunks),
        }
        for displacement, chunks in profile
    ]
    return {
        "success": True,
        "status": "completed",
        "sensitivity": mode,
        "capture_displacements": [view["displacement_px"] for view in views],
        "views": views,
    }


@action_info(
    name="resonance_pc.collect_consciousness_deep_dive_capture_results",
    public=False,
    read_only=True,
    timeout=5,
    description="Normalize capture outputs returned by sequential view sub-tasks.",
)
def resonance_pc_collect_consciousness_deep_dive_capture_results(
    profile: Mapping[str, Any],
    runs: List[Any],
) -> Dict[str, Any]:
    expected_views = list(profile.get("views") or [])
    run_rows = list(runs or [])
    if len(expected_views) != 4 or len(run_rows) != 4:
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_result_count_invalid",
            "识海深潜四角度素材采集结果数量异常。",
            {
                "expected_views": len(expected_views),
                "actual_runs": len(run_rows),
            },
        )

    captures: List[Dict[str, Any]] = []
    for index, (expected_view, run) in enumerate(zip(expected_views, run_rows), 1):
        nodes = run.get("nodes") if isinstance(run, Mapping) else None
        capture_node = nodes.get("capture_view") if isinstance(nodes, Mapping) else None
        capture = (
            capture_node.get("output")
            if isinstance(capture_node, Mapping)
            else None
        )
        expected_displacement = int(expected_view["displacement_px"])
        actual_displacement = (
            int(capture.get("displacement_px") or 0)
            if isinstance(capture, Mapping)
            else 0
        )
        if (
            not isinstance(capture, Mapping)
            or capture.get("success") is not True
            or actual_displacement != expected_displacement
        ):
            raise ConsciousnessDeepDiveCaptureError(
                "deep_dive_capture_result_invalid",
                "识海深潜单个角度的素材采集结果异常。",
                {
                    "view_index": index,
                    "expected_displacement_px": expected_displacement,
                    "actual_displacement_px": actual_displacement,
                },
            )
        captures.append(dict(capture))

    return {
        "success": True,
        "status": "completed",
        "sensitivity": str(profile.get("sensitivity") or "slow"),
        "capture_displacements": [
            int(view["displacement_px"]) for view in expected_views
        ],
        "capture_count": len(captures),
        "captures": captures,
    }


@action_info(
    name="resonance_pc.capture_consciousness_deep_dive_view",
    public=True,
    read_only=False,
    timeout=15,
    description="Capture one fixed-displacement Consciousness Deep Dive board view.",
)
@requires_services(app="plans/aura_base/app")
def resonance_pc_capture_consciousness_deep_dive_view(
    displacement_px: int,
    app: Any = None,
) -> Dict[str, Any]:
    if app is None:
        raise RuntimeError("app service is required")
    if is_current_task_cancel_requested():
        raise StopTaskException("识海深潜素材采集已取消。", success=False)

    displacement = int(displacement_px)
    if displacement not in _CAPTURE_DISPLACEMENTS:
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_displacement_invalid",
            "识海深潜素材截图位移不受支持。",
            {
                "displacement_px": displacement,
                "supported": sorted(_CAPTURE_DISPLACEMENTS),
            },
        )

    window_size = app.get_window_size()
    if tuple(window_size or ()) != _EXPECTED_CLIENT_SIZE:
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_window_size_mismatch",
            "识海深潜素材采集要求游戏客户区为 1280×720。",
            {
                "expected": list(_EXPECTED_CLIENT_SIZE),
                "actual": list(window_size or ()),
                "displacement_px": displacement,
            },
        )

    capture = app.capture()
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_failed",
            "WGC 未能取得识海深潜素材画面。",
            {
                "displacement_px": displacement,
                "error": str(getattr(capture, "error_message", "") or ""),
            },
        )

    output_path, timestamp = _next_output_path(displacement)
    image_bgr = cv2.cvtColor(capture.image, cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(output_path), image_bgr):
        raise ConsciousnessDeepDiveCaptureError(
            "deep_dive_capture_save_failed",
            "识海深潜素材截图保存失败。",
            {"displacement_px": displacement, "output_path": str(output_path)},
        )

    relative_path = output_path.relative_to(resolve_base_path())
    result = {
        "success": True,
        "status": "completed",
        "displacement_px": displacement,
        "timestamp": timestamp,
        "output_path": str(output_path),
        "relative_path": relative_path.as_posix(),
        "image_size": list(capture.image_size or _EXPECTED_CLIENT_SIZE),
    }
    logger.info(
        "[DeepDiveCapture] displacement_px=%s output_path=%s",
        displacement,
        output_path,
    )
    return result


__all__ = [
    "ConsciousnessDeepDiveCaptureError",
    "resonance_pc_collect_consciousness_deep_dive_capture_results",
    "resonance_pc_prepare_consciousness_deep_dive_capture_profile",
    "resonance_pc_capture_consciousness_deep_dive_view",
]

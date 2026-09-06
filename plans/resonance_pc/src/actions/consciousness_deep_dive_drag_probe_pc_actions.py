"""Capture helpers for the Consciousness Deep Dive drag-distance probe."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any, Dict

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException


_EXPECTED_CLIENT_SIZE = (1280, 720)
_RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}(?:-\d{3})?$")
_FRAME_COUNT = 100
_DRAG_STEP_PX = 80


class ConsciousnessDeepDiveDragProbeError(RuntimeError):
    """Expected drag-probe failure with a stable error code."""

    def __init__(self, code: str, message: str, detail: Dict[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _probe_root() -> Path:
    return resolve_base_path() / "logs" / "test" / "drag_distance_probe"


def _resolve_run_dir(run_id: str) -> Path:
    normalized = str(run_id or "").strip()
    if not _RUN_ID_PATTERN.fullmatch(normalized):
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_run_id_invalid",
            "拖动距离测算运行编号无效。",
            {"run_id": normalized},
        )
    return _probe_root() / normalized


@action_info(
    name="resonance_pc.start_consciousness_deep_dive_drag_probe",
    public=False,
    read_only=False,
    description="Create one output directory for a Deep Dive drag-distance probe.",
)
def resonance_pc_start_consciousness_deep_dive_drag_probe() -> Dict[str, Any]:
    output_root = _probe_root()
    output_root.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
        output_dir = output_root / run_id
        try:
            output_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            time.sleep(0.001)
            continue
        return {
            "success": True,
            "status": "started",
            "run_id": run_id,
            "output_dir": str(output_dir),
            "frame_count": _FRAME_COUNT,
            "drag_step_px": _DRAG_STEP_PX,
        }
    raise ConsciousnessDeepDiveDragProbeError(
        "deep_dive_drag_probe_run_id_collision",
        "无法创建不重复的拖动距离测算素材目录。",
        {"output_root": str(output_root)},
    )


@action_info(
    name="resonance_pc.capture_consciousness_deep_dive_drag_probe_frame",
    public=False,
    read_only=False,
    timeout=15,
    description="Save one pre-drag frame for a Deep Dive drag-distance probe.",
)
@requires_services(app="plans/aura_base/app")
def resonance_pc_capture_consciousness_deep_dive_drag_probe_frame(
    run_id: str,
    round_index: int,
    displacement_px: int,
    app: Any = None,
) -> Dict[str, Any]:
    if app is None:
        raise RuntimeError("app service is required")
    if is_current_task_cancel_requested():
        raise StopTaskException("拖动距离测算已取消。", success=False)

    index = int(round_index)
    displacement = int(displacement_px)
    expected_displacement = (index - 1) * _DRAG_STEP_PX
    if index < 1 or index > _FRAME_COUNT or displacement != expected_displacement:
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_frame_invalid",
            "拖动距离测算帧编号或累计位移无效。",
            {
                "round_index": index,
                "displacement_px": displacement,
                "expected_displacement_px": expected_displacement,
            },
        )

    window_size = app.get_window_size()
    if tuple(window_size or ()) != _EXPECTED_CLIENT_SIZE:
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_window_size_mismatch",
            "拖动距离测算要求游戏客户区为 1280x720。",
            {
                "expected": list(_EXPECTED_CLIENT_SIZE),
                "actual": list(window_size or ()),
            },
        )

    output_dir = _resolve_run_dir(run_id)
    if not output_dir.is_dir():
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_output_dir_missing",
            "拖动距离测算素材目录不存在。",
            {"output_dir": str(output_dir)},
        )

    capture = app.capture()
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_capture_failed",
            "WGC 未能取得拖动距离测算画面。",
            {
                "round_index": index,
                "error": str(getattr(capture, "error_message", "") or ""),
            },
        )

    output_path = output_dir / f"round_{index:02d}_before_drag.png"
    image_bgr = cv2.cvtColor(capture.image, cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(output_path), image_bgr):
        raise ConsciousnessDeepDiveDragProbeError(
            "deep_dive_drag_probe_save_failed",
            "拖动距离测算截图保存失败。",
            {"output_path": str(output_path)},
        )

    result = {
        "success": True,
        "status": "captured",
        "run_id": str(run_id),
        "round_index": index,
        "displacement_px": displacement,
        "output_path": str(output_path),
    }
    logger.info(
        "[DeepDiveDragProbe] round=%s displacement_px=%s output_path=%s",
        index,
        displacement,
        output_path,
    )
    return result


__all__ = [
    "ConsciousnessDeepDiveDragProbeError",
    "resonance_pc_capture_consciousness_deep_dive_drag_probe_frame",
    "resonance_pc_start_consciousness_deep_dive_drag_probe",
]

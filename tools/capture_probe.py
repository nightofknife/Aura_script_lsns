from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import (
    add_common_output_flag,
    add_common_windows_target_args,
    build_overlay_config,
    build_runtime_overlay_from_args,
    maybe_print,
    normalize_payload,
    parse_rect,
    plan_scope,
    sanitize_filename,
    suppress_framework_console_logs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe a configured Windows capture backend and optionally save a preview image."
    )
    parser.add_argument("--plan", help="Optional current plan context.")
    add_common_windows_target_args(parser)
    parser.add_argument("--capture-backend", help="Temporarily probe with a specific capture backend.")
    parser.add_argument("--capture-candidates", help="Comma-separated candidate backends to probe.")
    parser.add_argument("--rect", help="Optional client rect as x,y,w,h.")
    parser.add_argument("--save", type=Path, help="Optional path to save the captured preview PNG.")
    parser.add_argument("--probe-candidates", action="store_true", help="Probe all configured candidate backends.")
    add_common_output_flag(parser)
    return parser


def collect_capture_probe(args: argparse.Namespace) -> tuple[dict[str, Any], np.ndarray | None]:
    from plans.aura_base.src.platform.windows.capture_backends import build_capture_backend
    from plans.aura_base.src.platform.windows.window_target import WindowTarget
    from plans.aura_base.src.platform.runtime_config import resolve_runtime_config
    from plans.aura_base.src.services.target_runtime_service import TargetRuntimeService
    from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService

    overlay = build_runtime_overlay_from_args(args)
    config = build_overlay_config(plan_name=args.plan, overlay=overlay)
    rect = parse_rect(args.rect)

    with suppress_framework_console_logs(), plan_scope(args.plan):
        target_runtime = TargetRuntimeService(config)
        diagnostics = WindowsDiagnosticsService(config, target_runtime)
        payload: dict[str, Any]
        image: np.ndarray | None = None

        if args.probe_candidates:
            payload = {"candidates": diagnostics.probe_capture_candidates()}
        else:
            payload = {"probe": diagnostics.probe_capture_backend(backend=args.capture_backend, rect=rect)}
            resolved = resolve_runtime_config(config)
            target = WindowTarget.create(resolved.target)
            backend_name = str(args.capture_backend or resolved.capture.backend)
            backend = build_capture_backend(backend_name, target, resolved.capture.provider_options("windows"))
            try:
                capture = backend.capture(rect=rect)
                image = np.asarray(capture.image).copy() if capture.image is not None else None
                payload["preview"] = {
                    "ok": bool(image is not None),
                    "backend": backend_name,
                    "image_size": list(capture.image_size) if capture.image_size else None,
                    "relative_rect": list(capture.relative_rect) if capture.relative_rect else None,
                }
            finally:
                backend.close()

        return normalize_payload(payload), image


def save_preview(image: np.ndarray | None, destination: Path) -> Path | None:
    if image is None:
        return None
    resolved = destination.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image).save(resolved)
    return resolved


def render_text(payload: dict[str, Any]) -> str:
    lines = []
    probe = payload.get("probe")
    if isinstance(probe, dict):
        lines.append(
            f"Probe: backend={probe.get('backend')} ok={probe.get('ok')} "
            f"image_size={probe.get('image_size')} error={_probe_error_text(probe)}"
        )
    preview = payload.get("preview")
    if isinstance(preview, dict):
        lines.append(
            f"Preview: ok={preview.get('ok')} backend={preview.get('backend')} "
            f"image_size={preview.get('image_size')}"
        )
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        lines.append(f"Candidate probes: {len(candidates)}")
        for item in candidates:
            candidate = item.get("candidate", {})
            probe_item = item.get("probe", {})
            lines.append(
                f"- {candidate.get('backend')}: ok={probe_item.get('ok')} "
                f"image_size={probe_item.get('image_size')} error={_probe_error_text(probe_item)}"
            )
    return "\n".join(lines)


def _probe_error_text(probe: dict[str, Any]) -> str:
    error = probe.get("error")
    if isinstance(error, dict):
        return f"{error.get('code')}: {error.get('message')}"
    if error:
        return str(error)
    return "-"


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload, image = collect_capture_probe(args)
        if args.save:
            destination = args.save
        elif image is not None:
            filename = sanitize_filename(str((payload.get("probe") or {}).get("backend") or "capture_probe")) + ".png"
            destination = Path("logs") / filename
        else:
            destination = None
        if destination is not None and image is not None:
            saved = save_preview(image, destination)
            payload["saved_preview"] = str(saved) if saved else None
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    maybe_print(payload, as_json=args.json, text_renderer=render_text)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()

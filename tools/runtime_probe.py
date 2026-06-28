from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

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
    plan_scope,
    suppress_framework_console_logs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a Windows runtime health probe for the current Aura runtime configuration."
    )
    parser.add_argument("--plan", help="Optional current plan context.")
    add_common_windows_target_args(parser)
    parser.add_argument("--capture-backend", help="Temporarily probe with a specific capture backend.")
    parser.add_argument("--capture-candidates", help="Comma-separated candidate backends to probe.")
    parser.add_argument("--input-backend", help="Temporarily probe with a specific input backend.")
    parser.add_argument("--activation-mode", help="Override activation mode for focus probe.")
    parser.add_argument("--activation-sleep-ms", type=int, help="Override activation sleep in milliseconds.")
    parser.add_argument("--activation-click-point", help="Activation click point as x,y.")
    parser.add_argument("--activation-click-button", help="Activation click button.")
    parser.add_argument("--skip-capture-candidates", action="store_true", help="Skip candidate backend probing.")
    parser.add_argument("--skip-dpi", action="store_true", help="Skip DPI diagnostics.")
    parser.add_argument("--skip-window-spec", action="store_true", help="Skip window spec diagnostics.")
    add_common_output_flag(parser)
    return parser


def collect_runtime_probe(args: argparse.Namespace) -> dict[str, Any]:
    from plans.aura_base.src.services.target_runtime_service import TargetRuntimeService
    from plans.aura_base.src.services.windows_diagnostics_service import WindowsDiagnosticsService

    overlay = build_runtime_overlay_from_args(args)
    config = build_overlay_config(plan_name=args.plan, overlay=overlay)

    with suppress_framework_console_logs(), plan_scope(args.plan):
        target_runtime = TargetRuntimeService(config)
        diagnostics = WindowsDiagnosticsService(config, target_runtime)
        payload: dict[str, Any] = {
            "runtime": target_runtime.self_check(),
            "capabilities": diagnostics.show_runtime_capabilities(),
        }
        if not args.skip_dpi:
            payload["dpi"] = diagnostics.show_dpi_info()
        if not args.skip_window_spec:
            try:
                payload["window_spec"] = diagnostics.check_window_spec()
            except Exception as exc:
                payload["window_spec"] = {"ok": False, "error": str(exc)}
        if not args.skip_capture_candidates:
            payload["capture_candidates"] = diagnostics.probe_capture_candidates()
        return normalize_payload(payload)


def render_text(payload: dict[str, Any]) -> str:
    runtime = payload.get("runtime", {})
    capabilities = payload.get("capabilities", {})
    lines = [
        f"Runtime ok: {runtime.get('ok')}",
        f"Provider: {runtime.get('provider') or capabilities.get('provider')}",
        f"Family: {runtime.get('family') or capabilities.get('family')}",
    ]

    target = runtime.get("target") or {}
    if isinstance(target, dict):
        lines.append(
            "Target: "
            f"hwnd={target.get('hwnd')} title={target.get('title') or '-'} "
            f"process={target.get('process_name') or '-'}"
        )

    capture = runtime.get("capture") or {}
    if isinstance(capture, dict):
        lines.append(f"Capture backend: {capture.get('backend')}")
    input_payload = runtime.get("input") or {}
    if isinstance(input_payload, dict):
        lines.append(f"Input backend: {input_payload.get('backend')}")

    warnings = runtime.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")

    dpi = payload.get("dpi")
    if isinstance(dpi, dict):
        lines.append("")
        lines.append("DPI:")
        lines.append(
            f"- window_dpi={dpi.get('window_dpi')} "
            f"window_scale_factor={dpi.get('window_scale_factor')} "
            f"monitor_scale_factor={dpi.get('monitor_scale_factor')}"
        )

    window_spec = payload.get("window_spec")
    if isinstance(window_spec, dict):
        lines.append("")
        lines.append("Window spec:")
        lines.append(f"- ok={window_spec.get('ok')} mode={window_spec.get('mode') or window_spec.get('status')}")

    candidate_payload = payload.get("capture_candidates")
    if isinstance(candidate_payload, list):
        lines.append("")
        lines.append("Capture candidates:")
        for item in candidate_payload:
            probe = item.get("probe", {})
            candidate = item.get("candidate", {})
            lines.append(
                f"- {candidate.get('backend')}: ok={probe.get('ok')} "
                f"image_size={probe.get('image_size')} error={_probe_error_text(probe)}"
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
        payload = collect_runtime_probe(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    maybe_print(payload, as_json=args.json, text_renderer=render_text)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()

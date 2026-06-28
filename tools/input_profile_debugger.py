from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import (
    add_common_output_flag,
    build_overlay_config,
    maybe_print,
    normalize_payload,
    plan_scope,
    suppress_framework_console_logs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and validate Aura input profile mappings for a plan."
    )
    parser.add_argument("--plan", help="Optional current plan context.")
    parser.add_argument("--profile", help="Profile name. Defaults to input.profile or default_pc.")
    parser.add_argument("--action", action="append", default=[], help="Resolve a specific action name. Repeatable.")
    parser.add_argument("--resolve-all", action="store_true", help="Resolve every action in the chosen profile.")
    add_common_output_flag(parser)
    return parser


def collect_profile_debug(args: argparse.Namespace) -> dict[str, Any]:
    from plans.aura_base.src.services.gamepad_service import GamepadService
    from plans.aura_base.src.services.input_mapping_service import InputMappingService

    config = build_overlay_config(plan_name=args.plan)

    with suppress_framework_console_logs(), plan_scope(args.plan):
        gamepad = GamepadService(config)
        service = InputMappingService(config, gamepad)
        active_profile = service.get_active_profile(args.profile)
        actions = service.list_actions(profile=args.profile)
        type_counts = Counter(
            str(binding.get("type") or "unknown")
            for binding in actions.values()
            if isinstance(binding, dict)
        )

        resolution_targets = list(dict.fromkeys(args.action))
        if args.resolve_all:
            resolution_targets = sorted(actions.keys())

        resolved: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for action_name in resolution_targets:
            try:
                resolved.append(service.resolve_binding(action_name, profile=args.profile))
            except Exception as exc:
                errors.append({"action_name": action_name, "error": str(exc)})

        try:
            gamepad_payload = gamepad.self_check()
        except Exception as exc:
            gamepad_payload = {"ok": False, "error": str(exc)}

        return normalize_payload(
            {
                "plan": args.plan,
                "active_profile": active_profile,
                "available_profiles": service.available_profiles(),
                "summary": {
                    "action_count": len(actions),
                    "type_counts": dict(sorted(type_counts.items())),
                    "resolved_count": len(resolved),
                    "error_count": len(errors),
                },
                "actions": actions,
                "resolved": resolved,
                "errors": errors,
                "gamepad": gamepad_payload,
            }
        )


def render_text(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        f"Plan: {payload.get('plan') or '-'}",
        f"Active profile: {payload.get('active_profile')}",
        f"Available profiles: {', '.join(payload.get('available_profiles') or []) or '-'}",
        f"Actions: {summary.get('action_count', 0)}",
    ]

    type_counts = summary.get("type_counts") or {}
    if type_counts:
        lines.append("Types:")
        for binding_type, count in type_counts.items():
            lines.append(f"- {binding_type}: {count}")

    resolved = payload.get("resolved") or []
    if resolved:
        lines.append("")
        lines.append("Resolved bindings:")
        for item in resolved:
            lines.append(
                f"- {item.get('action_name')}: type={item.get('type')} "
                f"binding={_binding_summary(item)}"
            )

    errors = payload.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Errors:")
        for item in errors:
            lines.append(f"- {item.get('action_name')}: {item.get('error')}")

    gamepad = payload.get("gamepad")
    if isinstance(gamepad, dict):
        lines.append("")
        lines.append(
            f"Gamepad: enabled={gamepad.get('enabled')} backend={gamepad.get('backend')} ok={gamepad.get('ok')}"
        )

    return "\n".join(lines)


def _binding_summary(binding: dict[str, Any]) -> str:
    binding_type = str(binding.get("type") or "")
    if binding_type == "key":
        return f"key={binding.get('key')}"
    if binding_type == "mouse_button":
        return f"button={binding.get('button')}"
    if binding_type == "chord":
        return f"keys={binding.get('keys')}"
    if binding_type == "look":
        if "direction" in binding:
            return f"direction={binding.get('direction')} strength={binding.get('strength')}"
        return f"dx={binding.get('dx')} dy={binding.get('dy')}"
    if binding_type == "gamepad_button":
        return f"button={binding.get('button')}"
    if binding_type == "gamepad_stick":
        return f"stick={binding.get('stick')} x={binding.get('x')} y={binding.get('y')}"
    if binding_type == "trigger":
        return f"side={binding.get('side')} value={binding.get('value')}"
    return str(binding)


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = collect_profile_debug(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    maybe_print(payload, as_json=args.json, text_renderer=render_text)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()

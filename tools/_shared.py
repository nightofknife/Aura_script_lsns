from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class OverlayConfig:
    """Minimal config object compatible with Aura services that call .get()."""

    def __init__(self, payload: Mapping[str, Any] | None = None):
        self.payload = dict(payload or {})

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self.payload
        for part in str(key).split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def repo_root() -> Path:
    return REPO_ROOT


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return dict(data) if isinstance(data, Mapping) else {}


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def aura_env_overlay() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.upper().startswith("AURA_"):
            continue
        path_parts = key.upper().removeprefix("AURA_").lower().split("_")
        current = payload
        for part in path_parts[:-1]:
            current = current.setdefault(part, {})
        current[path_parts[-1]] = value
    return payload


def plan_path(plan_name: str) -> Path:
    return repo_root() / "plans" / str(plan_name)


def discover_plan_names() -> list[str]:
    plans_dir = repo_root() / "plans"
    if not plans_dir.is_dir():
        return []
    return sorted(
        item.name
        for item in plans_dir.iterdir()
        if item.is_dir() and not item.name.startswith(".") and not item.name.startswith("__")
    )


def build_overlay_config(
    *,
    plan_name: str | None = None,
    overlay: Mapping[str, Any] | None = None,
) -> OverlayConfig:
    payload: dict[str, Any] = {}
    payload = deep_merge(payload, load_yaml_file(repo_root() / "config.yaml"))
    if plan_name:
        payload = deep_merge(payload, load_yaml_file(plan_path(plan_name) / "config.yaml"))
    payload = deep_merge(payload, aura_env_overlay())
    if overlay:
        payload = deep_merge(payload, overlay)
    return OverlayConfig(payload)


@contextlib.contextmanager
def plan_scope(plan_name: str | None) -> Iterator[None]:
    if not plan_name:
        yield
        return
    from packages.aura_core.context.plan import current_plan_name

    token = current_plan_name.set(str(plan_name))
    try:
        yield
    finally:
        current_plan_name.reset(token)


@contextlib.contextmanager
def suppress_framework_console_logs() -> Iterator[None]:
    try:
        from packages.aura_core.observability.logging.core_logger import logger as core_logger
    except Exception:
        yield
        return

    logger_obj = getattr(core_logger, "logger", None)
    if logger_obj is None:
        yield
        return

    console_handler = None
    previous_level = None
    for handler in logger_obj.handlers:
        if getattr(handler, "name", None) == "console":
            console_handler = handler
            previous_level = handler.level
            handler.setLevel(logging.CRITICAL + 1)
            break
    try:
        yield
    finally:
        if console_handler is not None and previous_level is not None:
            console_handler.setLevel(previous_level)


def dump_json(payload: Any) -> str:
    return json.dumps(normalize_payload(payload), ensure_ascii=False, indent=2, sort_keys=False)


def normalize_payload(payload: Any) -> Any:
    if is_dataclass(payload):
        return normalize_payload(asdict(payload))
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, Mapping):
        return {str(key): normalize_payload(value) for key, value in payload.items()}
    if isinstance(payload, tuple):
        return [normalize_payload(item) for item in payload]
    if isinstance(payload, list):
        return [normalize_payload(item) for item in payload]
    return payload


def maybe_print(payload: Any, *, as_json: bool, text_renderer) -> None:
    normalized = normalize_payload(payload)
    if as_json:
        print(dump_json(normalized))
        return
    print(text_renderer(normalized))


def add_common_output_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")


def sanitize_filename(name: str, *, fallback: str = "template") -> str:
    value = re.sub(r"[^\w.-]+", "_", str(name or "").strip(), flags=re.UNICODE).strip("._")
    return value or fallback


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_point(text: str | None) -> tuple[int, int] | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    chunks = [part.strip() for part in raw.split(",")]
    if len(chunks) != 2:
        raise ValueError(f"Invalid point '{text}'. Expected format: x,y")
    return int(chunks[0]), int(chunks[1])


def parse_rect(text: str | None) -> tuple[int, int, int, int] | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    chunks = [part.strip() for part in raw.split(",")]
    if len(chunks) != 4:
        raise ValueError(f"Invalid rect '{text}'. Expected format: x,y,w,h")
    return int(chunks[0]), int(chunks[1]), int(chunks[2]), int(chunks[3])


def build_runtime_overlay_from_args(args: argparse.Namespace) -> dict[str, Any]:
    runtime: dict[str, Any] = {"runtime": {"provider": "windows", "family": "windows_desktop"}}
    target: dict[str, Any] = {}
    capture: dict[str, Any] = {}
    input_payload: dict[str, Any] = {}

    if getattr(args, "hwnd", None) is not None:
        target["hwnd"] = int(args.hwnd)
        target["mode"] = "hwnd"
    if getattr(args, "title", None):
        target["title"] = str(args.title)
        target.setdefault("mode", "title")
    if getattr(args, "title_regex", None):
        target["title_regex"] = str(args.title_regex)
        target.setdefault("mode", "title")
    if getattr(args, "process_name", None):
        target["process_name"] = str(args.process_name)
        target.setdefault("mode", "process")
    if getattr(args, "class_name", None):
        target["class_name"] = str(args.class_name)
    if getattr(args, "class_regex", None):
        target["class_regex"] = str(args.class_regex)
    if getattr(args, "pid", None) is not None:
        target["pid"] = int(args.pid)
        target.setdefault("mode", "process")
    if getattr(args, "monitor_index", None) is not None:
        target["monitor_index"] = int(args.monitor_index)
    if getattr(args, "title_exact", False):
        target["title_exact"] = True
    if getattr(args, "class_exact", False):
        target["class_exact"] = True
    if getattr(args, "allow_child_window", False):
        target["allow_child_window"] = True
    if getattr(args, "allow_empty_title", False):
        target["allow_empty_title"] = True
    if getattr(args, "require_visible", None) is not None:
        target["require_visible"] = bool(args.require_visible)
    if getattr(args, "prefer_largest_client_area", False):
        target["prefer_largest_client_area"] = True
    if getattr(args, "prefer_newest_process", False):
        target["prefer_newest_process"] = True

    if getattr(args, "capture_backend", None):
        capture["backend"] = str(args.capture_backend)
    if getattr(args, "capture_candidates", None):
        capture["candidates"] = [{"backend": item.strip()} for item in str(args.capture_candidates).split(",") if item.strip()]

    if getattr(args, "input_backend", None):
        input_payload["backend"] = str(args.input_backend)
    if getattr(args, "activation_mode", None):
        input_payload.setdefault("activation", {})
        input_payload["activation"]["mode"] = str(args.activation_mode)
    if getattr(args, "activation_sleep_ms", None) is not None:
        input_payload.setdefault("activation", {})
        input_payload["activation"]["sleep_ms"] = int(args.activation_sleep_ms)
    activation_click_point = parse_point(getattr(args, "activation_click_point", None))
    if activation_click_point is not None:
        input_payload.setdefault("activation", {})
        input_payload["activation"]["click_point"] = list(activation_click_point)
    if getattr(args, "activation_click_button", None):
        input_payload.setdefault("activation", {})
        input_payload["activation"]["click_button"] = str(args.activation_click_button)

    if target:
        runtime["runtime"]["target"] = target
    if capture:
        runtime["runtime"]["capture"] = capture
    if input_payload:
        runtime["runtime"]["input"] = input_payload
    return runtime


def add_common_windows_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hwnd", type=int, help="Target window handle.")
    parser.add_argument("--title", help="Substring title selector.")
    parser.add_argument("--title-exact", action="store_true", help="Require exact title match.")
    parser.add_argument("--title-regex", help="Regex title selector.")
    parser.add_argument("--process-name", help="Process name selector.")
    parser.add_argument("--pid", type=int, help="Process id selector.")
    parser.add_argument("--class-name", help="Class name selector.")
    parser.add_argument("--class-exact", action="store_true", help="Require exact class match.")
    parser.add_argument("--class-regex", help="Regex class selector.")
    parser.add_argument("--monitor-index", type=int, help="Restrict to a monitor index.")
    parser.add_argument("--allow-child-window", action="store_true", help="Allow child windows.")
    parser.add_argument("--allow-empty-title", action="store_true", help="Allow empty-title windows.")
    parser.add_argument(
        "--require-visible",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require the target window to be visible.",
    )
    parser.add_argument("--prefer-largest-client-area", action="store_true", help="Prefer the largest client area.")
    parser.add_argument("--prefer-newest-process", action="store_true", help="Prefer the newest process.")


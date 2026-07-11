# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.environ.get("AURA_PROBE_BYPASS_ADMIN") == "1":
    import packages.aura_core.scheduler.core as scheduler_core

    scheduler_core.ensure_admin_startup = lambda _context="Aura Scheduler": None

from packages.aura_core.api import service_registry
from packages.aura_core.context.plan import current_plan_name
from packages.aura_game.runner import EmbeddedGameRunner


OUT_DIR = ROOT / "logs" / "probe" / time.strftime("mumu-run-%Y%m%d-%H%M%S")


TASKS: list[dict[str, Any]] = [
    {
        "label": "market_data_get_latest",
        "task_ref": "tasks:market_data.yaml:market_data_get_latest",
        "inputs": {},
        "timeout_sec": 90,
    },
    {
        "label": "trade_plan_best_cycle",
        "task_ref": "tasks:trade_planner.yaml:trade_plan_best_cycle",
        "inputs": {
            "cargo_capacity": 120,
            "book_budget": 0,
            "book_profit_threshold": 0,
            "max_cycle_hops": 4,
        },
        "timeout_sec": 90,
    },
    {
        "label": "auto_battle_input_preview",
        "task_ref": "tasks:auto_battle_input_preview.yaml:auto_battle_input_preview",
        "inputs": {
            "jobs": [
                {
                    "route_id": "gp.action_summary.global_supply.savior",
                    "difficulty": 1,
                    "formation_index": 1,
                }
            ],
            "stop_on_failure": True,
        },
        "timeout_sec": 90,
    },
    {
        "label": "city_shop_get_city_name",
        "task_ref": "tasks:city_shop.yaml:get_city_name",
        "inputs": {},
        "timeout_sec": 45,
    },
]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


class ScreenshotRecorder:
    def __init__(self, target_runtime: Any):
        self.target_runtime = target_runtime
        self.counter = 0
        self.records: list[dict[str, Any]] = []

    def save(self, label: str, *, task_label: str | None = None, note: str | None = None) -> dict[str, Any]:
        self.counter += 1
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:80]
        path = OUT_DIR / f"{self.counter:03d}_{safe_label}.png"
        record: dict[str, Any] = {
            "index": self.counter,
            "label": label,
            "task_label": task_label,
            "note": note,
            "path": str(path),
            "ok": False,
        }
        try:
            capture = self.target_runtime.capture()
            record["backend"] = getattr(capture, "backend", None)
            record["quality_flags"] = list(getattr(capture, "quality_flags", []) or [])
            image = getattr(capture, "image", None)
            if not getattr(capture, "success", False) or image is None:
                record["error"] = getattr(capture, "error_message", None) or "capture failed"
            else:
                arr = np.asarray(image)
                if arr.ndim == 2:
                    img = Image.fromarray(arr)
                elif arr.ndim == 3 and arr.shape[2] == 4:
                    img = Image.fromarray(arr.astype("uint8"), mode="RGBA")
                else:
                    img = Image.fromarray(arr.astype("uint8"))
                img.save(path)
                record["ok"] = True
                record["size"] = [int(img.width), int(img.height)]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        self.records.append(record)
        print(json.dumps({"event": "screenshot", **record}, ensure_ascii=False), flush=True)
        return record


def _patch_runtime_for_screenshots(target_runtime: Any, recorder: ScreenshotRecorder) -> None:
    methods = [
        "click",
        "move_to",
        "move_relative",
        "mouse_down",
        "mouse_up",
        "drag_to",
        "look_delta",
        "scroll",
        "press_key",
        "key_down",
        "key_up",
        "type_text",
    ]

    for method_name in methods:
        original = getattr(target_runtime, method_name, None)
        if not callable(original):
            continue

        def make_wrapper(name: str, func: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                print(
                    json.dumps(
                        {
                            "event": "operation",
                            "method": name,
                            "args": _json_safe(list(args)),
                            "kwargs": _json_safe(kwargs),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                result = func(*args, **kwargs)
                time.sleep(0.35)
                recorder.save(f"after_{name}", note=json.dumps({"args": _json_safe(list(args)), "kwargs": _json_safe(kwargs)}, ensure_ascii=False))
                return result

            return wrapper

        setattr(target_runtime, method_name, make_wrapper(method_name, original))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"out_dir": str(OUT_DIR), "tasks": [], "screenshots": []}
    runner = EmbeddedGameRunner(profile="embedded_full", startup_timeout_sec=30)
    try:
        print(json.dumps({"event": "runner_start"}, ensure_ascii=False), flush=True)
        runner.start()
        token = current_plan_name.set("resonance")
        try:
            target_runtime = service_registry.get_service_instance("target_runtime")
            recorder = ScreenshotRecorder(target_runtime)
            _patch_runtime_for_screenshots(target_runtime, recorder)
            status = target_runtime.self_check()
            report["target_status"] = _json_safe(status)
            print(json.dumps({"event": "target_status", "status": report["target_status"]}, ensure_ascii=False), flush=True)
            recorder.save("000_initial", note="before tasks")
        finally:
            current_plan_name.reset(token)

        for index, task in enumerate(TASKS, start=1):
            label = task["label"]
            task_record: dict[str, Any] = {
                "index": index,
                "label": label,
                "task_ref": task["task_ref"],
                "inputs": task["inputs"],
                "status": "started",
            }
            report["tasks"].append(task_record)
            print(json.dumps({"event": "task_start", **task_record}, ensure_ascii=False), flush=True)
            token = current_plan_name.set("resonance")
            try:
                recorder.save(f"{index:02d}_{label}_before", task_label=label)
            finally:
                current_plan_name.reset(token)

            started = time.time()
            try:
                result = runner.run_task(
                    game_name="resonance",
                    task_ref=task["task_ref"],
                    inputs=task["inputs"],
                    wait=True,
                    timeout_sec=float(task["timeout_sec"]),
                )
                detail = (((result.get("run") or {}).get("detail") or {}) if isinstance(result, dict) else {})
                final_result = detail.get("final_result") if isinstance(detail, dict) else None
                final_status = str((final_result or {}).get("status") or detail.get("status") or "").strip().lower()
                task_record["status"] = "success" if final_status == "success" else (final_status or "completed")
                task_record["elapsed_sec"] = round(time.time() - started, 3)
                task_record["result"] = _json_safe(result)
            except Exception as exc:
                task_record["status"] = "exception"
                task_record["elapsed_sec"] = round(time.time() - started, 3)
                task_record["error"] = f"{type(exc).__name__}: {exc}"
                task_record["traceback"] = traceback.format_exc()
            finally:
                token = current_plan_name.set("resonance")
                try:
                    recorder.save(f"{index:02d}_{label}_after", task_label=label)
                finally:
                    current_plan_name.reset(token)
            print(json.dumps({"event": "task_end", **task_record}, ensure_ascii=False), flush=True)

        report["screenshots"] = recorder.records
        report_path = OUT_DIR / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "out_dir": str(OUT_DIR),
            "target_status": report.get("target_status"),
            "tasks": [
                {
                    "index": item.get("index"),
                    "label": item.get("label"),
                    "task_ref": item.get("task_ref"),
                    "status": item.get("status"),
                    "elapsed_sec": item.get("elapsed_sec"),
                    "error": item.get("error"),
                    "run_status": (((item.get("result") or {}).get("run") or {}).get("detail") or {}).get("status")
                    if isinstance(item.get("result"), dict)
                    else None,
                    "final_status": (
                        ((((item.get("result") or {}).get("run") or {}).get("detail") or {}).get("final_result") or {})
                        .get("status")
                        if isinstance(item.get("result"), dict)
                        else None
                    ),
                }
                for item in report["tasks"]
            ],
            "screenshots": recorder.records,
        }
        summary_path = OUT_DIR / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": "report", "path": str(report_path)}, ensure_ascii=False), flush=True)
        print(json.dumps({"event": "summary", "path": str(summary_path)}, ensure_ascii=False), flush=True)
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())

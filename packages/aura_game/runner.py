from __future__ import annotations

import io
import json
import multiprocessing
import os
import queue
import threading
import time
import traceback
from typing import Any, Dict, List, Mapping, Optional

from packages.aura_core.api import ACTION_REGISTRY, hook_manager, service_registry
from packages.aura_core.config.loader import get_config_value
from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.runtime.bootstrap import create_runtime, peek_runtime, start_runtime, stop_runtime

_TERMINAL_STATUSES = {"success", "error", "failed", "timeout", "cancelled"}
_DEFAULT_PROFILE = "embedded_full"
_ENV_START_LOCK = threading.RLock()
_SUBPROCESS_START_MARGIN_SEC = 30.0
_SUBPROCESS_REQUEST_MIN_TIMEOUT_SEC = 30.0


def _startup_timeout_sec() -> int:
    return int(
        get_config_value(
            "runtime.startup_timeout_sec",
            get_config_value("backend.scheduler_startup_timeout_sec", 10),
        )
    )


def _reset_global_runtime_state() -> None:
    runtime = peek_runtime()
    if runtime is not None:
        try:
            runtime.observability.run_store.close()
        except Exception:
            pass
    stop_runtime()
    ACTION_REGISTRY.clear()
    service_registry.clear()
    hook_manager.clear()


def _normalize_run_row(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    final_result_raw = normalized.pop("final_result_json", None)
    if final_result_raw is not None and "final_result" not in normalized:
        try:
            normalized["final_result"] = json.loads(final_result_raw)
        except Exception:
            normalized["final_result"] = {"raw": final_result_raw}
    if "plan_name" in normalized and "game_name" not in normalized:
        normalized["game_name"] = normalized["plan_name"]
    return normalized


def _normalize_event_row(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    payload = normalized.get("payload")
    if isinstance(payload, dict) and "plan_name" in payload and "game_name" not in payload:
        payload = dict(payload)
        payload["game_name"] = payload.get("plan_name")
        normalized["payload"] = payload
    return normalized


class EmbeddedGameRunner:
    """Direct in-process local SDK."""

    def __init__(self, *, profile: str = _DEFAULT_PROFILE, startup_timeout_sec: Optional[int] = None):
        self.profile = profile
        self.startup_timeout_sec = int(startup_timeout_sec or _startup_timeout_sec())

    def _ensure_runtime(self):
        return create_runtime(profile=self.profile)

    def _is_running(self) -> bool:
        runtime = peek_runtime()
        if runtime is None:
            return False
        try:
            return bool(runtime.get_master_status().get("is_running"))
        except Exception:
            return False

    def start(self) -> Dict[str, Any]:
        start_runtime(profile=self.profile, startup_timeout_sec=self.startup_timeout_sec)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        _reset_global_runtime_state()
        return self.status()

    def close(self) -> None:
        _reset_global_runtime_state()

    def status(self) -> Dict[str, Any]:
        runtime = peek_runtime()
        if runtime is None:
            return {
                "profile": self.profile,
                "scheduler_initialized": False,
                "scheduler_running": False,
                "ready": False,
            }

        master_status = runtime.get_master_status()
        return {
            "profile": self.profile,
            "scheduler_initialized": True,
            "scheduler_running": bool(master_status.get("is_running")),
            "ready": bool(master_status.get("is_running")),
            "raw": master_status,
        }

    def list_games(self, *, include_shared: bool = False) -> List[Dict[str, Any]]:
        runtime = self._ensure_runtime()
        tasks = runtime.get_all_task_definitions_with_meta()
        errors = runtime.get_task_load_errors()
        by_name: Dict[str, Dict[str, Any]] = {}
        unique_task_refs: Dict[str, set[str]] = {}
        unique_entry_refs: Dict[str, set[str]] = {}

        for plan_name in runtime.get_all_plans():
            by_name[plan_name] = {
                "game_name": plan_name,
                "kind": "shared",
                "task_count": 0,
                "entry_task_count": 0,
                "task_error_count": 0,
            }

        for task in tasks:
            plan_name = str(task.get("plan_name") or "")
            if not plan_name:
                continue
            task_ref = str(task.get("task_ref") or "")
            if not task_ref:
                continue
            row = by_name.setdefault(
                plan_name,
                {
                    "game_name": plan_name,
                    "kind": "shared",
                    "task_count": 0,
                    "entry_task_count": 0,
                    "task_error_count": 0,
                },
            )
            unique_task_refs.setdefault(plan_name, set()).add(task_ref)
            if bool((task.get("meta") or {}).get("entry_point", False)):
                unique_entry_refs.setdefault(plan_name, set()).add(task_ref)
            row["task_count"] = len(unique_task_refs.get(plan_name, set()))
            row["entry_task_count"] = len(unique_entry_refs.get(plan_name, set()))
            if row["task_count"] > 0:
                row["kind"] = "game"

        for error in errors:
            plan_name = str(error.get("plan_name") or "")
            if not plan_name:
                continue
            row = by_name.setdefault(
                plan_name,
                {
                    "game_name": plan_name,
                    "kind": "shared",
                    "task_count": 0,
                    "entry_task_count": 0,
                    "task_error_count": 0,
                },
            )
            row["task_error_count"] += 1

        rows = sorted(by_name.values(), key=lambda item: str(item["game_name"]))
        if include_shared:
            return rows
        return [row for row in rows if row.get("task_count", 0) > 0]

    def list_tasks(self, game_name: str) -> List[Dict[str, Any]]:
        runtime = self._ensure_runtime()
        rows_by_ref: Dict[str, Dict[str, Any]] = {}
        for task in runtime.get_all_task_definitions_with_meta():
            if task.get("plan_name") != game_name:
                continue
            meta = task.get("meta") or {}
            task_ref = str(task.get("task_ref") or "")
            if not task_ref or task_ref in rows_by_ref:
                continue
            rows_by_ref[task_ref] = (
                {
                    "game_name": game_name,
                    "task_ref": task_ref,
                    "title": meta.get("title") or task_ref,
                    "description": meta.get("description") or "",
                    "entry_point": bool(meta.get("entry_point", False)),
                    "inputs": meta.get("inputs") or [],
                }
            )
        return sorted(rows_by_ref.values(), key=lambda item: str(item.get("task_ref") or ""))

    def run_task(
        self,
        *,
        game_name: str,
        task_ref: str,
        inputs: Optional[Dict[str, Any]] = None,
        wait: bool = False,
        timeout_sec: float = 600.0,
    ) -> Dict[str, Any]:
        runtime = self._ensure_runtime()
        if not self._is_running():
            self.start()
            runtime = self._ensure_runtime()

        dispatch = runtime.run_ad_hoc_task(game_name, task_ref, inputs or {})
        dispatch = _normalize_run_row(dispatch)
        dispatch.setdefault("game_name", game_name)
        if wait and dispatch.get("cid"):
            return {
                "dispatch": dispatch,
                "run": self.wait_for_run(str(dispatch["cid"]), timeout_sec=timeout_sec),
            }
        return dispatch

    def wait_for_run(self, cid: str, *, timeout_sec: float = 600.0, poll_interval_sec: float = 0.1) -> Dict[str, Any]:
        runtime = self._ensure_runtime()
        deadline = time.time() + max(float(timeout_sec), 1.0)

        while time.time() < deadline:
            rows = runtime.get_batch_task_status([cid])
            row = rows[0] if rows else {}
            status = str(row.get("status") or "").strip().lower()
            if status in _TERMINAL_STATUSES:
                return {
                    "summary": _normalize_run_row(row),
                    "detail": _normalize_run_row(runtime.get_run_detail(cid)),
                }
            time.sleep(max(float(poll_interval_sec), 0.05))

        raise TimeoutError(f"Timed out waiting for run '{cid}' after {timeout_sec} seconds.")

    def cancel_task(self, cid: str) -> Dict[str, Any]:
        runtime = self._ensure_runtime()
        return runtime.cancel_task(str(cid))

    def target_status(self, *, game_name: str) -> Dict[str, Any]:
        runtime = self._ensure_running_runtime()
        del runtime
        token = current_plan_name.set(str(game_name))
        try:
            target_runtime = service_registry.get_service_instance("target_runtime")
            summary = target_runtime.target_summary()
            return {
                "ok": True,
                "game_name": game_name,
                "target": summary,
            }
        finally:
            current_plan_name.reset(token)

    def target_snapshot(self, *, game_name: str, backend: Optional[str] = None) -> Dict[str, Any]:
        runtime = self._ensure_running_runtime()
        del runtime
        token = current_plan_name.set(str(game_name))
        try:
            target_runtime = service_registry.get_service_instance("target_runtime")
            capture = target_runtime.capture(backend=backend)
            target = target_runtime.target_summary()
            if not capture.success or capture.image is None:
                return {
                    "ok": False,
                    "game_name": game_name,
                    "backend": capture.backend,
                    "target": target,
                    "message": capture.error_message or "目标窗口截图失败。",
                    "quality_flags": list(capture.quality_flags),
                }
            image_png = _capture_image_to_png_bytes(capture.image)
            return {
                "ok": True,
                "game_name": game_name,
                "backend": capture.backend,
                "target": target,
                "image_png": image_png,
                "image_size": list(capture.image_size or (0, 0)),
                "window_rect": list(capture.window_rect) if capture.window_rect is not None else None,
                "relative_rect": list(capture.relative_rect) if capture.relative_rect is not None else None,
                "quality_flags": list(capture.quality_flags),
            }
        finally:
            current_plan_name.reset(token)

    def get_run(self, cid: str) -> Dict[str, Any]:
        runtime = self._ensure_runtime()
        return _normalize_run_row(runtime.get_run_detail(cid))

    def _ensure_running_runtime(self):
        runtime = self._ensure_runtime()
        if not self._is_running():
            self.start()
            runtime = self._ensure_runtime()
        return runtime

    def list_runs(
        self,
        *,
        limit: int = 50,
        game_name: Optional[str] = None,
        task_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        runtime = self._ensure_runtime()
        rows = runtime.list_run_history(limit=limit, plan_name=game_name, task_name=task_name, status=status)
        return [_normalize_run_row(row) for row in rows]

    def poll_events(self, *, limit: int = 100, timeout_sec: float = 0.0) -> List[Dict[str, Any]]:
        runtime = self._ensure_runtime()
        event_queue = runtime.get_ui_event_queue()
        items: List[Dict[str, Any]] = []
        max_items = max(int(limit), 0)
        if max_items == 0:
            return items

        deadline = time.time() + max(float(timeout_sec), 0.0)
        while len(items) < max_items:
            try:
                if items:
                    item = event_queue.get_nowait()
                elif timeout_sec > 0:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    item = event_queue.get(timeout=remaining)
                else:
                    item = event_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, dict):
                items.append(_normalize_event_row(item))
        return items

    def doctor(self, *, include_shared: bool = True) -> Dict[str, Any]:
        runtime = self._ensure_runtime()
        games = self.list_games(include_shared=include_shared)
        runtime_target = None
        try:
            screen_service = service_registry.get_service_instance("screen")
            runtime_target = screen_service.self_check()
        except Exception as exc:
            runtime_target = {
                "ok": False,
                "message": str(exc),
            }
        return {
            "framework": "Aura Game Framework",
            "profile": self.profile,
            "status": self.status(),
            "runtime_target": runtime_target,
            "games": games,
            "counts": {
                "game_count": len([item for item in games if item.get("kind") == "game"]),
                "shared_count": len([item for item in games if item.get("kind") == "shared"]),
                "action_count": len(runtime.actions),
                "service_count": len(runtime.get_all_services_for_api()),
            },
        }


def _subprocess_entry(connection, profile: str, startup_timeout_sec: int) -> None:
    runner = EmbeddedGameRunner(profile=profile, startup_timeout_sec=startup_timeout_sec)
    try:
        while True:
            try:
                message = connection.recv()
            except (EOFError, BrokenPipeError, OSError):
                break
            op = message.get("op")
            kwargs = dict(message.get("kwargs") or {})
            if op == "close":
                runner.close()
                try:
                    connection.send({"ok": True, "result": None})
                except (EOFError, BrokenPipeError, OSError):
                    pass
                break

            try:
                method = getattr(runner, op)
                result = method(**kwargs)
                try:
                    connection.send({"ok": True, "result": result})
                except (EOFError, BrokenPipeError, OSError):
                    break
            except Exception as exc:  # noqa: BLE001
                try:
                    connection.send(
                        {
                            "ok": False,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                                "traceback": traceback.format_exc(),
                            },
                        }
                    )
                except (EOFError, BrokenPipeError, OSError):
                    break
    finally:
        runner.close()
        try:
            connection.close()
        except Exception:
            pass


class SubprocessGameRunner:
    """Spawn-isolated local SDK for GUI or host-process integration."""

    def __init__(
        self,
        *,
        profile: str = _DEFAULT_PROFILE,
        startup_timeout_sec: Optional[int] = None,
        env_overrides: Mapping[str, str] | None = None,
    ):
        self.profile = profile
        self.startup_timeout_sec = int(startup_timeout_sec or _startup_timeout_sec())
        self.env_overrides = {str(key): str(value) for key, value in dict(env_overrides or {}).items()}
        self._ctx = multiprocessing.get_context("spawn")
        self._parent_conn = None
        self._process: Optional[multiprocessing.Process] = None

    def _start_process_with_env(self, process: multiprocessing.Process) -> None:
        if not self.env_overrides:
            process.start()
            return

        with _ENV_START_LOCK:
            previous: dict[str, str | None] = {key: os.environ.get(key) for key in self.env_overrides}
            try:
                os.environ.update(self.env_overrides)
                process.start()
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.is_alive():
            return

        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(
            target=_subprocess_entry,
            args=(child_conn, self.profile, self.startup_timeout_sec),
            daemon=True,
        )
        self._start_process_with_env(process)
        child_conn.close()
        self._parent_conn = parent_conn
        self._process = process

    def _request_timeout_sec(self, op: str, kwargs: Mapping[str, Any]) -> float:
        if op == "start":
            return max(
                float(self.startup_timeout_sec) + _SUBPROCESS_START_MARGIN_SEC,
                _SUBPROCESS_REQUEST_MIN_TIMEOUT_SEC,
            )
        if op == "run_task" and kwargs.get("wait"):
            return max(float(kwargs.get("timeout_sec") or 600.0), self.startup_timeout_sec, 30.0) + 5.0
        if op == "poll_events":
            return max(float(kwargs.get("timeout_sec") or 0.0), 5.0)
        return max(float(self.startup_timeout_sec), _SUBPROCESS_REQUEST_MIN_TIMEOUT_SEC)

    def _discard_process(self) -> None:
        parent_conn = self._parent_conn
        self._parent_conn = None
        if parent_conn is not None:
            try:
                parent_conn.close()
            except Exception:
                pass

        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
        except Exception:
            pass
        try:
            process.join(timeout=5)
        except Exception:
            pass

    def _request(self, op: str, **kwargs):
        self._ensure_process()
        assert self._parent_conn is not None

        self._parent_conn.send({"op": op, "kwargs": kwargs})
        timeout = self._request_timeout_sec(op, kwargs)
        if not self._parent_conn.poll(timeout):
            self._discard_process()
            raise TimeoutError(f"Subprocess runner request '{op}' timed out after {timeout:.1f}s.")

        payload = self._parent_conn.recv()
        if not payload.get("ok"):
            error = payload.get("error") or {}
            raise RuntimeError(f"{error.get('type', 'SubprocessError')}: {error.get('message', 'unknown error')}")
        return payload.get("result")

    def start(self) -> Dict[str, Any]:
        return self._request("start")

    def stop(self) -> Dict[str, Any]:
        return self._request("stop")

    def status(self) -> Dict[str, Any]:
        return self._request("status")

    def list_games(self, *, include_shared: bool = False) -> List[Dict[str, Any]]:
        return self._request("list_games", include_shared=include_shared)

    def list_tasks(self, game_name: str) -> List[Dict[str, Any]]:
        return self._request("list_tasks", game_name=game_name)

    def run_task(
        self,
        *,
        game_name: str,
        task_ref: str,
        inputs: Optional[Dict[str, Any]] = None,
        wait: bool = False,
        timeout_sec: float = 600.0,
    ) -> Dict[str, Any]:
        return self._request(
            "run_task",
            game_name=game_name,
            task_ref=task_ref,
            inputs=inputs or {},
            wait=wait,
            timeout_sec=timeout_sec,
        )

    def get_run(self, cid: str) -> Dict[str, Any]:
        return self._request("get_run", cid=cid)

    def cancel_task(self, cid: str) -> Dict[str, Any]:
        return self._request("cancel_task", cid=cid)

    def target_status(self, *, game_name: str) -> Dict[str, Any]:
        return self._request("target_status", game_name=game_name)

    def target_snapshot(self, *, game_name: str, backend: Optional[str] = None) -> Dict[str, Any]:
        return self._request("target_snapshot", game_name=game_name, backend=backend)

    def list_runs(
        self,
        *,
        limit: int = 50,
        game_name: Optional[str] = None,
        task_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._request(
            "list_runs",
            limit=limit,
            game_name=game_name,
            task_name=task_name,
            status=status,
        )

    def poll_events(self, *, limit: int = 100, timeout_sec: float = 0.0) -> List[Dict[str, Any]]:
        return self._request("poll_events", limit=limit, timeout_sec=timeout_sec)

    def doctor(self, *, include_shared: bool = True) -> Dict[str, Any]:
        return self._request("doctor", include_shared=include_shared)

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.is_alive():
                self._request("close")
        except Exception:
            pass
        finally:
            if self._parent_conn is not None:
                self._parent_conn.close()
                self._parent_conn = None
            process = self._process
            if process is not None:
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            self._process = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _capture_image_to_png_bytes(image: Any) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()

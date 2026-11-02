"""Bounded, observed refresh-only UI transactions; a match is never applied.

Native game scripts are build-time references only. Runtime inputs go through
the normal Aura app service; no game business function or network API is used.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable

from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ....aura_base.src.actions.input_actions import click as aura_click
from ._affix_refresh_policy import (
    PolicyError, affix_signature, load_catalog, matches,
    preflight, validate_config, validate_frame,
)
from ._affix_refresh_vision import AffixRefreshVision
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


PROGRESS_EVENT = "task.resonance_pc_affix_refresh_progress"
PROGRESS_SCHEMA = "resonance_pc.affix_refresh_progress.v1"
PLAN_ROOT = Path(__file__).resolve().parents[2]
OBSERVATION_TIMEOUT = 15.0
TRANSACTION_TIMEOUT = 30.0
POLL_INTERVAL = 0.25
# Allow the game to finish refreshing before accepting a stable candidate.
# Text may legitimately be identical across consecutive rounds.
REFRESH_SETTLE_SECONDS = 1.0


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("刷新词缀已停止")


async def _blocking(call: Callable, *args, **kwargs):
    """Join in-flight capture/input workers before allowing cancellation out."""
    _check_cancelled()
    stopped = threading.Event()

    def work():
        if stopped.is_set():
            raise asyncio.CancelledError()
        _check_cancelled()
        return call(*args, **kwargs)

    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        stopped.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
    _check_cancelled()
    return result


def _plan_root(engine) -> Path:
    root = getattr(getattr(engine, "orchestrator", None), "current_plan_path", None)
    return Path(root).resolve() if root else PLAN_ROOT


def _snapshot(frame: dict) -> dict:
    """Never persist images or OCR implementation objects in public state."""
    return copy.deepcopy({key: frame[key] for key in (
        "valid", "scene", "candidate", "replace_mode",
    ) if key in frame})


def _point(frame: dict, key: str) -> list[int]:
    if key not in {"refresh", "dialog_confirm"}:
        raise PolicyError("input_disabled", "当前版本只允许刷新及刷新确认。")
    point = frame.get("controls", {}).get(key)
    if (not isinstance(point, (list, tuple)) or len(point) != 2
            or any(type(v) is not int for v in point)
            or not 0 <= point[0] < 1280 or not 0 <= point[1] < 720):
        raise PolicyError("control_unconfirmed", f"无法确认操作位置：{key}。")
    return list(point)


def _frame_signature(frame: dict) -> tuple:
    if frame.get("scene") != "affix":
        controls = frame.get("controls", {})
        return (frame.get("scene"), tuple(controls.get("dialog_confirm", ())),
                tuple(controls.get("dialog_cancel", ())))
    return (affix_signature(frame["candidate"]) if frame.get("candidate") else None,
            frame.get("replace_mode"))


class AffixRefreshRuntime:
    def __init__(self, *, mode, app, vision, ocr, state_store, event_bus, engine, context):
        self.root = _plan_root(engine)
        self.catalog = load_catalog(self.root)
        self.observer = AffixRefreshVision(app, vision, self.catalog, self.root, ocr=ocr)
        self.app, self.store, self.bus = app, state_store, event_bus
        cid = str(getattr(context, "data", {}).get("cid")
                  or getattr(getattr(engine, "root_context", None), "data", {}).get("cid") or "")
        if not cid:
            raise PolicyError("cid_missing", "框架没有提供本次运行标识。")
        self.key = f"affix_refresh:{cid}"
        self.state = {
            "schema": PROGRESS_SCHEMA, "cid": cid, "sequence": 0, "mode": mode,
            "status": "running", "stage": "read" if mode == "read" else "preflight",
            "refresh_count": 0, "refresh_limit": 0, "current_candidate": None,
            # Legacy result aliases: spending is derived from submitted rounds,
            # never from an observed balance. No separate reservation is used.
            "film_spent": 0, "film_reserved": 0, "film_limit": 0, "film_remaining": None,
            "matched": False, "replaced": False, "auto_replace": False, "result_uncertain": False,
        }
        self.config: dict = {}
        self.last_frame: dict = {}

    async def commit(self, stage: str | None = None, **fields):
        if stage:
            self.state["stage"] = stage
        self.state.update(fields)
        # Expose the real managed log to the GUI; it can tail OCR records by CID.
        from packages.aura_core.observability.logging.coordinator import get_current_log_path
        path = get_current_log_path()
        if path:
            self.state["ocr_log_path"] = path
        self.state["sequence"] += 1
        payload = copy.deepcopy(self.state)
        if self.store is not None:
            await self.store.set(self.key, payload)
        if self.bus is not None:
            try:
                await self.bus.publish(Event(name=PROGRESS_EVENT, payload=payload))
            except Exception as exc:
                logger.warning("Affix refresh progress delivery failed: %s", exc)

    async def wait(self, predicate: Callable[[dict], bool], *, timeout=OBSERVATION_TIMEOUT,
                   label="词缀界面") -> dict:
        deadline, previous, count = time.monotonic() + timeout, None, 0
        last_error = ""
        while time.monotonic() < deadline:
            _check_cancelled()
            frame = await _blocking(self.observer.observe)
            if isinstance(frame, dict) and frame.get("valid") is True:
                self.last_frame = frame
                try:
                    accepted = predicate(frame)
                except Exception as exc:
                    await self.save_observation_failure(label, str(exc))
                    raise
                if accepted:
                    signature = _frame_signature(frame)
                    count = count + 1 if signature == previous else 1
                    previous = signature
                    if count >= 2:
                        return frame
                else:
                    previous, count = None, 0
            else:
                previous, count = None, 0
                last_error = str(frame.get("error") or "") if isinstance(frame, dict) else "Invalid observation"
            await asyncio.sleep(POLL_INTERVAL)
        logger.warning("[AffixRefresh] cid=%s observation_timeout label=%s last_error=%s",
                       self.state["cid"], label, last_error)
        await self.save_observation_failure(label, last_error or "Observation did not stabilize before timeout")
        raise PolicyError("observation_timeout", f"无法确认{label}，已停止；不会重复提交刷新。"
                          + (f" 识别原因：{last_error}" if last_error else ""))

    async def save_observation_failure(self, label: str, error: str) -> None:
        # Save only when this observation wait fails, not on every polling
        # rejection. Evidence I/O must not hide the original recognition error.
        try:
            evidence = await _blocking(self.observer.save_failure_evidence,
                                       cid=self.state["cid"], label=label, error=error)
        except Exception as exc:
            logger.warning("[AffixRefreshScreenshot] cid=%s status=failed error=%s",
                           self.state["cid"], exc)
            return
        self.state.setdefault("failure_evidence", []).append(evidence)
        logger.warning("[AffixRefreshScreenshot] cid=%s evidence=%s", self.state["cid"],
                       json.dumps(evidence, ensure_ascii=False, default=str))

    def guard(self, frame: dict):
        validate_frame(frame, self.catalog)

    async def current(self) -> dict:
        def accept(frame):
            if frame.get("scene") != "affix":
                return False
            self.guard(frame)
            return True
        return await self.wait(accept)

    async def click(self, point: list[int]):
        _check_cancelled()

        def send():
            # The common click action uses the selected system/bridge input mode.
            result = aura_click(app=self.app, x=point[0], y=point[1])
            if result is False:
                raise PolicyError("input_failed", "游戏点击没有成功。")

        await _blocking(send)
        await asyncio.sleep(POLL_INTERVAL)
        _check_cancelled()

    async def terminal(self, status: str, reason: str, message: str, **fields) -> dict:
        await self.commit(status=status, reason=reason, message=message,
                          success=status in {"completed", "stopped", "already_satisfied"}, **fields)
        logger.info("[AffixRefresh] cid=%s status=%s reason=%s rounds=%s limit=%s replaced=%s",
                    self.state["cid"], status, reason, self.state["refresh_count"],
                    self.state["refresh_limit"], self.state["replaced"])
        return copy.deepcopy(self.state)

    async def read(self) -> dict:
        await self.commit()
        await _blocking(resonance_pc_require_client_resolution, app=self.app)
        frame = await self.current()
        return await self.terminal(
            "completed", "read_completed", "已读取当前词缀界面。",
            snapshot=_snapshot(frame), eligible_affixes=copy.deepcopy(self.catalog["affixes"]),
            current_candidate=frame.get("candidate"),
        )

    async def refresh(self, frame: dict) -> dict:
        self.guard(frame)
        if self.state["refresh_count"] >= self.state["refresh_limit"]:
            raise PolicyError("refresh_limit_reached", "刷新轮数已达到上限。")
        point = _point(frame, "refresh")
        _check_cancelled()
        # Count once at submission, before input can be interrupted. Never
        # refund an uncertain attempt or count its confirmation as another round.
        round_count = self.state["refresh_count"] + 1
        await self.commit("refresh", refresh_count=round_count, film_spent=round_count,
                          result_uncertain=True)
        await self.click(point)
        settled_after = time.monotonic() + REFRESH_SETTLE_SECONDS

        def outcome(observed):
            scene = observed.get("scene")
            if scene == "refresh_confirm":
                _point(observed, "dialog_confirm")
                return True
            if scene != "affix":
                return False
            self.guard(observed)
            return time.monotonic() >= settled_after and observed.get("candidate") is not None

        observed = await self.wait(outcome, timeout=TRANSACTION_TIMEOUT, label="刷新结果或刷新确认提示")
        if observed["scene"] == "refresh_confirm":
            await self.commit("refresh_confirm")
            await self.click(_point(observed, "dialog_confirm"))
            settled_after = time.monotonic() + REFRESH_SETTLE_SECONDS

            def settled(value):
                return value.get("scene") == "affix" and outcome(value)

            observed = await self.wait(settled, timeout=TRANSACTION_TIMEOUT, label="刷新后的候选词缀")
        await self.commit("evaluate",
                          current_candidate=observed["candidate"], result_uncertain=False)
        return observed

    async def run(self, config) -> dict:
        self.config = validate_config(config, self.catalog)
        await self.commit(refresh_limit=self.config["max_film_cost"],
                          film_limit=self.config["max_film_cost"], auto_replace=False)
        await _blocking(resonance_pc_require_client_resolution, app=self.app)
        frame = await self.current()
        preflight(self.config, _snapshot(frame), self.catalog)
        await self.commit(current_candidate=frame.get("candidate"))
        while True:
            _check_cancelled()
            self.guard(frame)
            candidate = frame.get("candidate")
            await self.commit("evaluate", current_candidate=candidate)
            # Evaluate the Nth candidate BEFORE enforcing the round limit.
            if candidate is not None and matches(candidate, self.config, self.catalog):
                return await self.terminal(
                    "completed", "target_found", "已命中目标，请手动替换；当前候选已保留。",
                    matched=True, replaced=False, result_uncertain=False, snapshot=_snapshot(frame))
            if self.state["refresh_count"] >= self.state["refresh_limit"]:
                return await self.terminal("stopped", "refresh_limit_reached", "刷新轮数达到上限，未命中目标；保留当前界面。")
            frame = await self.refresh(frame)


async def execute(*, mode, config=None, app=None, vision=None, ocr=None,
                  state_store=None, event_bus=None, engine=None, context=None) -> dict:
    runtime = AffixRefreshRuntime(mode=mode, app=app, vision=vision, ocr=ocr,
                                  state_store=state_store, event_bus=event_bus,
                                  engine=engine, context=context)
    try:
        return await (runtime.read() if mode == "read" else runtime.run(config))
    except asyncio.CancelledError:
        # No cleanup click: a prompt/candidate remains available for the user.
        await runtime.commit(status="cancelled", success=False, reason="user_cancelled",
                             message="已停止；不会继续刷新或替换。")
        raise
    except Exception as exc:
        code = getattr(exc, "code", "affix_refresh_failed")
        message = str(getattr(exc, "message", None) or exc)
        return await runtime.terminal("failed", code, message, error=message)

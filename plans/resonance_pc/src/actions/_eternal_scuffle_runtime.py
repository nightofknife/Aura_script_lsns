"""Confirmed UI operations and CID-isolated state for Eternal Scuffle.

Task ordering lives in the task YAML. Probes are read-only; clicks are only sent
between probes, with a fresh scene/identity guard before every retry.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ....aura_base.src.actions._shared import poll_until
from ....aura_base.src.actions.input_actions import click as aura_click
from ....aura_base.src.actions.wait_actions import sleep as aura_sleep
from ._eternal_scuffle_policy import (
    SLOT_TYPES, choose_character, choose_equipment, choose_loot, load_catalog, validate_inputs,
)
from ._eternal_scuffle_vision import ScuffleVision
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution

SCHEMA = "resonance_pc.eternal_scuffle_session.v1"
PROGRESS_EVENT = "task.resonance_pc_eternal_scuffle_progress"
PROGRESS_SCHEMA = "resonance_pc.eternal_scuffle_progress.v1"
POLL_INTERVAL = 0.3
CONTROL_TIMEOUT = 20.0
TRANSITION_TIMEOUT = 45.0
BATTLE_TIMEOUT = 600.0
CLICK_SETTLE = 0.3
RETRY_WINDOW = 2.0
MAX_CLICKS = 4
COIN_INTERVAL = 0.4
PLAN_ROOT = Path(__file__).resolve().parents[2]


class ScuffleError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code, self.message, self.detail = code, message, detail or {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("无垠乱斗已取消")


async def _join_worker(task, stopped):
    """Cancellation must not leave an input or observer running after return."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume a late worker error; cancellation wins.
        raise


async def _run_blocking(call, *args, check_cancel=True, **kwargs):
    stopped = threading.Event()
    if check_cancel:
        check_cancelled()

    def work():
        if stopped.is_set():
            raise asyncio.CancelledError()
        if check_cancel:
            check_cancelled()
        return call(*args, **kwargs)

    result = await _join_worker(asyncio.create_task(asyncio.to_thread(work)), stopped)
    if check_cancel:
        check_cancelled()
    return result


async def _poll_until(*, timeout, interval, probe, predicate):
    # Keep the shared poller's worker-thread probes, but join an in-flight probe
    # on cancellation before diagnostics or another action can reuse its cache.
    stopped = threading.Event()
    deadline = time.monotonic() + max(float(timeout), 0.0)

    def guarded_probe():
        if stopped.is_set():
            raise asyncio.CancelledError()
        check_cancelled()
        result = probe()
        if stopped.is_set():
            raise asyncio.CancelledError()
        check_cancelled()
        return result

    return await _join_worker(asyncio.create_task(poll_until(
        timeout=timeout, interval=interval, probe=guarded_probe,
        predicate=lambda result: time.monotonic() <= deadline and predicate(result),
    )), stopped)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return str(value)


def plan_root_for(engine=None) -> Path:
    path = getattr(getattr(engine, "orchestrator", None), "current_plan_path", None)
    return Path(path).resolve() if path else PLAN_ROOT


def catalog_for(engine=None) -> dict:
    root = plan_root_for(engine)
    cache = getattr(getattr(getattr(engine, "root_context", None), "plan_context", None), "cache", None)
    key = f"eternal_scuffle.catalog:{root}"
    catalog = cache.get(key) if cache is not None else None
    if catalog is None:
        catalog = load_catalog(root)
        if cache is not None:
            cache.set(key, catalog)
    return catalog


def make_observer(app, vision, catalog, engine=None):
    """Separate construction seam for read-only scripted replay tests."""
    cache = getattr(getattr(getattr(engine, "root_context", None), "plan_context", None), "cache", None)
    key = f"eternal_scuffle.observer:{plan_root_for(engine)}:{id(app)}:{id(vision)}"
    observer = cache.get(key) if cache is not None else None
    if observer is None:
        observer = ScuffleVision(app, vision, catalog, plan_root_for(engine))
        if cache is not None:
            cache.set(key, observer)
    return observer


async def read_session(state_store, key: str) -> dict:
    state = await state_store.get(key)
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise ScuffleError("scuffle_session_missing", "运行会话不存在，请从活动首页重新启动。")
    if key != "eternal_scuffle:" + str(state.get("cid")):
        raise ScuffleError("scuffle_session_mismatch", "运行会话标识不一致。")
    return copy.deepcopy(state)


def child_completed(result: Any, label: str) -> dict:
    """aura.run_task returns framework_data, not the task's user_data."""
    output = (result or {}).get("nodes", {}).get("finish", {}).get("output") if isinstance(result, dict) else None
    if not isinstance(output, dict) or output.get("success") is not True or output.get("status") != "completed":
        raise ScuffleError("scuffle_child_incomplete", f"{label}未返回已确认的完成结果。")
    return output


class ScuffleRuntime:
    def __init__(self, state, state_store, app, vision, event_bus=None, engine=None):
        self.state, self.store = state, state_store
        self.app, self.vision, self.event_bus, self.engine = app, vision, event_bus, engine
        self.key = "eternal_scuffle:" + state["cid"]
        self.catalog = catalog_for(engine)
        self.observer = make_observer(app, vision, self.catalog, engine)
        self.last_observation: dict = {}
        self.last_target: dict = {}
        self.log_dir = Path(state["log_dir"])
        self.equipment = {int(r["id"]): r for r in self.catalog["equipment"]}

    @property
    def round(self):
        return self.state["round"]

    async def record(self, kind: str, **fields):
        await _run_blocking(self._record_sync, kind, **fields)

    def _record_sync(self, kind: str, **fields):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        row = {"type": kind, "time": time.time(), "run_index": self.state["run_index"], **fields}
        with (self.log_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_json_safe(row), ensure_ascii=False, allow_nan=False) + "\n")

    async def commit(self, stage: str | None = None, **event_fields):
        if stage is not None:
            self.state["stage"] = stage
        self.state["sequence"] += 1
        await self.store.set(self.key, copy.deepcopy(self.state))
        payload = {
            "schema": PROGRESS_SCHEMA, "cid": self.state["cid"],
            "sequence": self.state["sequence"], "run_index": self.state["run_index"],
            "run_count": self.state["run_count"], "completed_runs": self.state["completed_runs"],
            "stage": self.state["stage"], "status": self.state["status"],
            "log_dir": str(self.log_dir), **event_fields,
        }
        if self.event_bus is not None:
            try:
                await self.event_bus.publish(Event(name=PROGRESS_EVENT, payload=payload))
            except Exception as exc:
                logger.warning("Scuffle progress delivery failed: %s", exc)

    def probe(self) -> dict:
        frame = self.observer.observe()
        self.last_observation = frame
        return frame

    async def wait(self, probe: Callable, predicate: Callable, *, timeout=CONTROL_TIMEOUT,
                   stable=2, signature: Callable | None = None, interval=POLL_INTERVAL, label="页面"):
        previous, count = None, 0

        def accept(value):
            nonlocal previous, count
            check_cancelled()
            if not predicate(value):
                previous, count = None, 0
                return False
            current = signature(value) if signature else True
            count = count + 1 if current == previous else 1
            previous = current
            return count >= stable

        check_cancelled()
        ok, result = await _poll_until(timeout=timeout, interval=interval, probe=probe, predicate=accept)
        check_cancelled()
        if not ok:
            raise ScuffleError("scuffle_observation_timeout", f"等待{label}超时。", {"timeout_sec": timeout})
        return result

    async def scene(self, *scenes, timeout=CONTROL_TIMEOUT, interval=POLL_INTERVAL):
        return await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene") in scenes,
                               signature=lambda f: f["scene"], timeout=timeout, interval=interval, label="/".join(scenes))

    async def candidates(self, kind: str):
        started = time.monotonic()

        def probe():
            f = self.probe()
            if not f.get("valid"):
                return []
            rows = self.observer.read_candidates(f, kind, include_spine=time.monotonic()-started >= 3)
            return rows

        rows = await self.wait(probe, lambda r: len(r) == 3, stable=3,
                               signature=lambda r: tuple((v["id"], v["index"]) for v in r),
                               timeout=TRANSITION_TIMEOUT, label="三个候选身份")
        await self.record("candidates", candidate_kind=kind, scene=self.last_observation.get("scene"), candidates=rows)
        return rows

    async def team(self, *, expected_scene: str, selected_id: int | None = None, timeout=TRANSITION_TIMEOUT):
        started = time.monotonic()

        def probe():
            f = self.probe()
            if not f.get("valid") or f.get("scene") != expected_scene:
                return []
            return self.observer.read_team(f, include_spine=time.monotonic()-started >= 3)

        def valid(rows):
            return (len(rows) == 5 and len({int(x["id"]) for x in rows}) == 5
                    and all(x.get("slots", {}).get(slot) in {"empty", "occupied"} for x in rows for slot in SLOT_TYPES)
                    and (selected_id is None or any(int(x["id"]) == selected_id and x.get("selected") for x in rows)))

        return await self.wait(probe, valid, stable=3,
                               signature=lambda r: tuple((x["id"], x["screen_index"], tuple(x["slots"][s] for s in SLOT_TYPES), bool(x.get("selected"))) for x in r),
                               timeout=timeout, label="队伍与装备槽")

    async def selected_character(self, character_id: int, screen_index: int, *, timeout=RETRY_WINDOW):
        def probe():
            frame = self.probe()
            if not frame.get("valid") or frame.get("scene") != "assign":
                return None
            return self.observer.read_selected_character(frame)

        return await self.wait(
            probe, lambda row: bool(row) and row.get("selected") is True
            and int(row["id"]) == character_id and int(row["screen_index"]) == screen_index,
            stable=3, signature=lambda row: (row["id"], row["screen_index"]),
            timeout=timeout, label="目标角色的 SELECT 标识",
        )

    def reconcile_team(self, rows):
        known = {int(r["id"]): r for r in self.round["team"]}
        if set(known) != {int(r["id"]) for r in rows}:
            raise ScuffleError("scuffle_team_mismatch", "画面队员与本局已确认选择不一致。")
        for row in rows:
            member = known[int(row["id"])]
            for slot in SLOT_TYPES:
                expected = member["equipment"][slot]
                if row["slots"][slot] != ("empty" if expected is None else "occupied"):
                    raise ScuffleError("scuffle_slot_mismatch", "画面装备槽与本局记录不一致。", {"character_id": row["id"], "slot": slot})
                actual = row.get("occupied_ids", {}).get(slot)
                if actual is not None and expected is not None and int(actual) != int(expected):
                    raise ScuffleError("scuffle_equipment_mismatch", "装备图案与本局记录不一致。")
            member["screen_index"] = int(row["screen_index"])
        self.round["positions"] = [{"id": r["id"], "screen_index": r["screen_index"], "center": r["center"]} for r in rows]

    async def send_click(self, point, label, attempt=1):
        check_cancelled()
        x, y = map(int, point)
        if not 0 <= x < 1280 or not 0 <= y < 720:
            raise ScuffleError("scuffle_click_outside_client", "点击位置超出游戏客户区。")
        self.last_target = {"label": label, "center": [x, y]}

        def click_and_record():
            aura_click(app=self.app, x=x, y=y)
            self._record_sync("click", target=label, point=[x, y], attempt=attempt)

        await _run_blocking(click_and_record)
        await aura_sleep(CLICK_SETTLE)
        check_cancelled()

    async def click_transition(self, source: str, control: str, targets: tuple[str, ...], *,
                               point_for: Callable | None = None, guard: Callable | None = None,
                               allow_departure=False):
        frame = await self.scene(source)
        start = time.monotonic()
        attempts = 0
        departed = False
        while time.monotonic() - start < TRANSITION_TIMEOUT:
            check_cancelled()
            if attempts == 0 or not departed:
                def locate_target():
                    # Identity guards call the synchronous VisionService API,
                    # just like polling probes, so they must run off its loop.
                    if not frame.get("valid") or frame.get("scene") != source or (guard and not guard(frame)):
                        return False, None
                    point = point_for(frame) if point_for else frame.get("controls", {}).get(control, {}).get("center")
                    return True, point

                valid_target, point = await _run_blocking(locate_target)
                check_cancelled()
                if time.monotonic() - start >= TRANSITION_TIMEOUT:
                    raise ScuffleError("scuffle_transition_timeout", f"点击{control}前复核超时。")
                if not valid_target:
                    raise ScuffleError("scuffle_target_changed", "点击前页面或候选已变化，停止操作。")
                if point is None:
                    raise ScuffleError("scuffle_control_missing", f"未找到可点击控件：{control}。")
                if attempts >= MAX_CLICKS:
                    raise ScuffleError("scuffle_click_ineffective", f"多次点击{control}后仍无效果。")
                attempts += 1
                await self.send_click(point, control, attempts)
            last_kind, count = None, 0

            def probe():
                f = self.probe()
                if not f.get("valid"):
                    return ("invalid", f)
                scene = f.get("scene")
                if scene in targets:
                    return ("target:" + scene, f)
                if scene != source:
                    return ("departed", f)
                if guard and not guard(f):
                    return ("changed", f)
                present = bool(point_for(f)) if point_for else bool(f.get("controls", {}).get(control))
                return ("original" if present else "departed", f)

            def accept(value):
                nonlocal last_kind, count
                check_cancelled()
                kind, _ = value
                if kind == "invalid":
                    count, last_kind = 0, None
                    return False
                count = count + 1 if kind == last_kind else 1
                last_kind = kind
                return count >= 2 and (kind.startswith("target:") or kind == "departed")

            ok, result = await _poll_until(timeout=min(RETRY_WINDOW, max(0, TRANSITION_TIMEOUT-(time.monotonic()-start))),
                                          interval=POLL_INTERVAL, probe=probe, predicate=accept)
            check_cancelled()
            if time.monotonic() - start >= TRANSITION_TIMEOUT:
                raise ScuffleError("scuffle_transition_timeout", f"点击{control}后页面转换超时。")
            kind, frame = result
            if ok and kind.startswith("target:"):
                await self.record("click_confirmed", target=control, scene=frame["scene"], attempts=attempts)
                return frame
            if ok and kind == "departed":
                departed = True
                if allow_departure and frame.get("scene") == "unknown":
                    await self.record("click_departed", target=control, attempts=attempts)
                    return frame
            if departed:
                # Once the original page has gone, never click its coordinates again.
                return await self.scene(*targets, timeout=max(0.1, TRANSITION_TIMEOUT-(time.monotonic()-start)))
            if not (kind == "original" and count >= 2):
                # Invalid captures or changed cards are not proof that a click failed.
                return await self.scene(*targets, timeout=max(0.1, TRANSITION_TIMEOUT-(time.monotonic()-start)))
        raise ScuffleError("scuffle_transition_timeout", f"点击{control}后页面转换超时。")

    async def select_candidate(self, source, kind, candidates, selected, target):
        fingerprint = tuple((int(r["id"]), int(r["index"])) for r in candidates)
        cached_frame, cached_candidate = None, None

        def current(f):
            nonlocal cached_frame, cached_candidate
            if f is cached_frame:
                return cached_candidate
            cached_frame, cached_candidate = f, None
            rows = self.observer.read_candidates(f, kind, include_spine=True)
            if tuple((int(r["id"]), int(r["index"])) for r in rows) != fingerprint:
                return None
            cached_candidate = next((r for r in rows if r["id"] == selected["id"] and r["index"] == selected["index"]), None)
            return cached_candidate

        await self.click_transition(source, "select", (target,),
                                    guard=lambda f: current(f) is not None,
                                    point_for=lambda f: (current(f) or {}).get("select_point"))

    async def begin_round(self, run_index: int):
        if self.state["status"] != "running" or run_index != self.state["completed_runs"] + 1:
            raise ScuffleError("scuffle_previous_round_incomplete", "上一局尚未完成，禁止再次投币。")
        if not 1 <= run_index <= self.state["run_count"]:
            raise ScuffleError("scuffle_run_index_invalid", "局次超出本次设置。")
        await self.scene("home")
        self.state["run_index"] = run_index
        self.state["round"] = {"index": run_index, "phase": "coin", "team": [], "positions": [],
                               "pending_role": None, "pending_loot": None, "outcome": None,
                               "returned_home": False, "started_at": time.time(), "opened_boxes": 0}
        await self.record("round_started")
        await self.commit("coin")

    async def coin(self):
        self.require_phase("coin")
        await self.click_transition("home", "play", ("coin",))
        sequence = ["max"] if self.state["coins_per_run"] == 5 else ["min"] + ["plus"] * (self.state["coins_per_run"] - 1)
        for control in sequence:
            f = await self.scene("coin")
            point = f.get("controls", {}).get(control, {}).get("center")
            if point is None:
                raise ScuffleError("scuffle_coin_control_missing", f"投币控件不可用：{control}。")
            await self.send_click(point, control)
            await aura_sleep(max(0, COIN_INTERVAL - CLICK_SETTLE))
        await self.click_transition("coin", "confirm", ("role_select",))
        self.round["phase"] = "draft"
        await self.commit("choose_role")

    def require_phase(self, *phases):
        check_cancelled()
        if self.state["status"] != "running" or self.round.get("phase") not in phases:
            raise ScuffleError("scuffle_phase_mismatch", "本局阶段与操作不一致。", {"phase": self.round.get("phase"), "expected": phases})

    async def choose_role(self):
        self.require_phase("draft")
        if len(self.round["team"]) >= 5 or self.round["pending_role"] is not None:
            raise ScuffleError("scuffle_draft_invalid", "本局选人记录异常。")
        await self.commit("choose_role")
        await self.scene("role_select")
        candidates = await self.candidates("role")
        selected = choose_character(candidates, self.catalog)
        if any(int(r["id"]) == int(selected["id"]) for r in self.round["team"]):
            raise ScuffleError("scuffle_duplicate_character", "出现本局已选角色，无法唯一分配装备。")
        await self.record("role_choice_intent", selected=selected)
        await self.select_candidate("role_select", "role", candidates, selected, "initial_equipment")
        self.round["pending_role"] = int(selected["id"])
        self.round["phase"] = "draft_equipment"
        await self.record("role_selected", character_id=selected["id"])
        await self.commit("choose_equipment")

    async def choose_initial_equipment(self):
        self.require_phase("draft_equipment")
        candidates = await self.candidates("equipment")
        selected = choose_equipment(candidates, self.catalog)
        target = "captain" if len(self.round["team"]) == 4 else "role_select"
        await self.record("initial_equipment_intent", character_id=self.round["pending_role"], selected=selected)
        await self.select_candidate("initial_equipment", "equipment", candidates, selected, target)
        slots = {slot: None for slot in SLOT_TYPES}
        slots[self.equipment[int(selected["id"])]["slot_type"]] = int(selected["id"])
        self.round["team"].append({"id": self.round["pending_role"], "screen_index": len(self.round["team"]), "equipment": slots})
        self.round["pending_role"] = None
        self.round["phase"] = "captain" if target == "captain" else "draft"
        await self.record("initial_equipment_selected", team=self.round["team"])
        await self.commit("captain" if target == "captain" else "choose_role")

    async def captain(self):
        self.require_phase("captain")
        rows = await self.team(expected_scene="captain")
        self.reconcile_team(rows)
        await self.click_transition("captain", "confirm", ("stage",))
        self.round["phase"] = "stage"
        await self.commit("stage")

    async def advance(self):
        self.require_phase("stage", "battle", "victory", "defeat", "abandon")
        phase = self.round["phase"]
        if phase == "stage":
            await self.commit("stage")
            frame = await self.click_transition("stage", "start", ("victory", "defeat"), allow_departure=True)
            self.round["phase"] = frame["scene"] if frame["scene"] in {"victory", "defeat"} else "battle"
            await self.commit("battle")
        elif phase == "battle":
            frame = await self.scene("victory", "defeat", timeout=BATTLE_TIMEOUT, interval=1.0)
            self.round["phase"] = frame["scene"]
            await self.record("battle_result", outcome=frame["scene"])
            await self.commit("battle")
        elif phase == "victory":
            frame = await self.click_transition("victory", "next", ("loot_select", "settlement"))
            if frame["scene"] == "settlement":
                self.round.update(phase="ready_settlement", outcome="cleared")
                await self.commit("settlement")
            else:
                self.round["phase"] = "loot"
                await self.commit("loot")
        elif phase == "defeat":
            await self.click_transition("defeat", "next", ("stage",))
            self.round["phase"] = "abandon"
            await self.commit("abandon")
        else:
            await self.click_transition("stage", "abandon", ("abandon_confirm",))
            await self.click_transition("abandon_confirm", "confirm", ("settlement",))
            self.round.update(phase="ready_settlement", outcome="abandoned")
            await self.commit("settlement")

    async def choose_loot(self):
        self.require_phase("loot")
        await self.scene("loot_select")
        candidates = await self.candidates("equipment")
        decision = choose_loot(candidates, self.round["team"], self.catalog)
        selected = next(r for r in candidates if int(r["id"]) == decision["equipment_id"])
        await self.record("loot_choice_intent", decision=decision, candidates=candidates)
        await self.select_candidate("loot_select", "equipment", candidates, selected, "assign")
        self.round["pending_loot"] = {"equipment_id": decision["equipment_id"], "candidates": _json_safe(candidates)}
        self.round["phase"] = "assign"
        await self.commit("assign")

    async def assign_loot(self):
        self.require_phase("assign")
        rows = await self.team(expected_scene="assign")
        self.reconcile_team(rows)
        pending = self.round["pending_loot"]
        # Recompute recipient using the CURRENT screen order (including fallback).
        decision = choose_loot(pending["candidates"], self.round["team"], self.catalog)
        if decision["equipment_id"] != pending["equipment_id"]:
            raise ScuffleError("scuffle_loot_decision_changed", "装备记录变化，无法确认当前分配。")
        target = next(r for r in rows if int(r["id"]) == decision["character_id"])
        for attempt in range(1, MAX_CLICKS + 1):
            if target.get("selected"):
                break
            await self.send_click(target["center"], "character", attempt)
            try:
                await self.selected_character(decision["character_id"], int(target["screen_index"]))
                target = {**target, "selected": True}
                break
            except ScuffleError as exc:
                if exc.code != "scuffle_observation_timeout" or attempt == MAX_CLICKS:
                    raise
                rows = await self.team(expected_scene="assign")
                self.reconcile_team(rows)
                target = next(r for r in rows if int(r["id"]) == decision["character_id"])
        if not target.get("selected"):
            raise ScuffleError("scuffle_selection_unconfirmed", "无法确认目标角色已选中。")
        # Full inventory reconciliation has its own identity timeout. It is not
        # part of the short, SELECT-only click response window.
        rows = await self.team(expected_scene="assign", selected_id=decision["character_id"])
        self.reconcile_team(rows)
        target = next(row for row in rows if int(row["id"]) == decision["character_id"])

        def still_selected(frame):
            selected = self.observer.read_selected_character(frame)
            return (bool(selected) and selected.get("selected") is True
                    and int(selected["id"]) == decision["character_id"]
                    and int(selected["screen_index"]) == int(target["screen_index"]))

        await self.record("assignment_intent", decision=decision)
        frame = await self.click_transition("assign", "confirm", ("stage", "settlement"), guard=still_selected)
        member = next(r for r in self.round["team"] if int(r["id"]) == decision["character_id"])
        slot = self.equipment[decision["equipment_id"]]["slot_type"]
        member["equipment"][slot] = decision["equipment_id"]
        self.round["pending_loot"] = None
        self.round["phase"] = "stage" if frame["scene"] == "stage" else "ready_settlement"
        if frame["scene"] == "settlement":
            self.round["outcome"] = "cleared"
        await self.record("equipment_assigned", decision=decision, team=self.round["team"])
        await self.commit("stage" if frame["scene"] == "stage" else "settlement")

    async def open_box(self):
        self.require_phase("ready_settlement")
        frame = await self.scene("settlement")
        boxes = frame.get("boxes", [])
        if not boxes:
            await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene") == "settlement" and not f.get("boxes") and bool(f.get("controls", {}).get("back")),
                            stable=3, label="全部奖励已打开及返回按钮")
            self.round["phase"] = "return_home"
            await self.commit("return_home")
            return
        target = min(boxes, key=lambda b: (b["center"][1], b["center"][0]))
        x,y=target["center"]
        def present(f):
            return next((b for b in f.get("boxes", []) if abs(b["center"][0]-x)<30 and abs(b["center"][1]-y)<30), None)
        for attempt in range(1, MAX_CLICKS+1):
            frame = await self.scene("settlement")
            current = present(frame)
            if current is None:
                # Never increment from a single missed match.
                await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene")=="settlement" and present(f) is None, stable=3, timeout=10, label="宝箱已揭开")
                break
            await self.send_click(current["center"], "box", attempt)
            try:
                await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene")=="settlement" and present(f) is None, stable=3, timeout=RETRY_WINDOW, label="宝箱已揭开")
                break
            except ScuffleError as exc:
                if exc.code != "scuffle_observation_timeout" or attempt==MAX_CLICKS:
                    raise
                # A retry needs fresh, valid evidence of the same unopened box.
                await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene")=="settlement" and present(f) is not None, stable=2, timeout=10, label="原宝箱仍未打开")
        self.round["opened_boxes"] += 1
        await self.record("box_opened", position=[x,y], opened_boxes=self.round["opened_boxes"])
        await self.commit("settlement")

    async def return_home(self):
        self.require_phase("return_home")
        await self.wait(self.probe, lambda f: f.get("valid") and f.get("scene")=="settlement" and not f.get("boxes") and bool(f.get("controls",{}).get("back")), stable=3, label="结算可返回")
        await self.click_transition("settlement", "back", ("home",), guard=lambda f: not f.get("boxes"))
        self.round.update(returned_home=True, phase="home")
        await self.commit("return_home")

    async def complete_round(self):
        self.require_phase("home")
        if not self.round["returned_home"] or self.round["outcome"] not in {"cleared","abandoned"}:
            raise ScuffleError("scuffle_round_not_complete", "本局没有完成结算返回，不能计数。")
        if self.state["completed_runs"] != self.state["run_index"]-1:
            raise ScuffleError("scuffle_duplicate_round_commit", "本局已计数或局次不一致。")
        await self.scene("home")
        result={"run_index":self.state["run_index"],"outcome":self.round["outcome"],"elapsed_ms":max(0,int((time.time()-self.round["started_at"])*1000))}
        await self.record("round_completed", result=result, team=self.round["team"])
        self.state["completed_runs"] += 1
        self.state["cleared_runs" if result["outcome"]=="cleared" else "abandoned_runs"] += 1
        self.round["phase"]="completed"
        await self.commit("return_home", round_result=result)
        return {"success":True,"status":"completed",**result}

    async def checkpoint(self, phases=None, expected_pairs=0, child_result=None, child_results=None,
                         require_child=False, expected_children=0, label="子任务"):
        self.require_phase(*(phases or [self.round.get("phase")]))
        if require_child or child_result is not None:
            child_completed(child_result,label)
        if expected_children and (not isinstance(child_results,list) or len(child_results)!=expected_children):
            raise ScuffleError("scuffle_child_count_mismatch",f"{label}的已完成子任务数量不一致。")
        if child_results is not None:
            for result in child_results:
                child_completed(result,label)
        if expected_pairs and len(self.round["team"]) != expected_pairs:
            raise ScuffleError("scuffle_pair_count_mismatch","已确认选人配装组数不一致。")
        return {"success":True,"status":"completed","phase":self.round["phase"]}

    def _save_failure(self, error, cancelled):
        self.log_dir.mkdir(parents=True,exist_ok=True)
        image=self.last_observation.get("_image")
        if image is not None:
            Image.fromarray(image).save(self.log_dir/"last_frame.png")
            x,y=self.last_target.get("center",[640,360]);w,h=100,60
            Image.fromarray(image).crop((max(0,x-w),max(0,y-h),min(1280,x+w),min(720,y+h))).save(self.log_dir/"last_target.png")
        detail={"error":error,"state":self.state,"observation":_json_safe(self.last_observation),"last_target":self.last_target}
        (self.log_dir/"failure.json").write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding="utf-8")
        self._record_sync("cancelled" if cancelled else "failure", error=error, phase=self.round.get("phase"))

    async def fail(self, exc, cancelled=False):
        self.state["status"]="cancelled" if cancelled else "failed"
        error=str(exc) or "用户取消了无垠乱斗任务。"
        try:
            await _run_blocking(self._save_failure, error, cancelled, check_cancel=False)
        except Exception as diagnostic_error:
            logger.error("Scuffle diagnostics failed: %s",diagnostic_error)
        finally:
            await self.commit(error=error)


async def initialize(coins_per_run,run_count,app,vision,state_store,event_bus=None,engine=None,context=None):
    inputs=validate_inputs(coins_per_run,run_count)
    check_cancelled()
    await _run_blocking(resonance_pc_require_client_resolution, app=app)
    cid=str(getattr(context,"data",{}).get("cid") or getattr(getattr(engine,"root_context",None),"data",{}).get("cid") or "")
    if not cid:
        raise ScuffleError("scuffle_cid_missing","框架没有提供本次运行标识。")
    log_dir=plan_root_for(engine).parents[1]/"logs"/"eternal_scuffle"/re.sub(r"[^a-zA-Z0-9_-]","_",cid)
    state={"schema":SCHEMA,"cid":cid,**inputs,"run_index":0,"completed_runs":0,"cleared_runs":0,"abandoned_runs":0,
           "stage":"preflight","status":"running","sequence":0,"log_dir":str(log_dir),"round":{"phase":"idle"}}
    runtime=await _run_blocking(ScuffleRuntime,state,state_store,app,vision,event_bus,engine)
    try:
        await runtime.commit("preflight")
        await runtime.record("session_started",inputs=inputs,ranking_version=runtime.catalog.get("ranking_version"))
        await runtime.scene("home")
    except asyncio.CancelledError as exc:
        await runtime.fail(exc,True);raise
    except Exception as exc:
        await runtime.fail(exc);raise ScuffleError("scuffle_start_failed",f"启动检查失败：{exc}；诊断：{log_dir}") from exc
    return {"success":True,"session_key":runtime.key,"log_dir":str(log_dir)}


async def invoke(session_key,operation,app,vision,state_store,event_bus=None,engine=None,**params):
    state=await read_session(state_store,session_key)
    runtime=await _run_blocking(ScuffleRuntime,state,state_store,app,vision,event_bus,engine)
    try:
        check_cancelled()
        result=await getattr(runtime,operation)(**params)
        return result if result is not None else {"success":True,"status":"completed","phase":runtime.round["phase"]}
    except asyncio.CancelledError as exc:
        await runtime.fail(exc,True);raise
    except Exception as exc:
        await runtime.fail(exc)
        raise ScuffleError(getattr(exc,"code","scuffle_operation_failed"),f"{exc}；已完成{state['completed_runs']}局；诊断：{runtime.log_dir}") from exc


def _finish_journal(state):
    rounds=[]
    with (Path(state["log_dir"])/"events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row.get("type")=="round_completed":rounds.append(row["result"])
    if [r["run_index"]for r in rounds]!=list(range(1,state["run_count"]+1)):
        raise ScuffleError("scuffle_journal_incomplete","每局结果日志不完整，不能报告全部完成。")
    result={"success":True,"status":"completed","run_count":state["run_count"],"completed_runs":state["completed_runs"],
            "cleared_runs":state["cleared_runs"],"abandoned_runs":state["abandoned_runs"],"rounds":rounds,"log_dir":state["log_dir"]}
    (Path(state["log_dir"])/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result


async def finish(session_key,state_store,event_bus=None):
    state=await read_session(state_store,session_key)
    check_cancelled()
    if state["status"]!="running" or state["completed_runs"]!=state["run_count"] or state["round"].get("phase")!="completed":
        raise ScuffleError("scuffle_incomplete","本次任务未完成全部局数。")
    result=await _run_blocking(_finish_journal,state)
    if event_bus is not None:
        try:
            await event_bus.publish(Event(name=PROGRESS_EVENT,payload={"schema":PROGRESS_SCHEMA,"cid":state["cid"],"sequence":state["sequence"]+1,
                "run_index":state["run_index"],"run_count":state["run_count"],"completed_runs":state["completed_runs"],"stage":"completed","status":"completed","log_dir":state["log_dir"]}))
        except Exception as exc:
            logger.warning("Scuffle final progress delivery failed: %s", exc)
    await state_store.delete(session_key)
    return result

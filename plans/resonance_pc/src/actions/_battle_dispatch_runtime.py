"""Ordered battle business flow, using the engine's canonical task lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from typing import Any

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.observability.events import Event
from packages.aura_core.scheduler.operation_scope import current_operation_scope
from packages.resonance_shared.battle_schedule import resolve_battle_plan
from packages.resonance_shared.battle_control import register_control, release_control, stop_requested

from .battle_dispatch_pc_actions import _load_catalog


_TASKS = "tasks:auto_battle_dispatch_pc.yaml:"
_RESULT_SCHEMA = "resonance_pc.battle_plan_result.v1"
_ORDINARY_OUTCOMES = {"victory", "defeat", "swept", "insufficient_stamina", "cancelled"}


class _BattleProgress:
    def __init__(self, context, engine, state_store, event_bus):
        self.cid = str(context.data.get("cid") or "")
        if not self.cid:
            raise RuntimeError("Battle progress requires a task CID")
        self.context, self.engine = context, engine
        self.state_store, self.event_bus = state_store, event_bus
        self.sequence = 0
        self.key = f"resonance_pc.battle_plan.{self.cid}"

    async def emit(self, result, stage, error=None):
        self.sequence += 1
        result["counts"]["not_started"] = (
            result["counts"]["planned"]-result["counts"]["started"]-result["counts"]["swept"]
        )
        terminal = stage in {"completed", "stopped", "failed", "cancelled", "skipped"}
        snapshot_result = deepcopy(result if terminal else {
            **result, "results": result["results"][-20:],
        })
        snapshot = {
            "schema": "resonance_pc.battle_plan_progress.v1", "cid": self.cid,
            "sequence": self.sequence, "stage": stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "effective_date": result["effective_date"], "weekday": result["weekday"],
            "results_total": len(result["results"]), "result": snapshot_result,
        }
        if error is not None:
            snapshot["error"] = {"type": type(error).__name__, "message": str(error)}
        # Framework context and logs remain available even if a persistence or
        # subscriber fails. Never suppress the source exception with telemetry.
        self.context.data["battle_plan_progress"] = snapshot
        self.engine.root_context.data["battle_plan_progress"] = snapshot
        summary = {key: value for key, value in snapshot.items() if key != "result"}
        summary.update(status=result["status"], counts=deepcopy(result["counts"]),
                       cleanup=deepcopy(result["cleanup"]), latest_result=deepcopy(result["results"][-1:]))
        logger.info("[BattlePlanProgress] %s", json.dumps(summary, ensure_ascii=False, default=str))
        if self.state_store is not None:
            try:
                await self.state_store.set(self.key, deepcopy(snapshot))
            except Exception:
                logger.exception("[BattlePlanProgress] Could not save %s", self.key)
        if self.event_bus is not None:
            try:
                await self.event_bus.publish(Event(name="task.resonance_pc_battle_plan_progress", payload=summary))
            except Exception:
                logger.exception("[BattlePlanProgress] Could not publish progress for %s", self.cid)


async def _return_to_main(execution: Any) -> None:
    scope = current_operation_scope()
    result = await execution.call_action(
        "resonance_pc.wait_and_click_back_button",
        {
            "region": [10, 5, 160, 70],
            "template": "templates/battle/navigation/back_button.png",
            "threshold": 0.9,
            "use_grayscale": True,
            "after_click_sec": 1.0,
            "timeout_sec": 20.0,
            "interval_sec": 0.2,
            "stable_scans": 2,
            "already_at_target_template": "templates/battle/navigation/main_terminal_marker.png",
            "already_at_target_region": [1080, 375, 190, 80],
            "already_at_target_threshold": 0.95,
            "already_at_target_use_grayscale": True,
            "repeat_until_target": True,
            "max_clicks": 4,
            "target_stable_scans": 2,
        },
        scope=scope.child(22.0) if scope is not None else None,
    )
    if not isinstance(result, dict) or result.get("already_at_target") is not True:
        raise RuntimeError("Battle cleanup did not confirm the main page")


async def _enter_route(execution: Any, category: str, subcategory: str) -> None:
    await execution.run_task(
        _TASKS + "auto_battle_enter_terminal_pc",
        {"main_category": category},
        result_assertions={"entered": True},
    )
    await execution.run_task(
        _TASKS + "auto_battle_enter_category_pc",
        {"main_category": category, "sub_category": subcategory},
        result_assertions={"entered": True},
    )


async def _run_ordinary(execution: Any, job: dict, *, reuse_sweep_page=False, control_cid=None) -> dict:
    if reuse_sweep_page:
        sweep = await execution.call_action("resonance_pc.try_battle_sweep", {
            "capture_count": job.get("capture_count"),
            "formation_index": job.get("formation_index"),
            "control_cid": control_cid,
        })
        if not isinstance(sweep, dict) or sweep.get("outcome") not in {
            "swept", "unsupported", "insufficient_stamina", "cancelled",
        }:
            raise RuntimeError("Sweep returned no recognized outcome")
        if sweep["outcome"] != "unsupported":
            return {"success": True, "route_id": job["route_id"], **sweep}
        if sweep.get("ready_for_next") is not True:
            raise RuntimeError("Unavailable sweep did not confirm its return page")
        # A changed availability state is not a completed sweep. Re-enter the
        # normal selected route before falling back to its actual combat path.
        await _return_to_main(execution)
        if control_cid and stop_requested(control_cid):
            return {"outcome": "cancelled", "battle_started": False,
                    "page_after": "main", "ready_for_next": True, "branch": "cancelled"}
    category = job["main_category"]
    subcategory = job.get("ct_subcategory") if category == "ct" else job.get("gp_subcategory")
    await _enter_route(execution, category, subcategory)
    if category == "ct" and subcategory == "tie_an":
        task = "auto_battle_ct_tie_an_run_one_pc"
        fields = ("route_id", "city_name", "mission_type", "stage", "difficulty", "formation_index", "capture_count")
        inputs = {key: job.get(key) for key in fields}
    elif category == "ct" and subcategory == "regional_ops_center":
        task = "auto_battle_ct_regional_ops_run_one_pc"
        fields = ("route_id", "city_name", "threat_level", "difficulty", "formation_index", "capture_count")
        inputs = {key: job.get(key) for key in fields}
    elif category == "gp" and subcategory == "action_summary":
        task = "auto_battle_gp_action_summary_run_group_pc"
        inputs = {
            "route_id": job["route_id"],
            "gp_group_name": job["gp_group_name"],
            "gp_stage_name": job["gp_stage_name"],
            "jobs": [job],
            "return_to_list": False,
        }
    else:
        raise ValueError(f"Unsupported ordinary battle route: {job.get('route_id')}")
    inputs["control_cid"] = control_cid
    return await execution.run_task(_TASKS + task, inputs)


def _check_ordinary_result(result: Any) -> None:
    if not isinstance(result, dict) or result.get("outcome") not in _ORDINARY_OUTCOMES:
        raise RuntimeError("Battle task returned no recognized outcome")
    if type(result.get("battle_started")) is not bool:
        raise RuntimeError("Battle task returned no explicit start state")
    if result["outcome"] in {"victory", "defeat"} and result["battle_started"] is not True:
        raise RuntimeError("Battle task reported a result without starting a battle")
    if result["outcome"] == "swept" and result["battle_started"] is not False:
        raise RuntimeError("A sweep must not be reported as a started battle")
    if result["outcome"] == "cancelled" and result["battle_started"] is not False:
        raise RuntimeError("A cooperative pre-battle stop must not report a started battle")
    if result.get("ready_for_next") is not True:
        raise RuntimeError("Battle task did not confirm a stable return page")


async def _execute_battle_plan(
    *, jobs: Any = None, structural_types: Any = None, weekly_plan: Any = None,
    context: Any, engine: Any,
    state_store: Any = None, event_bus: Any = None,
) -> dict:
    if context is None or engine is None:
        raise RuntimeError("Battle dispatch requires the current execution context and engine")
    plan = resolve_battle_plan(
        jobs=jobs, structural_types=structural_types, weekly_plan=weekly_plan, catalog=_load_catalog(),
    )
    execution = engine.bind_execution(context)
    progress = _BattleProgress(context, engine, state_store, event_bus)
    compact_jobs = plan["jobs"]
    counts = {
        "planned": sum(job["run_count"] for job in compact_jobs),
        "started": 0, "won": 0, "lost": 0, "swept": 0, "not_started": 0,
    }
    result = {
        "schema": _RESULT_SCHEMA,
        "effective_date": plan["effective_date"],
        "weekday": plan["weekday"],
        "status": "completed", "stop_reason": None,
        "counts": counts, "results": [], "structural": None,
        "job_results": [
            {"job_index": index, "route_id": job["route_id"], "requested": job["run_count"],
             "started": 0, "won": 0, "lost": 0, "swept": 0,
             "not_started": job["run_count"], "status": "not_started"}
            for index, job in enumerate(compact_jobs)
        ],
        "cleanup": {"returned_to_main": False, "required": False, "touched_game": False},
        "continue_workflow": False,
    }
    control_cid = str(context.data.get("cid") or "")
    if stop_requested(control_cid):
        result.update(status="cancelled", stop_reason="cancelled")
        await progress.emit(result, "cancelled")
        return result
    if not compact_jobs and not plan["structural_types"]:
        result.update(status="skipped", stop_reason="empty_plan", continue_workflow=True)
        await progress.emit(result, "skipped")
        return result

    # Keep requested counts compact. The legacy validator receives only its own
    # fields, and every validated job remains paired with the original count.
    ordinary_inputs = [{key: value for key, value in job.items() if key != "run_count"} for job in compact_jobs]
    normalized = await execution.call_action("resonance_pc.normalize_battle_jobs", {"jobs": ordinary_inputs})
    validated = await execution.call_action(
        "resonance_pc.validate_battle_jobs", {"jobs": normalized["normalized_jobs"]},
    )
    ordinary_jobs = validated["normalized_jobs"]
    if len(ordinary_jobs) != len(compact_jobs):
        raise RuntimeError("Battle validation changed the compact job count")

    at_main = False
    touched_game = False
    active_job = None
    structural_invoked = False
    await progress.emit(result, "prepared")
    try:
        for job_index, job in enumerate(ordinary_jobs):
            if stop_requested(control_cid):
                result.update(status="cancelled", stop_reason="cancelled")
                break
            active_job = result["job_results"][job_index]
            active_job["status"] = "running"
            reuse_sweep_page = False
            for run_index in range(compact_jobs[job_index]["run_count"]):
                if stop_requested(control_cid):
                    result.update(status="cancelled", stop_reason="cancelled")
                    active_job["status"] = "cancelled"
                    break
                scope = current_operation_scope()
                if scope is not None:
                    scope.checkpoint()
                touched_game = True
                at_main = False
                logger.info(
                    "[BattlePlan] job=%s route=%s run=%s/%s started=%s won=%s lost=%s",
                    job_index + 1, job["route_id"], run_index + 1,
                    compact_jobs[job_index]["run_count"], counts["started"], counts["won"], counts["lost"],
                )
                battle = await _run_ordinary(
                    execution, job, reuse_sweep_page=reuse_sweep_page, control_cid=control_cid,
                )
                reuse_sweep_page = False
                result["results"].append({
                    "phase": "ordinary", "job_index": job_index, "run_index": run_index,
                    "route_id": job["route_id"],
                    **(battle if isinstance(battle, dict) else {"raw_result": battle}),
                })
                if isinstance(battle, dict):
                    counts["started"] += int(battle.get("battle_started") is True)
                    counts["won"] += int(battle.get("outcome") == "victory" and battle.get("battle_started") is True)
                    counts["lost"] += int(battle.get("outcome") == "defeat" and battle.get("battle_started") is True)
                    counts["swept"] += int(battle.get("outcome") == "swept" and battle.get("battle_started") is False)
                    active_job["started"] += int(battle.get("battle_started") is True)
                    active_job["won"] += int(battle.get("outcome") == "victory" and battle.get("battle_started") is True)
                    active_job["lost"] += int(battle.get("outcome") == "defeat" and battle.get("battle_started") is True)
                    active_job["swept"] += int(battle.get("outcome") == "swept" and battle.get("battle_started") is False)
                    active_job["not_started"] = active_job["requested"]-active_job["started"]-active_job["swept"]
                    active_job["status"] = (
                        "stopped" if battle.get("outcome") in {"defeat", "insufficient_stamina"}
                        else "completed" if active_job["won"]+active_job["swept"] == active_job["requested"] else "running"
                    )
                    active_job["stop_reason"] = battle.get("outcome") if active_job["status"] == "stopped" else None
                result["cleanup"].update(required=True, touched_game=True, returned_to_main=False)
                await progress.emit(result, "battle_returned")
                _check_ordinary_result(battle)
                if (battle["outcome"] == "swept"
                        and run_index+1 < compact_jobs[job_index]["run_count"]
                        and not stop_requested(control_cid)):
                    # Native sweep is single-shot. Repeat on the confirmed
                    # current stage instead of navigating home between sweeps.
                    reuse_sweep_page = True
                    continue
                await _return_to_main(execution)
                at_main = True
                if battle["outcome"] == "cancelled" or stop_requested(control_cid):
                    result.update(status="cancelled", stop_reason="cancelled")
                    active_job["status"] = "cancelled"
                    break
                if battle["outcome"] not in {"victory", "swept"}:
                    result.update(status="stopped", stop_reason=battle["outcome"])
                    break
            if result["status"] in {"stopped", "cancelled"}:
                break

        if stop_requested(control_cid):
            result.update(status="cancelled", stop_reason="cancelled")
        if result["status"] == "cancelled" and touched_game and not at_main:
            # A sweep can retain its confirmed detail page for the next run.
            # A stop arriving at that next loop boundary still owns ordinary
            # page cleanup, even though no new sweep or battle was started.
            await _return_to_main(execution)
            at_main = True
        if result["status"] == "completed" and plan["structural_types"]:
            touched_game = True
            at_main = False
            await _enter_route(execution, "gp", "structural_exploration")
            structural_invoked = True
            structural = await execution.call_action(
                "resonance_pc.run_structural_exploration", {
                    "types": plan["structural_types"], "control_cid": control_cid,
                },
            )
            if not isinstance(structural, dict) or structural.get("outcome") not in {
                "completed", "defeat", "insufficient_stamina", "cancelled",
            }:
                raise RuntimeError("Structural exploration returned no recognized outcome")
            result["structural"] = structural
            result["results"].append({"phase": "structural", **structural})
            observed = structural.get("observed")
            expected_reason = {
                "completed": "no_matching_targets", "defeat": "defeat",
                "insufficient_stamina": "insufficient_stamina", "cancelled": "cancelled",
            }[structural["outcome"]]
            if (structural.get("schema") != "resonance_pc.structural_native_result.v1"
                    or structural.get("mode") != "native_auto"
                    or structural.get("reason") != expected_reason
                    or structural.get("statistics_complete") is not False
                    or not isinstance(observed, dict)
                    or any(type(observed.get(key)) is not int or observed[key] < 0 for key in ("won", "lost"))
                    or structural.get("native_stopped") is not True
                    or structural.get("ready_for_next") is not True
                    or structural.get("page_after") != "world_scan"
                    or (structural["outcome"] != "cancelled"
                        and structural.get("configured_types") != plan["structural_types"])):
                raise RuntimeError("Structural exploration did not confirm native stop and handoff")
            if ((structural["outcome"] == "defeat" and observed["lost"] < 1)
                    or (structural["outcome"] in {"completed", "insufficient_stamina"} and observed["lost"] != 0)):
                raise RuntimeError("Structural exploration stop reason contradicts observed defeat")
            result["cleanup"].update(required=True, touched_game=True, returned_to_main=False)
            await progress.emit(result, "structural_returned")
            await _return_to_main(execution)
            at_main = True
            if structural["outcome"] == "cancelled" or stop_requested(control_cid):
                result.update(status="cancelled", stop_reason="cancelled")
            elif structural["outcome"] != "completed":
                result.update(status="stopped", stop_reason=structural["outcome"])
    except asyncio.CancelledError as exc:
        # The native-automation owner handles its own stop. Do not create a new
        # scope or issue more inputs after the shared cancellation was latched.
        result.update(status="cancelled", stop_reason="cancelled", continue_workflow=False)
        partial = getattr(exc, "structural_partial_result", None)
        if partial is not None:
            result["structural"] = partial
        if active_job is not None and active_job["status"] == "running":
            active_job["status"] = "cancelled"
        result["cleanup"].update(required=touched_game, touched_game=touched_game, returned_to_main=at_main)
        try:
            await progress.emit(result, "cancelled", exc)
        except asyncio.CancelledError:
            pass
        raise
    except Exception as exc:
        result.update(status="failed", stop_reason="execution_error", continue_workflow=False)
        if active_job is not None and active_job["status"] == "running":
            active_job["status"] = "failed"
        partial = getattr(exc, "structural_partial_result", None)
        if partial is not None:
            result["structural"] = partial
        native_stop_confirmed = not structural_invoked or (
            isinstance(result.get("structural"), dict)
            and result["structural"].get("native_stopped") is True
        )
        if not native_stop_confirmed:
            result["cleanup"]["error"] = {
                "type": "NativeStopUnconfirmed", "message": "原生自动停止未确认，禁止通用返回与后续流程。",
            }
        if touched_game and not at_main and native_stop_confirmed:
            scope = current_operation_scope()
            if scope is None or (not scope.cancelled and not scope.expired):
                try:
                    await _return_to_main(execution)
                    at_main = True
                except asyncio.CancelledError as cleanup_cancel:
                    result.update(status="cancelled", stop_reason="cancelled")
                    result["cleanup"].update(required=touched_game, touched_game=touched_game, returned_to_main=at_main)
                    try:
                        await progress.emit(result, "cancelled", exc)
                    except asyncio.CancelledError:
                        pass
                    raise cleanup_cancel from exc
                except Exception as cleanup_error:
                    result["cleanup"]["error"] = {"type": type(cleanup_error).__name__, "message": str(cleanup_error)}
                    logger.exception("[BattlePlan] Cleanup failed after an execution error")
        result["cleanup"].update(required=touched_game, touched_game=touched_game, returned_to_main=at_main)
        try:
            await progress.emit(result, "failed", exc)
        except asyncio.CancelledError:
            pass
        raise

    counts["not_started"] = counts["planned"]-counts["started"]-counts["swept"]
    if stop_requested(control_cid):
        result.update(status="cancelled", stop_reason="cancelled")
    result["cleanup"] = {
        "returned_to_main": at_main, "required": touched_game, "touched_game": touched_game,
    }
    result["continue_workflow"] = at_main and result["status"] in {"completed", "stopped"}
    try:
        await progress.emit(result, result["status"])
    except asyncio.CancelledError as exc:
        result.update(status="cancelled", stop_reason="cancelled", continue_workflow=False)
        try:
            await progress.emit(result, "cancelled", exc)
        except asyncio.CancelledError:
            pass
        raise
    # Telemetry yields to the runner IPC thread. Preserve a stop request that
    # arrived while the final snapshot was being persisted or published.
    if stop_requested(control_cid) and result["status"] != "cancelled":
        result.update(status="cancelled", stop_reason="cancelled", continue_workflow=False)
        await progress.emit(result, "cancelled")
    return result


async def execute_battle_plan(
    *, jobs: Any = None, structural_types: Any = None, weekly_plan: Any = None,
    context: Any, engine: Any, state_store: Any = None, event_bus: Any = None,
) -> dict:
    """Own the cooperative-stop CID until native and page cleanup have drained."""
    if context is None:
        raise RuntimeError("Battle dispatch requires the current execution context")
    cid = str(context.data.get("cid") or "")
    register_control(cid)
    try:
        return await _execute_battle_plan(
            jobs=jobs, structural_types=structural_types, weekly_plan=weekly_plan,
            context=context, engine=engine, state_store=state_store, event_bus=event_bus,
        )
    finally:
        release_control(cid)

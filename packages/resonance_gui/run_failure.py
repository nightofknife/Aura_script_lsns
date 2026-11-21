"""Turn terminal run results into short, actionable UI messages."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .logic import extract_final_result, extract_status, normalize_run_payload


_INCOMPLETE = {"failed", "error", "blocked", "timeout", "cancelled", "no_plan"}
_TASKS = {
    "enter_main": "进入主界面",
    "close_game": "关闭游戏",
    "player_data_refresh": "货运前数据刷新",
    "player_data_get_latest": "读取用户数据缓存",
    "trade_start_preflight_pc": "跑商起始检查",
    "auto_cycle_trade_pc": "PC 自动跑商",
    "preview_trade_plan_pc": "计算跑商方案",
    "auto_battle_dispatch_pc": "PC 自动战斗",
    "collect_rewards_pc": "奖励领取",
    "collect_daily_mission_rewards_pc": "每日任务奖励",
    "collect_battle_pass_rewards_pc": "通行证奖励",
    "affix_refresh_pc": "装备词缀刷新",
    "affix_refresh_read_pc": "读取装备词缀",
    "calculation_recording_pc": "虚演刻录",
    "eternal_scuffle_pc": "无垠乱斗",
    "consciousness_deep_dive_plan_pc": "识海深潜移动规划",
    "consciousness_deep_dive_scan_pc": "识海深潜布局扫描",
    "consciousness_deep_dive_planned_run_pc": "识海深潜关卡内自动运行",
    "consciousness_deep_dive_loop_pc": "识海深潜循环",
    "team_recommendation_pc": "配队推荐",
    "recruit_passengers_by_flyer": "传单揽客",
}
_STEPS = {
    "refresh": "刷新用户数据",
    "get_latest": "读取缓存",
    "enter_main": "进入列车主界面",
    "preflight": "检查起始画面",
    "require_resolution": "检查游戏窗口",
    "collect": "领取奖励",
    "dispatch": "执行作战",
    "read": "读取界面",
    "plan": "规划路线",
    "execute": "执行路线",
    "run": "执行任务",
}
_KNOWN = {
    "input_integrity_mismatch": (
        "操作游戏窗口", "Aura 无法操作游戏窗口。", "请用相同权限重新启动 Aura 和游戏。"),
    "pc_client_resolution_unavailable": (
        "检查游戏窗口", "无法确认游戏窗口的尺寸。",
        "请设为 1280×720 窗口模式，并让窗口完整显示在屏幕内。"),
    "pc_client_resolution_mismatch": (
        "检查游戏窗口", "游戏窗口分辨率不符合要求。", "请在游戏内选择 1280×720 窗口模式。"),
    "window_position_recovery_failed": (
        "检查游戏窗口", "游戏窗口没有完整显示在屏幕内。", "请把游戏窗口移到屏幕内后再执行。"),
    "window_not_found": (
        "连接游戏", "未找到游戏窗口。", "请先启动《雷索纳斯》，进入游戏后再执行。"),
    "capture_failed": (
        "获取游戏画面", "无法获取游戏画面。", "请确认游戏窗口仍然打开。"),
    "target_unreachable": (
        "规划路线", "当前条件下无法达到目标收益。", "请检查目标收益和线路限制。"),
    "fixed_route_infeasible": (
        "规划路线", "当前条件下无法执行所选线路。", "请检查线路和疲劳预算。"),
    "no_plan": (
        "规划路线", "当前条件下没有可执行的路线。", "请检查线路和疲劳预算。"),
    "no_positive_profit_route": (
        "规划路线", "当前条件下没有预期收益为正的路线。", "请检查线路和疲劳预算。"),
    "arrival_requirements_unmet": (
        "规划路线", "当前条件下无法满足最低到达次数。", "请检查城市的最低到达次数。"),
}
_STAGE_MESSAGES = {
    "list_tasks": ("加载任务", "暂时无法加载任务列表。"),
    "list_runs": ("加载历史", "暂时无法读取运行历史。"),
    "poll_run": ("读取任务状态", "暂时无法读取执行状态。"),
    "cancel_task": ("请求停止", "停止请求未确认，请等待执行端回报。"),
}
_DISPATCH_STAGES = {"run_task", "run_pc_task", "run_pc_trade", "preview_pc_trade", "run_pc_battle"}


@dataclass(frozen=True)
class RunFailure:
    task: str
    step: str
    message: str
    next_step: str = ""

    @property
    def title(self) -> str:
        if not self.step or self.step == self.task:
            return self.task
        return f"{self.task} · {self.step}"

    @property
    def text(self) -> str:
        return f"{self.title}：{self.message}{self.next_step}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _task_key(value: Any) -> str:
    return str(value or "").rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def _label(value: Any) -> str:
    return value if isinstance(value, str) and re.search(r"[\u3400-\u9fff]", value) else ""


def run_task_label(payload: Mapping[str, Any], fallback: str = "任务") -> str:
    item = _mapping(payload.get("gui_item"))
    for field in (item.get("label"), payload.get("label"), payload.get("trace_label")):
        if name := _label(field):
            return name
    for field in (item.get("task_ref"), payload.get("task_ref"), payload.get("task_name")):
        if name := _TASKS.get(_task_key(field)):
            return name
    return fallback


def _envelopes(payload: Mapping[str, Any]):
    data = normalize_run_payload(payload)
    yield data
    run = _mapping(data.get("run"))
    detail = _mapping(run.get("detail") or data.get("detail"))
    if detail:
        yield detail
    final = _mapping(detail.get("final_result") or data.get("final_result"))
    if final:
        yield final


def _recognition(task: str, info: Mapping[str, Any]) -> RunFailure | None:
    step = _label(info.get("step"))
    target = info.get("target")
    outcome = info.get("outcome")
    if not isinstance(target, str) or not isinstance(outcome, str):
        return None
    target = target.strip()
    if not step or not target:
        return None
    action = "读到" if info.get("kind") == "text" else "识别到"
    seconds = info.get("timeout_sec")
    wait = f"等待 {seconds:g} 秒后仍未" if type(seconds) in {int, float} and seconds > 0 else "等待超时，未"
    messages = {
        "timeout": f"{wait}{action}「{target}」。",
        "not_found": f"当前画面没有{action}「{target}」。",
        "unconfirmed": f"未能确认「{target}」。",
        "unavailable": f"识别时出错，未能确认「{target}」。",
    }
    if outcome not in messages:
        return None
    return RunFailure(task, step, messages[outcome], _label(info.get("next_step")))


def present_run_failure(
    payload: Mapping[str, Any], *, task_label: str = "", result: Mapping[str, Any] | None = None,
) -> RunFailure | None:
    task = task_label or run_task_label(payload)
    envelopes = list(_envelopes(payload))
    public = extract_final_result(payload) if result is None else result
    for key in ("player_data", "affix_refresh", "calculation_recording", "eternal_scuffle", "team_recommendations",
                "deep_dive_scan", "deep_dive_plan", "deep_dive_planned_run", "deep_dive_loop"):
        child = _mapping(public.get(key))
        if child.get("success") is False or str(child.get("status") or "").lower() in _INCOMPLETE:
            public = child
            break
    status = extract_status(payload) or str(envelopes[-1].get("status") or "").lower()
    state = str(public.get("status") or "").lower()
    stage = str(payload.get("stage") or "")
    business_failed = public.get("success") is False or state in _INCOMPLETE
    if not (stage in _STAGE_MESSAGES or stage in _DISPATCH_STAGES or payload.get("gui_timeout_cancelled")
            or status in _INCOMPLETE or business_failed):
        return None
    if payload.get("gui_timeout_cancelled"):
        return RunFailure(task, "", "执行超时。", "请查看游戏当前画面。")
    if status == "cancelled" or state == "cancelled":
        return RunFailure(task, "", "任务已停止。")
    if stage in _STAGE_MESSAGES:
        return RunFailure(task, *_STAGE_MESSAGES[stage])

    errors = []
    for source in (envelopes[-1], public, _mapping(public.get("execution")),
                   _mapping(public.get("final_sale")), *envelopes[:-1]):
        error = _mapping(source.get("error"))
        if error:
            chain = [error]
            while len(chain) < 12 and (cause := _mapping(chain[-1].get("cause"))):
                chain.append(cause)
            errors.extend(reversed(chain))
        if isinstance(source.get("recognition"), Mapping):
            errors.append(source)

    for error in errors:
        if error.get("code") == "market_refresh_failed_no_cache":
            return RunFailure(
                task,
                "刷新行情",
                _label(error.get("message")) or "科伦巴行情更新失败，且没有可用的历史行情。",
                "请检查「交易 → 行情连接」后重试。",
            )

    # Prefer the failing operation over the wrapper node. Old runs may only
    # have an error code in the evidence attached to that operation.
    root_error = _mapping(envelopes[-1].get("error"))
    for error in errors:
        info = _mapping(error.get("recognition") or _mapping(error.get("detail")).get("recognition"))
        if failure := _recognition(task, info):
            cause_task = _TASKS.get(_task_key(error.get("task")))
            if cause_task and cause_task != run_task_label(payload, "") and "cause" in root_error:
                return RunFailure(f"{task} › {cause_task}", failure.step, failure.message, failure.next_step)
            return failure
        if known := _KNOWN.get(str(error.get("code") or "")):
            return RunFailure(task, _label(error.get("step")) or known[0], known[1], known[2])

    primary = errors[0] if errors else {}
    refs = primary.get("failure_evidence") or envelopes[-1].get("failure_evidence") or []
    if not isinstance(refs, (list, tuple)):
        refs = []
    for ref in refs:
        if known := _KNOWN.get(str(_mapping(ref).get("error_code") or "")):
            return RunFailure(task, *known)

    # Compatibility for the few older logs with no structured recognition data.
    message = str(primary.get("message") or "")
    if message.startswith("Player data refresh failed: expected main page before player data refresh template"):
        return RunFailure(task, "确认主界面", "等待 8 秒后仍未识别到「列车主界面」。",
                          "请返回列车主界面后再刷新。")
    if message.startswith("Resonance PC is not running and launch_if_not_running is false."):
        return RunFailure(task, *_KNOWN["window_not_found"])
    for key in (public.get("reason"), public.get("planning_status"), state):
        if isinstance(key, str) and key in _KNOWN:
            return RunFailure(task, *_KNOWN[key])
    step = _label(primary.get("step")) or _label(primary.get("node_name")) or _STEPS.get(str(primary.get("node_id") or ""), "")
    if not step and refs:
        step = _STEPS.get(str(_mapping(refs[0]).get("node") or ""), "")
    if primary.get("type") == "OperationTimeout" or status == "timeout" or state == "timeout":
        return RunFailure(task, step, "等待任务步骤完成时超时。", "请查看游戏当前画面。")
    if stage in _DISPATCH_STAGES:
        return RunFailure(task, "启动任务", "任务未能启动。", "请查看或复制排查信息。")
    return RunFailure(task, step, "执行中断。", "请查看或复制排查信息。")

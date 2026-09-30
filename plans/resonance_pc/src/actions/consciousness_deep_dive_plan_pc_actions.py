"""Offline movement planning from a completed Deep Dive cube scan."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
import threading
import time
from uuid import uuid4

from packages.aura_core.api import action_info
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException

from ._deep_dive_movement_planner import plan_layout


def _load_completed_layout(path: Path) -> dict:
    layout = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(layout, dict) or not (
        layout.get("status") == "completed"
        and layout.get("success") is True
        and layout.get("layout_complete") is True
    ):
        raise ValueError("需要成功完成的完整魔方扫描结果。")
    cells = layout.get("cells")
    if not isinstance(cells, list) or len(cells) != 54:
        raise ValueError("魔方扫描结果必须包含 54 个格子。")
    expected = {(face, row, col) for face in "URFDLB" for row in range(3) for col in range(3)}
    actual = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("魔方格子格式不正确。")
        row, col = cell.get("row"), cell.get("col")
        if type(row) is not int or type(col) is not int:
            raise ValueError("魔方格子坐标必须为整数。")
        actual.add((cell.get("face"), row, col))
    if actual != expected:
        raise ValueError("魔方扫描结果存在缺失或重复坐标。")
    return layout


def load_planning_layout(layout_path: str = "", *, base_path: Path | None = None,
                         cancel_check=None) -> tuple[Path, dict]:
    """Resolve an explicit scan, or the newest complete successful scan.

    Recency is the file modification time; malformed and partial reports are
    skipped only during automatic selection. Explicit files fail visibly.
    """
    base = Path(base_path) if base_path is not None else resolve_base_path()
    if cancel_check:
        cancel_check()
    if str(layout_path or "").strip():
        path = Path(str(layout_path).strip()).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        return path, _load_completed_layout(path)
    candidates = []
    for path in (base / "logs" / "deep_dive_scan").glob("*/layout.json"):
        if cancel_check:
            cancel_check()
        try:
            candidates.append((path.stat().st_mtime_ns, str(path), path))
        except OSError:
            continue
    for _, _, path in sorted(candidates, reverse=True):
        if cancel_check:
            cancel_check()
        try:
            return path.resolve(), _load_completed_layout(path)
        except (OSError, ValueError, TypeError):
            continue
    raise ValueError("未找到完整成功的扫描结果，请先扫描或指定 layout_path。")


def _write_result(result: dict, output_dir: Path) -> None:
    """Publish JSON last so readers never see a partially written report."""
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    strategy_label = {"chase": "只追奇点", "inspiration": "最大化灵感收益"}.get(
        result.get("strategy"), str(result.get("strategy")))
    summary = "\n".join([
        "# 识海深潜移动规划", "",
        f"策略：{strategy_label}。回合预算：{result.get('turn_budget')}。",
        f"状态：{result.get('status')}。来源：{result.get('source_layout_path') or '未加载'}。", "",
        f"下一步：{json.dumps(result.get('next_action'), ensure_ascii=False)}。",
        f"计算指标：{json.dumps(result.get('metrics', {}), ensure_ascii=False)}。", "",
        "此报告仅计算下一步和策略分支，不会操作游戏。奇点吞掉的灵感不算作玩家收益。",
        "算法的最优性、随机规则和完成概率以 JSON 中的搜索结果为准；超时结果不能当作已证明最优。", "",
        "## 结构化规划结果", "", "```json", payload, "```", "",
    ])
    report_tmp = output_dir / "plan.md.tmp"
    report_tmp.write_text(summary, encoding="utf-8")
    report_tmp.replace(output_dir / "plan.md")
    json_tmp = output_dir / "plan.json.tmp"
    json_tmp.write_text(payload, encoding="utf-8")
    json_tmp.replace(output_dir / "plan.json")


def run_layout_planning(layout_path: str = "", strategy: str = "chase",
                        turn_budget: int = 6, time_budget_sec: float = 30,
                        *, base_path: Path | None = None, cancel_check=None) -> dict:
    """Synchronous file entry shared by the task and CLI; always saves status."""
    base = Path(base_path) if base_path is not None else resolve_base_path()
    started = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    output_dir = base / "logs" / "deep_dive_plan" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    source = None
    result = {}
    try:
        if strategy not in ("chase", "inspiration"):
            raise ValueError("strategy 必须是 chase 或 inspiration。")
        turns = float(turn_budget)
        budget = float(time_budget_sec)
        if isinstance(turn_budget, bool) or not math.isfinite(turns) or not turns.is_integer() or turns < 1:
            raise ValueError("turn_budget 必须是正整数。")
        if isinstance(time_budget_sec, bool) or not math.isfinite(budget) or budget <= 0:
            raise ValueError("time_budget_sec 必须是有限正数。")
        source, layout = load_planning_layout(layout_path, base_path=base, cancel_check=cancel_check)
        result = plan_layout(layout, strategy=strategy, turn_budget=int(turns),
                             time_budget_sec=budget, cancel_check=cancel_check)
        if not isinstance(result, dict):
            raise TypeError("规划器必须返回字典结果。")
        if cancel_check:
            cancel_check()
    except StopTaskException:
        result = {"status": "cancelled", "success": False, "reason": "cancel_requested"}
    except Exception as exc:
        result = {"status": "blocked", "success": False, "reason": f"{type(exc).__name__}: {exc}"}
    result.setdefault("schema", "resonance_pc.deep_dive_plan.v1")
    def parameter_for_report(value):
        # Invalid CLI/task numbers still need a serializable blocked report.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return value if math.isfinite(value) else str(value)
            except OverflowError:
                return str(value)
        return value if isinstance(value, (str, bool, type(None))) else repr(value)

    result.update(run_id=run_id, source_layout_path=str(source) if source else None,
                  strategy=parameter_for_report(strategy), turn_budget=parameter_for_report(turn_budget),
                  time_budget_sec=parameter_for_report(time_budget_sec),
                  output_dir=str(output_dir), json_path=str(output_dir / "plan.json"),
                  report_path=str(output_dir / "plan.md"),
                  total_elapsed_sec=round(time.monotonic() - started, 3), execution_mode="planning_only")
    try:
        _write_result(result, output_dir)
    except (OSError, ValueError, TypeError) as exc:
        result.update(status="blocked", success=False, report_error=f"{type(exc).__name__}: {exc}")
    return result


async def _drain_worker(worker, cancellation: threading.Event):
    """Shield the worker and cooperatively stop it before leaving the action."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(worker), cancelled
        except asyncio.CancelledError:
            cancelled = True
            cancellation.set()
            if worker.done():
                return worker.result(), cancelled


@action_info(name="resonance_pc.plan_consciousness_deep_dive_layout", public=True,
             read_only=True, timeout=-1,
             description="Plan chase or inspiration actions from a saved full cube layout without game input.")
async def plan_consciousness_deep_dive_layout(layout_path: str = "", strategy: str = "chase",
                                             turn_budget: int = 6, time_budget_sec: float = 30):
    cancellation = threading.Event()

    def cancel_check():
        if cancellation.is_set() or is_current_task_cancel_requested():
            raise StopTaskException("魔方移动规划已取消。", success=False)

    worker = asyncio.create_task(asyncio.to_thread(
        run_layout_planning, layout_path, strategy, turn_budget, time_budget_sec,
        cancel_check=cancel_check))
    result, cancelled = await _drain_worker(worker, cancellation)
    if cancelled and result.get("status") != "cancelled":
        # Cancellation may arrive just after the worker's final checkpoint.
        # Persist that boundary explicitly and drain this writer as well.
        result.update(status="cancelled", success=False, reason="cancel_requested")
        result.pop("next_action", None)
        result.pop("player_turn_actions", None)
        writer = asyncio.create_task(asyncio.to_thread(_write_result, result, Path(result["output_dir"])))
        try:
            await _drain_worker(writer, cancellation)
        except (OSError, ValueError, TypeError) as exc:
            result["report_error"] = f"{type(exc).__name__}: {exc}"
    logger.info("[DeepDivePlan] strategy=%s status=%s output=%s",
                strategy, result.get("status"), result.get("output_dir"))
    return result

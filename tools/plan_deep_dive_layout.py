"""Plan from a saved full cube scan; no capture or game input is performed.

From the repository root:
    python -m tools.plan_deep_dive_layout --strategy chase --turn-budget 6
    python -m tools.plan_deep_dive_layout --layout-path logs/deep_dive_scan/RUN/layout.json --strategy inspiration
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import threading


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout-path", default="", help="完整扫描 JSON；省略时选择最新成功扫描。")
    parser.add_argument("--strategy", choices=("chase", "inspiration"), default="chase")
    parser.add_argument("--turn-budget", type=int, default=6)
    parser.add_argument("--time-budget-sec", type=float, default=30)
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    from plans.resonance_pc.src.actions.consciousness_deep_dive_plan_pc_actions import run_layout_planning
    from packages.aura_core.utils.exceptions import StopTaskException

    cancelled = threading.Event()

    def request_cancel(signum, frame):
        cancelled.set()

    def cancel_check():
        if cancelled.is_set():
            raise StopTaskException("魔方移动规划已取消。", success=False)

    previous_handler = signal.signal(signal.SIGINT, request_cancel)
    try:
        result = run_layout_planning(args.layout_path, args.strategy, args.turn_budget,
                                     args.time_budget_sec, base_path=base, cancel_check=cancel_check)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if result.get("status") == "cancelled":
        return 130
    if result.get("status") == "search_budget_exhausted":
        return 2
    return 1 if result.get("status") == "blocked" or result.get("report_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

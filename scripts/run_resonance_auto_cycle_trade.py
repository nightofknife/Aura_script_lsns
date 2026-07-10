from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.aura_game import EmbeddedGameRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Resonance auto_cycle_trade with stable Windows process args.")
    parser.add_argument("--fatigue-budget", type=int, required=True)
    parser.add_argument("--cargo-capacity", type=int, required=True)
    parser.add_argument("--book-budget", type=int, default=0)
    parser.add_argument("--book-profit-threshold", type=float, default=0.0)
    parser.add_argument("--max-cycle-hops", type=int, default=6)
    parser.add_argument("--max-rounds", type=int, default=64)
    parser.add_argument("--use-fatigue-medicine", action="store_true")
    parser.add_argument(
        "--allowed-fatigue-medicine",
        action="append",
        default=[],
        help="Allowed fatigue medicine name. Repeat for multiple values, e.g. 提神口香糖.",
    )
    parser.add_argument("--fatigue-medicine-max-uses", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = _parser().parse_args(argv)
    inputs: dict[str, Any] = {
        "fatigue_budget": args.fatigue_budget,
        "cargo_capacity": args.cargo_capacity,
        "book_budget": args.book_budget,
        "book_profit_threshold": args.book_profit_threshold,
        "max_cycle_hops": args.max_cycle_hops,
        "max_rounds": args.max_rounds,
        "use_fatigue_medicine": args.use_fatigue_medicine,
        "allowed_fatigue_medicines": args.allowed_fatigue_medicine,
        "fatigue_medicine_max_uses": args.fatigue_medicine_max_uses,
    }

    runner = EmbeddedGameRunner()
    try:
        result = runner.run_task(
            game_name="resonance",
            task_ref="tasks:auto_cycle_trade.yaml:auto_cycle_trade",
            inputs=inputs,
            wait=True,
            timeout_sec=0,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

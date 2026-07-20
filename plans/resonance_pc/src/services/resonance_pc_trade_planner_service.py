"""ResonancePc trading planner service (V1)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from packages.aura_core.api import service_info

from .resonance_pc_market_data_service import (
    ResonancePcMarketDataError,
    ResonancePcMarketDataService,
)
from .resonance_pc_trade_exact_solver import ResonancePcExactTradeSolver


class ResonancePcTradePlannerError(RuntimeError):
    """Structured planner error."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class _SearchState:
    city_id: str
    remaining_fatigue: int
    remaining_books: int
    holdings: Dict[str, Dict[str, float]]
    cum_profit: float
    cum_fatigue: int
    total_books_used: int
    trace: List[Dict[str, Any]]


@service_info(
    alias="resonance_pc_trade_planner",
    public=True,
    singleton=True,
    description="ResonancePc exact full-budget trade planner with legacy planning helpers.",
    deps={"resonance_pc_market_data": "resonance_pc_market_data"},
)
class ResonancePcTradePlannerService:
    BUY_LOT_SCHEMA_VERSION = "1.0.0"
    TRADE_CONSTRAINTS_SCHEMA_VERSION = "1.0.0"
    TRADE_RULES_SCHEMA_VERSION = "2.0.0"
    MAX_ROLLING_WINDOW = 6
    DEFAULT_BEAM_WIDTH = 16
    MAX_SELL_BRANCH_PRODUCTS = 3
    DEFAULT_CYCLE_MAX_HOPS = 6
    DEFAULT_CYCLE_BEAM_WIDTH = 64
    DEFAULT_CYCLE_TOPK_NEXT = 6
    DEFAULT_ALLOWED_CITY_IDS = ["3", "4", "1", "5", "7", "8", "9", "2"]
    DEFAULT_CITY_ID_TO_KEY = {
        "3": "freeport",
        "4": "clarity_data_center_administration_bureau",
        "1": "shoggolith_city",
        "5": "anita_weapon_research_institute",
        "7": "wilderness_station",
        "8": "mander_mine",
        "9": "onederland",
        "2": "brcl_outpost",
    }
    KNOWN_CITY_KEY_TO_ID = {
        **{value: key for key, value in DEFAULT_CITY_ID_TO_KEY.items()},
        "anita_energy_research_institute": "6",
        "anita_rocket_base": "10",
        "cape_city": "11",
        "confluence_tower": "13",
        "gronru_city": "19",
    }

    def __init__(
        self,
        resonance_pc_market_data: ResonancePcMarketDataService,
        plan_root: Optional[Path] = None,
        beam_width: int = DEFAULT_BEAM_WIDTH,
    ):
        self.market_data = resonance_pc_market_data
        self.plan_root = Path(plan_root) if plan_root else Path(__file__).resolve().parents[2]
        self.meta_dir = self.plan_root / "data" / "meta"
        self.buy_lot_file = self.meta_dir / "buy_lot.json"
        self.trade_constraints_file = self.meta_dir / "trade_constraints.json"
        self.trade_rules_file = self.meta_dir / "trade_rules.json"
        self.beam_width = max(int(beam_width), 1)
        self._buy_lot_payload: Optional[Dict[str, Any]] = None
        self._trade_constraints_payload: Optional[Dict[str, Any]] = None
        self._trade_rules_payload: Optional[Dict[str, Any]] = None
        self._max_sell_price_cache: Dict[str, float] = {}

    def plan_optimal_route(
        self,
        *,
        fatigue_budget: int = 100,
        cargo_capacity: int = 650,
        book_budget: int = 0,
        book_profit_threshold: Any = 0,
        negotiation_budget: int = 0,
        all_plan: int = 0,
        bargain_success_rates_bps: Optional[List[Any]] = [5000],
        bargain_step_bps: Optional[Any] = 1000,
        raise_success_rates_bps: Optional[List[Any]] = [5000],
        raise_step_bps: Optional[Any] = 1000,
        trade_level: int = 20,
        available_city_ids: Optional[List[str]] = None,
        city_prestige: Optional[Dict[str, Any]] = None,
        product_unlocks: Optional[Dict[str, Any]] = None,
        active_events: Optional[List[Any]] = None,
        current_city_key: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city: Optional[str] = None,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the exact best complete route for one frozen market snapshot."""

        constraints = self._load_trade_constraints_payload()
        if snapshot_id:
            snapshot = self.market_data.get_snapshot(snapshot_id=str(snapshot_id))
        else:
            snapshot = self.market_data.get_latest()
        if not isinstance(snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid.",
            )

        fatigue_payload = self.market_data.get_all_travel_fatigue()
        self._validate_fatigue_payload(fatigue_payload)
        resolved_city_id = self._resolve_current_city_id(
            current_city_id=current_city_id,
            current_city_key=current_city_key,
            current_city=current_city,
            fatigue_payload=fatigue_payload,
            city_key_to_id=constraints["key_to_city_id"],
        )
        if resolved_city_id is None:
            raise ResonancePcTradePlannerError(
                code="current_city_required",
                message="One current city input is required for optimal route planning.",
            )

        supported_city_ids = [
            city_id
            for city_id in constraints["allowed_city_ids"]
            if city_id in (fatigue_payload.get("costs") or {})
        ]
        if available_city_ids is None:
            allowed_city_ids = supported_city_ids
        else:
            if not isinstance(available_city_ids, list):
                raise ResonancePcTradePlannerError(
                    code="invalid_available_city_ids",
                    message="available_city_ids must be a list.",
                )
            requested_city_ids = list(
                dict.fromkeys(str(city_id).strip() for city_id in available_city_ids if str(city_id).strip())
            )
            unsupported_city_ids = [
                city_id for city_id in requested_city_ids if city_id not in supported_city_ids
            ]
            if unsupported_city_ids:
                raise ResonancePcTradePlannerError(
                    code="unsupported_selected_cities",
                    message="Some selected cities are outside PC trade constraints.",
                    detail={
                        "unsupported_city_ids": unsupported_city_ids,
                        "supported_city_ids": supported_city_ids,
                    },
                )
            allowed_city_ids = [
                city_id for city_id in supported_city_ids if city_id in requested_city_ids
            ]
            if len(allowed_city_ids) < 2:
                raise ResonancePcTradePlannerError(
                    code="insufficient_selected_cities",
                    message="At least two available_city_ids are required.",
                    detail={"available_city_ids": allowed_city_ids},
                )
        if resolved_city_id not in allowed_city_ids:
            raise ResonancePcTradePlannerError(
                code=(
                    "current_city_not_selected"
                    if resolved_city_id in supported_city_ids
                    else "unsupported_city"
                ),
                message=(
                    f"Current city '{resolved_city_id}' is not selected for planning."
                    if resolved_city_id in supported_city_ids
                    else f"Current city '{resolved_city_id}' is outside PC trade constraints."
                ),
                detail={"allowed_city_ids": allowed_city_ids},
            )

        solver = ResonancePcExactTradeSolver(
            snapshot=snapshot,
            fatigue_payload=fatigue_payload,
            buy_lot=self._load_buy_lot_payload()["city_product_buy_lot"],
            trade_rules=self._load_trade_rules_payload(),
            allowed_city_ids=allowed_city_ids,
        )
        try:
            return solver.solve(
                start_city_id=resolved_city_id,
                fatigue_budget=fatigue_budget,
                cargo_capacity=cargo_capacity,
                book_budget=book_budget,
                book_profit_threshold=book_profit_threshold,
                negotiation_budget=negotiation_budget,
                all_plan=all_plan,
                bargain_success_rates_bps=bargain_success_rates_bps,
                bargain_step_bps=bargain_step_bps,
                raise_success_rates_bps=raise_success_rates_bps,
                raise_step_bps=raise_step_bps,
                trade_level=trade_level,
                city_prestige=city_prestige,
                product_unlocks=product_unlocks,
                active_events=active_events,
            )
        except (TypeError, ValueError) as exc:
            raise ResonancePcTradePlannerError(
                code="invalid_optimal_route_input",
                message=str(exc),
            ) from exc

    def plan_next_step(
        self,
        *,
        start_city_id: str,
        fatigue_budget: int,
        book_budget: int,
        cargo_capacity: int,
        book_profit_threshold: float,
        available_city_ids: List[str],
        station_product_whitelist: Optional[Dict[str, List[str]]] = None,
        snapshot_id: Optional[str] = None,
        current_holdings: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Dict[str, Any]:
        """Plan one rolling step and return the selected next move with full trace preview."""
        (
            city_id,
            remaining_fatigue,
            remaining_books,
            capacity,
            threshold,
            city_ids,
            snapshot,
            fatigue_payload,
            holdings,
        ) = self._prepare_common_inputs(
            start_city_id=start_city_id,
            fatigue_budget=fatigue_budget,
            book_budget=book_budget,
            cargo_capacity=cargo_capacity,
            book_profit_threshold=book_profit_threshold,
            available_city_ids=available_city_ids,
            snapshot_id=snapshot_id,
            current_holdings=current_holdings,
        )

        horizon = self._resolve_horizon(remaining_fatigue, city_ids, fatigue_payload["costs"])
        if horizon <= 0:
            return {
                "status": "no_feasible_move",
                "reason": "insufficient_fatigue_budget",
                "snapshot_id": snapshot.get("snapshot_id"),
                "planning_window": 0,
                "next_step": None,
                "selected_plan": None,
                "book_marginals": [],
            }

        allowed_products = self._resolve_station_product_scope(
            city_ids=city_ids,
            snapshot=snapshot,
            station_product_whitelist=station_product_whitelist,
        )
        if city_id not in allowed_products:
            allowed_products[city_id] = set()

        initial_state = _SearchState(
            city_id=city_id,
            remaining_fatigue=remaining_fatigue,
            remaining_books=remaining_books,
            holdings=self._clone_holdings(holdings),
            cum_profit=0.0,
            cum_fatigue=0,
            total_books_used=0,
            trace=[],
        )

        profit_by_cap: List[float] = []
        best_state_by_cap: List[Optional[_SearchState]] = []
        for cap in range(remaining_books + 1):
            state = self._beam_search(
                snapshot=snapshot,
                fatigue_costs=fatigue_payload["costs"],
                allowed_city_ids=city_ids,
                allowed_products=allowed_products,
                capacity=capacity,
                horizon=horizon,
                max_books_cap=cap,
                initial_state=initial_state,
            )
            best_state_by_cap.append(state)
            profit_by_cap.append(state.cum_profit if state else float("-inf"))

        selected_cap, marginals = self._select_book_cap(
            profits=profit_by_cap,
            threshold=threshold,
        )
        selected_state = best_state_by_cap[selected_cap]
        if selected_state is None or not selected_state.trace:
            return {
                "status": "no_feasible_move",
                "reason": "optimizer_no_transition",
                "snapshot_id": snapshot.get("snapshot_id"),
                "planning_window": horizon,
                "next_step": None,
                "selected_plan": None,
                "book_marginals": marginals,
            }

        station_sequence = [city_id]
        for step in selected_state.trace:
            station_sequence.append(step["to_city_id"])
        ratio = self._safe_ratio(selected_state.cum_profit, selected_state.cum_fatigue)
        next_step = copy.deepcopy(selected_state.trace[0])
        return {
            "status": "ok",
            "snapshot_id": snapshot.get("snapshot_id"),
            "planning_window": horizon,
            "selected_book_cap": selected_cap,
            "book_marginals": marginals,
            "next_step": next_step,
            "selected_plan": {
                "station_sequence": station_sequence,
                "steps": copy.deepcopy(selected_state.trace),
                "totals": {
                    "profit": selected_state.cum_profit,
                    "fatigue": selected_state.cum_fatigue,
                    "profit_per_fatigue": ratio,
                    "books_used": selected_state.total_books_used,
                    "remaining_books": selected_state.remaining_books,
                    "remaining_fatigue": selected_state.remaining_fatigue,
                },
            },
        }

    def simulate_until_stop(
        self,
        *,
        start_city_id: str,
        fatigue_budget: int,
        book_budget: int,
        cargo_capacity: int,
        book_profit_threshold: float,
        available_city_ids: List[str],
        station_product_whitelist: Optional[Dict[str, List[str]]] = None,
        snapshot_id: Optional[str] = None,
        max_iterations: int = 128,
    ) -> Dict[str, Any]:
        """Run rolling planning until stop condition and return execution trace."""
        (
            city_id,
            remaining_fatigue,
            remaining_books,
            capacity,
            threshold,
            city_ids,
            snapshot,
            fatigue_payload,
            holdings,
        ) = self._prepare_common_inputs(
            start_city_id=start_city_id,
            fatigue_budget=fatigue_budget,
            book_budget=book_budget,
            cargo_capacity=cargo_capacity,
            book_profit_threshold=book_profit_threshold,
            available_city_ids=available_city_ids,
            snapshot_id=snapshot_id,
            current_holdings=None,
        )

        executed_steps: List[Dict[str, Any]] = []
        total_profit = 0.0
        total_fatigue = 0
        loop_guard = max(int(max_iterations), 1)

        for _ in range(loop_guard):
            if remaining_fatigue <= 0:
                break
            decision = self.plan_next_step(
                start_city_id=city_id,
                fatigue_budget=remaining_fatigue,
                book_budget=remaining_books,
                cargo_capacity=capacity,
                book_profit_threshold=threshold,
                available_city_ids=city_ids,
                station_product_whitelist=station_product_whitelist,
                snapshot_id=str(snapshot.get("snapshot_id") or ""),
                current_holdings=holdings,
            )
            if decision.get("status") != "ok":
                break

            step = decision.get("next_step")
            if not isinstance(step, dict):
                break
            step_fatigue = int(step.get("fatigue_cost") or 0)
            if step_fatigue <= 0 or step_fatigue > remaining_fatigue:
                break

            step_profit = float(step.get("profit_delta") or 0.0)
            remaining_books = int(step.get("state_after", {}).get("remaining_books", remaining_books))
            remaining_fatigue = int(step.get("state_after", {}).get("remaining_fatigue", remaining_fatigue - step_fatigue))
            city_id = str(step.get("to_city_id") or city_id)
            holdings = self._normalize_holdings(step.get("state_after", {}).get("holdings", {}))
            total_profit += step_profit
            total_fatigue += step_fatigue
            executed_steps.append(copy.deepcopy(step))

        station_sequence = [str(start_city_id)]
        for step in executed_steps:
            station_sequence.append(str(step.get("to_city_id")))
        return {
            "status": "ok" if executed_steps else "stopped",
            "snapshot_id": snapshot.get("snapshot_id"),
            "station_sequence": station_sequence,
            "steps": executed_steps,
            "totals": {
                "profit": total_profit,
                "fatigue": total_fatigue,
                "profit_per_fatigue": self._safe_ratio(total_profit, total_fatigue),
                "books_used": int(book_budget) - remaining_books,
                "remaining_books": remaining_books,
                "remaining_fatigue": remaining_fatigue,
                "remaining_holdings": self._holdings_to_output(holdings),
            },
        }

    def plan_best_cycle(
        self,
        *,
        cargo_capacity: int = 120,
        book_budget: int = 0,
        book_profit_threshold: float = 0,
        available_city_ids: Optional[List[str]] = None,
        start_city_id: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city: Optional[str] = None,
        max_cycle_hops: int = DEFAULT_CYCLE_MAX_HOPS,
        station_product_whitelist: Optional[Dict[str, List[str]]] = None,
        snapshot_id: Optional[str] = None,
        cycle_beam_width: int = DEFAULT_CYCLE_BEAM_WIDTH,
        cycle_topk_next: int = DEFAULT_CYCLE_TOPK_NEXT,
    ) -> Dict[str, Any]:
        selected_payload = self._select_best_cycle_internal(
            cargo_capacity=cargo_capacity,
            book_budget=book_budget,
            book_profit_threshold=book_profit_threshold,
            available_city_ids=available_city_ids,
            start_city_id=start_city_id,
            current_city_id=current_city_id,
            current_city=current_city,
            max_cycle_hops=max_cycle_hops,
            station_product_whitelist=station_product_whitelist,
            snapshot_id=snapshot_id,
            cycle_beam_width=cycle_beam_width,
            cycle_topk_next=cycle_topk_next,
        )
        if "plan" in selected_payload:
            return selected_payload["plan"]

        return self._build_public_cycle_plan(
            selected=selected_payload["selected"],
            snapshot=selected_payload["snapshot"],
            fatigue_payload=selected_payload["fatigue_payload"],
            snapshot_id=selected_payload["snapshot"].get("snapshot_id"),
            books_budget=selected_payload["books_budget"],
        )

    def plan_next_cycle_execution(
        self,
        *,
        fatigue_budget: int,
        current_city_key: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city: Optional[str] = None,
        cargo_capacity: int = 650,
        book_budget: int = 0,
        book_profit_threshold: float = 0,
        max_cycle_hops: int = DEFAULT_CYCLE_MAX_HOPS,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        remaining_fatigue = self._coerce_non_negative_int("fatigue_budget", fatigue_budget)
        selected_payload = self._select_best_cycle_internal(
            cargo_capacity=cargo_capacity,
            book_budget=book_budget,
            book_profit_threshold=book_profit_threshold,
            available_city_ids=None,
            start_city_id=None,
            current_city_id=current_city_id,
            current_city_key=current_city_key,
            current_city=current_city,
            max_cycle_hops=max_cycle_hops,
            station_product_whitelist=None,
            snapshot_id=snapshot_id,
            use_trade_constraints=True,
        )
        if "plan" in selected_payload:
            plan = copy.deepcopy(selected_payload["plan"])
            plan["round_complete"] = False
            return plan

        selected = selected_payload["selected"]
        snapshot = selected_payload["snapshot"]
        fatigue_payload = selected_payload["fatigue_payload"]
        books_budget = int(selected_payload["books_budget"])
        full_fatigue = int(selected.get("fatigue") or 0)
        if full_fatigue <= remaining_fatigue:
            plan = self._build_public_cycle_plan(
                selected=selected,
                snapshot=snapshot,
                fatigue_payload=fatigue_payload,
                snapshot_id=snapshot.get("snapshot_id"),
                books_budget=books_budget,
            )
            plan["round_complete"] = True
            return plan

        prefix_selected = self._select_budgeted_cycle_prefix(
            selected=selected,
            fatigue_budget=remaining_fatigue,
        )
        if prefix_selected is None:
            plan = self._empty_cycle_plan(
                reason="insufficient_fatigue_for_positive_prefix",
                snapshot_id=snapshot.get("snapshot_id"),
                books_budget=books_budget,
            )
            plan["round_complete"] = False
            return plan

        plan = self._build_public_cycle_plan(
            selected=prefix_selected,
            snapshot=snapshot,
            fatigue_payload=fatigue_payload,
            snapshot_id=snapshot.get("snapshot_id"),
            books_budget=books_budget,
        )
        plan["round_complete"] = False
        return plan

    def _select_best_cycle_internal(
        self,
        *,
        cargo_capacity: int = 120,
        book_budget: int = 0,
        book_profit_threshold: float = 0,
        available_city_ids: Optional[List[str]] = None,
        start_city_id: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city_key: Optional[str] = None,
        current_city: Optional[str] = None,
        max_cycle_hops: int = DEFAULT_CYCLE_MAX_HOPS,
        station_product_whitelist: Optional[Dict[str, List[str]]] = None,
        snapshot_id: Optional[str] = None,
        cycle_beam_width: int = DEFAULT_CYCLE_BEAM_WIDTH,
        cycle_topk_next: int = DEFAULT_CYCLE_TOPK_NEXT,
        use_trade_constraints: bool = False,
    ) -> Dict[str, Any]:
        capacity = self._coerce_non_negative_int("cargo_capacity", cargo_capacity)
        if capacity <= 0:
            raise ResonancePcTradePlannerError(
                code="invalid_cargo_capacity",
                message="cargo_capacity must be greater than 0.",
            )
        max_books = self._coerce_non_negative_int("book_budget", book_budget)
        try:
            threshold = float(book_profit_threshold)
        except (TypeError, ValueError) as exc:
            raise ResonancePcTradePlannerError(
                code="invalid_book_profit_threshold",
                message="book_profit_threshold must be a number.",
            ) from exc

        horizon = max(int(max_cycle_hops), 2)
        del cycle_beam_width, cycle_topk_next
        constraints: Optional[Dict[str, Any]] = None
        if use_trade_constraints:
            constraints = self._load_trade_constraints_payload()

        if snapshot_id:
            snapshot = self.market_data.get_snapshot(snapshot_id=str(snapshot_id))
        else:
            snapshot = self.market_data.get_latest()
        if not isinstance(snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid.",
            )

        fatigue_payload = self.market_data.get_all_travel_fatigue()
        self._validate_fatigue_payload(fatigue_payload)
        all_costs = fatigue_payload.get("costs") or {}
        if not isinstance(all_costs, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must include costs object.",
            )

        if available_city_ids is None and constraints is not None:
            city_ids = [city_id for city_id in constraints["allowed_city_ids"] if city_id in all_costs]
        elif available_city_ids is None:
            city_ids = [str(item) for item in sorted([str(v) for v in all_costs.keys()], key=self._sort_key)]
        else:
            city_ids = self._normalize_city_list(available_city_ids, all_costs)
        if len(city_ids) < 2:
            return {
                "plan": self._empty_cycle_plan(
                    reason="insufficient_city_count",
                    snapshot_id=snapshot.get("snapshot_id"),
                    books_budget=max_books,
                )
            }

        normalized_current = self._resolve_current_city_id(
            current_city_id=current_city_id,
            current_city_key=current_city_key,
            current_city=current_city,
            fatigue_payload=fatigue_payload,
            city_key_to_id=(constraints or {}).get("key_to_city_id") if constraints is not None else None,
        )
        normalized_start = str(start_city_id).strip() if start_city_id is not None else None
        if normalized_start and normalized_current is None:
            if normalized_start not in city_ids:
                raise ResonancePcTradePlannerError(
                    code="invalid_start_city_id",
                    message=f"start_city_id '{normalized_start}' is not in available_city_ids.",
                )
            start_candidates = [normalized_start]
        else:
            start_candidates = list(city_ids)

        planning_city_ids = list(city_ids)
        if normalized_current is not None:
            if normalized_current not in all_costs:
                return {
                    "plan": self._empty_cycle_plan(
                        reason="current_city_not_in_fatigue_graph",
                        snapshot_id=snapshot.get("snapshot_id"),
                        books_budget=max_books,
                    )
                }
            if normalized_current not in planning_city_ids:
                planning_city_ids.append(normalized_current)
        elif current_city_id or current_city_key or current_city:
            raise ResonancePcTradePlannerError(
                code="current_city_not_resolved",
                message="Unable to resolve current city.",
                detail={
                    "city_key": current_city_key,
                    "current_city_id": current_city_id,
                    "current_city": current_city,
                },
            )

        allowed_products = self._resolve_station_product_scope(
            city_ids=planning_city_ids,
            snapshot=snapshot,
            station_product_whitelist=station_product_whitelist,
        )
        edge_table = self._build_cycle_edge_table(
            snapshot=snapshot,
            fatigue_costs=all_costs,
            allowed_city_ids=planning_city_ids,
            allowed_products=allowed_products,
            capacity=capacity,
            max_books=max_books,
        )

        selected = self._find_best_cycle_with_books(
            snapshot=snapshot,
            fatigue_payload=fatigue_payload,
            start_candidates=start_candidates,
            allowed_city_ids=city_ids,
            edge_table=edge_table,
            max_books=max_books,
            book_profit_threshold=threshold,
            max_cycle_hops=horizon,
            current_city_id=normalized_current,
        )
        if selected is None:
            return {
                "plan": self._empty_cycle_plan(
                    reason="no_profitable_cycle_found",
                    snapshot_id=snapshot.get("snapshot_id"),
                    books_budget=max_books,
                )
            }

        return {
            "selected": selected,
            "snapshot": snapshot,
            "fatigue_payload": fatigue_payload,
            "books_budget": max_books,
        }

    def get_trade_constraints(self) -> Dict[str, Any]:
        return copy.deepcopy(self._load_trade_constraints_payload())

    def assert_allowed_city(
        self,
        *,
        city_key: Optional[str] = None,
        city_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        constraints = self._load_trade_constraints_payload()
        normalized_city_key = str(city_key or "").strip()
        normalized_city_id = str(city_id or "").strip()
        if not normalized_city_key and not normalized_city_id:
            raise ResonancePcTradePlannerError(
                code="invalid_city",
                message="city_key or city_id is required.",
            )

        if normalized_city_key and normalized_city_key not in constraints["allowed_city_keys"]:
            raise ResonancePcTradePlannerError(
                code="unsupported_city",
                message=f"City '{normalized_city_key}' is not allowed in current trade constraints.",
                detail={
                    "city_key": normalized_city_key,
                    "allowed_city_ids": constraints["allowed_city_ids"],
                    "allowed_city_keys": constraints["allowed_city_keys"],
                },
            )
        if normalized_city_id and normalized_city_id not in constraints["allowed_city_ids"]:
            raise ResonancePcTradePlannerError(
                code="unsupported_city",
                message=f"City id '{normalized_city_id}' is not allowed in current trade constraints.",
                detail={
                    "city_id": normalized_city_id,
                    "allowed_city_ids": constraints["allowed_city_ids"],
                    "allowed_city_keys": constraints["allowed_city_keys"],
                },
            )

        if normalized_city_key and normalized_city_id:
            mapped_city_id = constraints["key_to_city_id"].get(normalized_city_key)
            if mapped_city_id != normalized_city_id:
                raise ResonancePcTradePlannerError(
                    code="city_mapping_conflict",
                    message="city_key and city_id do not refer to the same city.",
                    detail={
                        "city_key": normalized_city_key,
                        "city_id": normalized_city_id,
                        "mapped_city_id": mapped_city_id,
                    },
                )

        resolved_city_key = normalized_city_key or constraints["city_id_to_key"][normalized_city_id]
        resolved_city_id = normalized_city_id or constraints["key_to_city_id"][normalized_city_key]
        return {
            "ok": True,
            "city_key": resolved_city_key,
            "city_id": resolved_city_id,
            "allowed_city_ids": constraints["allowed_city_ids"],
            "allowed_city_keys": constraints["allowed_city_keys"],
        }

    def plan_cycle_execution(
        self,
        *,
        fatigue_budget: int,
        current_city_key: Optional[str] = None,
        current_city_id: Optional[str] = None,
        current_city: Optional[str] = None,
        cargo_capacity: int = 650,
        book_budget: int = 0,
        book_profit_threshold: float = 0,
        max_cycle_hops: int = DEFAULT_CYCLE_MAX_HOPS,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        constraints = self._load_trade_constraints_payload()
        normalized_city_key = str(current_city_key or "").strip()
        fixed_snapshot_id = str(snapshot_id or "").strip()

        budget = self._coerce_non_negative_int("fatigue_budget", fatigue_budget)
        capacity = self._coerce_non_negative_int("cargo_capacity", cargo_capacity)
        if capacity <= 0:
            raise ResonancePcTradePlannerError(
                code="invalid_cargo_capacity",
                message="cargo_capacity must be greater than 0.",
            )

        if fixed_snapshot_id:
            initial_snapshot = self.market_data.get_snapshot(snapshot_id=fixed_snapshot_id)
        else:
            initial_snapshot = self.market_data.get_latest()
        if not isinstance(initial_snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid.",
            )
        self._build_max_sell_price_cache(initial_snapshot)

        fatigue_payload = self.market_data.get_all_travel_fatigue()
        self._validate_fatigue_payload(fatigue_payload)
        fatigue_costs = fatigue_payload.get("costs") or {}
        if not isinstance(fatigue_costs, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must include costs object.",
            )

        allowed_city_ids = [city_id for city_id in constraints["allowed_city_ids"] if city_id in fatigue_costs]
        if len(allowed_city_ids) < 2:
            return self._empty_cycle_plan(
                reason="insufficient_city_count",
                snapshot_id=initial_snapshot.get("snapshot_id"),
                books_budget=self._coerce_non_negative_int("book_budget", book_budget),
            )

        resolved_current_city_id = self._resolve_current_city_id(
            current_city_id=current_city_id,
            current_city_key=normalized_city_key or None,
            current_city=current_city,
            fatigue_payload=fatigue_payload,
            city_key_to_id=constraints["key_to_city_id"],
        )
        if resolved_current_city_id is None:
            raise ResonancePcTradePlannerError(
                code="current_city_not_resolved",
                message="Unable to resolve current city.",
                detail={
                    "city_key": normalized_city_key,
                    "current_city_id": current_city_id,
                    "current_city": current_city,
                },
            )
        if resolved_current_city_id not in fatigue_costs:
            return self._empty_cycle_plan(
                reason="current_city_not_in_fatigue_graph",
                snapshot_id=initial_snapshot.get("snapshot_id"),
                books_budget=self._coerce_non_negative_int("book_budget", book_budget),
            )

        max_books = self._coerce_non_negative_int("book_budget", book_budget)
        if fixed_snapshot_id:
            active_snapshot = initial_snapshot
        else:
            active_snapshot = self.market_data.refresh(force=False)
        if not isinstance(active_snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid after refresh.",
            )
        self._build_max_sell_price_cache(active_snapshot)

        cycle_result = self.plan_best_cycle(
            cargo_capacity=capacity,
            book_budget=max_books,
            book_profit_threshold=book_profit_threshold,
            available_city_ids=allowed_city_ids,
            current_city_id=resolved_current_city_id,
            current_city=current_city,
            max_cycle_hops=max_cycle_hops,
            snapshot_id=str(active_snapshot.get("snapshot_id") or ""),
        )
        if cycle_result.get("status") != "ok":
            return cycle_result
        if int(cycle_result.get("fatigue_used") or 0) > budget:
            return self._empty_cycle_plan(
                reason="insufficient_fatigue_for_full_cycle",
                snapshot_id=cycle_result.get("snapshot_id") or active_snapshot.get("snapshot_id"),
                books_budget=max_books,
            )
        return cycle_result

    def _empty_cycle_plan(
        self,
        *,
        reason: str,
        snapshot_id: Any,
        books_budget: int,
    ) -> Dict[str, Any]:
        return {
            "status": "no_plan",
            "reason": reason,
            "snapshot_id": snapshot_id,
            "expected_profit": 0.0,
            "fatigue_used": 0,
            "books_budget": int(books_budget),
            "books_used": 0,
            "entry_route_count": 0,
            "city_cycle": [],
            "route": [],
        }

    def _select_budgeted_cycle_prefix(
        self,
        *,
        selected: Dict[str, Any],
        fatigue_budget: int,
    ) -> Optional[Dict[str, Any]]:
        budget = self._coerce_non_negative_int("fatigue_budget", fatigue_budget)
        steps = list(selected.get("steps") or [])
        if budget <= 0 or not steps:
            return None

        best_count = 0
        best_rank: Optional[Tuple[float, int, int]] = None
        total_profit = 0.0
        total_fatigue = 0
        total_books = 0
        running: List[Tuple[float, int, int]] = []
        for step in steps:
            step_fatigue = int((step or {}).get("fatigue_cost") or 0)
            if step_fatigue <= 0:
                break
            if total_fatigue + step_fatigue > budget:
                break
            total_profit += float((step or {}).get("profit_delta") or 0.0)
            total_fatigue += step_fatigue
            total_books += int((step or {}).get("books_used") or 0)
            running.append((total_profit, total_fatigue, total_books))
            rank = (float(total_profit), -int(total_fatigue), -int(total_books))
            if total_profit > 0 and (best_rank is None or rank > best_rank):
                best_rank = rank
                best_count = len(running)

        if best_count <= 0:
            return None

        prefix_steps = copy.deepcopy(steps[:best_count])
        prefix_profit, prefix_fatigue, prefix_books = running[best_count - 1]
        entry_route_count = int(selected.get("entry_route_count") or 0)
        if entry_route_count > best_count:
            entry_route_count = best_count
        return {
            "city_sequence": copy.deepcopy(selected.get("city_sequence") or []),
            "steps": prefix_steps,
            "profit": float(prefix_profit),
            "fatigue": int(prefix_fatigue),
            "books_used": int(prefix_books),
            "entry_route_count": int(entry_route_count),
        }

    def _build_public_cycle_plan(
        self,
        *,
        selected: Dict[str, Any],
        snapshot: Dict[str, Any],
        fatigue_payload: Dict[str, Any],
        snapshot_id: Any,
        books_budget: int,
    ) -> Dict[str, Any]:
        city_ids = [str(item) for item in selected.get("city_sequence") or []]
        city_cycle = [self._resolve_city_display_name(snapshot, fatigue_payload, city_id) for city_id in city_ids]
        route: List[Dict[str, Any]] = []
        for step in selected.get("steps") or []:
            from_city_id = str(step.get("from_city_id") or "")
            to_city_id = str(step.get("to_city_id") or "")
            route.append(
                {
                    "from_city": self._resolve_city_display_name(snapshot, fatigue_payload, from_city_id),
                    "to_city": self._resolve_city_display_name(snapshot, fatigue_payload, to_city_id),
                    "buy_products": list(step.get("buy_product_names") or []),
                    "books_used": int(step.get("books_used") or 0),
                }
            )
        return {
            "status": "ok",
            "reason": None,
            "snapshot_id": snapshot_id,
            "expected_profit": float(selected.get("profit") or 0.0),
            "fatigue_used": int(selected.get("fatigue") or 0),
            "books_budget": int(books_budget),
            "books_used": int(selected.get("books_used") or 0),
            "entry_route_count": int(selected.get("entry_route_count") or 0),
            "city_cycle": city_cycle,
            "route": route,
        }

    def _find_best_cycle_with_books(
        self,
        *,
        snapshot: Dict[str, Any],
        fatigue_payload: Dict[str, Any],
        start_candidates: List[str],
        allowed_city_ids: List[str],
        edge_table: Dict[Tuple[str, str], Dict[str, Any]],
        max_books: int,
        book_profit_threshold: float,
        max_cycle_hops: int,
        current_city_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        del fatigue_payload
        city_to_bit = {str(city_id): idx for idx, city_id in enumerate(allowed_city_ids)}
        best_state: Optional[Dict[str, Any]] = None
        best_rank: Optional[Tuple[float, int, int]] = None
        normalized_current = str(current_city_id).strip() if current_city_id is not None else None
        entry_candidates = list(allowed_city_ids) if normalized_current else list(start_candidates)

        for raw_entry in entry_candidates:
            entry_city = str(raw_entry)
            if entry_city not in city_to_bit:
                continue
            entry_is_direct = normalized_current is None or normalized_current == entry_city
            stable_city_ids = [
                str(city_id)
                for city_id in allowed_city_ids
                if entry_is_direct or str(city_id) != normalized_current
            ]
            if entry_city not in stable_city_ids:
                continue

            start_mask = 1 << city_to_bit[entry_city]
            states: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
            if entry_is_direct:
                states[(start_mask, entry_city, 0)] = {
                    "path": [entry_city],
                    "edge_books": [],
                    "profit": 0.0,
                    "fatigue": 0,
                    "entry_route_count": 0,
                }
            else:
                entry_edge = edge_table.get((str(normalized_current), entry_city))
                if entry_edge is None:
                    continue
                for entry_books in self._valid_book_counts_for_edge(
                    entry_edge,
                    max_books=max_books,
                    book_profit_threshold=book_profit_threshold,
                ):
                    plans = entry_edge.get("plans") or []
                    if entry_books >= len(plans):
                        continue
                    plan = plans[entry_books]
                    states[(start_mask, entry_city, int(entry_books))] = {
                        "path": [entry_city],
                        "edge_books": [int(entry_books)],
                        "profit": float((plan or {}).get("profit") or 0.0),
                        "fatigue": int(entry_edge.get("fatigue") or 0),
                        "entry_route_count": 1,
                    }

            for _depth in range(max_cycle_hops):
                next_states: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
                for (mask, last_city, used_books), state in states.items():
                    path = [str(item) for item in state.get("path") or []]
                    if not path:
                        continue
                    path_edges = len(path) - 1
                    if path_edges >= 1:
                        return_edge = edge_table.get((last_city, entry_city))
                        if return_edge is not None and path_edges + 1 <= max_cycle_hops:
                            remaining_books = int(max_books) - int(used_books)
                            for return_books in self._valid_book_counts_for_edge(
                                return_edge,
                                max_books=remaining_books,
                                book_profit_threshold=book_profit_threshold,
                            ):
                                plans = return_edge.get("plans") or []
                                if return_books >= len(plans):
                                    continue
                                return_plan = plans[return_books]
                                profit_total = float(state.get("profit") or 0.0) + float(
                                    (return_plan or {}).get("profit") or 0.0
                                )
                                if profit_total <= 0:
                                    continue
                                fatigue_total = int(state.get("fatigue") or 0) + int(return_edge.get("fatigue") or 0)
                                books_total = int(used_books) + int(return_books)
                                rank = (float(profit_total), -int(fatigue_total), -int(books_total))
                                if best_rank is None or rank > best_rank:
                                    entry_route_count = int(state.get("entry_route_count") or 0)
                                    route_sequence = list(path) + [entry_city]
                                    if entry_route_count:
                                        route_sequence = [str(normalized_current)] + route_sequence
                                    best_rank = rank
                                    best_state = {
                                        "city_sequence": path + [entry_city],
                                        "route_sequence": route_sequence,
                                        "edge_books": list(state.get("edge_books") or []) + [int(return_books)],
                                        "profit": float(profit_total),
                                        "fatigue": int(fatigue_total),
                                        "books_used": int(books_total),
                                        "entry_route_count": entry_route_count,
                                    }

                    if path_edges >= max_cycle_hops - 1:
                        continue

                    for next_city in stable_city_ids:
                        next_city = str(next_city)
                        bit = city_to_bit.get(next_city)
                        if bit is None or (mask & (1 << bit)):
                            continue
                        edge = edge_table.get((last_city, next_city))
                        if edge is None:
                            continue
                        remaining_books = int(max_books) - int(used_books)
                        for books_on_edge in self._valid_book_counts_for_edge(
                            edge,
                            max_books=remaining_books,
                            book_profit_threshold=book_profit_threshold,
                        ):
                            plans = edge.get("plans") or []
                            if books_on_edge >= len(plans):
                                continue
                            plan = plans[books_on_edge]
                            next_used = int(used_books) + int(books_on_edge)
                            if next_used > max_books:
                                continue
                            next_mask = mask | (1 << bit)
                            candidate = {
                                "path": path + [next_city],
                                "edge_books": list(state.get("edge_books") or []) + [int(books_on_edge)],
                                "profit": float(state.get("profit") or 0.0)
                                + float((plan or {}).get("profit") or 0.0),
                                "fatigue": int(state.get("fatigue") or 0) + int(edge.get("fatigue") or 0),
                                "entry_route_count": int(state.get("entry_route_count") or 0),
                            }
                            key = (next_mask, next_city, next_used)
                            existing = next_states.get(key)
                            if existing is None or self._cycle_state_is_better(candidate, existing):
                                next_states[key] = candidate
                if not next_states:
                    break
                states = next_states

        if best_state is None:
            return None

        city_sequence = [str(item) for item in best_state.get("city_sequence") or []]
        route_sequence = [str(item) for item in best_state.get("route_sequence") or city_sequence]
        edge_books = [int(item) for item in best_state.get("edge_books") or []]
        steps: List[Dict[str, Any]] = []
        fatigue_total = 0
        for idx in range(len(route_sequence) - 1):
            from_city = route_sequence[idx]
            to_city = route_sequence[idx + 1]
            books_used = edge_books[idx] if idx < len(edge_books) else 0
            edge = edge_table.get((from_city, to_city))
            if edge is None:
                return None
            plans = edge.get("plans") or []
            if books_used >= len(plans):
                return None
            plan = plans[books_used]
            edge_fatigue = int(edge.get("fatigue") or 0)
            fatigue_total += edge_fatigue
            steps.append(
                {
                    "from_city_id": from_city,
                    "to_city_id": to_city,
                    "fatigue_cost": edge_fatigue,
                    "books_used": int(books_used),
                    "profit_delta": float((plan or {}).get("profit") or 0.0),
                    "buy_product_names": self._buy_product_names_from_plan(snapshot, plan or {}),
                }
            )

        return {
            "city_sequence": city_sequence,
            "steps": steps,
            "profit": float(best_state.get("profit") or 0.0),
            "fatigue": int(fatigue_total),
            "books_used": int(best_state.get("books_used") or 0),
            "entry_route_count": int(best_state.get("entry_route_count") or 0),
        }

    @staticmethod
    def _cycle_state_is_better(candidate: Dict[str, Any], existing: Dict[str, Any]) -> bool:
        candidate_profit = float(candidate.get("profit") or 0.0)
        existing_profit = float(existing.get("profit") or 0.0)
        if candidate_profit != existing_profit:
            return candidate_profit > existing_profit
        return int(candidate.get("fatigue") or 0) < int(existing.get("fatigue") or 0)

    @staticmethod
    def _valid_book_counts_for_edge(
        edge: Dict[str, Any],
        *,
        max_books: int,
        book_profit_threshold: float,
    ) -> List[int]:
        plans = edge.get("plans") or []
        if not plans:
            return []
        allowed = [0]
        marginals = edge.get("marginals") or []
        limit = min(max(int(max_books), 0), len(plans) - 1)
        for book_idx in range(1, limit + 1):
            marginal = float(marginals[book_idx - 1]) if book_idx - 1 < len(marginals) else float("-inf")
            if marginal >= float(book_profit_threshold):
                allowed.append(book_idx)
            else:
                break
        return allowed

    def _buy_product_names_from_plan(self, snapshot: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
        product_names: List[str] = []
        seen_product_ids: set[str] = set()
        for buy in plan.get("buys") or []:
            product_id = str((buy or {}).get("product_id") or "").strip()
            if not product_id or product_id in seen_product_ids:
                continue
            seen_product_ids.add(product_id)
            product_names.append(self._resolve_product_name(snapshot, product_id))
        return product_names

    def _resolve_current_city_id(
        self,
        *,
        current_city_id: Optional[str],
        current_city_key: Optional[str],
        current_city: Optional[str],
        fatigue_payload: Dict[str, Any],
        city_key_to_id: Optional[Dict[str, str]],
    ) -> Optional[str]:
        resolved: List[Tuple[str, str]] = []
        provided: List[Tuple[str, str]] = []
        for label, token in (
            ("current_city_id", current_city_id),
            ("current_city_key", current_city_key),
            ("current_city", current_city),
        ):
            raw_value = str(token or "").strip()
            if raw_value:
                provided.append((label, raw_value))
            city_id = self._resolve_city_id_token(
                token,
                fatigue_payload=fatigue_payload,
                city_key_to_id=city_key_to_id,
            )
            if city_id is not None:
                resolved.append((label, city_id))

        if not resolved:
            if provided:
                raise ResonancePcTradePlannerError(
                    code="current_city_not_resolved",
                    message="Unable to resolve current city.",
                    detail={label: value for label, value in provided},
                )
            return None
        first_city_id = resolved[0][1]
        conflicts = [(label, city_id) for label, city_id in resolved if city_id != first_city_id]
        if conflicts:
            raise ResonancePcTradePlannerError(
                code="current_city_conflict",
                message="Current city inputs do not resolve to the same city.",
                detail={
                    "resolved": [{"field": label, "city_id": city_id} for label, city_id in resolved],
                },
            )
        return first_city_id

    def _resolve_city_id_token(
        self,
        token: Optional[str],
        *,
        fatigue_payload: Dict[str, Any],
        city_key_to_id: Optional[Dict[str, str]],
    ) -> Optional[str]:
        value = str(token or "").strip()
        if not value:
            return None

        fatigue_costs = fatigue_payload.get("costs") or {}
        if isinstance(fatigue_costs, dict) and value in fatigue_costs:
            return value

        key_maps: List[Dict[str, str]] = []
        if isinstance(city_key_to_id, dict):
            key_maps.append({str(k): str(v) for k, v in city_key_to_id.items()})
        key_maps.append(self.KNOWN_CITY_KEY_TO_ID)
        for key_map in key_maps:
            city_id = key_map.get(value)
            if city_id and (not isinstance(fatigue_costs, dict) or city_id in fatigue_costs):
                return str(city_id)

        city_names = fatigue_payload.get("cities") or {}
        if isinstance(city_names, dict):
            alias_payload = self._load_city_name_aliases()
            aliases = {str(k): str(v) for k, v in alias_payload.items()}
            lookup_values = [value]
            if value in aliases:
                lookup_values.append(aliases[value])

            normalized_lookup = {self._normalize_city_lookup_text(item) for item in lookup_values}
            for raw_city_id, raw_name in city_names.items():
                city_id = str(raw_city_id)
                city_name = str(raw_name or "").strip()
                if not city_name:
                    continue
                if value == city_name or self._normalize_city_lookup_text(city_name) in normalized_lookup:
                    return city_id
        return None

    def _load_city_name_aliases(self) -> Dict[str, str]:
        alias_file = self.meta_dir / "city_aliases.json"
        if not alias_file.is_file():
            return {}
        try:
            payload = json.loads(alias_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items() if str(k).strip() and str(v).strip()}

    @staticmethod
    def _normalize_city_lookup_text(raw: Any) -> str:
        return "".join(ch for ch in str(raw).lower() if ch.isalnum())

    @staticmethod
    def _resolve_city_display_name(snapshot: Dict[str, Any], fatigue_payload: Dict[str, Any], city_id: str) -> str:
        city_key = str(city_id)
        snapshot_cities = snapshot.get("cities") or {}
        if isinstance(snapshot_cities, dict):
            city_payload = snapshot_cities.get(city_key)
            if isinstance(city_payload, dict):
                name = str(city_payload.get("name") or "").strip()
                if name:
                    return name
            elif isinstance(city_payload, str) and city_payload.strip():
                return city_payload.strip()

        fatigue_cities = fatigue_payload.get("cities") or {}
        if isinstance(fatigue_cities, dict):
            name = str(fatigue_cities.get(city_key) or "").strip()
            if name:
                return name
        return city_key

    def _rotate_cycle_steps(self, steps: List[Dict[str, Any]], start_city_id: str) -> List[Dict[str, Any]]:
        target_city = str(start_city_id or "").strip()
        if not target_city:
            return []
        if not steps:
            return []
        for index, step in enumerate(steps):
            if str(step.get("from_city_id") or "") == target_city:
                return copy.deepcopy(steps[index:] + steps[:index])
        return []

    def _build_cycle_leg(
        self,
        *,
        cycle_step: Dict[str, Any],
        snapshot: Dict[str, Any],
        city_id_to_key: Dict[str, str],
    ) -> Dict[str, Any]:
        from_city_id = str(cycle_step.get("from_city_id") or "")
        to_city_id = str(cycle_step.get("to_city_id") or "")
        buy_names: List[str] = []
        buy_ids: List[str] = []
        for buy in cycle_step.get("buys") or []:
            product_id = str((buy or {}).get("product_id") or "").strip()
            if not product_id:
                continue
            if product_id not in buy_ids:
                buy_ids.append(product_id)
                buy_names.append(self._resolve_product_name(snapshot, product_id))
        return {
            "phase": "cycle",
            "from_city_id": from_city_id,
            "to_city_id": to_city_id,
            "from_city_key": city_id_to_key.get(from_city_id, from_city_id),
            "to_city_key": city_id_to_key.get(to_city_id, to_city_id),
            "fatigue_cost": int(cycle_step.get("fatigue_cost") or 0),
            "profit_estimate": float(cycle_step.get("profit_delta") or 0.0),
            "buy_product_ids": buy_ids,
            "buy_product_names": buy_names,
            "books_used": int(cycle_step.get("books_used") or 0),
        }

    def _plan_one_way_entry_leg(
        self,
        *,
        current_city_id: str,
        cycle_steps: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
        fatigue_costs: Dict[str, Dict[str, int]],
        cargo_capacity: int,
        city_id_to_key: Dict[str, str],
    ) -> Dict[str, Any]:
        candidate_city_ids: List[str] = []
        first_hop_profit: Dict[str, float] = {}
        for step in cycle_steps:
            from_city = str(step.get("from_city_id") or "")
            if not from_city:
                continue
            if from_city not in candidate_city_ids:
                candidate_city_ids.append(from_city)
            first_hop_profit[from_city] = float(step.get("profit_delta") or 0.0)

        row = fatigue_costs.get(current_city_id) or {}
        profitable_options: List[Dict[str, Any]] = []
        fallback_options: List[Dict[str, Any]] = []
        for candidate_city_id in candidate_city_ids:
            fatigue = int(row.get(candidate_city_id, 0))
            if fatigue <= 0:
                continue
            trade = self._build_one_way_trade_buys(
                from_city_id=current_city_id,
                to_city_id=candidate_city_id,
                snapshot=snapshot,
                cargo_capacity=cargo_capacity,
            )
            option = {
                "to_city_id": candidate_city_id,
                "fatigue_cost": fatigue,
                "profit_estimate": float(trade["profit_estimate"]),
                "buy_product_ids": trade["buy_product_ids"],
                "buy_product_names": trade["buy_product_names"],
                "first_hop_profit": float(first_hop_profit.get(candidate_city_id, 0.0)),
            }
            if option["profit_estimate"] > 0:
                profitable_options.append(option)
            fallback_options.append(option)

        if profitable_options:
            selected = max(
                profitable_options,
                key=lambda item: (
                    self._safe_ratio(float(item["profit_estimate"]), int(item["fatigue_cost"])),
                    float(item["profit_estimate"]),
                    float(item["first_hop_profit"]),
                    -int(item["fatigue_cost"]),
                ),
            )
            phase = "one_way_trade"
        elif fallback_options:
            selected = min(
                fallback_options,
                key=lambda item: (
                    int(item["fatigue_cost"]),
                    -float(item["first_hop_profit"]),
                    str(item["to_city_id"]),
                ),
            )
            selected = {
                **selected,
                "profit_estimate": 0.0,
                "buy_product_ids": [],
                "buy_product_names": [],
            }
            phase = "one_way_empty"
        else:
            raise ResonancePcTradePlannerError(
                code="entry_path_not_found",
                message=f"Unable to find reachable entry city from '{current_city_id}'.",
            )

        to_city_id = str(selected["to_city_id"])
        return {
            "phase": phase,
            "from_city_id": current_city_id,
            "to_city_id": to_city_id,
            "from_city_key": city_id_to_key.get(current_city_id, current_city_id),
            "to_city_key": city_id_to_key.get(to_city_id, to_city_id),
            "fatigue_cost": int(selected["fatigue_cost"]),
            "profit_estimate": float(selected["profit_estimate"]),
            "buy_product_ids": list(selected["buy_product_ids"]),
            "buy_product_names": list(selected["buy_product_names"]),
            "books_used": 0,
        }

    def _build_one_way_trade_buys(
        self,
        *,
        from_city_id: str,
        to_city_id: str,
        snapshot: Dict[str, Any],
        cargo_capacity: int,
    ) -> Dict[str, Any]:
        products = snapshot.get("products") or {}
        if not isinstance(products, dict):
            products = {}
        buy_lot = self._load_buy_lot_payload()["city_product_buy_lot"]
        lots = buy_lot.get(from_city_id) or {}
        if not isinstance(lots, dict):
            lots = {}

        candidates: List[Tuple[float, str, int]] = []
        for product_id in sorted(products.keys(), key=self._sort_key):
            buy_price = self._buy_price(products, str(product_id), from_city_id)
            sell_price = self._sell_price(products, str(product_id), to_city_id)
            if buy_price is None or sell_price is None:
                continue
            try:
                lot = int(lots.get(str(product_id), 0))
            except (TypeError, ValueError):
                lot = 0
            if lot <= 0:
                continue
            unit_profit = float(sell_price) - float(buy_price)
            if unit_profit <= 0:
                continue
            candidates.append((unit_profit, str(product_id), lot))
        candidates.sort(key=lambda row: (-row[0], self._sort_key(row[1])))

        free_capacity = int(cargo_capacity)
        total_profit = 0.0
        buy_product_ids: List[str] = []
        buy_product_names: List[str] = []
        for unit_profit, product_id, lot in candidates:
            if free_capacity <= 0:
                break
            qty = min(int(lot), int(free_capacity))
            if qty <= 0:
                continue
            free_capacity -= qty
            total_profit += float(unit_profit) * qty
            if product_id not in buy_product_ids:
                buy_product_ids.append(product_id)
                buy_product_names.append(self._resolve_product_name(snapshot, product_id))
        return {
            "profit_estimate": float(total_profit),
            "buy_product_ids": buy_product_ids,
            "buy_product_names": buy_product_names,
        }

    @staticmethod
    def _resolve_product_name(snapshot: Dict[str, Any], product_id: str) -> str:
        products = snapshot.get("products") or {}
        product = products.get(str(product_id)) if isinstance(products, dict) else None
        if isinstance(product, dict):
            name = str(product.get("name") or "").strip()
            if name:
                return name
        return f"unknown_{product_id}"

    def _prepare_common_inputs(
        self,
        *,
        start_city_id: str,
        fatigue_budget: int,
        book_budget: int,
        cargo_capacity: int,
        book_profit_threshold: float,
        available_city_ids: Iterable[str],
        snapshot_id: Optional[str],
        current_holdings: Optional[Dict[str, Dict[str, float]]],
    ) -> Tuple[str, int, int, int, float, List[str], Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, float]]]:
        city_id = str(start_city_id or "").strip()
        if not city_id:
            raise ResonancePcTradePlannerError(
                code="invalid_start_city_id",
                message="start_city_id is required.",
            )

        remaining_fatigue = self._coerce_non_negative_int("fatigue_budget", fatigue_budget)
        remaining_books = self._coerce_non_negative_int("book_budget", book_budget)
        capacity = self._coerce_non_negative_int("cargo_capacity", cargo_capacity)
        if capacity <= 0:
            raise ResonancePcTradePlannerError(
                code="invalid_cargo_capacity",
                message="cargo_capacity must be greater than 0.",
            )

        try:
            threshold = float(book_profit_threshold)
        except (TypeError, ValueError) as exc:
            raise ResonancePcTradePlannerError(
                code="invalid_book_profit_threshold",
                message="book_profit_threshold must be a number.",
            ) from exc

        if snapshot_id:
            snapshot = self.market_data.get_snapshot(snapshot_id=str(snapshot_id))
        else:
            snapshot = self.market_data.get_latest()
        if not isinstance(snapshot, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_snapshot",
                message="Market snapshot payload is invalid.",
            )
        self._build_max_sell_price_cache(snapshot)

        fatigue_payload = self.market_data.get_all_travel_fatigue()
        self._validate_fatigue_payload(fatigue_payload)

        city_ids = self._normalize_city_list(available_city_ids, fatigue_payload["costs"])
        if city_id not in city_ids:
            city_ids.append(city_id)

        holdings = self._normalize_holdings(current_holdings or {})
        return (
            city_id,
            remaining_fatigue,
            remaining_books,
            capacity,
            threshold,
            city_ids,
            snapshot,
            fatigue_payload,
            holdings,
        )

    def _build_cycle_edge_table(
        self,
        *,
        snapshot: Dict[str, Any],
        fatigue_costs: Dict[str, Dict[str, int]],
        allowed_city_ids: List[str],
        allowed_products: Dict[str, set[str]],
        capacity: int,
        max_books: int,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        products = snapshot.get("products") or {}
        if not isinstance(products, dict):
            products = {}
        buy_lot = self._load_buy_lot_payload()["city_product_buy_lot"]

        table: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for from_city in allowed_city_ids:
            row = fatigue_costs.get(from_city) or {}
            for to_city in allowed_city_ids:
                if from_city == to_city:
                    continue
                fatigue = int(row.get(to_city, 0))
                if fatigue <= 0:
                    continue
                plans: List[Dict[str, Any]] = []
                for books_used in range(max_books + 1):
                    plan = self._build_edge_trade_plan(
                        from_city=from_city,
                        to_city=to_city,
                        products=products,
                        allowed_products=allowed_products.get(from_city, set()),
                        buy_lot=(buy_lot.get(from_city) or {}),
                        capacity=capacity,
                        books_used=books_used,
                    )
                    plans.append(plan)
                marginals: List[float] = []
                for idx in range(1, len(plans)):
                    marginals.append(float(plans[idx]["profit"]) - float(plans[idx - 1]["profit"]))
                table[(from_city, to_city)] = {
                    "fatigue": fatigue,
                    "plans": plans,
                    "marginals": marginals,
                }
        return table

    def _build_edge_trade_plan(
        self,
        *,
        from_city: str,
        to_city: str,
        products: Dict[str, Any],
        allowed_products: set[str],
        buy_lot: Dict[str, Any],
        capacity: int,
        books_used: int,
    ) -> Dict[str, Any]:
        multiplier = int(books_used) + 1
        free_capacity = int(capacity)
        if free_capacity <= 0 or not allowed_products:
            return {"profit": 0.0, "buys": [], "sells": []}

        candidates: List[Tuple[float, str, float, float, int]] = []
        for product_id in allowed_products:
            buy_price = self._buy_price(products, product_id, from_city)
            sell_price = self._sell_price(products, product_id, to_city)
            if buy_price is None or sell_price is None:
                continue
            unit_profit = float(sell_price) - float(buy_price)
            if unit_profit <= 0:
                continue
            try:
                lot = int((buy_lot or {}).get(product_id, 0))
            except (TypeError, ValueError):
                lot = 0
            if lot <= 0:
                continue
            qty_max = lot * multiplier
            if qty_max <= 0:
                continue
            candidates.append((unit_profit, str(product_id), float(buy_price), float(sell_price), int(qty_max)))
        candidates.sort(key=lambda row: (-row[0], self._sort_key(row[1])))

        buys: List[Dict[str, Any]] = []
        sells: List[Dict[str, Any]] = []
        total_profit = 0.0
        for unit_profit, product_id, buy_price, sell_price, qty_max in candidates:
            if free_capacity <= 0:
                break
            qty = min(int(qty_max), int(free_capacity))
            if qty <= 0:
                continue
            profit = float(unit_profit) * int(qty)
            total_profit += profit
            free_capacity -= int(qty)
            buys.append(
                {
                    "product_id": product_id,
                    "qty": int(qty),
                    "buy_price": float(buy_price),
                    "sell_price": float(sell_price),
                    "unit_profit": float(unit_profit),
                }
            )
            sells.append(
                {
                    "product_id": product_id,
                    "qty": int(qty),
                    "sell_price": float(sell_price),
                    "avg_buy_price": float(buy_price),
                    "profit": float(profit),
                    "action": "sell_all",
                }
            )
        return {"profit": float(total_profit), "buys": buys, "sells": sells}

    def _enumerate_cycle_candidates(
        self,
        *,
        start_city_id: str,
        allowed_city_ids: List[str],
        edge_table: Dict[Tuple[str, str], Dict[str, Any]],
        max_cycle_hops: int,
        beam_width: int,
        topk_next: int,
    ) -> List[Dict[str, Any]]:
        frontier: List[Tuple[List[str], float]] = [([start_city_id], 0.0)]
        candidates: List[Dict[str, Any]] = []

        for depth in range(1, max_cycle_hops + 1):
            expanded: List[Tuple[List[str], float]] = []
            for path, path_profit in frontier:
                current = path[-1]
                edge_back = edge_table.get((current, start_city_id))
                if edge_back is not None and depth >= 2:
                    cycle_profit = float(path_profit) + float((edge_back["plans"][0] or {}).get("profit") or 0.0)
                    candidates.append({"cycle_nodes": path + [start_city_id], "base_profit": cycle_profit})

                if depth >= max_cycle_hops:
                    continue
                options: List[Tuple[float, str]] = []
                visited = set(path)
                for next_city in allowed_city_ids:
                    if next_city in visited:
                        continue
                    edge = edge_table.get((current, next_city))
                    if edge is None:
                        continue
                    edge_profit = float((edge["plans"][0] or {}).get("profit") or 0.0)
                    options.append((edge_profit, next_city))
                options.sort(key=lambda row: row[0], reverse=True)
                for edge_profit, next_city in options[:topk_next]:
                    expanded.append((path + [next_city], float(path_profit) + float(edge_profit)))

            if not expanded:
                break
            expanded.sort(key=lambda item: item[1], reverse=True)
            frontier = expanded[:beam_width]
        return candidates

    def _evaluate_cycle_with_books(
        self,
        *,
        cycle_nodes: List[str],
        edge_table: Dict[Tuple[str, str], Dict[str, Any]],
        max_books: int,
        book_profit_threshold: float,
    ) -> Optional[Dict[str, Any]]:
        if len(cycle_nodes) < 3:
            return None
        edges: List[Tuple[str, str, Dict[str, Any]]] = []
        for idx in range(len(cycle_nodes) - 1):
            a = cycle_nodes[idx]
            b = cycle_nodes[idx + 1]
            edge = edge_table.get((a, b))
            if edge is None:
                return None
            edges.append((a, b, edge))

        valid_books_per_edge: List[List[int]] = []
        for _, _, edge in edges:
            plans = edge.get("plans") or []
            marginals = edge.get("marginals") or []
            allowed = [0]
            for book_idx in range(1, min(max_books, len(plans) - 1) + 1):
                marginal = float(marginals[book_idx - 1]) if book_idx - 1 < len(marginals) else float("-inf")
                if marginal >= float(book_profit_threshold):
                    allowed.append(book_idx)
                else:
                    break
            valid_books_per_edge.append(allowed)

        edge_count = len(edges)
        dp = [[float("-inf")] * (max_books + 1) for _ in range(edge_count + 1)]
        parent: List[List[Optional[Tuple[int, int]]]] = [[None] * (max_books + 1) for _ in range(edge_count + 1)]
        dp[0][0] = 0.0

        for i in range(edge_count):
            plans = edges[i][2]["plans"]
            allowed_books = valid_books_per_edge[i]
            for used_books in range(max_books + 1):
                base = dp[i][used_books]
                if base == float("-inf"):
                    continue
                for books_on_edge in allowed_books:
                    next_used = used_books + int(books_on_edge)
                    if next_used > max_books:
                        continue
                    edge_profit = float((plans[books_on_edge] or {}).get("profit") or 0.0)
                    cand = base + edge_profit
                    if cand > dp[i + 1][next_used]:
                        dp[i + 1][next_used] = cand
                        parent[i + 1][next_used] = (used_books, books_on_edge)

        best_used = 0
        best_profit = float("-inf")
        for used_books in range(max_books + 1):
            value = dp[edge_count][used_books]
            if value > best_profit:
                best_profit = value
                best_used = used_books
        if best_profit == float("-inf"):
            return None

        books_per_edge = [0] * edge_count
        cursor_used = best_used
        for i in range(edge_count, 0, -1):
            prev = parent[i][cursor_used]
            if prev is None:
                return None
            prev_used, chosen_books = prev
            books_per_edge[i - 1] = int(chosen_books)
            cursor_used = prev_used

        fatigue_total = 0
        steps: List[Dict[str, Any]] = []
        for idx, (from_city, to_city, edge) in enumerate(edges):
            books_used = books_per_edge[idx]
            plan = edge["plans"][books_used]
            edge_fatigue = int(edge.get("fatigue") or 0)
            fatigue_total += edge_fatigue
            steps.append(
                {
                    "from_city_id": from_city,
                    "to_city_id": to_city,
                    "fatigue_cost": edge_fatigue,
                    "books_used": books_used,
                    "buy_multiplier": books_used + 1,
                    "profit_delta": float(plan.get("profit") or 0.0),
                    "buys": copy.deepcopy(plan.get("buys") or []),
                    "sells": copy.deepcopy(plan.get("sells") or []),
                }
            )

        profit_total = float(best_profit)
        return {
            "city_sequence": cycle_nodes,
            "steps": steps,
            "profit": profit_total,
            "fatigue": int(fatigue_total),
            "profit_per_fatigue": self._safe_ratio(profit_total, fatigue_total),
            "books_used": int(best_used),
        }

    def _canonical_cycle_key(self, cycle_nodes_without_tail: List[str]) -> Tuple[str, ...]:
        if not cycle_nodes_without_tail:
            return tuple()
        nodes = [str(item) for item in cycle_nodes_without_tail]
        size = len(nodes)
        rotations: List[Tuple[str, ...]] = []
        for offset in range(size):
            rotations.append(tuple(nodes[offset:] + nodes[:offset]))
        return min(rotations)

    def _resolve_station_product_scope(
        self,
        *,
        city_ids: List[str],
        snapshot: Dict[str, Any],
        station_product_whitelist: Optional[Dict[str, List[str]]],
    ) -> Dict[str, set[str]]:
        buy_lot = self._load_buy_lot_payload()["city_product_buy_lot"]
        products = snapshot.get("products") or {}
        if not isinstance(products, dict):
            products = {}

        market_buy_index: Dict[str, set[str]] = {}
        for product_id, product_data in products.items():
            buy_map = (((product_data or {}).get("market") or {}).get("buy") or {})
            if not isinstance(buy_map, dict):
                continue
            for city_id in buy_map.keys():
                market_buy_index.setdefault(str(city_id), set()).add(str(product_id))

        if station_product_whitelist is not None:
            allowed: Dict[str, set[str]] = {city_id: set() for city_id in city_ids}
            for raw_city_id, raw_products in station_product_whitelist.items():
                city_id = str(raw_city_id).strip()
                if city_id not in allowed:
                    continue
                if not isinstance(raw_products, list):
                    continue
                product_ids = [str(pid).strip() for pid in raw_products if str(pid).strip()]
                for product_id in product_ids:
                    if product_id in market_buy_index.get(city_id, set()) and int(
                        buy_lot.get(city_id, {}).get(product_id, 0)
                    ) > 0:
                        allowed[city_id].add(product_id)
            return allowed

        default_scope: Dict[str, set[str]] = {city_id: set() for city_id in city_ids}
        for city_id in city_ids:
            lots = buy_lot.get(city_id, {})
            if not isinstance(lots, dict):
                continue
            for product_id, lot in lots.items():
                if int(lot) <= 0:
                    continue
                pid = str(product_id)
                if pid in market_buy_index.get(city_id, set()):
                    default_scope[city_id].add(pid)
        return default_scope

    def _resolve_horizon(self, remaining_fatigue: int, city_ids: List[str], fatigue_costs: Dict[str, Dict[str, int]]) -> int:
        if remaining_fatigue <= 0:
            return 0
        min_positive = None
        for city_a in city_ids:
            row = fatigue_costs.get(city_a) or {}
            for city_b in city_ids:
                if city_a == city_b:
                    continue
                value = int(row.get(city_b, 0))
                if value <= 0:
                    continue
                if min_positive is None or value < min_positive:
                    min_positive = value
        if min_positive is None:
            return 0
        feasible_hops = remaining_fatigue // min_positive
        return min(self.MAX_ROLLING_WINDOW, int(feasible_hops))

    def _load_buy_lot_payload(self) -> Dict[str, Any]:
        if self._buy_lot_payload is not None:
            return self._buy_lot_payload
        if not self.buy_lot_file.is_file():
            raise ResonancePcTradePlannerError(
                code="buy_lot_missing",
                message=f"buy_lot metadata file not found: {self.buy_lot_file}",
            )
        payload = json.loads(self.buy_lot_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="buy_lot_invalid",
                message="buy_lot payload must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.BUY_LOT_SCHEMA_VERSION:
            raise ResonancePcTradePlannerError(
                code="buy_lot_invalid",
                message=(
                    f"Unsupported buy_lot schema_version '{schema_version}', "
                    f"expected '{self.BUY_LOT_SCHEMA_VERSION}'."
                ),
            )
        city_product_buy_lot = payload.get("city_product_buy_lot")
        if not isinstance(city_product_buy_lot, dict):
            raise ResonancePcTradePlannerError(
                code="buy_lot_invalid",
                message="buy_lot payload must include object field city_product_buy_lot.",
            )
        normalized: Dict[str, Dict[str, int]] = {}
        for city_id, products in city_product_buy_lot.items():
            if not isinstance(products, dict):
                continue
            ckey = str(city_id).strip()
            if not ckey:
                continue
            row: Dict[str, int] = {}
            for product_id, lot in products.items():
                pkey = str(product_id).strip()
                if not pkey:
                    continue
                try:
                    value = int(lot)
                except (TypeError, ValueError) as exc:
                    raise ResonancePcTradePlannerError(
                        code="buy_lot_invalid",
                        message=f"buy_lot '{ckey}->{pkey}' must be integer.",
                    ) from exc
                if value < 0:
                    raise ResonancePcTradePlannerError(
                        code="buy_lot_invalid",
                        message=f"buy_lot '{ckey}->{pkey}' must be >= 0.",
                    )
                row[pkey] = value
            normalized[ckey] = row

        self._buy_lot_payload = {
            "schema_version": schema_version,
            "city_product_buy_lot": normalized,
        }
        return self._buy_lot_payload

    def _load_trade_rules_payload(self) -> Dict[str, Any]:
        if self._trade_rules_payload is not None:
            return self._trade_rules_payload
        if not self.trade_rules_file.is_file():
            raise ResonancePcTradePlannerError(
                code="trade_rules_missing",
                message=f"trade rules metadata file not found: {self.trade_rules_file}",
            )
        try:
            payload = json.loads(self.trade_rules_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules metadata is not valid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules payload must be an object.",
            )
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != self.TRADE_RULES_SCHEMA_VERSION:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message=(
                    f"Unsupported trade rules schema_version '{schema_version}', "
                    f"expected '{self.TRADE_RULES_SCHEMA_VERSION}'."
                ),
            )
        prestige_levels = payload.get("prestige_levels")
        if not isinstance(prestige_levels, dict) or set(prestige_levels) != {
            str(level) for level in range(1, 21)
        }:
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define every prestige level from 1 through 20.",
            )
        negotiation = payload.get("negotiation")
        if not isinstance(negotiation, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define negotiation rules.",
            )
        if str(negotiation.get("model") or "") != "binary_to_cap_expected_fatigue":
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must use the binary_to_cap_expected_fatigue model.",
            )
        max_adjustment_bps = negotiation.get("max_adjustment_bps")
        attempt_fatigue = negotiation.get("attempt_fatigue")
        if (
            isinstance(max_adjustment_bps, bool)
            or not isinstance(max_adjustment_bps, int)
            or max_adjustment_bps <= 0
            or max_adjustment_bps > 10_000
        ):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define max_adjustment_bps between 1 and 10000.",
            )
        if (
            isinstance(attempt_fatigue, bool)
            or not isinstance(attempt_fatigue, int)
            or attempt_fatigue <= 0
        ):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define a positive negotiation attempt fatigue cost.",
            )
        defaults = negotiation.get("defaults")
        if not isinstance(defaults, dict):
            raise ResonancePcTradePlannerError(
                code="trade_rules_invalid",
                message="trade rules must define negotiation defaults.",
            )
        for key in ("bargain_success_rates_bps", "raise_success_rates_bps"):
            values = defaults.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or value > 10_000
                    for value in values
                )
            ):
                raise ResonancePcTradePlannerError(
                    code="trade_rules_invalid",
                    message=f"trade rules default '{key}' must be a non-empty 0..10000 integer list.",
                )
        for key in ("bargain_step_bps", "raise_step_bps"):
            value = defaults.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > 2000
            ):
                raise ResonancePcTradePlannerError(
                    code="trade_rules_invalid",
                    message=f"trade rules default '{key}' must be between 1 and 2000.",
                )
        self._trade_rules_payload = payload
        return self._trade_rules_payload

    def _load_trade_constraints_payload(self) -> Dict[str, Any]:
        if self._trade_constraints_payload is not None:
            return self._trade_constraints_payload

        if self.trade_constraints_file.is_file():
            payload = json.loads(self.trade_constraints_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message="trade_constraints payload must be an object.",
                )
            schema_version = str(payload.get("schema_version") or "").strip()
            if schema_version != self.TRADE_CONSTRAINTS_SCHEMA_VERSION:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=(
                        f"Unsupported trade_constraints schema_version '{schema_version}', "
                        f"expected '{self.TRADE_CONSTRAINTS_SCHEMA_VERSION}'."
                    ),
                )
            raw_allowed_ids = payload.get("allowed_city_ids")
            raw_city_id_to_key = payload.get("city_id_to_key")
        else:
            raw_allowed_ids = list(self.DEFAULT_ALLOWED_CITY_IDS)
            raw_city_id_to_key = dict(self.DEFAULT_CITY_ID_TO_KEY)

        if not isinstance(raw_allowed_ids, list):
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints.allowed_city_ids must be a list.",
            )
        if not isinstance(raw_city_id_to_key, dict):
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints.city_id_to_key must be an object.",
            )

        allowed_city_ids: List[str] = []
        seen_ids: set[str] = set()
        for raw_city_id in raw_allowed_ids:
            city_id = str(raw_city_id).strip()
            if not city_id or city_id in seen_ids:
                continue
            if city_id not in raw_city_id_to_key:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"city_id_to_key is missing mapping for city_id '{city_id}'.",
                )
            seen_ids.add(city_id)
            allowed_city_ids.append(city_id)

        city_id_to_key: Dict[str, str] = {}
        key_to_city_id: Dict[str, str] = {}
        for city_id in allowed_city_ids:
            city_key = str(raw_city_id_to_key.get(city_id) or "").strip()
            if not city_key:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"city_id_to_key for city_id '{city_id}' must be non-empty string.",
                )
            if city_key in key_to_city_id and key_to_city_id[city_key] != city_id:
                raise ResonancePcTradePlannerError(
                    code="trade_constraints_invalid",
                    message=f"Duplicate city_key mapping in trade constraints: '{city_key}'.",
                )
            city_id_to_key[city_id] = city_key
            key_to_city_id[city_key] = city_id

        if len(allowed_city_ids) < 2:
            raise ResonancePcTradePlannerError(
                code="trade_constraints_invalid",
                message="trade_constraints must include at least two allowed_city_ids.",
            )

        self._trade_constraints_payload = {
            "schema_version": self.TRADE_CONSTRAINTS_SCHEMA_VERSION,
            "allowed_city_ids": allowed_city_ids,
            "allowed_city_keys": [city_id_to_key[city_id] for city_id in allowed_city_ids],
            "city_id_to_key": city_id_to_key,
            "key_to_city_id": key_to_city_id,
        }
        return self._trade_constraints_payload

    def _beam_search(
        self,
        *,
        snapshot: Dict[str, Any],
        fatigue_costs: Dict[str, Dict[str, int]],
        allowed_city_ids: List[str],
        allowed_products: Dict[str, set[str]],
        capacity: int,
        horizon: int,
        max_books_cap: int,
        initial_state: _SearchState,
    ) -> Optional[_SearchState]:
        frontier: List[_SearchState] = [copy.deepcopy(initial_state)]
        visited: List[_SearchState] = [copy.deepcopy(initial_state)]

        for depth in range(horizon):
            candidates: List[_SearchState] = []
            remaining_hops_after_step = horizon - depth - 1
            for state in frontier:
                if state.remaining_fatigue <= 0:
                    continue
                next_states = self._expand_state(
                    state=state,
                    snapshot=snapshot,
                    fatigue_costs=fatigue_costs,
                    allowed_city_ids=allowed_city_ids,
                    allowed_products=allowed_products,
                    capacity=capacity,
                    remaining_hops_after_step=remaining_hops_after_step,
                    max_books_cap=max_books_cap,
                )
                candidates.extend(next_states)

            if not candidates:
                break
            deduped = self._dedupe_states(candidates)
            deduped.sort(key=self._state_rank_key, reverse=True)
            frontier = deduped[: self.beam_width]
            visited.extend(copy.deepcopy(frontier))

        feasible = [state for state in visited if state.cum_fatigue > 0]
        if not feasible:
            return None
        feasible.sort(key=self._state_rank_key, reverse=True)
        return feasible[0]

    def _expand_state(
        self,
        *,
        state: _SearchState,
        snapshot: Dict[str, Any],
        fatigue_costs: Dict[str, Dict[str, int]],
        allowed_city_ids: List[str],
        allowed_products: Dict[str, set[str]],
        capacity: int,
        remaining_hops_after_step: int,
        max_books_cap: int,
    ) -> List[_SearchState]:
        max_books_here = min(state.remaining_books, max_books_cap - state.total_books_used)
        if max_books_here < 0:
            return []
        expanded: List[_SearchState] = []
        row = fatigue_costs.get(state.city_id) or {}
        for next_city in allowed_city_ids:
            if next_city == state.city_id:
                continue
            fatigue_cost = int(row.get(next_city, 0))
            if fatigue_cost <= 0 or fatigue_cost > state.remaining_fatigue:
                continue
            for books_used in range(max_books_here + 1):
                outcomes = self._simulate_transition(
                    state=state,
                    next_city=next_city,
                    fatigue_cost=fatigue_cost,
                    books_used=books_used,
                    snapshot=snapshot,
                    allowed_products=allowed_products,
                    capacity=capacity,
                    remaining_hops_after_step=remaining_hops_after_step,
                )
                expanded.extend(outcomes)
        return expanded

    def _simulate_transition(
        self,
        *,
        state: _SearchState,
        next_city: str,
        fatigue_cost: int,
        books_used: int,
        snapshot: Dict[str, Any],
        allowed_products: Dict[str, set[str]],
        capacity: int,
        remaining_hops_after_step: int,
    ) -> List[_SearchState]:
        products = snapshot.get("products") or {}
        buy_lot = self._load_buy_lot_payload()["city_product_buy_lot"]
        holdings_before = self._clone_holdings(state.holdings)
        holdings_for_buy = self._clone_holdings(holdings_before)

        buys, holdings_after_buy = self._buy_greedy(
            current_city=state.city_id,
            next_city=next_city,
            products=products,
            allowed_products=allowed_products.get(state.city_id, set()),
            buy_lot=buy_lot.get(state.city_id, {}),
            holdings=holdings_for_buy,
            capacity=capacity,
            books_used=books_used,
        )

        sell_choices = self._enumerate_sell_hold_choices(
            holdings=holdings_after_buy,
            next_city=next_city,
            products=products,
            remaining_hops_after_step=remaining_hops_after_step,
        )

        outcomes: List[_SearchState] = []
        for choice in sell_choices:
            holdings_after = self._clone_holdings(holdings_after_buy)
            sells: List[Dict[str, Any]] = []
            holds: List[Dict[str, Any]] = []
            realized_profit = 0.0
            for product_id in sorted(list(holdings_after.keys()), key=self._sort_key):
                bucket = holdings_after.get(product_id) or {}
                qty = int(bucket.get("qty") or 0)
                if qty <= 0:
                    holdings_after.pop(product_id, None)
                    continue
                sell_price = self._sell_price(products, product_id, next_city)
                if sell_price is None:
                    holds.append({"product_id": product_id, "qty": qty, "reason": "no_sell_quote"})
                    continue

                sell_now = bool(choice.get(product_id, False))
                unit_buy = self._safe_ratio(float(bucket.get("total_cost") or 0.0), qty)
                if sell_now:
                    revenue = float(sell_price) * qty
                    cost = float(bucket.get("total_cost") or 0.0)
                    profit = revenue - cost
                    realized_profit += profit
                    sells.append(
                        {
                            "product_id": product_id,
                            "qty": qty,
                            "sell_price": float(sell_price),
                            "avg_buy_price": unit_buy,
                            "profit": profit,
                            "action": "sell_all",
                        }
                    )
                    holdings_after.pop(product_id, None)
                else:
                    holds.append(
                        {
                            "product_id": product_id,
                            "qty": qty,
                            "action": "hold",
                            "sell_price": float(sell_price),
                            "avg_buy_price": unit_buy,
                        }
                    )

            next_state = _SearchState(
                city_id=next_city,
                remaining_fatigue=state.remaining_fatigue - fatigue_cost,
                remaining_books=state.remaining_books - books_used,
                holdings=holdings_after,
                cum_profit=state.cum_profit + realized_profit,
                cum_fatigue=state.cum_fatigue + fatigue_cost,
                total_books_used=state.total_books_used + books_used,
                trace=copy.deepcopy(state.trace),
            )
            next_state.trace.append(
                {
                    "from_city_id": state.city_id,
                    "to_city_id": next_city,
                    "fatigue_cost": fatigue_cost,
                    "books_used": books_used,
                    "buy_multiplier": books_used + 1,
                    "buys": buys,
                    "sells": sells,
                    "holds": holds,
                    "profit_delta": realized_profit,
                    "cum_profit": next_state.cum_profit,
                    "cum_fatigue": next_state.cum_fatigue,
                    "cum_profit_per_fatigue": self._safe_ratio(next_state.cum_profit, next_state.cum_fatigue),
                    "state_after": {
                        "city_id": next_state.city_id,
                        "remaining_fatigue": next_state.remaining_fatigue,
                        "remaining_books": next_state.remaining_books,
                        "holdings": self._holdings_to_output(next_state.holdings),
                    },
                }
            )
            outcomes.append(next_state)
        return outcomes

    def _buy_greedy(
        self,
        *,
        current_city: str,
        next_city: str,
        products: Dict[str, Any],
        allowed_products: set[str],
        buy_lot: Dict[str, Any],
        holdings: Dict[str, Dict[str, float]],
        capacity: int,
        books_used: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, float]]]:
        current_load = self._holding_qty(holdings)
        free_capacity = max(capacity - current_load, 0)
        if free_capacity <= 0:
            return [], holdings

        multiplier = books_used + 1
        candidates: List[Tuple[float, str, float, int, Optional[float]]] = []
        for product_id in allowed_products:
            product_data = products.get(product_id) or {}
            buy_price = self._buy_price(products, product_id, current_city)
            if buy_price is None:
                continue
            try:
                lot = int((buy_lot or {}).get(product_id, 0))
            except (TypeError, ValueError):
                lot = 0
            if lot <= 0:
                continue
            qty = lot * multiplier
            if qty <= 0:
                continue
            best_future_sell = self._max_sell_price(product_id)
            if best_future_sell is None:
                continue
            expected_unit_profit = best_future_sell - float(buy_price)
            if expected_unit_profit <= 0:
                continue
            immediate_sell = self._sell_price(products, product_id, next_city)
            candidates.append((expected_unit_profit, product_id, float(buy_price), int(qty), immediate_sell))

        candidates.sort(key=lambda row: (row[0], row[4] or -1.0), reverse=True)
        buys: List[Dict[str, Any]] = []
        for expected_unit_profit, product_id, buy_price, qty, immediate_sell in candidates:
            if free_capacity <= 0:
                break
            take_qty = min(free_capacity, qty)
            if take_qty <= 0:
                continue
            bucket = holdings.setdefault(product_id, {"qty": 0.0, "total_cost": 0.0})
            bucket["qty"] = float(bucket.get("qty", 0.0)) + float(take_qty)
            bucket["total_cost"] = float(bucket.get("total_cost", 0.0)) + float(take_qty) * float(buy_price)
            free_capacity -= int(take_qty)
            buys.append(
                {
                    "product_id": product_id,
                    "qty": int(take_qty),
                    "buy_price": float(buy_price),
                    "expected_unit_profit": float(expected_unit_profit),
                    "immediate_next_sell_price": None if immediate_sell is None else float(immediate_sell),
                }
            )
        return buys, holdings

    def _enumerate_sell_hold_choices(
        self,
        *,
        holdings: Dict[str, Dict[str, float]],
        next_city: str,
        products: Dict[str, Any],
        remaining_hops_after_step: int,
    ) -> List[Dict[str, bool]]:
        if not holdings:
            return [{}]

        forced_sell: Dict[str, bool] = {}
        forced_hold: Dict[str, bool] = {}
        ambiguous: List[Tuple[str, float]] = []

        for product_id in sorted(holdings.keys(), key=self._sort_key):
            bucket = holdings.get(product_id) or {}
            qty = int(bucket.get("qty") or 0)
            if qty <= 0:
                continue
            sell_price = self._sell_price(products, product_id, next_city)
            if sell_price is None:
                forced_hold[product_id] = False
                continue

            total_cost = float(bucket.get("total_cost") or 0.0)
            sell_now_profit = float(sell_price) * qty - total_cost
            if remaining_hops_after_step <= 0:
                if sell_now_profit >= 0:
                    forced_sell[product_id] = True
                else:
                    forced_hold[product_id] = False
                continue

            best_future_sell = self._max_sell_price(product_id)
            if best_future_sell is None:
                if sell_now_profit >= 0:
                    forced_sell[product_id] = True
                else:
                    forced_hold[product_id] = False
                continue

            future_best_profit = best_future_sell * qty - total_cost
            if sell_now_profit < 0 and future_best_profit <= sell_now_profit:
                forced_hold[product_id] = False
                continue
            if future_best_profit <= sell_now_profit:
                forced_sell[product_id] = True
                continue
            uplift = future_best_profit - sell_now_profit
            ambiguous.append((product_id, uplift))

        ambiguous.sort(key=lambda row: row[1], reverse=True)
        branch_products = [pid for pid, _ in ambiguous[: self.MAX_SELL_BRANCH_PRODUCTS]]
        choices: List[Dict[str, bool]] = []
        branch_count = len(branch_products)
        if branch_count == 0:
            decision: Dict[str, bool] = {}
            decision.update(forced_sell)
            decision.update(forced_hold)
            return [decision]

        for mask in range(1 << branch_count):
            decision = {}
            decision.update(forced_sell)
            decision.update(forced_hold)
            for index, product_id in enumerate(branch_products):
                decision[product_id] = bool((mask >> index) & 1)
            choices.append(decision)
        return choices

    def _select_book_cap(self, *, profits: List[float], threshold: float) -> Tuple[int, List[Dict[str, Any]]]:
        if not profits:
            return 0, []
        selected_cap = 0
        marginals: List[Dict[str, Any]] = []
        previous = profits[0]
        for idx in range(1, len(profits)):
            current = profits[idx]
            if previous == float("-inf") or current == float("-inf"):
                marginal = float("-inf")
            else:
                marginal = current - previous
            allowed = marginal >= threshold
            marginals.append(
                {
                    "book_index": idx,
                    "profit_with_books": current,
                    "profit_with_prev_books": previous,
                    "marginal_profit": marginal,
                    "threshold": threshold,
                    "allowed": allowed,
                }
            )
            if allowed:
                selected_cap = idx
                previous = current
            else:
                break
        return selected_cap, marginals

    def _dedupe_states(self, states: List[_SearchState]) -> List[_SearchState]:
        best_by_key: Dict[Tuple[Any, ...], _SearchState] = {}
        for state in states:
            key = self._state_signature(state)
            existing = best_by_key.get(key)
            if existing is None or self._state_rank_key(state) > self._state_rank_key(existing):
                best_by_key[key] = state
        return list(best_by_key.values())

    def _state_signature(self, state: _SearchState) -> Tuple[Any, ...]:
        holdings_key = tuple(
            (
                product_id,
                int(bucket.get("qty") or 0),
                round(float(bucket.get("total_cost") or 0.0), 4),
            )
            for product_id, bucket in sorted(state.holdings.items(), key=lambda row: self._sort_key(row[0]))
            if int((bucket or {}).get("qty") or 0) > 0
        )
        return (
            state.city_id,
            int(state.remaining_fatigue),
            int(state.remaining_books),
            holdings_key,
        )

    def _state_rank_key(self, state: _SearchState) -> Tuple[float, float, int]:
        return (
            self._safe_ratio(state.cum_profit, state.cum_fatigue),
            float(state.cum_profit),
            int(state.remaining_fatigue),
        )

    def _build_max_sell_price_cache(self, snapshot: Dict[str, Any]) -> None:
        products = snapshot.get("products") or {}
        cache: Dict[str, float] = {}
        if isinstance(products, dict):
            for product_id, product_data in products.items():
                sell_map = (((product_data or {}).get("market") or {}).get("sell") or {})
                if not isinstance(sell_map, dict):
                    continue
                best = None
                for quote in sell_map.values():
                    if not isinstance(quote, dict):
                        continue
                    price = quote.get("price")
                    if price is None:
                        continue
                    try:
                        number = float(price)
                    except (TypeError, ValueError):
                        continue
                    if best is None or number > best:
                        best = number
                if best is not None:
                    cache[str(product_id)] = float(best)
        self._max_sell_price_cache = cache

    def _max_sell_price(self, product_id: str) -> Optional[float]:
        return self._max_sell_price_cache.get(str(product_id))

    @staticmethod
    def _buy_price(products: Dict[str, Any], product_id: str, city_id: str) -> Optional[float]:
        buy_map = (((products.get(product_id) or {}).get("market") or {}).get("buy") or {})
        quote = buy_map.get(city_id) if isinstance(buy_map, dict) else None
        if not isinstance(quote, dict):
            return None
        price = quote.get("price")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _sell_price(products: Dict[str, Any], product_id: str, city_id: str) -> Optional[float]:
        sell_map = (((products.get(product_id) or {}).get("market") or {}).get("sell") or {})
        quote = sell_map.get(city_id) if isinstance(sell_map, dict) else None
        if not isinstance(quote, dict):
            return None
        price = quote.get("price")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    def _validate_fatigue_payload(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must be an object.",
            )
        costs = payload.get("costs")
        if not isinstance(costs, dict):
            raise ResonancePcTradePlannerError(
                code="invalid_fatigue_payload",
                message="Travel fatigue payload must include costs object.",
            )

    @staticmethod
    def _normalize_city_list(values: Iterable[str], costs: Dict[str, Any]) -> List[str]:
        result: List[str] = []
        seen: set[str] = set()
        for raw in values:
            token = str(raw).strip()
            if not token or token in seen:
                continue
            if token not in costs:
                continue
            seen.add(token)
            result.append(token)
        return result

    @staticmethod
    def _clone_holdings(holdings: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
        return {
            str(pid): {"qty": float(bucket.get("qty") or 0.0), "total_cost": float(bucket.get("total_cost") or 0.0)}
            for pid, bucket in (holdings or {}).items()
            if float((bucket or {}).get("qty") or 0.0) > 0
        }

    def _normalize_holdings(self, holdings: Any) -> Dict[str, Dict[str, float]]:
        if holdings is None:
            return {}
        normalized: Dict[str, Dict[str, float]] = {}
        if isinstance(holdings, list):
            for row in holdings:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("product_id") or "").strip()
                if not pid:
                    continue
                qty = int(row.get("qty") or 0)
                total_cost = float(row.get("total_cost") or (float(row.get("avg_buy_price") or 0.0) * qty))
                if qty <= 0:
                    continue
                normalized[pid] = {"qty": float(qty), "total_cost": float(total_cost)}
            return normalized
        if not isinstance(holdings, dict):
            return {}
        for raw_pid, raw_bucket in holdings.items():
            pid = str(raw_pid).strip()
            if not pid or not isinstance(raw_bucket, dict):
                continue
            try:
                qty = float(raw_bucket.get("qty") or 0.0)
                total_cost = float(raw_bucket.get("total_cost") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            normalized[pid] = {"qty": qty, "total_cost": total_cost}
        return normalized

    def _holdings_to_output(self, holdings: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for product_id, bucket in sorted(holdings.items(), key=lambda row: self._sort_key(row[0])):
            qty = int(bucket.get("qty") or 0)
            if qty <= 0:
                continue
            total_cost = float(bucket.get("total_cost") or 0.0)
            rows.append(
                {
                    "product_id": product_id,
                    "qty": qty,
                    "total_cost": total_cost,
                    "avg_buy_price": self._safe_ratio(total_cost, qty),
                }
            )
        return rows

    @staticmethod
    def _holding_qty(holdings: Dict[str, Dict[str, float]]) -> int:
        total = 0
        for bucket in holdings.values():
            total += int(bucket.get("qty") or 0)
        return total

    @staticmethod
    def _coerce_non_negative_int(field: str, value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ResonancePcTradePlannerError(
                code=f"invalid_{field}",
                message=f"{field} must be an integer.",
            ) from exc
        if number < 0:
            raise ResonancePcTradePlannerError(
                code=f"invalid_{field}",
                message=f"{field} must be >= 0.",
            )
        return number

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        if denominator <= 0:
            return 0.0
        return float(numerator) / float(denominator)

    @staticmethod
    def _sort_key(raw: Any) -> Tuple[int, Any]:
        token = str(raw)
        if token.isdigit():
            return (0, int(token))
        return (1, token)

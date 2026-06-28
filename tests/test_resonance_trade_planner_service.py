from __future__ import annotations

import copy
import json
from pathlib import Path

from plans.resonance.src.services.resonance_market_data_service import ResonanceMarketDataService
from plans.resonance.src.services.resonance_trade_planner_service import (
    ResonanceTradePlannerError,
    ResonanceTradePlannerService,
)


class _FakeMarketData:
    def __init__(self, snapshot: dict, fatigue_payload: dict):
        self._snapshot = copy.deepcopy(snapshot)
        self._fatigue = copy.deepcopy(fatigue_payload)

    def get_latest(self):
        return copy.deepcopy(self._snapshot)

    def get_snapshot(self, snapshot_id: str):
        payload = copy.deepcopy(self._snapshot)
        payload["snapshot_id"] = snapshot_id
        return payload

    def refresh(self, force: bool = False):
        del force
        return copy.deepcopy(self._snapshot)

    def get_all_travel_fatigue(self):
        return copy.deepcopy(self._fatigue)


def _write_buy_lot(path: Path, city_product_buy_lot: dict) -> None:
    payload = {
        "schema_version": "1.0.0",
        "city_product_buy_lot": city_product_buy_lot,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_trade_constraints(path: Path, allowed_city_ids: list[str], city_id_to_key: dict[str, str]) -> None:
    payload = {
        "schema_version": "1.0.0",
        "allowed_city_ids": allowed_city_ids,
        "city_id_to_key": city_id_to_key,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_service(
    tmp_path: Path,
    snapshot: dict,
    fatigue_payload: dict,
    buy_lot: dict,
    *,
    trade_constraints: dict | None = None,
) -> ResonanceTradePlannerService:
    plan_root = tmp_path / "resonance"
    _write_buy_lot(plan_root / "data" / "meta" / "buy_lot.json", buy_lot)
    if trade_constraints is not None:
        _write_trade_constraints(
            plan_root / "data" / "meta" / "trade_constraints.json",
            allowed_city_ids=list(trade_constraints["allowed_city_ids"]),
            city_id_to_key=dict(trade_constraints["city_id_to_key"]),
        )
    market = _FakeMarketData(snapshot=snapshot, fatigue_payload=fatigue_payload)
    return ResonanceTradePlannerService(resonance_market_data=market, plan_root=plan_root, beam_width=24)


def _fatigue_payload(cities: dict[str, str], costs: dict[str, dict[str, int]]) -> dict:
    return {"schema_version": "1.0.0", "cities": cities, "costs": costs}


def test_global_search_can_choose_hold_for_better_future(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-global",
        "products": {
            "p1": {
                "market": {
                    "buy": {"1": {"price": 10}},
                    "sell": {"2": {"price": 16}, "3": {"price": 40}},
                }
            }
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C"},
        {
            "1": {"1": 0, "2": 5, "3": 20},
            "2": {"1": 5, "2": 0, "3": 5},
            "3": {"1": 20, "2": 5, "3": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 1}, "2": {}, "3": {}})

    result = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=10,
        book_budget=0,
        cargo_capacity=1,
        book_profit_threshold=0,
        available_city_ids=["1", "2", "3"],
    )

    assert result["status"] == "ok"
    assert result["selected_plan"]["station_sequence"][:3] == ["1", "2", "3"]
    first_step = result["next_step"]
    assert first_step["to_city_id"] == "2"
    assert first_step["sells"] == []
    assert any(row["product_id"] == "p1" and row["action"] == "hold" for row in first_step["holds"])


def test_planning_window_shrinks_with_budget(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-window",
        "products": {
            "p1": {
                "market": {
                    "buy": {"1": {"price": 10}},
                    "sell": {"2": {"price": 20}},
                }
            }
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 1}, "2": {}})

    blocked = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=4,
        book_budget=0,
        cargo_capacity=1,
        book_profit_threshold=0,
        available_city_ids=["1", "2"],
    )
    assert blocked["status"] == "no_feasible_move"
    assert blocked["planning_window"] == 0

    one_hop = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=7,
        book_budget=0,
        cargo_capacity=1,
        book_profit_threshold=0,
        available_city_ids=["1", "2"],
    )
    assert one_hop["planning_window"] == 1


def test_book_threshold_allows_partial_or_zero_book_usage(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-book",
        "products": {
            "p1": {
                "market": {
                    "buy": {"1": {"price": 10}},
                    "sell": {"2": {"price": 20}},
                }
            }
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 2}, "2": {}})

    no_book = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=5,
        book_budget=2,
        cargo_capacity=10,
        book_profit_threshold=25,
        available_city_ids=["1", "2"],
    )
    assert no_book["selected_book_cap"] == 0
    assert no_book["next_step"]["books_used"] == 0

    use_books = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=5,
        book_budget=2,
        cargo_capacity=10,
        book_profit_threshold=20,
        available_city_ids=["1", "2"],
    )
    assert use_books["selected_book_cap"] == 2
    assert use_books["next_step"]["books_used"] == 2


def test_sell_actions_are_binary_without_partial_sell(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-binary",
        "products": {
            "p1": {
                "market": {
                    "buy": {"1": {"price": 10}},
                    "sell": {"2": {"price": 18}, "3": {"price": 30}},
                }
            }
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C"},
        {
            "1": {"1": 0, "2": 5, "3": 10},
            "2": {"1": 5, "2": 0, "3": 5},
            "3": {"1": 10, "2": 5, "3": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 3}, "2": {}, "3": {}})

    result = service.simulate_until_stop(
        start_city_id="1",
        fatigue_budget=10,
        book_budget=1,
        cargo_capacity=6,
        book_profit_threshold=0,
        available_city_ids=["1", "2", "3"],
    )
    for step in result["steps"]:
        for sell in step["sells"]:
            assert sell["action"] == "sell_all"
            assert int(sell["qty"]) >= 1
        for hold in step["holds"]:
            assert hold["action"] == "hold"


def test_whitelist_and_buy_lot_constraints(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-whitelist",
        "products": {
            "p1": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p2": {"market": {"buy": {"1": {"price": 5}}, "sell": {"2": {"price": 25}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 1, "p2": 0}, "2": {}})

    default_scope = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=5,
        book_budget=0,
        cargo_capacity=10,
        book_profit_threshold=0,
        available_city_ids=["1", "2"],
    )
    buy_ids = [row["product_id"] for row in default_scope["next_step"]["buys"]]
    assert "p1" in buy_ids
    assert "p2" not in buy_ids

    strict_whitelist = service.plan_next_step(
        start_city_id="1",
        fatigue_budget=5,
        book_budget=0,
        cargo_capacity=10,
        book_profit_threshold=0,
        available_city_ids=["1", "2"],
        station_product_whitelist={"1": ["p2"]},
    )
    assert strict_whitelist["next_step"]["buys"] == []


def test_fatigue_budget_hard_constraint(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-budget",
        "products": {
            "p1": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}, "3": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C"},
        {
            "1": {"1": 0, "2": 5, "3": 7},
            "2": {"1": 5, "2": 0, "3": 5},
            "3": {"1": 7, "2": 5, "3": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p1": 1}, "2": {}, "3": {}})

    result = service.simulate_until_stop(
        start_city_id="1",
        fatigue_budget=5,
        book_budget=0,
        cargo_capacity=5,
        book_profit_threshold=0,
        available_city_ids=["1", "2", "3"],
    )

    assert result["totals"]["fatigue"] <= 5
    for step in result["steps"]:
        assert step["fatigue_cost"] <= 5


def test_e2e_with_cached_snapshot_20260313():
    market_service = ResonanceMarketDataService()
    planner = ResonanceTradePlannerService(resonance_market_data=market_service)

    result = planner.simulate_until_stop(
        start_city_id="1",
        fatigue_budget=120,
        book_budget=2,
        cargo_capacity=120,
        book_profit_threshold=0,
        available_city_ids=[str(i) for i in range(1, 21)],
        snapshot_id="20260313T191517Z_6a617b35f2",
        max_iterations=24,
    )

    assert result["snapshot_id"] == "20260313T191517Z_6a617b35f2"
    assert result["totals"]["fatigue"] <= 120
    assert isinstance(result["steps"], list)
    assert len(result["station_sequence"]) >= 1


def test_plan_best_cycle_selects_highest_profit_cycle(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-cycle",
        "products": {
            "p12": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p23": {"market": {"buy": {"2": {"price": 10}}, "sell": {"3": {"price": 19}}}},
            "p31": {"market": {"buy": {"3": {"price": 10}}, "sell": {"1": {"price": 18}}}},
            "p13": {"market": {"buy": {"1": {"price": 10}}, "sell": {"3": {"price": 13}}}},
            "p32": {"market": {"buy": {"3": {"price": 10}}, "sell": {"2": {"price": 12}}}},
            "p21": {"market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 11}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C"},
        {
            "1": {"1": 0, "2": 5, "3": 5},
            "2": {"1": 5, "2": 0, "3": 5},
            "3": {"1": 5, "2": 5, "3": 0},
        },
    )
    buy_lot = {
        "1": {"p12": 1, "p13": 1},
        "2": {"p23": 1, "p21": 1},
        "3": {"p31": 1, "p32": 1},
    }
    service = _build_service(tmp_path, snapshot, fatigue, buy_lot)

    result = service.plan_best_cycle(
        start_city_id="1",
        available_city_ids=["1", "2", "3"],
        cargo_capacity=1,
        book_budget=0,
        book_profit_threshold=0,
        max_cycle_hops=4,
    )

    assert result["status"] == "ok"
    assert result["cycle"]["city_sequence"] == ["1", "2", "3", "1"]
    assert result["cycle"]["totals"]["profit"] == 27.0
    assert result["cycle"]["totals"]["books_used"] == 0


def test_plan_best_cycle_respects_book_threshold(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-cycle-threshold",
        "products": {
            "p12": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 12}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    buy_lot = {"1": {"p12": 1}, "2": {"p21": 1}}
    service = _build_service(tmp_path, snapshot, fatigue, buy_lot)

    strict = service.plan_best_cycle(
        start_city_id="1",
        available_city_ids=["1", "2"],
        cargo_capacity=5,
        book_budget=2,
        book_profit_threshold=11,
        max_cycle_hops=3,
    )
    assert strict["status"] == "ok"
    assert strict["cycle"]["totals"]["books_used"] == 0

    relaxed = service.plan_best_cycle(
        start_city_id="1",
        available_city_ids=["1", "2"],
        cargo_capacity=5,
        book_budget=2,
        book_profit_threshold=10,
        max_cycle_hops=3,
    )
    assert relaxed["status"] == "ok"
    assert relaxed["cycle"]["totals"]["books_used"] == 2
    first_step = relaxed["cycle"]["steps"][0]
    assert first_step["books_used"] == 2


def test_plan_cycle_execution_respects_trade_constraints_whitelist(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-constraints",
        "products": {
            "p12": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"market": {"buy": {"2": {"price": 8}}, "sell": {"1": {"price": 15}}}},
            "p13": {"market": {"buy": {"1": {"price": 10}}, "sell": {"3": {"price": 40}}}},
            "p31": {"market": {"buy": {"3": {"price": 10}}, "sell": {"1": {"price": 12}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C"},
        {
            "1": {"1": 0, "2": 5, "3": 5},
            "2": {"1": 5, "2": 0, "3": 5},
            "3": {"1": 5, "2": 5, "3": 0},
        },
    )
    buy_lot = {"1": {"p12": 2, "p13": 2}, "2": {"p21": 2}, "3": {"p31": 2}}
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        buy_lot,
        trade_constraints={
            "allowed_city_ids": ["1", "2"],
            "city_id_to_key": {"1": "city_a", "2": "city_b"},
        },
    )

    result = service.plan_cycle_execution(current_city_key="city_a", fatigue_budget=30, cargo_capacity=10)

    assert result["status"] == "ok"
    assert result["allowed_city_ids"] == ["1", "2"]
    for cycle in result["cycles"]:
        for leg in cycle["legs"]:
            assert leg["from_city_id"] in {"1", "2"}
            assert leg["to_city_id"] in {"1", "2"}


def test_plan_cycle_execution_fails_for_unsupported_start_city(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-unsupported",
        "products": {
            "p12": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"market": {"buy": {"2": {"price": 8}}, "sell": {"1": {"price": 15}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    buy_lot = {"1": {"p12": 2}, "2": {"p21": 2}}
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        buy_lot,
        trade_constraints={
            "allowed_city_ids": ["1", "2"],
            "city_id_to_key": {"1": "city_a", "2": "city_b"},
        },
    )

    try:
        service.plan_cycle_execution(current_city_key="city_x", fatigue_budget=30, cargo_capacity=10)
        assert False, "expected unsupported_start_city error"
    except Exception as exc:  # noqa: BLE001
        assert isinstance(exc, ResonanceTradePlannerError)
        assert exc.code == "unsupported_start_city"


def test_plan_cycle_execution_adds_one_way_entry_when_outside_cycle(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-one-way",
        "products": {
            "p12": {"market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p23": {"market": {"buy": {"2": {"price": 10}}, "sell": {"3": {"price": 25}}}},
            "p32": {"market": {"buy": {"3": {"price": 9}}, "sell": {"2": {"price": 20}}}},
            "p42": {"market": {"buy": {"4": {"price": 8}}, "sell": {"2": {"price": 18}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "3": "C", "4": "D"},
        {
            "1": {"1": 0, "2": 4, "3": 8, "4": 9},
            "2": {"1": 4, "2": 0, "3": 4, "4": 5},
            "3": {"1": 8, "2": 4, "3": 0, "4": 9},
            "4": {"1": 9, "2": 5, "3": 9, "4": 0},
        },
    )
    buy_lot = {"1": {"p12": 3}, "2": {"p23": 3}, "3": {"p32": 3}, "4": {"p42": 3}}
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        buy_lot,
        trade_constraints={
            "allowed_city_ids": ["1", "2", "3", "4"],
            "city_id_to_key": {"1": "city_a", "2": "city_b", "3": "city_c", "4": "city_d"},
        },
    )

    result = service.plan_cycle_execution(current_city_key="city_d", fatigue_budget=24, cargo_capacity=10)

    assert result["status"] == "ok"
    first_leg = result["cycles"][0]["legs"][0]
    assert first_leg["from_city_key"] == "city_d"
    assert first_leg["phase"].startswith("one_way")

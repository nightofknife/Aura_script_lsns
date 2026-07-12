from __future__ import annotations

import copy
import json
from pathlib import Path

from plans.resonance_pc.src.services.resonance_pc_market_data_service import ResonancePcMarketDataService
from plans.resonance_pc.src.services.resonance_pc_trade_planner_service import (
    ResonancePcTradePlannerError,
    ResonancePcTradePlannerService,
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
) -> ResonancePcTradePlannerService:
    plan_root = tmp_path / "resonance_pc"
    _write_buy_lot(plan_root / "data" / "meta" / "buy_lot.json", buy_lot)
    if trade_constraints is not None:
        _write_trade_constraints(
            plan_root / "data" / "meta" / "trade_constraints.json",
            allowed_city_ids=list(trade_constraints["allowed_city_ids"]),
            city_id_to_key=dict(trade_constraints["city_id_to_key"]),
        )
    market = _FakeMarketData(snapshot=snapshot, fatigue_payload=fatigue_payload)
    return ResonancePcTradePlannerService(resonance_pc_market_data=market, plan_root=plan_root, beam_width=24)


def _fatigue_payload(cities: dict[str, str], costs: dict[str, dict[str, int]]) -> dict:
    return {"schema_version": "1.0.0", "cities": cities, "costs": costs}


CYCLE_PLAN_KEYS = {
    "status",
    "reason",
    "snapshot_id",
    "expected_profit",
    "fatigue_used",
    "books_budget",
    "books_used",
    "entry_route_count",
    "city_cycle",
    "route",
}
NEXT_CYCLE_PLAN_KEYS = CYCLE_PLAN_KEYS | {"round_complete"}


def _assert_simplified_cycle_shape(result: dict) -> None:
    assert set(result.keys()) == CYCLE_PLAN_KEYS
    for leg in result["route"]:
        assert set(leg.keys()) == {"from_city", "to_city", "buy_products", "books_used"}


def _assert_next_cycle_shape(result: dict) -> None:
    assert set(result.keys()) == NEXT_CYCLE_PLAN_KEYS
    for leg in result["route"]:
        assert set(leg.keys()) == {"from_city", "to_city", "buy_products", "books_used"}


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
    market_service = ResonancePcMarketDataService()
    planner = ResonancePcTradePlannerService(resonance_pc_market_data=market_service)

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
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p23": {"name": "B-C", "market": {"buy": {"2": {"price": 10}}, "sell": {"3": {"price": 19}}}},
            "p31": {"name": "C-A", "market": {"buy": {"3": {"price": 10}}, "sell": {"1": {"price": 18}}}},
            "p13": {"name": "A-C", "market": {"buy": {"1": {"price": 10}}, "sell": {"3": {"price": 13}}}},
            "p32": {"name": "C-B", "market": {"buy": {"3": {"price": 10}}, "sell": {"2": {"price": 12}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 11}}}},
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
    _assert_simplified_cycle_shape(result)
    assert result["reason"] is None
    assert result["city_cycle"] == ["A", "B", "C", "A"]
    assert result["expected_profit"] == 27.0
    assert result["fatigue_used"] == 15
    assert result["books_budget"] == 0
    assert result["books_used"] == 0
    assert result["entry_route_count"] == 0
    assert result["route"] == [
        {"from_city": "A", "to_city": "B", "buy_products": ["A-B"], "books_used": 0},
        {"from_city": "B", "to_city": "C", "buy_products": ["B-C"], "books_used": 0},
        {"from_city": "C", "to_city": "A", "buy_products": ["C-A"], "books_used": 0},
    ]


def test_plan_best_cycle_book_budgets_0_4_8(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-cycle-books",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 15}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p12": 1}, "2": {"p21": 1}})

    for books_budget in [0, 4, 8]:
        result = service.plan_best_cycle(
            start_city_id="1",
            available_city_ids=["1", "2"],
            cargo_capacity=20,
            book_budget=books_budget,
            book_profit_threshold=0,
            max_cycle_hops=3,
        )

        _assert_simplified_cycle_shape(result)
        assert result["status"] == "ok"
        assert result["books_budget"] == books_budget
        assert result["books_used"] == books_budget
        assert result["entry_route_count"] == 0
        assert result["expected_profit"] == 15.0 + (10.0 * books_budget)
        assert result["route"][0]["books_used"] == books_budget
        assert result["route"][1]["books_used"] == 0


def test_plan_best_cycle_respects_book_threshold(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-cycle-threshold",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 12}}}},
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
    _assert_simplified_cycle_shape(strict)
    assert strict["books_used"] == 0
    assert strict["entry_route_count"] == 0

    relaxed = service.plan_best_cycle(
        start_city_id="1",
        available_city_ids=["1", "2"],
        cargo_capacity=5,
        book_budget=2,
        book_profit_threshold=10,
        max_cycle_hops=3,
    )
    assert relaxed["status"] == "ok"
    _assert_simplified_cycle_shape(relaxed)
    assert relaxed["books_used"] == 2
    assert relaxed["entry_route_count"] == 0
    assert relaxed["route"][0]["books_used"] == 2
    assert relaxed["route"][1]["books_used"] == 0


def test_plan_best_cycle_caps_goods_by_cargo_capacity_and_sorts_buy_products(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-cycle-cargo",
        "products": {
            "p_high": {"name": "High", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 60}}}},
            "p_mid": {"name": "Mid", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 40}}}},
            "p_low": {"name": "Low", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p_back": {"name": "Back", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 11}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    buy_lot = {"1": {"p_high": 1, "p_mid": 3, "p_low": 3}, "2": {"p_back": 1}}
    service = _build_service(tmp_path, snapshot, fatigue, buy_lot)

    result = service.plan_best_cycle(
        start_city_id="1",
        available_city_ids=["1", "2"],
        cargo_capacity=2,
        book_budget=0,
        book_profit_threshold=0,
        max_cycle_hops=3,
    )

    _assert_simplified_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["entry_route_count"] == 0
    assert result["expected_profit"] == 81.0
    assert result["route"][0]["buy_products"] == ["High", "Mid"]
    assert "Low" not in result["route"][0]["buy_products"]


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
    _assert_simplified_cycle_shape(result)
    assert result["entry_route_count"] == 0
    assert result["city_cycle"][0] == result["city_cycle"][-1] == "A"
    assert set(result["city_cycle"]) <= {"A", "B"}
    for index, leg in enumerate(result["route"]):
        assert leg["from_city"] == result["city_cycle"][index]
        assert leg["to_city"] == result["city_cycle"][index + 1]


def test_plan_best_cycle_adds_entry_leg_when_current_city_is_outside_cycle(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-entry",
        "products": {
            "p41": {"name": "X-A", "market": {"buy": {"4": {"price": 10}}, "sell": {"1": {"price": 13}}}},
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 15}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "4": "X"},
        {
            "1": {"1": 0, "2": 5, "4": 3},
            "2": {"1": 5, "2": 0, "4": 7},
            "4": {"1": 3, "2": 7, "4": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p12": 1}, "2": {"p21": 1}, "4": {"p41": 1}})

    result = service.plan_best_cycle(
        current_city_id="4",
        available_city_ids=["1", "2"],
        cargo_capacity=10,
        book_budget=0,
        book_profit_threshold=0,
        max_cycle_hops=3,
    )

    _assert_simplified_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["entry_route_count"] == 1
    assert result["city_cycle"] == ["A", "B", "A"]
    assert result["route"] == [
        {"from_city": "X", "to_city": "A", "buy_products": ["X-A"], "books_used": 0},
        {"from_city": "A", "to_city": "B", "buy_products": ["A-B"], "books_used": 0},
        {"from_city": "B", "to_city": "A", "buy_products": ["B-A"], "books_used": 0},
    ]
    assert result["expected_profit"] == 18.0
    assert result["fatigue_used"] == 13


def test_plan_best_cycle_can_skip_current_city_as_stable_cycle_member(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-entry-current-allowed",
        "products": {
            "p31": {"name": "C-A", "market": {"buy": {"3": {"price": 10}}, "sell": {"1": {"price": 11}}}},
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 110}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 110}}}},
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
    buy_lot = {"1": {"p12": 1}, "2": {"p21": 1}, "3": {"p31": 1}}
    service = _build_service(tmp_path, snapshot, fatigue, buy_lot)

    result = service.plan_best_cycle(
        current_city_id="3",
        available_city_ids=["1", "2", "3"],
        cargo_capacity=10,
        book_budget=0,
        book_profit_threshold=0,
        max_cycle_hops=3,
    )

    _assert_simplified_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["entry_route_count"] == 1
    assert result["city_cycle"] == ["A", "B", "A"]
    assert result["route"][0] == {"from_city": "C", "to_city": "A", "buy_products": ["C-A"], "books_used": 0}
    assert "C" not in result["city_cycle"]


def test_entry_leg_competes_for_books_and_threshold(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-entry-books",
        "products": {
            "p41": {"name": "X-A", "market": {"buy": {"4": {"price": 10}}, "sell": {"1": {"price": 110}}}},
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "4": "X"},
        {
            "1": {"1": 0, "2": 5, "4": 3},
            "2": {"1": 5, "2": 0, "4": 7},
            "4": {"1": 3, "2": 7, "4": 0},
        },
    )
    service = _build_service(tmp_path, snapshot, fatigue, {"1": {"p12": 1}, "2": {"p21": 1}, "4": {"p41": 1}})

    relaxed = service.plan_best_cycle(
        current_city_id="4",
        available_city_ids=["1", "2"],
        cargo_capacity=10,
        book_budget=1,
        book_profit_threshold=100,
        max_cycle_hops=3,
    )
    assert relaxed["status"] == "ok"
    assert relaxed["books_used"] == 1
    assert relaxed["route"][0]["books_used"] == 1
    assert relaxed["expected_profit"] == 220.0

    strict = service.plan_best_cycle(
        current_city_id="4",
        available_city_ids=["1", "2"],
        cargo_capacity=10,
        book_budget=1,
        book_profit_threshold=101,
        max_cycle_hops=3,
    )
    assert strict["status"] == "ok"
    assert strict["books_used"] == 0
    assert strict["route"][0]["books_used"] == 0
    assert strict["expected_profit"] == 120.0


def test_plan_cycle_execution_allows_non_whitelist_current_city_and_blocks_by_total_fatigue(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-execution-entry",
        "products": {
            "p41": {"name": "X-A", "market": {"buy": {"4": {"price": 10}}, "sell": {"1": {"price": 13}}}},
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 15}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "4": "X"},
        {
            "1": {"1": 0, "2": 5, "4": 3},
            "2": {"1": 5, "2": 0, "4": 7},
            "4": {"1": 3, "2": 7, "4": 0},
        },
    )
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        {"1": {"p12": 1}, "2": {"p21": 1}, "4": {"p41": 1}},
        trade_constraints={
            "allowed_city_ids": ["1", "2"],
            "city_id_to_key": {"1": "city_a", "2": "city_b"},
        },
    )

    ok = service.plan_cycle_execution(
        current_city_key=None,
        current_city_id="4",
        fatigue_budget=13,
        cargo_capacity=10,
    )
    assert ok["status"] == "ok"
    assert ok["entry_route_count"] == 1
    assert ok["route"][0]["from_city"] == "X"
    assert ok["city_cycle"] == ["A", "B", "A"]

    blocked = service.plan_cycle_execution(
        current_city_key=None,
        current_city_id="4",
        fatigue_budget=12,
        cargo_capacity=10,
    )
    _assert_simplified_cycle_shape(blocked)
    assert blocked["status"] == "no_plan"
    assert blocked["reason"] == "insufficient_fatigue_for_full_cycle"
    assert blocked["entry_route_count"] == 0


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
        assert False, "expected current_city_not_resolved error"
    except Exception as exc:  # noqa: BLE001
        assert isinstance(exc, ResonancePcTradePlannerError)
        assert exc.code == "current_city_not_resolved"


def test_plan_cycle_execution_returns_no_plan_when_full_cycle_exceeds_fatigue(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-fatigue-limit",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 15}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {
            "1": {"1": 0, "2": 5},
            "2": {"1": 5, "2": 0},
        },
    )
    buy_lot = {"1": {"p12": 3}, "2": {"p21": 3}}
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

    result = service.plan_cycle_execution(current_city_key="city_a", fatigue_budget=9, cargo_capacity=10)

    _assert_simplified_cycle_shape(result)
    assert result == {
        "status": "no_plan",
        "reason": "insufficient_fatigue_for_full_cycle",
        "snapshot_id": "s-fatigue-limit",
        "expected_profit": 0.0,
        "fatigue_used": 0,
        "books_budget": 0,
        "books_used": 0,
        "entry_route_count": 0,
        "city_cycle": [],
        "route": [],
    }


def test_plan_next_cycle_execution_returns_complete_round_with_per_round_books(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-next-full",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 20}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {"1": {"1": 0, "2": 5}, "2": {"1": 5, "2": 0}},
    )
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        {"1": {"p12": 1}, "2": {"p21": 1}},
        trade_constraints={"allowed_city_ids": ["1", "2"], "city_id_to_key": {"1": "city_a", "2": "city_b"}},
    )

    result = service.plan_next_cycle_execution(
        current_city_key="city_a",
        fatigue_budget=20,
        cargo_capacity=10,
        book_budget=3,
        book_profit_threshold=0,
        snapshot_id="s-next-full",
    )

    _assert_next_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["round_complete"] is True
    assert result["fatigue_used"] == 10
    assert result["books_budget"] == 3
    assert result["books_used"] == 3
    assert sum(int(leg["books_used"]) for leg in result["route"]) == 3
    assert [(leg["from_city"], leg["to_city"], leg["buy_products"]) for leg in result["route"]] == [
        ("A", "B", ["A-B"]),
        ("B", "A", ["B-A"]),
    ]


def test_plan_next_cycle_execution_returns_final_prefix_when_full_round_exceeds_budget(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-next-prefix",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {"1": {"1": 0, "2": 5}, "2": {"1": 5, "2": 0}},
    )
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        {"1": {"p12": 1}, "2": {"p21": 1}},
        trade_constraints={"allowed_city_ids": ["1", "2"], "city_id_to_key": {"1": "city_a", "2": "city_b"}},
    )

    result = service.plan_next_cycle_execution(
        current_city_key="city_a",
        fatigue_budget=5,
        cargo_capacity=10,
        book_budget=0,
        book_profit_threshold=0,
        snapshot_id="s-next-prefix",
    )

    _assert_next_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["round_complete"] is False
    assert result["fatigue_used"] == 5
    assert result["expected_profit"] == 20.0
    assert result["route"] == [
        {"from_city": "A", "to_city": "B", "buy_products": ["A-B"], "books_used": 0}
    ]


def test_plan_next_cycle_execution_stops_when_no_positive_prefix_fits(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-next-none",
        "products": {
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B"},
        {"1": {"1": 0, "2": 5}, "2": {"1": 5, "2": 0}},
    )
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        {"1": {"p12": 1}, "2": {"p21": 1}},
        trade_constraints={"allowed_city_ids": ["1", "2"], "city_id_to_key": {"1": "city_a", "2": "city_b"}},
    )

    result = service.plan_next_cycle_execution(
        current_city_key="city_a",
        fatigue_budget=4,
        cargo_capacity=10,
        book_budget=0,
        book_profit_threshold=0,
        snapshot_id="s-next-none",
    )

    _assert_next_cycle_shape(result)
    assert result["status"] == "no_plan"
    assert result["round_complete"] is False
    assert result["reason"] == "insufficient_fatigue_for_positive_prefix"
    assert result["route"] == []


def test_plan_next_cycle_execution_allows_recomputed_entry_leg(tmp_path: Path):
    snapshot = {
        "snapshot_id": "s-next-entry",
        "products": {
            "p41": {"name": "X-A", "market": {"buy": {"4": {"price": 10}}, "sell": {"1": {"price": 15}}}},
            "p12": {"name": "A-B", "market": {"buy": {"1": {"price": 10}}, "sell": {"2": {"price": 30}}}},
            "p21": {"name": "B-A", "market": {"buy": {"2": {"price": 10}}, "sell": {"1": {"price": 20}}}},
        },
    }
    fatigue = _fatigue_payload(
        {"1": "A", "2": "B", "4": "X"},
        {
            "1": {"1": 0, "2": 5, "4": 3},
            "2": {"1": 5, "2": 0, "4": 7},
            "4": {"1": 3, "2": 7, "4": 0},
        },
    )
    service = _build_service(
        tmp_path,
        snapshot,
        fatigue,
        {"1": {"p12": 1}, "2": {"p21": 1}, "4": {"p41": 1}},
        trade_constraints={"allowed_city_ids": ["1", "2"], "city_id_to_key": {"1": "city_a", "2": "city_b"}},
    )

    result = service.plan_next_cycle_execution(
        current_city_id="4",
        fatigue_budget=13,
        cargo_capacity=10,
        book_budget=0,
        book_profit_threshold=0,
        snapshot_id="s-next-entry",
    )

    _assert_next_cycle_shape(result)
    assert result["status"] == "ok"
    assert result["round_complete"] is True
    assert result["entry_route_count"] == 1
    assert result["route"][0] == {"from_city": "X", "to_city": "A", "buy_products": ["X-A"], "books_used": 0}

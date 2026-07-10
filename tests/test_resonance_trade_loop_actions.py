import asyncio

from plans.resonance.src.actions.trade_planner_actions import (
    resonance_trade_route_execution_init,
    resonance_trade_route_execution_summary,
    resonance_trade_route_execution_update,
    resonance_trade_loop_cleanup,
    resonance_trade_loop_init,
    resonance_trade_loop_summary,
    resonance_trade_loop_update,
)


class _MemoryStateStore:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def _round_plan(snapshot_id: str, from_city: str, to_city: str) -> dict:
    return {
        "status": "ok",
        "reason": None,
        "snapshot_id": snapshot_id,
        "expected_profit": 100.0,
        "fatigue_used": 10,
        "books_budget": 3,
        "books_used": 3,
        "entry_route_count": 0,
        "city_cycle": [from_city, to_city, from_city],
        "route": [
            {"from_city": from_city, "to_city": to_city, "buy_products": ["A-B"], "books_used": 3},
        ],
        "round_complete": True,
    }


def test_trade_loop_state_accumulates_rounds_and_total_books():
    async def run():
        store = _MemoryStateStore()
        init = await resonance_trade_loop_init(
            current_city="A",
            current_city_key="city_a",
            fatigue_budget=100,
            book_budget=3,
            state_store=store,
        )
        run_key = init["run_key"]

        assert init["current_city_key"] == "city_a"
        first = await resonance_trade_loop_update(run_key, _round_plan("s1", "A", "B"), state_store=store)
        assert store.data[run_key]["current_city_key"] == ""
        second = await resonance_trade_loop_update(run_key, _round_plan("s2", "B", "A"), state_store=store)
        summary = await resonance_trade_loop_summary(run_key, state_store=store)
        cleanup = await resonance_trade_loop_cleanup(run_key, state_store=store)

        assert first["books_used"] == 3
        assert first["current_city"] == "B"
        assert second["books_used"] == 6
        assert summary["books_budget"] == 3
        assert summary["books_used"] == 6
        assert summary["fatigue_used"] == 20
        assert summary["remaining_fatigue"] == 80
        assert summary["rounds_completed"] == 2
        assert summary["rounds"][1]["route_start_index"] == 1
        assert summary["rounds"][1]["route_count"] == 1
        assert [leg["from_city"] for leg in summary["route"]] == ["A", "B"]
        assert cleanup["success"] is True
        assert run_key not in store.data

    asyncio.run(run())


def test_trade_loop_state_stops_after_final_prefix():
    async def run():
        store = _MemoryStateStore()
        init = await resonance_trade_loop_init(
            current_city="A",
            fatigue_budget=10,
            book_budget=3,
            state_store=store,
        )
        plan = _round_plan("s-prefix", "A", "B")
        plan["round_complete"] = False
        summary = await resonance_trade_loop_update(init["run_key"], plan, state_store=store)

        assert summary["status"] == "ok"
        assert summary["rounds_completed"] == 0
        assert summary["should_continue"] is False
        assert summary["route"][0]["to_city"] == "B"

    asyncio.run(run())


def test_route_execution_stops_on_fatigue_block_and_records_medicine():
    async def run():
        store = _MemoryStateStore()
        route = [
            {"from_city": "A", "to_city": "B", "buy_products": ["A-B"], "books_used": 0},
            {"from_city": "B", "to_city": "C", "buy_products": ["B-C"], "books_used": 0},
        ]
        init = await resonance_trade_route_execution_init(route, state_store=store)
        run_key = init["run_key"]

        first = await resonance_trade_route_execution_update(
            run_key,
            leg=route[0],
            travel_status="ok",
            state_store=store,
        )
        blocked = await resonance_trade_route_execution_update(
            run_key,
            leg=route[1],
            travel_status="blocked",
            reason="fatigue_recovery_required",
            blocked_at="departure",
            fatigue_medicine_used=[{"name": "提神口香糖", "count": 1}],
            fatigue_medicine_use_count=1,
            state_store=store,
        )
        summary = await resonance_trade_route_execution_summary(run_key, state_store=store)

        assert first["should_continue"] is True
        assert blocked["status"] == "blocked"
        assert blocked["should_continue"] is False
        assert blocked["blocked_leg"]["to_city"] == "C"
        assert summary["completed_leg_count"] == 1
        assert summary["fatigue_medicine_used"] == [{"name": "提神口香糖", "count": 1}]

    asyncio.run(run())


def test_trade_loop_update_records_blocked_execution_and_stops():
    async def run():
        store = _MemoryStateStore()
        init = await resonance_trade_loop_init(
            current_city="A",
            fatigue_budget=100,
            book_budget=3,
            state_store=store,
        )
        execution = {
            "status": "blocked",
            "reason": "fatigue_recovery_required",
            "completed_route": [{"from_city": "A", "to_city": "B", "buy_products": [], "books_used": 0}],
            "blocked_at": "departure",
            "blocked_leg": {"from_city": "B", "to_city": "C", "buy_products": ["B-C"], "books_used": 0},
            "fatigue_medicine_used": [{"name": "提神口香糖", "count": 1}],
            "fatigue_medicine_use_count": 1,
        }
        summary = await resonance_trade_loop_update(
            init["run_key"],
            plan=_round_plan("s-block", "A", "B"),
            execution=execution,
            state_store=store,
        )

        assert summary["status"] == "blocked"
        assert summary["reason"] == "fatigue_recovery_required"
        assert summary["should_continue"] is False
        assert summary["blocked_leg"]["from_city"] == "B"
        assert summary["route"][0]["to_city"] == "B"
        assert summary["fatigue_medicine_used"] == [{"name": "提神口香糖", "count": 1}]
        assert summary["fatigue_medicine_use_count"] == 1

    asyncio.run(run())

"""Daily plans resolve without accessing the game or account state."""
from datetime import datetime, timezone

import pytest

from packages.resonance_shared.battle_schedule import battle_game_day, resolve_battle_plan


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 26, 4, 59), ("2026-10-25", "sun")),
    (datetime(2026, 10, 26, 5), ("2026-10-26", "mon")),
    (datetime(2026, 10, 25, 21, tzinfo=timezone.utc), ("2026-10-26", "mon")),
])
def test_server_day_changes_at_shanghai_five_am(now, expected):
    assert battle_game_day(now) == expected


def test_weekly_plan_selects_day_and_keeps_order_without_mutating_saved_settings():
    week = {"mon": {"jobs": [{"route_id": "route-b", "run_count": 3}, {"route_id": "route-a"}],
                    "structural_types": ["echo_buoy", "disordered_roots", "echo_buoy"]},
            "tue": {"jobs": [{"route_id": "route-c"}]}}
    plan = resolve_battle_plan(weekly_plan=week, now=datetime(2026, 10, 26, 6))
    assert plan["jobs"] == [{"route_id": "route-b", "run_count": 3},
                            {"route_id": "route-a", "run_count": 1}]
    assert plan["structural_types"] == ["echo_buoy", "disordered_roots"]
    plan["jobs"][0]["run_count"] = 99
    assert week["mon"]["jobs"][0]["run_count"] == 3
    assert resolve_battle_plan(weekly_plan=week, now=datetime(2026, 10, 28, 6))["jobs"] == []


def test_closed_weekday_is_rejected_before_runtime_and_open_weekday_is_accepted():
    catalog = {"routes": [{"route_id": "weekday-stage", "open_weekdays": [2, 7]}]}
    jobs = [{"route_id": "weekday-stage"}]
    with pytest.raises(ValueError, match="未开放"):
        resolve_battle_plan(jobs=jobs, catalog=catalog, now=datetime(2026, 10, 26, 6))
    assert resolve_battle_plan(jobs=jobs, catalog=catalog,
                               now=datetime(2026, 10, 27, 6))["jobs"][0]["run_count"] == 1


@pytest.mark.parametrize("count", [True, 0, 1.5, 10000])
def test_invalid_count_cannot_reach_game_execution(count):
    with pytest.raises(ValueError, match="执行次数"):
        resolve_battle_plan(jobs=[{"route_id": "stage", "run_count": count}])


def test_direct_and_weekly_inputs_cannot_silently_override_each_other():
    with pytest.raises(ValueError, match="不能"):
        resolve_battle_plan(jobs=[], weekly_plan={})


def test_legacy_structural_jobs_become_priority_without_battle_counts():
    plan = resolve_battle_plan(jobs=[
        {"route_id": "stage", "run_count": 2},
        {"route_id": "gp.structural_exploration.birch_buoy", "run_count": 99},
    ], structural_types=["echo_buoy"])
    assert plan["jobs"] == [{"route_id": "stage", "run_count": 2}]
    assert plan["structural_types"] == ["echo_buoy", "birch_buoy"]

"""Offline fatigue accounting: observations, confirmed travel, and invalidation."""
import pytest

from plans.resonance_pc.src.actions._freight_fatigue_ledger import FreightFatigueLedger


def normal(**changes):
    return {"success": True, "status": "ok", "arrival_status": "arrived", **changes}


def test_trade_observation_replaces_negotiation_and_previous_route_consumption():
    ledger = FreightFatigueLedger()
    assert ledger.set_observed(77, "trade_page", max_fatigue=872) == 77
    assert ledger.observe_travel(normal(), 100) == 177
    assert ledger.observe_travel(normal(), 20) == 197
    snapshot = ledger.snapshot()
    assert snapshot["source"] == "route_derived"
    assert snapshot["last_observed_fatigue"] == 77
    assert snapshot["last_observed_source"] == "trade_page"
    assert snapshot["added_travel_fatigue"] == 120
    # The post-negotiation reading includes that negotiation already.
    assert ledger.set_observed(213, "trade_page") == 213
    assert ledger.snapshot()["added_travel_fatigue"] == 0
    assert ledger.observe_travel(normal(), 30) == 243


@pytest.mark.parametrize("source", ["profile", "water", "bento"])
def test_recovery_and_fallback_observations_rebase(source):
    ledger = FreightFatigueLedger()
    ledger.set_observed(600, "trade_page")
    ledger.observe_travel(normal(), 50)
    assert ledger.set_observed(300, source) == 300
    assert ledger.snapshot()["source"] == source
    assert ledger.snapshot()["added_travel_fatigue"] == 0
    assert ledger.observe_travel(normal(), 20) == 320


@pytest.mark.parametrize("value", [None, True, False, -1, "77", 77.0])
def test_invalid_observations_forbid_reusing_old_current(value):
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "trade_page")
    assert ledger.set_observed(value, "trade_page") is None
    assert ledger.snapshot()["current_fatigue"] is None
    assert ledger.snapshot()["source"] is None
    assert ledger.snapshot()["last_observed_fatigue"] == 77


@pytest.mark.parametrize("limit", [True, False, 0, -1, "872", 872.0])
def test_invalid_capacity_rejected(limit):
    ledger = FreightFatigueLedger()
    assert ledger.set_observed(77, "trade_page", max_fatigue=limit) is None
    assert ledger.snapshot()["invalid_reason"] == "invalid_max_fatigue"


def test_over_capacity_is_valid_game_state_and_never_clamped():
    ledger = FreightFatigueLedger()
    assert ledger.set_observed(900, "trade_page", max_fatigue=872) == 900
    assert ledger.observe_travel(normal(), 10) == 910


@pytest.mark.parametrize("travel,cost,reason", [
    (None, 50, "travel_arrival_unconfirmed"),
    (normal(success=False), 50, "travel_arrival_unconfirmed"),
    (normal(success=1), 50, "travel_arrival_unconfirmed"),
    (normal(status="blocked"), 50, "travel_arrival_unconfirmed"),
    (normal(status="cancelled"), 50, "travel_arrival_unconfirmed"),
    (normal(arrival_status=None), 50, "travel_arrival_unconfirmed"),
    (normal(arrival_status="traveling"), 50, "travel_arrival_unconfirmed"),
    (normal(fatigue_medicine_use_count=1), 50, "travel_fatigue_medicine_used"),
    (normal(fatigue_medicine_use_count=True), 50, "travel_medicine_usage_unknown"),
    (normal(fatigue_medicine_use_count=-1), 50, "travel_medicine_usage_unknown"),
    (normal(fatigue_medicine_used=[{"used": 1}]), 50, "travel_fatigue_medicine_used"),
    (normal(encounter_actions=1), 50, "travel_encounter_actions"),
    (normal(encounter_actions="1"), 50, "travel_encounter_usage_unknown"),
    (normal(encounter_actions=None), 50, "travel_encounter_usage_unknown"),
    (normal(travel_method="trailer"), 50, "travel_trailer_used"),
    (normal(trailer_service={"departed": True}), 50, "travel_trailer_used"),
    (normal(trailer_service={"status": "departed"}), 50, "travel_trailer_used"),
    (normal(travel_method="unknown"), 50, "travel_method_unknown"),
    (normal(), None, "travel_fatigue_cost_unknown"),
    (normal(), -1, "travel_fatigue_cost_unknown"),
    (normal(), True, "travel_fatigue_cost_unknown"),
    (normal(), "50", "travel_fatigue_cost_unknown"),
    (normal(), 50.0, "travel_fatigue_cost_unknown"),
])
def test_unpredictable_travel_requires_fallback_without_faking_actual(travel, cost, reason):
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "trade_page")
    ledger.begin_travel()
    assert ledger.observe_travel(travel, cost) is None
    assert ledger.snapshot()["invalid_reason"] == reason
    assert ledger.snapshot()["current_fatigue"] is None
    assert ledger.snapshot()["last_observed_fatigue"] == 77
    assert ledger.observe_travel(normal(), 10) is None
    assert ledger.set_observed(120, "profile") == 120
    assert ledger.observe_travel(normal(), 10) == 130


def test_denied_trailer_attempt_does_not_invalidate_confirmed_normal_travel():
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "trade_page")
    assert ledger.observe_travel(normal(
        travel_method="normal", trailer_service={"departed": False, "status": "blocked"}), 5) == 82


def test_arrival_success_contract_allows_absent_optional_arrival_status():
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "profile")
    assert ledger.observe_travel({"success": True, "status": "ok"}, 0) == 77


def test_departure_hides_stale_fatigue_until_arrival_then_adds_cost_once():
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "trade_page")
    ledger.begin_travel()
    ledger.begin_travel()  # A duplicate start notification cannot discard the private baseline.
    assert ledger.snapshot()["current_fatigue"] is None
    assert ledger.snapshot()["source"] is None
    assert ledger.snapshot()["invalid_reason"] == "travel_in_progress"
    assert ledger.observe_travel(normal(), 25) == 102
    ledger.begin_travel()
    assert ledger.observe_travel(normal(), 20) == 122
    assert ledger.snapshot()["added_travel_fatigue"] == 45


def test_invalidating_pending_travel_cannot_resurrect_departure_value():
    ledger = FreightFatigueLedger()
    ledger.set_observed(0, "profile")
    ledger.begin_travel()
    ledger.invalidate("travel_failed")
    assert ledger.observe_travel(normal(), 10) is None
    assert ledger.snapshot()["invalid_reason"] == "travel_failed"
    assert ledger.snapshot()["last_observed_fatigue"] == 0


def test_observing_after_departure_replaces_pending_baseline():
    ledger = FreightFatigueLedger()
    ledger.set_observed(77, "trade_page")
    ledger.begin_travel()
    ledger.set_observed(50, "profile")
    assert ledger.observe_travel(normal(), 10) == 60


def test_unknown_baseline_cannot_be_built_from_route_cost_alone():
    ledger = FreightFatigueLedger()
    assert ledger.observe_travel(normal(), 50) is None
    assert ledger.snapshot()["invalid_reason"] == "no_observed_baseline"
    ledger.begin_travel()
    assert ledger.observe_travel(normal(), 50) is None
    assert ledger.snapshot()["current_fatigue"] is None


def test_derived_source_is_not_accepted_as_a_measurement():
    ledger = FreightFatigueLedger()
    assert ledger.set_observed(77, "route_derived") is None
    assert ledger.snapshot()["invalid_reason"] == "invalid_observation_source"

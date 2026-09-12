"""Snapshot validation remains separate from arrival-time recovery planning."""

from __future__ import annotations

from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions import _sparkling_water_policy as policy
from plans.resonance_pc.src.actions._sparkling_water_policy import validate_recovery_snapshot


def _snapshot(current=1, remaining=6, maximum=300, limit=6):
    return {
        "status": {"fatigue": {"current": current, "max": maximum}},
        "recovery": {"sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": limit}},
        "metadata": {"persisted": True},
    }


def test_obsolete_selector_is_removed_without_compatibility():
    assert not hasattr(policy, "select_sparkling_water_stop")
    assert policy.__all__ == ["validate_recovery_snapshot"]


@pytest.mark.parametrize("path,value", [
    (("status",), None), (("status", "fatigue"), []),
    (("status", "fatigue", "current"), -1), (("status", "fatigue", "current"), True),
    (("status", "fatigue", "current"), 1.0), (("status", "fatigue", "current"), "1"),
    (("status", "fatigue", "max"), 0), (("status", "fatigue", "max"), False),
    (("recovery",), []), (("recovery", "sparkling_water"), None),
    (("recovery", "sparkling_water", "remaining_free_uses"), -1),
    (("recovery", "sparkling_water", "remaining_free_uses"), 7),
    (("recovery", "sparkling_water", "remaining_free_uses"), True),
    (("recovery", "sparkling_water", "remaining_free_uses"), "6"),
    (("recovery", "sparkling_water", "daily_free_limit"), 5),
    (("recovery", "sparkling_water", "daily_free_limit"), 7),
    (("recovery", "sparkling_water", "daily_free_limit"), 6.0),
    (("metadata",), None), (("metadata", "persisted"), False),
    (("metadata", "persisted"), 1), (("metadata", "persisted"), "true"),
])
def test_malformed_snapshot_fails(path, value):
    snapshot = _snapshot()
    target = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_recovery_snapshot(snapshot)


@pytest.mark.parametrize("snapshot", [None, {}, [], "snapshot"])
def test_validator_rejects_missing_or_wrong_snapshot(snapshot):
    with pytest.raises(ValueError):
        validate_recovery_snapshot(snapshot)


def test_validator_returns_independent_copy_and_preserves_unknown_sections():
    snapshot = _snapshot(301, remaining=0, limit=6)
    snapshot["future"] = {"items": [1]}
    result = validate_recovery_snapshot(snapshot)
    assert result == snapshot
    result["future"]["items"].append(2)
    result["status"]["fatigue"]["current"] = 0
    assert snapshot["future"]["items"] == [1]
    assert snapshot["status"]["fatigue"]["current"] == 301


@pytest.mark.parametrize("remaining", [0, 6])
def test_refresh_required_snapshot_is_rejected(remaining):
    snapshot = _snapshot(remaining=remaining)
    snapshot["recovery"]["sparkling_water"]["requires_refresh"] = True
    before = deepcopy(snapshot)
    with pytest.raises(ValueError, match="requires_refresh"):
        validate_recovery_snapshot(snapshot)
    assert snapshot == before


def test_cleared_refresh_marker_allows_snapshot():
    snapshot = _snapshot()
    snapshot["recovery"]["sparkling_water"]["requires_refresh"] = False
    assert validate_recovery_snapshot(snapshot) == snapshot


@pytest.mark.parametrize("limit", [0, -1, False])
def test_zero_remaining_still_requires_positive_daily_limit(limit):
    with pytest.raises(ValueError, match="daily_free_limit"):
        validate_recovery_snapshot(_snapshot(remaining=0, limit=limit))


def test_zero_remaining_with_minimum_positive_limit_is_valid():
    snapshot = _snapshot(remaining=0, limit=1)
    assert validate_recovery_snapshot(snapshot) == snapshot

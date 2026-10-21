"""The new collection flow keeps physical identities and one-shot consumption."""
from copy import deepcopy

import pytest

from plans.resonance_pc.src.actions import product_collection_pc_actions as collect
from plans.resonance_pc.src.actions import _product_collection_ui as ui


def handoff():
    return dict(success=True, safe_to_continue=True, returned_to_overview=True,
                page_after=collect.OVERVIEW)


@pytest.mark.parametrize("number", range(1, 7))
def test_title_requires_own_number_score_and_margin(monkeypatch, number):
    hit = {"rect": [100, 100, 80, 30]}
    scores = {n: .1 for n in range(1, 7)}
    scores[number] = 1.0
    monkeypatch.setattr(ui, "_hit", lambda *args: hit)
    specs = ui.catalog()["templates"]
    identities = {id(specs[f"room_{n}_title"]): n for n in range(1, 7)}
    monkeypatch.setattr(ui, "_number_score", lambda frame, anchor, spec: scores[identities[id(spec)]])
    assert ui._title(object(), number) is hit
    other = number % 6 + 1
    scores[other] = 1.0
    assert ui._title(object(), number) is None
    scores[other], scores[number] = .1, -1.0
    assert ui._title(object(), number) is None


def test_capacity_cancel_returns_overview_and_stops_next_room(monkeypatch):
    clicks, controls, navigations = [], [], []
    monkeypatch.setattr(collect, "_cancelled", lambda: None)
    monkeypatch.setattr(collect, "_physical", lambda *args: True)
    monkeypatch.setattr(collect.clicks, "read_frame", lambda app: object())
    monkeypatch.setattr(ui, "collection_state", lambda *args: "collectable")
    monkeypatch.setattr(ui, "control", lambda *args: {"center": [200, 300]})
    monkeypatch.setattr(collect.clicks, "send_click", lambda app, point, record: clicks.append(point))
    outcomes = iter(["collectable", "capacity_blocked"])
    monkeypatch.setattr(collect.clicks, "wait_confirmed", lambda *args, **kwargs: next(outcomes))

    def navigate(**kwargs):
        navigations.append(kwargs["room_name"])
        return dict(success=True, room_entered=True)

    def return_overview(app, vision, room, result):
        result.update(returned_to_overview=True, page_after=collect.OVERVIEW)

    monkeypatch.setattr(collect, "resonance_pc_open_reforming_center_room", navigate)
    monkeypatch.setattr(collect, "_back_to_overview", return_overview)
    monkeypatch.setattr(collect, "_control", lambda app, name, *args: controls.append(name))
    unlocks = dict.fromkeys(collect.ROOMS, "open")
    first = collect.resonance_pc_product_collection_collect_room(
        collect.ROOMS[0], unlocks, handoff(), app=object())
    assert first["status"] == "blocked" and first["reason"] == "warehouse_capacity_blocked"
    assert first["collection_attempted"] and first["returned_to_overview"]
    assert first["success"] is first["safe_to_continue"] is False
    assert clicks == [[200, 300]] and controls == ["capacity_cancel"]
    second = collect.resonance_pc_product_collection_collect_room(
        collect.ROOMS[1], unlocks, first, app=object())
    assert second["reason"] == "collection_previous_room_incomplete"
    assert navigations == [collect.ROOMS[0]] and len(clicks) == 1


def test_confirmed_locked_rooms_skip_without_input_and_aggregate_six_identities():
    unlocks = dict.fromkeys(collect.ROOMS, "locked")
    previous, results = handoff(), []
    for room in collect.ROOMS:
        previous = collect.resonance_pc_product_collection_collect_room(room, unlocks, previous)
        results.append(previous)
    scan = handoff() | {"unlocks": unlocks}
    summary = collect.resonance_pc_product_collection_summarize({"success": True}, scan, results)
    assert summary["success"] and summary["skipped_count"] == 6
    assert summary["visited_count"] == summary["collected_count"] == 0
    wrong_identity = deepcopy(results)
    wrong_identity[1]["room_name"] = collect.ROOMS[0]
    stopped = collect.resonance_pc_product_collection_summarize({"success": True}, scan, wrong_identity)
    assert stopped["status"] == "failed" and stopped["stopped_room"] == collect.ROOMS[1]
    assert all(row["status"] == "not_processed" for row in stopped["results"][2:])

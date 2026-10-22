"""Focused checks for new measured-map math; no template regression reruns."""
from types import SimpleNamespace

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _reforming_center_map_navigation as mapping
from plans.resonance_pc.src.actions import reforming_center_navigation_pc_actions as nav


def model():
    return {"rooms": {
        "A": {"world_anchor": [0, 0], "component": 0, "neighbors": ["B"]},
        "B": {"world_anchor": [300, 0], "component": 0, "neighbors": ["A", "C"]},
        "C": {"world_anchor": [300, 300], "component": 0, "neighbors": ["B"]},
        "D": {"world_anchor": [0, 0], "component": 1, "neighbors": []},
    }}


def hit(name, point):
    return {"room_name": name, "center": point, "score": .99}


def test_consensus_discards_an_inconsistent_anchor_and_predicts_target():
    pose = mapping.locate_viewport([hit("A", [100, 200]), hit("B", [402, 201]),
                                   hit("C", [900, 900])], model())
    assert pose["offset"] == [101, 200.5]
    assert pose["anchors"] == ["A", "B"] and pose["rejected_anchors"] == ["C"]
    assert mapping.predict_screen("C", pose, model()) == [401, 500.5]


def test_equal_conflicting_offsets_are_not_averaged():
    with pytest.raises(mapping.RoomMapError, match="anchors_inconsistent"):
        mapping.locate_viewport([hit("A", [100, 100]), hit("B", [500, 100])], model())


def test_no_anchor_never_seeds_a_guessed_location():
    with pytest.raises(mapping.RoomMapError, match="room_anchor_not_found"):
        mapping.locate_viewport([], model())


def test_route_uses_measured_neighbors_and_refuses_disconnected_component():
    pose = mapping.locate_viewport([hit("A", [100, 100])], model())
    waypoint = mapping.choose_waypoint("C", pose, model())
    assert waypoint["route"] == ["A", "B", "C"]
    assert waypoint["room_name"] == "B" and waypoint["screen_point"] == [400, 100]
    with pytest.raises(mapping.RoomMapError, match="component_disconnected"):
        mapping.choose_waypoint("D", pose, model())


@pytest.mark.parametrize("point,axis,sign", [([1200, 300], 0, -1), ([520, 900], 1, -1),
                                           ([-400, 300], 0, 1), ([520, -700], 1, 1)])
def test_direction_and_roi_limits(point, axis, sign):
    drag = mapping.plan_drag(point)
    assert drag["axis"] == axis and drag["content_shift"]*sign > 0
    for endpoint in (drag["start"], drag["end"]):
        assert nav.layout._point_in_region(endpoint, nav._SAFE_MAP_REGION, margin=20)
    assert drag["start"][1-axis] == drag["end"][1-axis]


def test_single_anchor_shortens_drag_and_near_target_does_not_overshoot():
    assert abs(mapping.plan_drag([1500, 300], True)["content_shift"]) == 120
    assert mapping.plan_drag([535, 300])["content_shift"] == -15


def test_actual_progress_is_observed_not_equal_to_mouse_pixels():
    before = {"component": 0, "offset": [0, 0]}
    drag = mapping.plan_drag([1500, 300])
    assert mapping.drag_progress(before, {"component": 0, "offset": [-50, 0]}, drag, 1) == 0
    count = mapping.drag_progress(before, before, drag, 0)
    with pytest.raises(mapping.RoomMapError, match="room_drag_stalled"):
        mapping.drag_progress(before, before, drag, count)
    with pytest.raises(mapping.RoomMapError, match="wrong_direction"):
        mapping.drag_progress(before, {"component": 0, "offset": [50, 0]}, drag, 0)


def test_last_allowed_drag_is_followed_by_observation_and_safe_click(monkeypatch):
    captures, clicks, drags = [], [], []
    app = SimpleNamespace(
        capture=lambda **kwargs: (captures.append(kwargs) or
                                 SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), np.uint8))),
        move_to=lambda **kwargs: None,
        drag=lambda **kwargs: drags.append(kwargs),
        click=lambda **kwargs: (clicks.append(kwargs) or SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(nav, "_settle", lambda _: None)
    monkeypatch.setattr(nav.room_map, "load_map", model)
    rows = iter([[hit("A", [1050, 300])], [hit("A", [520, 300])]])
    matcher = nav.RoomTemplateMatcher(SimpleNamespace())
    monkeypatch.setattr(matcher, "room_hits", lambda *_: next(rows))
    result = nav._locate_and_click(app, "A", {"A": {"click_offset": [120, 55]}}, {}, 1, .05, matcher)
    assert len(captures) == 2 and len(drags) == 1
    assert clicks == [{"x": 640, "y": 355}] and result["drag_count"] == 1


def test_measured_map_covers_current_templates_and_all_rooms_have_a_route():
    measured = mapping.load_map()
    assert set(measured["rooms"]) == set(nav.RoomTemplateMatcher.load_catalog()["rooms"])
    components = {room["component"] for room in measured["rooms"].values()}
    assert len(components) == 1
    start = next(iter(measured["rooms"]))
    pose = mapping.locate_viewport([hit(start, [520, 300])], measured)
    for target in measured["rooms"]:
        assert mapping.choose_waypoint(target, pose, measured)["route"][-1] == target


def test_target_rejected_by_map_consensus_cannot_be_clicked(monkeypatch):
    clicks, drags = [], []
    app = SimpleNamespace(
        capture=lambda **_: SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), np.uint8)),
        move_to=lambda **_: None, drag=lambda **kwargs: drags.append(kwargs),
        click=lambda **kwargs: (clicks.append(kwargs) or SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(nav, "_settle", lambda _: None)
    monkeypatch.setattr(nav.room_map, "load_map", model)
    rows = iter([[hit("A", [520, 300]), hit("B", [800, 300]), hit("C", [800, 600])],
                 [hit("A", [500, 300])]])
    matcher = nav.RoomTemplateMatcher(SimpleNamespace())
    monkeypatch.setattr(matcher, "room_hits", lambda *_: next(rows))
    nav._locate_and_click(app, "A", {"A": {"click_offset": [120, 55]}}, {}, 1, .05, matcher)
    assert len(drags) == 1 and clicks == [{"x": 620, "y": 355}]

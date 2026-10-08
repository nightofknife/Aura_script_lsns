"""A scan shortcut must prove current pixels and otherwise keep full vision."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_scan_scene as scene


@pytest.fixture
def probes(monkeypatch):
    rgb = np.zeros((720, 1280, 3), np.uint8)
    rgb[::2, ::2] = (80, 30, 100)
    values = {}
    calls = []

    def legacy(gray, name):
        calls.append(("legacy", name))
        return {"score": values.get(("legacy", name), .99 if name == "player_turn" else .1),
                "point": [10, 20]}

    def planned(gray, name):
        calls.append(("planned", name))
        return values.get(("planned", name), .99 if name == "board_actions_heading" else .1), [30, 40]

    def other(kind):
        def match(gray, name, region):
            calls.append((kind, name))
            return values.get((kind, name), .1), [50, 60]
        return match

    monkeypatch.setattr(scene.single, "_match", legacy)
    monkeypatch.setattr(scene.page, "_match", planned)
    monkeypatch.setattr(scene.battle, "match", other("battle"))
    monkeypatch.setattr(scene.event, "match", other("event"))
    monkeypatch.setattr(scene.page, "_legacy_cleanup_match", other("cleanup"))
    monkeypatch.setattr(scene.page, "_plane_icon", lambda gray: {
        "status": "recognized", "plane_index": 1, "scores": {"1": .99, "2": .1, "3": .1}})
    monkeypatch.setattr(scene.page, "_rest_modal_evidence", lambda image: None)
    monkeypatch.setattr(scene.event, "heading_present", lambda gray: False)
    fallback_calls = []

    def fallback(image, event_family="healing"):
        fallback_calls.append((image, event_family))
        return {"valid": True, "scene": "nonboard_full", "options": []}

    monkeypatch.setattr(scene.page, "observe", fallback)
    return rgb, values, calls, fallback_calls


def test_verified_board_uses_current_metadata_without_fabricated_missing_scores(probes):
    rgb, values, calls, fallback = probes
    values[("legacy", "move_pending")] = .85
    values[("event", "board_turn")] = .95
    values[("event", "board_actions_heading")] = .95
    result = scene.observe(rgb)
    assert result["valid"] and result["scene"] == "board" and result["fresh_scan_ready"]
    assert result["player_turn"] and result["event_board"] and result["move_pending"]
    assert result["single_action_flags"]["move_pending"]
    assert result["rotation_mode_evidence"]["active_button"] is False
    assert "click" not in result and "next_point" not in result
    assert result["scan_scene_evidence"]["action_authorization"] is False
    assert "shop_back" not in result["scores"]
    assert "shop_back" in result["legacy_probes_not_evaluated"]
    assert "insufficient_roles" not in result["planned_scores"]
    assert "insufficient_roles" in result["planned_probes_not_evaluated"]
    assert result["planned_scores"]["insufficient_roles_followup"] == .1
    assert not fallback


@pytest.mark.parametrize("kind,name,threshold", [
    ("legacy", "settlement_failure", .85), ("legacy", "shop_title", .85),
    ("legacy", "event_vortex_choice_title", .85), ("legacy", "event_enter", .80),
    ("legacy", "rotate_confirm", .80), ("planned", "settlement_layout", .85),
    ("planned", "rest_title", .88), ("planned", "insufficient_roles_followup", .82),
    ("cleanup", "confirm", .87), ("battle", "battle_formation_title", .85),
    ("battle", "battle_victory", .85), ("battle", "battle_reward_title", .85),
    ("battle", "reward_obtained_title", .85), ("event", "selection_confirm", .85),
    ("event", "result_exit", .90), ("event", "result_exit_item", .90),
])
def test_each_possible_priority_page_at_original_threshold_falls_back(probes, kind, name, threshold):
    rgb, values, _, fallback = probes
    values[(kind, name)] = threshold
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1 and fallback[0][0] is rgb


def test_battle_threshold_preserves_native_four_decimal_rounding(probes):
    rgb, values, _, fallback = probes
    values[("battle", "battle_victory")] = .84996
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1
    values[("battle", "battle_victory")] = .84994
    assert scene.observe(rgb)["scene"] == "board"


@pytest.mark.parametrize("kind,name", [("legacy", "player_turn"),
    ("planned", "board_actions_heading"), ("legacy", "rotate_confirm"),
    ("planned", "insufficient_roles_followup"), ("battle", "battle_victory"),
    ("event", "result_exit"), ("cleanup", "confirm"), ("event", "board_turn")])
def test_nonfinite_probe_never_grants_shortcut(probes, kind, name):
    rgb, values, _, fallback = probes
    values[(kind, name)] = float("nan")
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1


@pytest.mark.parametrize("value", [True, None])
def test_event_heading_present_or_unknown_uses_original_reader(probes, monkeypatch, value):
    rgb, _, _, fallback = probes
    monkeypatch.setattr(scene.event, "heading_present", lambda gray: value)
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1


def test_preview_modal_and_active_button_do_not_grant_shortcut(probes, monkeypatch):
    rgb, values, _, fallback = probes
    values[("legacy", "rotate_confirm")] = .80
    values[("legacy", "rotate_cancel")] = .80
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    values.clear()
    monkeypatch.setattr(scene.page, "_rest_modal_evidence", lambda image: {"text": "unknown"})
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    monkeypatch.setattr(scene.page, "_rest_modal_evidence", lambda image: None)
    rgb[375:400, 960:1040] = (0, 255, 255)  # Exactly 2000 active cyan pixels.
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 3


def test_changed_pixels_recompute_raw_options_arrows_and_flags(probes, monkeypatch):
    rgb, _, _, fallback = probes
    monkeypatch.setattr(scene.page, "_tile_frames", lambda mask, kind: [
        {"frame_color": kind, "evidence": int(cv2.countNonZero(mask))}])
    monkeypatch.setattr(scene.page, "_rotation_arrows", lambda mask: [
        {"evidence": int(cv2.countNonZero(mask))}])
    before = scene.observe(rgb)
    changed = rgb.copy()
    changed[457:477, 1165:1190] = (255, 255, 0)  # 500 current yellow pixels.
    changed[200:201, 500:501] = (0, 255, 255)
    after = scene.observe(changed)
    assert not before["rotate_done"] and after["rotate_done"]
    assert after["single_action_flags"]["rotate_done"]
    assert before["raw_rotation_options"] != after["raw_rotation_options"]
    assert before["raw_move_options"] != after["raw_move_options"]
    assert before["yellow_frame_candidates"] != after["yellow_frame_candidates"]
    assert not fallback


def test_one_changed_pixel_and_matcher_replacement_cannot_reuse_old_scene(probes, monkeypatch):
    rgb, _, _, fallback = probes
    assert scene.observe(rgb)["scene"] == "board"
    assert scene.observe(rgb.copy())["scene"] == "board"
    original = scene.single._match

    def matcher(gray, name):
        row = original(gray, name)
        if name == "event_enter" and gray[300, 300] > 100:
            row["score"] = .80
        return row

    monkeypatch.setattr(scene.single, "_match", matcher)
    assert scene.observe(rgb)["scene"] == "board"
    rgb[300, 300] = 255
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1


def test_missing_template_falls_back_and_total_failure_is_closed(probes, monkeypatch):
    rgb, _, _, fallback = probes

    def missing(*args, **kwargs):
        raise FileNotFoundError("template")

    monkeypatch.setattr(scene.page, "_match", missing)
    assert scene.observe(rgb)["scene"] == "nonboard_full"
    assert len(fallback) == 1
    monkeypatch.setattr(scene.page, "observe", missing)
    result = scene.observe(rgb)
    assert result["valid"] is False and result["scene"] == "unknown"
    assert result["fresh_scan_ready"] is False
    assert result["scan_scene_evidence"]["error_type"] == "FileNotFoundError"


@pytest.mark.parametrize("case", ["blank", "shape", "dtype", "vortex", "player", "plane"])
def test_unproved_board_falls_back(probes, monkeypatch, case):
    rgb, values, _, fallback = probes
    family = "healing"
    if case == "blank":
        rgb = np.zeros_like(rgb)
    elif case == "shape":
        rgb = rgb[:1]
    elif case == "dtype":
        rgb = rgb.astype(np.float32)
    elif case == "vortex":
        family = "vortex"
    elif case == "player":
        values[("legacy", "player_turn")] = .8499
    elif case == "plane":
        monkeypatch.setattr(scene.page, "_plane_icon", lambda gray: {"status": "unknown"})
    assert scene.observe(rgb, event_family=family)["scene"] == "nonboard_full"
    assert len(fallback) == 1 and fallback[0][1] == family


@pytest.mark.parametrize("page_name", ["unknown", "rotate_preview", "event_options",
                                      "rest_area", "battle_reward_selection"])
def test_full_fallback_preserves_page_options_and_exclusion_evidence(probes, monkeypatch, page_name):
    rgb, values, _, _ = probes
    values[("legacy", "player_turn")] = .1
    full = {"valid": page_name != "unknown", "scene": page_name,
            "options": [{"point": [2, 3]}], "unknown_modal_evidence": {"text": "modal"},
            "scores": {"rotate_confirm": .91}}
    monkeypatch.setattr(scene.page, "observe", lambda *args, **kwargs: full)
    assert scene.observe(rgb) is full


def test_actual_saved_board_fixture_preserves_all_non_score_outputs():
    path = Path(__file__).resolve().parents[1] / "fixtures/deep_dive_anchor/reading_only_binding04.png"
    rgb = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
    threads = cv2.getNumThreads()
    try:
        cv2.setNumThreads(1)
        original = scene.page.observe(rgb)
        result = scene.observe(rgb)
    finally:
        cv2.setNumThreads(threads)
    assert original["scene"] == "board" and original["player_turn"]
    assert result["scan_scene_evidence"]["mode"] == "original_predicate_short_circuit"
    for key, value in original.items():
        if key not in {"scores", "planned_scores", "planned_probes_not_evaluated"}:
            assert result[key] == value, key
    for key, value in result["scores"].items():
        assert original["scores"][key] == value

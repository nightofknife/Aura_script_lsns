from copy import deepcopy
import json
from pathlib import Path

import pytest

from plans.resonance_pc.src.actions._deep_dive_established_target_explainer import apply_current_explanations, explain_established_targets


@pytest.fixture
def real_case():
    fixture = Path(__file__).resolve().parents[1] / "fixtures/deep_dive_anchor/established_backface1083.json"
    case = json.loads(fixture.read_text(encoding="utf-8"))
    case.pop("evidence_scope")
    case["front_assigned"] = {int(key): value for key, value in case["front_assigned"].items()}
    case["hud_unchanged"] = True
    return case


def test_actual_backface_boss_explanation_does_not_create_positive(real_case):
    before = deepcopy(real_case)
    result = explain_established_targets(**real_case)
    assert real_case == before
    assert result["status"] == "explained"
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["cell_index"] == 31
    assert match["target_index"] == 1
    assert match["kind"] == "singularity"
    assert match["explanation_only"] is True
    assert match["positive_vote"] is False
    assert result["input_authorized"] is False
    evidence = match["association_evidence"]
    assert evidence["cosine"] < 0
    assert evidence["error"] == pytest.approx(.0862379, abs=1e-6)
    assert evidence["margin"] > .7
    assert evidence["anchor_center_reliability"]["margin"] > .12


@pytest.mark.parametrize("mutation, expected_reason", [
    ("hud", "hud_or_state_change_unresolved"),
    ("stale", "source_not_fresh_actual_wgc"),
    ("future", "source_not_fresh_actual_wgc"),
    ("not_renewed", "same_frame_glyph_pose_not_renewed"),
    ("old_anchor_source", "same_frame_glyph_pose_not_renewed"),
    ("map_changed", "same_frame_glyph_pose_not_renewed"),
    ("coverage_cached", "current_model_coverage_unproven"),
    ("backend", "source_not_fresh_actual_wgc"),
])
def test_source_and_state_gates(real_case, mutation, expected_reason):
    if mutation == "hud":
        real_case["hud_unchanged"] = False
    elif mutation == "stale":
        real_case["now"] += 1
    elif mutation == "future":
        real_case["now"] -= 1
    elif mutation == "not_renewed":
        real_case["anchor_proof"]["renewed"] = False
    elif mutation == "old_anchor_source":
        real_case["anchor_proof"]["source_frame_id"] -= 1
    elif mutation == "map_changed":
        real_case["source"]["map_revision"] += 1
    elif mutation == "coverage_cached":
        real_case["model_coverage"]["model_executed"] = False
    else:
        real_case["source"]["capture_backend"] = "fake"
    result = explain_established_targets(**real_case)
    assert not result["matches"]
    assert result["reason"] == expected_reason


@pytest.mark.parametrize("mutation, expected_reason", [
    ("not_known", "best_cell_not_established_same_kind"),
    ("different_kind", "best_cell_not_established_same_kind"),
    ("low_established_confidence", "best_cell_not_established_same_kind"),
    ("one_vote", "insufficient_independent_historical_front_support"),
    ("same_rotation", "insufficient_independent_historical_front_support"),
    ("future_support", "insufficient_independent_historical_front_support"),
    ("weak_model", "weak_or_non_model_candidate"),
    ("no_model", "weak_or_non_model_candidate"),
])
def test_historical_front_proof_and_model_are_required(real_case, mutation, expected_reason):
    cell = real_case["cells"][31]
    target = real_case["targets"][1]
    if mutation == "not_known":
        cell["occupant_status"] = "unknown"
    elif mutation == "different_kind":
        cell["occupant"] = "player"
    elif mutation == "low_established_confidence":
        cell["confidence"] = .69
    elif mutation == "one_vote":
        cell["evidence"] = cell["evidence"][:1]
    elif mutation == "same_rotation":
        rotation = cell["evidence"][0]["association_evidence"]["rotation"]
        for evidence in cell["evidence"]:
            evidence["association_evidence"]["rotation"] = rotation
    elif mutation == "future_support":
        for evidence in cell["evidence"]:
            evidence["association_evidence"]["source_frame_time"] = real_case["source"]["frame_time"] + 1
    elif mutation == "weak_model":
        target["confidence"] = .69
    else:
        target["source"] = "guessed_rule"
    result = explain_established_targets(**real_case)
    assert not result["matches"]
    assert any(row["target_index"] == 1 and row["reason"] == expected_reason for row in result["rejections"])


def test_two_strong_targets_cannot_both_explain_one_known_boss(real_case):
    real_case["targets"].append(deepcopy(real_case["targets"][1]))
    result = explain_established_targets(**real_case)
    assert not result["matches"]
    assert sum(row["reason"] == "non_unique_current_target_claim" for row in result["rejections"]) == 2


def test_front_count_is_not_replaced_or_increased_by_duplicate_back_claim(real_case):
    real_case["front_assigned"][31] = dict(real_case["targets"][1], point=[400., 400.])
    result = explain_established_targets(**real_case)
    assert not result["matches"]
    assert any(row["reason"] == "non_unique_current_target_claim" for row in result["rejections"])


def test_boss_endpoint_uncertainty_keeps_original_gate(real_case):
    boss = real_case["targets"][1]
    x, y = boss["point"]
    boss["box"] = [x - 150., y - 150., 300., 300.]
    result = explain_established_targets(**real_case)
    assert not result["matches"]
    assert any(row["target_index"] == 1 and row["reason"] == "bbox_center_height_boundary_ambiguous"
               for row in result["rejections"])


def test_weak_unmatched_boxes_are_not_consumed_or_removed(real_case):
    weak = deepcopy(real_case["targets"][-1])
    result = explain_established_targets(**real_case)
    assert weak in real_case["targets"]
    assert len(result["matches"]) == 1
    assert any(row["reason"] == "weak_or_non_model_candidate" for row in result["rejections"])


@pytest.fixture
def application(real_case):
    source = real_case["source"]
    full = {f"source_{key}": source[key] for key in ("frame_id", "frame_time", "session_id", "generation", "map_revision")}
    real_case["anchor_proof"].update(full)
    real_case["model_coverage"].update(full)
    real_case["hud_proof"] = dict(full, valid=True, unchanged=True, scene="board")
    explanation = explain_established_targets(**real_case)
    layout = {"cells": deepcopy(real_case["cells"]), "semantic_source": deepcopy(source),
              "map_revision": source["map_revision"], "target_clues": deepcopy(real_case["targets"]),
              "target_candidate_associations": deepcopy(real_case["targets"]),
              "player_cell": {"face": "U", "row": 1, "col": 1},
              "singularity_cell": {"face": "D", "row": 1, "col": 1},
              "inspiration_cells": [{"face": "F", "row": 2, "col": 2}, {"face": "B", "row": 1, "col": 2}]}
    return layout, explanation


def test_apply_only_clones_and_updates_current_candidate_association(application):
    layout, explanation = application
    before = deepcopy(layout)
    result = apply_current_explanations(layout, explanation)
    assert result["status"] == "applied"
    assert result["positive_vote"] is False
    assert result["input_authorized"] is False
    assert layout == before
    updated = result["layout"]
    assert updated["target_candidate_associations"][1]["cell_index"] == 31
    for key in layout:
        if key != "target_candidate_associations":
            assert updated[key] == layout[key]
    assert updated["target_candidate_associations"][0] == layout["target_candidate_associations"][0]
    assert updated["target_candidate_associations"][2] == layout["target_candidate_associations"][2]
    assert "cell_index" not in updated["target_candidate_associations"][3]


@pytest.mark.parametrize("mutation", [
    "no_source", "generation", "session", "frame", "time", "revision", "backend",
    "no_hud", "hud_generation", "hud_changed", "hud_invalid", "hud_scene",
    "anchor_generation", "model_generation", "duplicates", "manual_cell_index", "target_point",
    "candidate_box", "positive_vote", "input_authorized", "unread_only", "front_snapshot", "broaden_age",
])
def test_apply_atomic_fail_closed_for_invalid_source_or_tampered_matches(application, mutation):
    layout, explanation = application
    if mutation == "no_source":
        layout.pop("semantic_source")
    elif mutation in ("generation", "session", "frame", "time", "revision", "backend"):
        key = {"session": "session_id", "frame": "frame_id", "time": "frame_time", "revision": "map_revision",
               "backend": "capture_backend"}.get(mutation, mutation)
        layout["semantic_source"][key] = "invalid" if mutation in ("session", "backend") else layout["semantic_source"][key] + 1
    elif mutation == "no_hud":
        explanation["audit"]["hud_proof"] = None
    elif mutation == "hud_generation":
        explanation["audit"]["hud_proof"]["source_generation"] += 1
    elif mutation in ("hud_changed", "hud_invalid", "hud_scene"):
        key = {"hud_changed": "unchanged", "hud_invalid": "valid", "hud_scene": "scene"}[mutation]
        explanation["audit"]["hud_proof"][key] = "event" if key == "scene" else False
    elif mutation in ("anchor_generation", "model_generation"):
        proof = "anchor_proof" if mutation == "anchor_generation" else "model_coverage"
        explanation["audit"][proof]["source_generation"] += 1
    elif mutation == "duplicates":
        explanation["matches"].append(deepcopy(explanation["matches"][0]))
    elif mutation == "manual_cell_index":
        explanation["matches"][0]["cell_index"] = 27
    elif mutation == "target_point":
        layout["target_clues"][1]["point"][0] += 5
    elif mutation == "candidate_box":
        layout["target_candidate_associations"][1]["box"][0] += 5
    elif mutation in ("positive_vote", "input_authorized"):
        explanation[mutation] = True
    elif mutation == "unread_only":
        explanation["explanation_only"] = False
    elif mutation == "front_snapshot":
        explanation["audit"]["front_assigned"] = {}
    else:
        explanation["audit"]["max_age_sec"] = 10.
    before = deepcopy(layout)
    result = apply_current_explanations(layout, explanation)
    assert result["status"] == "rejected"
    assert layout == before
    assert result["layout"] == before


def test_apply_requires_complete_hud_proof_even_when_exploration_bool_passes(real_case):
    explanation = explain_established_targets(**real_case)
    assert explanation["matches"]
    layout = {"cells": real_case["cells"], "semantic_source": real_case["source"], "map_revision": 0,
              "target_clues": real_case["targets"], "target_candidate_associations": real_case["targets"]}
    assert apply_current_explanations(layout, explanation)["status"] == "rejected"

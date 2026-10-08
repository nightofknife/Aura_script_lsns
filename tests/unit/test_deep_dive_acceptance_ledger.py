from copy import deepcopy
import hashlib
import json

import pytest

from tools.deep_dive_acceptance import AcceptanceLedger, fingerprint_files


@pytest.fixture
def case(tmp_path):
    fingerprint = {"code_sha256": "1" * 64, "model_sha256": "2" * 64,
                   "config_sha256": "3" * 64, "provider": "CPU", "threads": 1}
    ledger = AcceptanceLedger(tmp_path / "ledger.json", tmp_path)
    ledger.initialize(fingerprint, initial_environment_sec=2.5)
    def image(name, content):
        path = tmp_path / name
        path.write_bytes(content)
        return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
    truth = {"truth_id": "truth1", "board_id": "board1", "state_id": "state1",
             "reviewer": "independent human source review", "coordinate_frame": "manual reset frame",
             "method": "manual_multiview", "independent_of_tested_output": True,
             "ambiguous": False, "images": [image("oracle1.png", b"oracle one"), image("oracle2.png", b"oracle two")],
             "entities": [{"kind": "player", "face": "U", "row": 1, "col": 1},
                          {"kind": "singularity", "face": "D", "row": 1, "col": 1},
                          {"kind": "inspiration", "face": "B", "row": 1, "col": 2}]}
    ledger.register_truth(truth)
    def run(index):
        source = dict(image(f"source{index}.png", f"actual capture {index}".encode()),
                      backend="wgc", role="positive", session_id="wgc-session-1", generation=index,
                      frame_time=float(index), map_revision=0)
        return {"run_id": f"run{index}", "truth_id": "truth1", "fingerprint": deepcopy(fingerprint),
                "business_ready": True, "business_status": "ready", "state_unchanged": True,
                "dispatch_wall_sec": 45.0, "invocation_elapsed_sec": 43.0,
                "entities": deepcopy(truth["entities"]), "sources": [source]}
    return ledger, fingerprint, truth, run, image


def test_twenty_consecutive_with_explicit_same_board_limit(case):
    ledger, _, _, run, _ = case
    for index in range(1, 21):
        assert ledger.record_run(run(index))["passed"]
        assert ledger.summary()["campaign_passed"] is (index >= 20)
    summary = ledger.summary()
    assert summary["same_board_only"] is True
    assert summary["generalization_established"] is False
    assert summary["current_streak_boards"] == ["board1"]
    assert summary["initial_environment_sec"] == 2.5
    reloaded = AcceptanceLedger(ledger.path, ledger.root)
    assert reloaded.summary() == summary


@pytest.mark.parametrize("change, reason", [
    ({"dispatch_wall_sec": 90.001}, "over_limit_dispatch_wall_sec"),
    ({"invocation_elapsed_sec": 90.001}, "over_limit_invocation_elapsed_sec"),
    ({"dispatch_wall_sec": float("nan")}, "invalid_dispatch_wall_sec"),
    ({"dispatch_wall_sec": True}, "invalid_dispatch_wall_sec"),
    ({"business_status": "targets_ready"}, "business_not_ready"),
    ({"business_ready": False}, "business_not_ready"),
    ({"state_unchanged": False}, "state_change_unresolved"),
    ({"truth_id": "unknown"}, "unregistered_truth"),
    ({"sources": []}, "missing_sources"),
])
def test_failure_resets_and_preserves_attempt(case, change, reason):
    ledger, _, _, run, _ = case
    assert ledger.record_run(run(1))["passed"]
    bad = run(2)
    bad.update(change)
    record = ledger.record_run(bad)
    assert not record["passed"]
    assert reason in record["failures"]
    assert ledger.summary()["consecutive_passes"] == 0
    assert ledger.summary()["attempts"] == 2
    assert ledger.record_run(run(3))["consecutive_passes"] == 1


def test_ninety_boundary_is_accepted(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row.update(dispatch_wall_sec=90.0, invocation_elapsed_sec=90.0)
    assert ledger.record_run(row)["passed"]


def test_targets_ready_is_recognition_success_with_both_business_proofs(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row.update(business_status="targets_ready", scan_success=True, targets_readiness=True)
    assert ledger.record_run(row)["passed"]
    row = run(2)
    row.update(business_status="targets_ready", scan_success=True, targets_readiness=False)
    assert "business_not_ready" in ledger.record_run(row)["failures"]


@pytest.mark.parametrize("mutation", ["missing", "wrong_cell", "duplicate", "phantom"])
def test_entity_multiset_never_dilutes_with_empty_tiles(case, mutation):
    ledger, _, _, run, _ = case
    row = run(1)
    if mutation == "missing":
        row["entities"].pop()
    elif mutation == "wrong_cell":
        row["entities"][1]["col"] = 0
    elif mutation == "duplicate":
        row["entities"].append(deepcopy(row["entities"][0]))
    else:
        row["entities"].append({"kind": "inspiration", "face": "F", "row": 0, "col": 0})
    assert "entity_set_mismatch" in ledger.record_run(row)["failures"]


def test_freeze_changes_are_failed_not_silent_new_campaign(case):
    ledger, _, _, run, _ = case
    assert ledger.record_run(run(1))["passed"]
    row = run(2)
    row["fingerprint"]["model_sha256"] = "9" * 64
    assert "frozen_version_changed" in ledger.record_run(row)["failures"]
    assert ledger.summary()["consecutive_passes"] == 0
    with pytest.raises(ValueError, match="reinitialized"):
        ledger.initialize(row["fingerprint"])


def test_same_image_new_generation_cannot_create_new_vote(case):
    ledger, _, _, run, _ = case
    first = run(1)
    assert ledger.record_run(first)["passed"]
    second = run(2)
    second["sources"][0].update(path=first["sources"][0]["path"], sha256=first["sources"][0]["sha256"], role="negative")
    assert "reused_image_evidence" in ledger.record_run(second)["failures"]


def test_same_source_new_image_cannot_create_new_vote(case):
    ledger, _, _, run, _ = case
    assert ledger.record_run(run(1))["passed"]
    second = run(2)
    second["sources"][0]["generation"] = 1
    assert "reused_capture_identity" in ledger.record_run(second)["failures"]


def test_actual_wgc_integer_session_identity_is_supported_and_canonicalized(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row["sources"][0]["session_id"] = 205300521429767784098712176179570744101
    record = ledger.record_run(row)
    assert record["passed"]
    assert record["verified_sources"][0]["session_id"] == "205300521429767784098712176179570744101"


def test_failed_attempt_sources_are_also_burned(case):
    ledger, _, _, run, _ = case
    first = run(1)
    first["business_ready"] = False
    assert not ledger.record_run(first)["passed"]
    repeat = deepcopy(first)
    repeat.update(run_id="another run", business_ready=True)
    assert "reused_capture_identity" in ledger.record_run(repeat)["failures"]


def test_metadata_and_actual_hash_are_checked(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row["sources"][0]["sha256"] = "0" * 64
    assert "evidence image hash mismatch" in ledger.record_run(row)["failures"]


def test_source_metadata_must_increase_within_run(case):
    ledger, _, _, run, _ = case
    row = run(1)
    extra = run(2)["sources"][0]
    extra["frame_time"] = 0.5
    row["sources"].append(extra)
    assert "non_increasing_capture_source" in ledger.record_run(row)["failures"]


def test_source_metadata_must_increase_across_runs(case):
    ledger, _, _, run, _ = case
    assert ledger.record_run(run(2))["passed"]
    assert "non_increasing_capture_source" in ledger.record_run(run(1))["failures"]


def test_registered_truth_images_cannot_change_after_freeze(case):
    ledger, _, _, run, _ = case
    (ledger.root / "oracle1.png").write_bytes(b"changed annotation source")
    assert "registered_truth_source_changed" in ledger.record_run(run(1))["failures"]


def test_metrics_count_wrong_cell_as_false_positive_and_false_negative(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row["entities"][1]["col"] = 0
    record = ledger.record_run(row)
    assert record["entity_metrics"]["singularity"] == {"tp": 0, "fp": 1, "fn": 1, "precision": 0.0, "recall": 0.0}


def test_summary_does_not_hide_failed_duration(case):
    ledger, _, _, run, _ = case
    row = run(1)
    row["dispatch_wall_sec"] = 100.0
    ledger.record_run(row)
    ledger.record_run(run(2))
    summary = ledger.summary()
    assert summary["max_dispatch_wall_sec"] == 45.0
    assert summary["all_attempts_max_dispatch_wall_sec"] == 100.0
    assert summary["all_attempts_mean_dispatch_wall_sec"] == 72.5


def test_truth_is_immutable_and_rejects_circular_or_ambiguous_annotation(case):
    ledger, _, truth, _, _ = case
    with pytest.raises(ValueError, match="immutable"):
        ledger.register_truth(truth)
    for update in ({"independent_of_tested_output": False}, {"ambiguous": True}, {"method": "detector_output"}):
        candidate = deepcopy(truth)
        candidate.update(truth_id="new", **update)
        with pytest.raises(ValueError):
            ledger.register_truth(candidate)


def test_confirmed_transition_uses_parent_oracle_and_bijection(case):
    ledger, _, truth, _, _ = case
    new = deepcopy(truth)
    new.update(truth_id="transition", state_id="state2", method="confirmed_transition",
               parent_truth_id="truth1", mapping=list(range(54)), action_confirmed=True,
               transition_review="actual action plus independent ordinary-glyph check")
    ledger.register_truth(new)
    bad = deepcopy(new)
    bad.update(truth_id="wrong transition")
    bad["entities"][0]["row"] = 0
    with pytest.raises(ValueError, match="mapping"):
        ledger.register_truth(bad)


def test_fingerprint_hashes_real_files_and_config(tmp_path):
    (tmp_path / "action.py").write_text("first version")
    (tmp_path / "model.onnx").write_bytes(b"weight bytes")
    first = fingerprint_files(tmp_path, ["action.py"], "model.onnx", {"threads": 1})
    (tmp_path / "action.py").write_text("second version")
    changed = fingerprint_files(tmp_path, ["action.py"], "model.onnx", {"threads": 1})
    assert changed["code_sha256"] != first["code_sha256"]
    assert changed["model_sha256"] == first["model_sha256"]
    assert fingerprint_files(tmp_path, ["action.py"], "model.onnx", {"threads": 16})["config_sha256"] != changed["config_sha256"]


def test_ledger_and_evidence_must_stay_inside_repo(case, tmp_path):
    ledger, _, _, run, _ = case
    with pytest.raises(ValueError, match="repository"):
        AcceptanceLedger(tmp_path.parent / "outside.json", tmp_path)
    row = run(1)
    row["sources"][0]["path"] = "../outside.png"
    assert "evidence file must stay in repository" in ledger.record_run(row)["failures"]

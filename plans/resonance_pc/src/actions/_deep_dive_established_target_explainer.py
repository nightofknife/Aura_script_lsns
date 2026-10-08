"""Explain a current back-face sprite using established front-face evidence.

This pure helper never creates, modifies or renews entity positive votes. Callers
must independently establish that the board/HUD is unchanged. Its results are
explanation_only and are unsuitable as occupancy evidence or input permission.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

import cv2
import numpy as np

from ._deep_dive_layout_vision import BASES, FACES, K, _anchor_heights, _point, _quad


def _real_model(row, historical=False):
    source = row.get("target_source" if historical else "source")
    evidence = row.get("target_box_evidence" if historical else "box_evidence")
    return source == "deep_dive_entity_onnx" or (
        source == "deep_dive_local_pink_head"
        and evidence == "existing_model_box_with_confirmed_head_center")


def _signature(row):
    return (row.get("kind"), tuple(row.get("point", [])), tuple(row.get("box", [])))


def _source_matches(proof, source):
    return all(proof.get(f"source_{key}") == source.get(key)
               for key in ("frame_id", "frame_time", "map_revision"))


def _front_support(cell, kind, source):
    rows = []
    for item in cell.get("evidence", []):
        proof = item.get("association_evidence", {})
        try:
            if (item.get("occupant") != kind or item.get("confidence", 0) < .70
                    or item.get("cosine", 0) < .12 or not _real_model(item, historical=True)
                    or proof.get("source_map_revision") != source["map_revision"]
                    or proof.get("source_frame_id") != item.get("frame_id")
                    or item["frame_id"] >= source["frame_id"]
                    or proof["source_frame_time"] >= source["frame_time"]
                    or not (proof["error"] < .30 and proof["margin"] > .12)):
                continue
            rotation = np.asarray(proof["rotation"], float)
            if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
                continue
            if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-5) or np.linalg.det(rotation) < .999:
                continue
            rows.append((item, rotation))
        except (KeyError, TypeError, ValueError):
            continue
    for index, (first, rotation) in enumerate(rows):
        for second, other in rows[index + 1:]:
            if first["frame_id"] == second["frame_id"]:
                continue
            angle = float(np.degrees(np.linalg.norm(cv2.Rodrigues(rotation @ other.T)[0])))
            if angle >= 8.0:
                return [first["frame_id"], second["frame_id"]]
    return []


def explain_established_targets(cells, targets, *, pose, source, anchor_proof,
                                model_coverage, front_assigned=None,
                                hud_unchanged=False, hud_proof=None, now=None, max_age_sec=.8):
    """Return same-frame back-face explanations, never accepted occupancy votes.

    ``source`` contains frame_id, frame_time, map_revision, generation,
    session_id and capture_backend/backend. ``anchor_proof`` and
    ``model_coverage`` contain source_frame_id/time/map_revision; the former
    has renewed=True, the latter model_executed=True and coverage_valid=True.
    ``front_assigned`` is the existing strict front-association index->entry map.
    """
    result = {"explanation_only": True, "positive_vote": False,
              "input_authorized": False, "matches": [], "rejections": [],
              "status": "rejected", "reason": None,
              "audit": deepcopy({"schema": "established_target_explanation.v1",
                  "source": source, "pose": pose, "anchor_proof": anchor_proof,
                  "model_coverage": model_coverage, "hud_proof": hud_proof,
                  "hud_unchanged": hud_unchanged, "evaluated_at": now,
                  "max_age_sec": max_age_sec, "front_assigned": front_assigned or {}})}
    front_assigned = front_assigned or {}
    if (not isinstance(source, dict) or not isinstance(pose, dict) or not isinstance(anchor_proof, dict)
            or not isinstance(model_coverage, dict) or not isinstance(front_assigned, dict)
            or not isinstance(targets, list) or any(not isinstance(target, dict) for target in targets)):
        result["reason"] = "invalid_current_snapshot"
        return result
    if hud_unchanged is not True:
        result["reason"] = "hud_or_state_change_unresolved"
        return result
    try:
        session = source["session_id"]
        session_valid = (isinstance(session, str) and bool(session.strip())
                         or type(session) is int and session >= 0)
        identity_valid = (type(source["frame_id"]) is int and type(source["generation"]) is int
                          and source["generation"] >= 0 and session_valid
                          and type(source["map_revision"]) is int
                          and source["map_revision"] >= 0
                          and source.get("capture_backend", source.get("backend")) == "wgc")
        age = float(now) - float(source["frame_time"])
        if not identity_valid or not math.isfinite(age) or not 0 <= age <= max_age_sec:
            raise ValueError("source_not_fresh_actual_wgc")
        if anchor_proof.get("renewed") is not True or not _source_matches(anchor_proof, source):
            raise ValueError("same_frame_glyph_pose_not_renewed")
        if (model_coverage.get("model_executed") is not True
                or model_coverage.get("coverage_valid") is not True
                or not _source_matches(model_coverage, source)):
            raise ValueError("current_model_coverage_unproven")
        if pose.get("map_revision") != source["map_revision"]:
            raise ValueError("pose_map_revision_mismatch")
        rotation = np.asarray(pose["rotation"], float)
        tvec = np.asarray(pose["tvec"], float).reshape(3)
        if rotation.shape != (3, 3) or not np.isfinite(rotation).all() or not np.isfinite(tvec).all():
            raise ValueError("invalid_pose")
        if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-5) or np.linalg.det(rotation) < .999:
            raise ValueError("invalid_pose_rotation")
        if len(cells) != 54 or len({(c["face"], c["row"], c["col"]) for c in cells}) != 54:
            raise ValueError("incomplete_surface_geometry")
        if any(c["face"] not in FACES or c["row"] not in range(3) or c["col"] not in range(3) for c in cells):
            raise ValueError("invalid_surface_geometry")
    except (KeyError, TypeError, ValueError) as error:
        result["reason"] = str(error)
        return result

    centres = np.asarray([_point(cell) for cell in cells])
    normals = np.asarray([BASES[cell["face"]][0] for cell in cells], float)
    camera = -rotation.T @ tvec
    cosine = np.einsum("ij,ij->i", normals, camera - centres) / np.linalg.norm(camera - centres, axis=1)
    def project(points):
        xyz = np.asarray(points) @ rotation.T + tvec
        pixels = xyz @ K.T
        return pixels[..., :2] / pixels[..., 2:3], xyz[..., 2]
    projected, depth = project(np.asarray([_quad(cell) for cell in cells]))
    valid = np.isfinite(projected).all(axis=(1, 2)) & (depth > 0).all(axis=1)
    scale = np.asarray([max(20., np.sqrt(abs(cv2.contourArea(np.float32(quad)))))
                        if ok else np.inf for quad, ok in zip(projected, valid)])
    try:
        front_signatures = {_signature(entry) for entry in front_assigned.values()}
        occupied_by_front = {(entry.get("kind"), int(index)) for index, entry in front_assigned.items()}
    except (AttributeError, TypeError, ValueError):
        result["reason"] = "invalid_front_snapshot"
        return result
    candidates = []
    for target_index, target in enumerate(targets):
        if _signature(target) in front_signatures:
            continue
        kind = target.get("kind")
        if kind not in ("player", "singularity", "inspiration"):
            continue
        try:
            if not (target.get("confirmable", True) and target.get("confidence", 0) >= .70 and _real_model(target)):
                raise ValueError("weak_or_non_model_candidate")
            point = np.asarray(target["point"], float)
            box = np.asarray(target["box"], float)
            if point.shape != (2,) or box.shape != (4,) or not np.isfinite(point).all() or not np.isfinite(box).all() or min(box[2:]) <= 0:
                raise ValueError("invalid_target_pixels")
            heights = np.linspace(*_anchor_heights(target), 16)
            samples = centres[:, None, :] + normals[:, None, :] * heights[None, :, None]
            path, sample_depth = project(samples)
            path_valid = valid & np.isfinite(path).all(axis=(1, 2)) & (sample_depth > 0).all(axis=1)
            pixel_errors = np.linalg.norm(path - point, axis=2).min(axis=1)
            errors = pixel_errors / scale
            errors[~path_valid] = np.inf
            ranks = np.argsort(errors)
            best, rival = int(ranks[0]), int(ranks[1])
            margin = float(errors[rival] - errors[best])
            if not (errors[best] < .30 and margin > .12):
                raise ValueError("all54_normal_corridor_competitor")
            cell = cells[best]
            if cell.get("occupant") != kind or cell.get("occupant_status") != "confirmed" or cell.get("confidence", 0) < .70:
                raise ValueError("best_cell_not_established_same_kind")
            support = _front_support(cell, kind, source)
            if not support:
                raise ValueError("insufficient_independent_historical_front_support")
            if cosine[best] >= .12:
                raise ValueError("unassigned_front_candidate_not_explainable")
            sample = int(np.argmin(np.linalg.norm(path[best] - point, axis=1)))
            evidence = {"error": float(errors[best]), "competitor_index": rival,
                        "competitor_error": float(errors[rival]), "margin": margin,
                        "cosine": float(cosine[best]), "height_sample": sample,
                        "fitted_height": float(heights[sample]), "historical_front_frames": support,
                        "source_frame_id": source["frame_id"], "source_frame_time": source["frame_time"],
                        "source_map_revision": source["map_revision"]}
            centre = box[:2] + box[2:] / 2
            if kind == "singularity" and sample in (0, 15) and np.linalg.norm(point - centre) <= 1.:
                radius = .20 * min(box[2:])
                robust = np.maximum(0., pixel_errors - radius) / scale
                robust[~path_valid] = np.inf
                robust_rival = int(np.argmin(np.where(np.arange(54) != best, robust, np.inf)))
                robust_margin = float(robust[robust_rival] - robust[best])
                evidence["anchor_center_reliability"] = {"centre_uncertainty_px": float(radius),
                    "competitor_index": robust_rival, "margin": robust_margin}
                if robust_margin <= .12:
                    raise ValueError("bbox_center_height_boundary_ambiguous")
            candidates.append({"target_index": target_index, "kind": kind, "cell_index": best,
                               "explanation_only": True, "positive_vote": False,
                               "target_signature": deepcopy({key: target[key] for key in ("kind", "point", "box")}),
                               "association_evidence": evidence})
        except (KeyError, TypeError, ValueError) as error:
            result["rejections"].append({"target_index": target_index, "kind": kind, "reason": str(error)})
    claims = Counter((row["kind"], row["cell_index"]) for row in candidates)
    for candidate in candidates:
        key = candidate["kind"], candidate["cell_index"]
        if claims[key] != 1 or key in occupied_by_front:
            result["rejections"].append({"target_index": candidate["target_index"],
                                        "kind": candidate["kind"], "reason": "non_unique_current_target_claim"})
        else:
            result["matches"].append(candidate)
    result["status"] = "explained" if result["matches"] else "unexplained"
    result["reason"] = None
    return result


def _full_source_equal(first, second):
    keys = ("frame_id", "frame_time", "session_id", "generation", "map_revision")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if any(key not in first or key not in second for key in keys):
        return False
    if any(first[key] != second[key] for key in keys):
        return False
    return (first.get("capture_backend", first.get("backend")) == "wgc"
            and second.get("capture_backend", second.get("backend")) == "wgc")


def _complete_proof_matches(proof, source):
    keys = ("frame_id", "frame_time", "session_id", "generation", "map_revision")
    return isinstance(proof, dict) and all(
        f"source_{key}" in proof and proof[f"source_{key}"] == source.get(key) for key in keys)


def apply_current_explanations(layout, explanation):
    """Atomically update only current candidate associations in a deep clone.

    Full ``layout.semantic_source`` and full same-source HUD/anchor/model proof
    are required. Geometry and established historical support are recomputed;
    an index list cannot authorize an explanation. The wrapper's status must be
    checked by the caller. This function does not call or change readiness.
    """
    cloned = deepcopy(layout)
    response = {"status": "rejected", "reason": None, "layout": cloned,
                "positive_vote": False, "input_authorized": False}
    def reject(reason):
        response["reason"] = reason
        return response
    if not isinstance(layout, dict) or not isinstance(explanation, dict):
        return reject("invalid_explanation_payload")
    if (explanation.get("status") != "explained" or explanation.get("explanation_only") is not True
            or explanation.get("positive_vote") is not False or explanation.get("input_authorized") is not False):
        return reject("explanation_not_read_only")
    audit = explanation.get("audit", {})
    if not isinstance(audit, dict) or audit.get("schema") != "established_target_explanation.v1":
        return reject("explanation_audit_missing")
    source = audit.get("source", {})
    if not _full_source_equal(layout.get("semantic_source"), source) or layout.get("map_revision") != source.get("map_revision"):
        return reject("semantic_source_mismatch")
    anchor = audit.get("anchor_proof")
    coverage = audit.get("model_coverage")
    hud = audit.get("hud_proof")
    if not _complete_proof_matches(anchor, source) or anchor.get("renewed") is not True:
        return reject("full_anchor_source_unproven")
    if (not _complete_proof_matches(coverage, source) or coverage.get("model_executed") is not True
            or coverage.get("coverage_valid") is not True):
        return reject("full_model_source_unproven")
    if (not _complete_proof_matches(hud, source) or hud.get("valid") is not True
            or hud.get("unchanged") is not True or hud.get("scene") != "board"
            or audit.get("hud_unchanged") is not True):
        return reject("full_hud_source_unproven")
    targets = layout.get("target_clues")
    associations = layout.get("target_candidate_associations")
    matches = explanation.get("matches")
    if (not isinstance(targets, list) or not isinstance(associations, list) or len(targets) != len(associations)
            or not isinstance(matches, list) or not matches):
        return reject("current_target_snapshot_incomplete")
    try:
        for target, candidate in zip(targets, associations):
            if not isinstance(target, dict) or not isinstance(candidate, dict) or _signature(target) != _signature(candidate):
                return reject("current_target_signature_mismatch")
    except (TypeError, ValueError):
        return reject("current_target_signature_mismatch")
    audited_front = audit.get("front_assigned", {})
    if not isinstance(audited_front, dict):
        return reject("front_snapshot_invalid")
    try:
        expected_front = {(candidate["cell_index"], _signature(candidate))
                          for candidate in associations if candidate.get("cell_index") is not None}
        actual_front = {(int(index), _signature(entry)) for index, entry in audited_front.items()}
    except (AttributeError, TypeError, ValueError):
        return reject("front_snapshot_invalid")
    if expected_front != actual_front:
        return reject("front_snapshot_changed")
    if (not isinstance(audit.get("max_age_sec"), (int, float))
            or not math.isfinite(audit["max_age_sec"]) or not 0 < audit["max_age_sec"] <= .8):
        return reject("source_freshness_gate_changed")
    try:
        recomputed = explain_established_targets(layout["cells"], targets, pose=audit["pose"], source=source,
            anchor_proof=anchor, model_coverage=coverage, front_assigned=audit.get("front_assigned"),
            hud_unchanged=True, hud_proof=hud, now=audit["evaluated_at"], max_age_sec=audit["max_age_sec"])
    except (KeyError, TypeError, ValueError):
        return reject("recompute_source_invalid")
    if recomputed["status"] != "explained" or recomputed["matches"] != matches:
        return reject("explanation_not_reproducible_from_current_evidence")
    target_indices, occupied = set(), set()
    for match in matches:
        if not isinstance(match, dict) or type(match.get("target_index")) is not int:
            return reject("invalid_match_index")
        index = match["target_index"]
        if not 0 <= index < len(associations) or index in target_indices:
            return reject("duplicate_or_invalid_match_index")
        candidate = associations[index]
        if (match.get("positive_vote") is not False or match.get("explanation_only") is not True
                or match.get("target_signature") != {key: candidate.get(key) for key in ("kind", "point", "box")}
                or candidate.get("cell_index") is not None):
            return reject("match_would_replace_existing_association")
        key = match.get("kind"), match.get("cell_index")
        if key in occupied:
            return reject("duplicate_target_cell_claim")
        occupied.add(key)
        target_indices.add(index)
    for match in matches:
        candidate = cloned["target_candidate_associations"][match["target_index"]]
        candidate["cell_index"] = match["cell_index"]
        candidate["current_explanation"] = deepcopy(match)
    response.update(status="applied", explanation_audit=deepcopy(audit), applied_count=len(matches))
    return response

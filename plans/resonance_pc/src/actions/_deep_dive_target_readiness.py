"""Target-only scan proof and the ordinary cells needed by a directed action.

No missing cell is assigned a class here. HUD remainder is an upper bound:
the Boss may have consumed inspiration without crediting the player.
"""
from __future__ import annotations

import math
from numbers import Integral, Real

from ._deep_dive_planner_rules import FACES, cell_to_slot

TARGETS = ("player", "singularity", "inspiration")


def _cells(layout):
    values = layout.get("cells")
    if not isinstance(values, list) or len(values) != 54:
        raise ValueError("targets_require_54_real_cells")
    result = {}
    for row in values:
        if not isinstance(row, dict):
            raise ValueError("invalid_cell")
        slot = cell_to_slot(row)
        if slot in result:
            raise ValueError("duplicate_cell_coordinate")
        result[slot] = row
    if set(result) != set(range(54)):
        raise ValueError("incomplete_cell_coordinates")
    return result


def _groups(row, kind=None):
    groups = set()
    entries = row.get("evidence", ())
    if not isinstance(entries, (list, tuple)):
        return groups
    for evidence in entries:
        if not isinstance(evidence, dict):
            continue
        if kind is not None and evidence.get("occupant", kind) != kind:
            continue
        group, frame = evidence.get("group"), evidence.get("frame_id")
        if (isinstance(group, Integral) and not isinstance(group, bool) and group >= 0
                and isinstance(frame, Integral) and not isinstance(frame, bool) and frame >= 0):
            groups.add(int(group))
    return groups


def _confidence(value):
    return (isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(value) and .70 <= value <= 1.)


def _relevant(candidate):
    if candidate.get("confirmable") is True:
        return True
    confidence = candidate.get("confidence")
    if isinstance(confidence, Real) and not isinstance(confidence, bool) and math.isfinite(confidence):
        return confidence >= .25
    return candidate.get("confirmable") is not False and candidate.get("kind") in TARGETS


def targets_readiness(layout, expected_inspirations=None):
    """Prove the complete target list while preserving unknown ordinary cells.

    expected_inspirations is the HUD total-minus-collected *upper bound*.
    Reaching it closes the inspiration list; below it all 54 occupancies must
    have actual confirmation. Ordinary node/page content is not required.
    """
    base = dict(ready=False, reason="targets_not_ready")
    if not isinstance(layout, dict):
        return dict(base, reason="invalid_layout")
    if (layout.get("schema") != "resonance_pc.deep_dive_layout.v1"
            or layout.get("coordinate_frame") != "scan_local"):
        return dict(base, reason="unsupported_target_layout")
    if layout.get("prediction_only"):
        return dict(base, reason="prediction_only_layout")
    try:
        cells = _cells(layout)
    except (ValueError, TypeError, KeyError) as error:
        return dict(base, reason=str(error))
    bound = layout.get("expected_inspirations") if expected_inspirations is None else expected_inspirations
    if not isinstance(bound, Integral) or isinstance(bound, bool) or not 0 <= bound <= 54:
        return dict(base, reason="inspiration_hud_unresolved")
    if (expected_inspirations is not None and "expected_inspirations" in layout
            and layout["expected_inspirations"] != bound):
        return dict(base, reason="inspiration_hud_contradiction")
    if layout.get("fusion_paused") or layout.get("map_valid") is False:
        return dict(base, reason="current_map_evidence_unavailable")
    observed = {row["face"] for row in cells.values() if _groups(row)}
    if observed != set(FACES) or layout.get("faces_observed") != 6:
        return dict(base, reason="six_faces_not_observed", observed_faces=sorted(observed))
    diagnostics = layout.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return dict(base, reason="current_glyph_anchor_required")
    age = layout.get("glyph_anchor_age_sec", diagnostics.get("glyph_anchor_age_sec"))
    anchor_reason = layout.get("glyph_anchor_reason", diagnostics.get("glyph_anchor_reason"))
    if (not isinstance(age, Real) or isinstance(age, bool) or not math.isfinite(age)
            or not 0 <= age <= 1.25 or not anchor_reason):
        return dict(base, reason="current_glyph_anchor_required")
    targets = {kind: [] for kind in TARGETS}
    occupancy_complete = True
    for slot, row in cells.items():
        kind, status = row.get("occupant"), row.get("occupant_status")
        counts = row.get("occupant_evidence_counts", {})
        if (not isinstance(counts, dict) or any(
                not isinstance(count, Integral) or isinstance(count, bool) or count < 0
                for count in counts.values())):
            return dict(base, reason="invalid_target_evidence", unresolved_cells=[slot])
        if status == "conflict":
            return dict(base, reason="target_occupancy_conflict", conflicting_cells=[slot])
        if status != "confirmed":
            occupancy_complete = False
            if any(counts.get(kind, 0) for kind in TARGETS):
                return dict(base, reason="unconfirmed_target_evidence", unresolved_cells=[slot])
            continue
        if kind not in ("none", *TARGETS):
            return dict(base, reason="invalid_confirmed_occupancy", unresolved_cells=[slot])
        if kind in TARGETS:
            count = counts.get(kind, 0)
            confidence = row.get("confidence", 0.)
            if count < 2 or len(_groups(row, kind)) < 2 or not _confidence(confidence):
                return dict(base, reason="independent_positive_target_evidence_required", unresolved_cells=[slot])
            targets[kind].append(slot)
        elif (counts.get("none", 0) < 3 or len(_groups(row, "none")) < 3
              or not _confidence(row.get("confidence", 0.))):
            # A flag without actual negative views cannot prove Boss consumption.
            occupancy_complete = False
    if len(targets["player"]) != 1 or len(targets["singularity"]) != 1:
        return dict(base, reason="unique_player_and_boss_required")
    inspirations = sorted(targets["inspiration"])
    if len(inspirations) > bound:
        return dict(base, reason="inspiration_count_exceeds_hud", confirmed_inspirations=len(inspirations), upper_bound=int(bound))
    if len(inspirations) < bound and not occupancy_complete:
        return dict(base, reason="inspiration_below_upper_bound_requires_complete_occupancy",
                    confirmed_inspirations=len(inspirations), upper_bound=int(bound))
    try:
        for field, actual in (("player_cell", targets["player"][0]), ("singularity_cell", targets["singularity"][0])):
            if layout.get(field) is None or cell_to_slot(layout[field]) != actual:
                return dict(base, reason=field + "_contradicts_target_evidence")
        declared = layout.get("inspiration_cells")
        if not isinstance(declared, list) or sorted(cell_to_slot(row) for row in declared) != inspirations:
            return dict(base, reason="inspiration_cells_contradict_target_evidence")
    except (ValueError, TypeError, KeyError):
        return dict(base, reason="invalid_declared_target_coordinates")
    unassociated = layout.get("unassociated_target_candidates", ())
    if not isinstance(unassociated, (list, tuple)):
        return dict(base, reason="invalid_target_candidate_evidence")
    if any(not isinstance(row, dict) or _relevant(row) for row in unassociated):
        return dict(base, reason="unassociated_target_candidates")
    associations = layout.get("target_candidate_associations", layout.get("mask_observation"))
    clues = layout.get("target_clues", ())
    if (not isinstance(clues, (list, tuple)) or any(not isinstance(row, dict) for row in clues)
            or associations is not None and (not isinstance(associations, (list, tuple))
                or any(not isinstance(row, dict) for row in associations))):
        return dict(base, reason="invalid_target_candidate_evidence")
    clues = [row for row in clues if _relevant(row)]
    if clues and associations is None:
        return dict(base, reason="target_candidate_associations_missing")
    if clues:
        relevant = [row for row in associations or () if isinstance(row, dict) and _relevant(row)]
        if any(sum(row.get("kind") == kind for row in clues) > sum(row.get("kind") == kind for row in relevant)
               for kind in TARGETS):
            return dict(base, reason="target_candidate_associations_incomplete")
    for candidate in associations or ():
        if not isinstance(candidate, dict) or not _relevant(candidate):
            continue
        slot, kind = candidate.get("cell_index"), candidate.get("kind")
        if (not isinstance(slot, Integral) or isinstance(slot, bool) or slot not in cells
                or kind not in targets or slot not in targets[kind]):
            return dict(base, reason="unassociated_target_candidates")
    return dict(ready=True, reason="targets_ready", player_slot=targets["player"][0],
                boss_slot=targets["singularity"][0], inspiration_slots=inspirations,
                upper_bound=int(bound), confirmed_inspirations=len(inspirations),
                occupancy_complete=occupancy_complete,
                target_list_complete_by="hud_upper_bound_reached" if len(inspirations) == bound else "all_occupancies_confirmed",
                observed_faces=sorted(observed))


def is_targets_layout(layout):
    return (isinstance(layout, dict) and layout.get("recognition_goal") == "targets"
            and layout.get("targets_ready") is True and layout.get("success") is True
            and layout.get("status") == "targets_ready" and layout.get("layout_complete") is False)


def required_cells(layout, action):
    """Ordinary move destinations need actual content before event execution."""
    if not isinstance(action, dict) or action.get("kind") not in ("move",):
        return []
    destination = action.get("destination_slot", action.get("destination"))
    slot = int(destination) if isinstance(destination, Integral) and not isinstance(destination, bool) else cell_to_slot(destination)
    cells = _cells(layout)
    if slot not in cells:
        raise ValueError("invalid_move_destination")
    row = cells[slot]
    if row.get("occupant_status") == "confirmed" and row.get("occupant") in TARGETS:
        return []
    known_ordinary = (row.get("occupant_status") == "confirmed" and row.get("occupant") == "none"
                      and row.get("node_status") == "known"
                      and (bool(row.get("icon_id")) or row.get("node_kind") == "empty"))
    return [] if known_ordinary else [slot]


def check_required_cells(layout, action):
    try:
        needed = required_cells(layout, action)
    except (ValueError, TypeError, KeyError) as error:
        return dict(ready=False, reason=str(error), required_cells=[])
    return dict(ready=not needed, reason="required_cells_unknown" if needed else "required_cells_ready", required_cells=needed)

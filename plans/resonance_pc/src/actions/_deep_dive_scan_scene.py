"""Current-frame player-board proof for scanning; never an action observer.

Every excluded page has a false necessary predicate from the original page
reader. A positive, missing or ambiguous probe falls back to that reader. There
is no scene cache or temporal board assumption here; existing exact-ROI matcher
caches retain their original pixel and dependency contracts.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from . import _deep_dive_planned_run_vision as page
from . import _deep_dive_single_run_vision as single
from . import _deep_dive_battle_vision as battle
from . import _deep_dive_event_vision as event


class _Unverified(Exception):
    pass


def _score(value) -> float:
    score = float(value)
    if not math.isfinite(score):
        raise _Unverified("nonfinite_probe")
    return score


def _player_board(rgb: np.ndarray) -> dict:
    if (not isinstance(rgb, np.ndarray) or rgb.shape != (720, 1280, 3)
            or rgb.dtype != np.uint8):
        raise _Unverified("invalid_frame")
    means, deviations = cv2.meanStdDev(rgb)
    variance = float(np.mean(deviations**2 + (means-float(means.mean()))**2))
    if not math.isfinite(variance) or variance < 4:
        raise _Unverified("blank_frame")
    # Keep each reader's native conversion, including legacy result_visual.
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    legacy_gray = cv2.cvtColor(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2GRAY)
    legacy, planned, negatives = {}, {}, {}

    def legacy_probe(name):
        if name not in legacy:
            row = single._match(legacy_gray, name)
            _score(row["score"])
            legacy[name] = row
        return legacy[name]

    def planned_probe(name):
        if name not in planned:
            row = page._match(gray, name)
            _score(row[0])
            planned[name] = row
        return planned[name]

    def absent(name, value, threshold):
        score = _score(value)
        if score >= threshold:
            raise _Unverified(name)
        negatives[name] = {"score": score, "threshold": threshold}

    if legacy_probe("player_turn")["score"] < .85:
        raise _Unverified("player_turn")
    if planned_probe("board_actions_heading")[0] < .85:
        raise _Unverified("board_actions_heading")
    plane = page._plane_icon(gray)
    if (plane.get("status") != "recognized" or plane.get("plane_index") not in (1, 2, 3)
            or set(plane.get("scores", {})) != {"1", "2", "3"}):
        raise _Unverified("plane")
    for score in plane["scores"].values():
        _score(score)

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    cyan = cv2.inRange(hsv, single.CYAN_LOW, single.CYAN_HIGH)
    move_cyan = int(cv2.countNonZero(single._crop(cyan, single.MOVE_BUTTON)))
    rotate_cyan = int(cv2.countNonZero(single._crop(cyan, single.ROTATE_BUTTON)))
    if move_cyan >= 2000 or rotate_cyan >= 2000:
        raise _Unverified("active_action_button")
    if page._rest_modal_evidence(rgb) is not None:
        raise _Unverified("unknown_modal")

    # Titles/controls below falsify one necessary term of every legacy overlay.
    for name, threshold in (("settlement_failure", .85), ("shop_title", .85),
                            ("event_vortex_choice_title", .85), ("event_enter", .80),
                            ("rotate_confirm", .80)):
        absent("legacy:"+name, legacy_probe(name)["score"], threshold)
    # No terminal can exist without either summary or details layout. The
    # followup alone falsifies the two-font insufficient-roles conjunction.
    for name, threshold in (("settlement_layout", .85), ("rest_title", .88),
                            ("insufficient_roles_followup", .82)):
        absent("planned:"+name, planned_probe(name)[0], threshold)
    absent("planned:settlement_details",
           page._legacy_cleanup_match(gray, "confirm", (510, 590, 790, 710))[0], .87)
    for name, region in (
        ("battle_formation_title", (500, 10, 790, 100)),
        ("battle_victory", (820, 70, 1140, 190)),
        ("battle_reward_title", (510, 8, 780, 90)),
        ("reward_obtained_title", (530, 50, 775, 145)),
    ):
        # detect_battle_page applies its threshold after four-decimal rounding.
        absent("battle:"+name, round(_score(battle.match(gray, name, region)[0]), 4), .85)
    for name, region, threshold in (
        ("selection_confirm", (500, 600, 810, 675), .85),
        ("result_exit", (500, 610, 790, 719), .90),
        ("result_exit_item", (500, 600, 790, 675), .90),
    ):
        absent("event:"+name, event.match(gray, name, region)[0], threshold)
    if event.heading_present(gray) is not False:
        raise _Unverified("event_heading")

    # Populate every non-score board output from this RGB, never old metadata.
    for name in ("move_pending", "move_done", "rotate_pending", "rotate_cancel"):
        legacy_probe(name)
    board_turn = _score(event.match(gray, "board_turn", (100, 100, 290, 200))[0])
    board_heading = _score(event.match(gray, "board_actions_heading", (1010, 320, 1270, 390))[0])
    yellow = cv2.inRange(hsv, np.array([18, 115, 120], np.uint8), np.array([40, 255, 255], np.uint8))
    white = cv2.inRange(hsv, np.array([0, 0, 185], np.uint8), np.array([179, 85, 255], np.uint8))
    rotate_done = cv2.countNonZero(single._crop(yellow, single.ROTATE_BUTTON)) >= 500
    rotation_options = page._rotation_arrows(cyan)
    flags = {name: bool(legacy[name]["score"] >= .85)
             for name in ("move_pending", "move_done", "rotate_pending")}
    return {
        "valid": True, "scene": "board", "options": [], "player_turn": True,
        "scores": {name: row["score"] for name, row in legacy.items()},
        "legacy_probes_not_evaluated": [name for name in single.REGIONS if name not in legacy],
        "planned_scores": {name: round(row[0], 4) for name, row in planned.items()},
        "planned_probes_not_evaluated": [name for name in page.REGIONS
                                        if not name.startswith("plane_") and name not in planned],
        "result_visual": cv2.resize(legacy_gray[75:510, 485:795], (24, 24),
                                    interpolation=cv2.INTER_AREA).flatten().tolist(),
        "rotate_confirm_point": legacy["rotate_confirm"]["point"],
        "rotate_cancel_point": legacy["rotate_cancel"]["point"],
        **flags, "event_board": board_turn >= .85 and board_heading >= .85,
        "cyan_pixels": {"move": move_cyan, "rotate": rotate_cyan},
        "rotate_done": bool(rotate_done), "raw_move_options": page._tile_frames(cyan, "cyan"),
        "raw_rotation_options": rotation_options,
        "yellow_frame_candidates": page._tile_frames(yellow, "yellow_unowned"),
        "white_frame_candidates": page._tile_frames(white, "white_unowned"),
        "fresh_scan_ready": True, "enemy_turn": False,
        "plane_evidence": plane, "plane_index": plane["plane_index"],
        "selected_cell_evidence": {
            "status": "absent", "point": None, "candidates": [],
            "reason": "client_entry_selection_has_no_distinct_tile_marker",
            "evidence_mode": "unsupported_by_client_visuals"},
        "battle_defeat_detection": "unsupported",
        "rotation_mode_evidence": {
            "active_button": False, "player_turn": True,
            "cyan_arrow_count": len(rotation_options), "arrow_roi": list(page.BOARD_ROI)},
        "single_action_flags": {**flags, "rotate_done": bool(rotate_done),
                                "requires_known_single_action_baseline": True},
        "scan_scene_evidence": {
            "schema": "resonance_pc.deep_dive_scan_scene.v1", "source": "current_rgb",
            "mode": "original_predicate_short_circuit", "excluded_pages": negatives,
            "event_heading_absent": True, "unknown_modal_absent": True,
            "action_authorization": False},
    }


def observe(rgb: np.ndarray, event_family: str = "healing") -> dict:
    """Observe for scanning only; ambiguous/other pages use full public vision.

    Partial score maps contain only evaluated probes. Callers requiring every
    legacy score or action planning must keep using the public page observer.
    """
    if event_family == "healing":
        try:
            return _player_board(rgb)
        except Exception:
            # Missing templates and nonfinite probes cannot grant the shortcut.
            pass
    try:
        return page.observe(rgb, event_family=event_family)
    except Exception as exc:
        return {"valid": False, "scene": "unknown", "options": [], "scores": {},
                "fresh_scan_ready": False,
                "scan_scene_evidence": {"mode": "full_observation_failed",
                                        "error_type": type(exc).__name__,
                                        "action_authorization": False}}


__all__ = ["observe"]

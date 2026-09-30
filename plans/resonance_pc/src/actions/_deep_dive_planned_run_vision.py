"""Input-free page/HUD observations for a planned, multi-plane Deep Dive run.

Every result describes one frame. The caller owns temporal stability, action
ownership and input confirmation. In particular, an entry card has no distinct
selected-tile marker in this client: coloured tile frames never prove its slot.
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
from typing import Any

import cv2
import numpy as np

from . import _deep_dive_single_run_vision as single
from . import _deep_dive_hud_templates as hud_templates


ROOT = Path(__file__).resolve().parents[2] / "templates/deep_dive_planned_run"
BOARD_ROI = (295, 80, 955, 620)
REGIONS = {
    "settlement_victory": (430, 0, 850, 195),
    "settlement_failure": (430, 0, 850, 195),
    "settlement_layout": (480, 600, 790, 719),
    "rest_title": (500, 0, 790, 105),
    "rest_next": (780, 100, 1260, 330),
    "boss_entry_title": (970, 265, 1235, 350),
    "insufficient_roles": (150, 225, 1130, 500),
    "insufficient_roles_followup": (150, 225, 1130, 500),
    "enemy_turn_title": (95, 105, 350, 195),
    "board_actions_heading": (1010, 320, 1270, 390),
    "plane_1": (580, 0, 705, 80),
    "plane_2": (580, 0, 705, 80),
    "plane_3": (580, 0, 705, 80),
}
# Bounds originate in the native 1920x1080 main prefab, at canvas scale 2/3.
# Overflowing/right-aligned text needs padding beyond its RectTransform bounds.
HUD_REGIONS = {
    "plane": (495, 65, 790, 105),
    # The number sits inside the cyan diamond. Its lower ACTION caption and
    # diamond edges are separate artwork, and must not enter numeric OCR.
    "rounds": (1190, 128, 1231, 160),
    "moves": (1165, 386, 1250, 432),
    "rotations": (1165, 457, 1250, 505),
    "inspirations": (990, 245, 1278, 292),
    "popup": (140, 225, 1140, 500),
    "entry_title": (970, 265, 1235, 350),
    "turn_label": (90, 115, 350, 185),
}
PLANE_NAMES = {"情感宣泄": 1, "思维解析": 2, "意义同化": 3}
BOARD_SCENES = frozenset({"board", "choose_move", "choose_rotate", "rotate_preview"})
# Static client proof: RubikCube.ShowCubeIcon (367-414) reads the TagFactory
# icon and binds that exact Texture2D to grid/Icon material _MainTex. The native
# source glyphs and Tag records are retained in templates/.../sources.json.
# The scan's orange_triple_eye family is deliberately absent: native boss and
# elite glyphs have different eye counts, and that family is not elite-only.
NODE_EVENT_TYPES = {
    "white_diamond": "empty", "yellow_hex": "item", "blue_scales": "shop",
    "green_burst": "healing", "purple_ring": "vortex", "red_single_eye": "battle",
}


@lru_cache(maxsize=None)
def _template(name: str) -> tuple[np.ndarray, np.ndarray | None]:
    path = ROOT / (name + ".png")
    source = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if source is None:
        raise FileNotFoundError(path)
    if source.ndim == 2:
        return source, None
    gray = cv2.cvtColor(source[:, :, :3], cv2.COLOR_BGR2GRAY)
    mask = None
    if source.shape[2] == 4:
        alpha = source[:, :, 3]
        # Include a narrow background ring. Matching only white glyph interiors
        # would also accept a uniform white patch as text.
        mask = cv2.dilate((alpha >= 24).astype(np.uint8) * 255,
                          np.ones((5, 5), np.uint8))
        gray = np.round(gray.astype(np.float32) * alpha.astype(np.float32) / 255).astype(np.uint8)
    return gray, mask


def _match(gray: np.ndarray, name: str) -> tuple[float, list[int] | None]:
    x1, y1, x2, y2 = REGIONS[name]
    reference, mask = _template(name)
    roi = gray[y1:y2, x1:x2]
    if roi.shape[0] < reference.shape[0] or roi.shape[1] < reference.shape[1]:
        return 0., None
    response = cv2.matchTemplate(roi, reference, cv2.TM_CCOEFF_NORMED, mask=mask)
    if not np.isfinite(response).any():
        return 0., None
    response = np.where(np.isfinite(response), response, -1.).astype(np.float32)
    _, score, _, (x, y) = cv2.minMaxLoc(response)
    return float(score), [x1+x+reference.shape[1]//2, y1+y+reference.shape[0]//2]


def _plane_icon(gray: np.ndarray) -> dict:
    values = [(i, *_match(gray, "plane_" + str(i))) for i in range(1, 4)]
    values.sort(key=lambda row: -row[1])
    accepted = values[0][1] >= .88 and values[0][1]-values[1][1] >= .08
    return {"plane_index": values[0][0] if accepted else None,
            "status": "recognized" if accepted else "unknown",
            "scores": {str(i): round(score, 4) for i, score, _ in values}}


def _tile_frames(mask: np.ndarray, kind: str) -> list[dict]:
    x0, y0, x2, y2 = BOARD_ROI
    contours, _ = cv2.findContours(mask[y0:y2, x0:x2], cv2.RETR_LIST,
                                  cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if not (75 <= width <= 360 and 35 <= height <= 175 and area >= 650):
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, .035 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        candidates.append({"point": [int(x0+x+width/2), int(y0+y+height/2)],
                           "box": [x+x0, y+y0, width, height],
                           "quad": (polygon[:, 0, :] + [x0, y0]).astype(int).tolist(),
                           "evidence": int(area), "frame_color": kind})
    return single._dedupe(candidates)


def _rotation_arrows(mask: np.ndarray) -> list[dict]:
    x0, y0, x2, y2 = BOARD_ROI
    count, _, stats, centers = cv2.connectedComponentsWithStats(mask[y0:y2, x0:x2])
    result = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if not (20 <= width <= 95 and 8 <= height <= 55 and 150 <= area <= 2400):
            continue
        result.append({"point": [round(float(centers[index, 0])+x0),
                                 round(float(centers[index, 1])+y0)],
                       "box": [x+x0, y+y0, width, height], "evidence": area})
    return single._dedupe(result)


def _terminal(result: dict, outcome: str, evidence: dict) -> dict:
    result.update(scene="settlement", outcome=outcome, options=[],
                  fresh_scan_ready=False, terminal_evidence=evidence)
    # Settlement is observation-only even if an inherited page provided a point.
    for key in tuple(result):
        if key == "click" or key.endswith("_point") or key == "next_point":
            result.pop(key, None)
    return result


def _rest_modal_evidence(rgb: np.ndarray) -> dict | None:
    """Conservatively flag an unclassified central white message on a dark band.

    Native common/Tips centers 36pt white text inside a 100px-high band. This
    probe can only freeze input; it never classifies the message or clicks it.
    """
    region = (200, 315, 1080, 405)
    x1, y1, x2, y2 = region
    roi = rgb[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    white = ((hsv[:, :, 1] < 70) & (hsv[:, :, 2] >= 185)).astype(np.uint8)
    count, _, stats, centers = cv2.connectedComponentsWithStats(white)
    glyphs = [i for i in range(1, count)
              if 14 <= stats[i, cv2.CC_STAT_HEIGHT] <= 30
              and 4 <= stats[i, cv2.CC_STAT_WIDTH] <= 34
              and stats[i, cv2.CC_STAT_AREA] >= 25]
    for index in glyphs:
        line = [i for i in glyphs if abs(centers[i, 1]-centers[index, 1]) <= 6]
        if len(line) < 4:
            continue
        left = min(stats[i, cv2.CC_STAT_LEFT] for i in line)
        right = max(stats[i, cv2.CC_STAT_LEFT]+stats[i, cv2.CC_STAT_WIDTH] for i in line)
        center = (left+right)/2+x1
        dark_fraction = float(np.mean(hsv[15:75, :, 2] <= 70))
        if right-left >= 80 and abs(center-640) <= 150 and dark_fraction >= .65:
            return {"source": "native_tips_white_text_band_geometry", "region": list(region),
                    "glyph_count": len(line), "dark_fraction": round(dark_fraction, 4),
                    "text_span": [int(left+x1), int(right+x1)], "action": "freeze_only"}
    return None


def observe(rgb: np.ndarray, event_family: str = "healing") -> dict:
    """Extend legacy page observations without input or temporal assumptions."""
    result = single.observe(rgb, event_family=event_family)
    result.update(raw_move_options=[], raw_rotation_options=[],
                  yellow_frame_candidates=[], white_frame_candidates=[],
                  fresh_scan_ready=False, enemy_turn=False, plane_index=None,
                  selected_cell_evidence={
                      "status": "absent", "point": None, "candidates": [],
                      "reason": "client_entry_selection_has_no_distinct_tile_marker",
                      "evidence_mode": "unsupported_by_client_visuals"},
                  battle_defeat_detection="unsupported")
    if not result.get("valid"):
        return result
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    probes = {name: _match(gray, name) for name in REGIONS if not name.startswith("plane_")}
    result["planned_scores"] = {name: round(value[0], 4) for name, value in probes.items()}
    # Title and a distinct summary/details layout marker are both required.
    summary = probes["settlement_layout"][0] >= .85
    detail_score, _ = _legacy_cleanup_match(gray, "confirm", (510, 590, 790, 710))
    layout = summary or detail_score >= .87
    failure = max(probes["settlement_failure"][0],
                  float(result.get("scores", {}).get("settlement_failure", 0.)))
    victory = probes["settlement_victory"][0]
    if layout and max(failure, victory) >= .86:
        outcome = ("victory" if victory >= .86 and victory-failure >= .06 else
                   "failure" if failure >= .86 and failure-victory >= .06 else "unknown")
        return _terminal(result, outcome,
                         {"failure_title": failure, "victory_title": victory,
                          "summary_layout": summary, "details_layout_score": detail_score})
    if result.get("scene") == "settlement":
        # A lone legacy failure title cannot become a terminal decision.
        result.update(scene="uncertain_settlement", outcome="unknown", options=[])
        result.pop("click", None)
        return result
    if (probes["insufficient_roles"][0] >= .86
            and probes["insufficient_roles_followup"][0] >= .82):
        result.update(scene="insufficient_battle_roles", options=[],
                      popup_evidence="native_font_body_and_followup")
        result.pop("click", None)
        return result
    if probes["rest_title"][0] >= .88 and probes["rest_next"][0] >= .85:
        modal = _rest_modal_evidence(rgb)
        result.update(scene="rest_area", options=[], next_point=probes["rest_next"][1],
                      click=probes["rest_next"][1])
        if modal is not None:
            result["unknown_modal_evidence"] = modal
            result.pop("click", None)
            result.pop("next_point", None)
        return result
    if (float(result.get("scores", {}).get("event_enter", 0.)) >= .80
            and probes["boss_entry_title"][0] >= .86):
        entry = result.get("entry_card") or {}
        ordinary = max((v.get("title", 0.) for k, v in entry.get("scores", {}).items()
                        if k in {"battle", "elite_battle"} and isinstance(v, dict)), default=0.)
        if probes["boss_entry_title"][0]-ordinary >= .06:
            point = single._match(gray, "event_enter")["point"]
            result.update(scene="event_entry", entry_kind="boss_battle", event_type="boss_battle",
                          click=point, entry_card={"status": "recognized", "kind": "boss_battle",
                                                  "confidence": probes["boss_entry_title"][0],
                                                  "enter_point": point,
                                                  "evidence_mode": "boss_title_and_enter"})
            return result
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    cyan = cv2.inRange(hsv, single.CYAN_LOW, single.CYAN_HIGH)
    yellow = cv2.inRange(hsv, np.array([18, 115, 120], np.uint8),
                         np.array([40, 255, 255], np.uint8))
    white = cv2.inRange(hsv, np.array([0, 0, 185], np.uint8),
                        np.array([179, 85, 255], np.uint8))
    result["raw_move_options"] = _tile_frames(cyan, "cyan")
    result["raw_rotation_options"] = _rotation_arrows(cyan)
    result["yellow_frame_candidates"] = _tile_frames(yellow, "yellow_unowned")
    result["white_frame_candidates"] = _tile_frames(white, "white_unowned")
    result["plane_evidence"] = _plane_icon(gray)
    result["plane_index"] = result["plane_evidence"]["plane_index"]
    # Raw geometry is returned on all frames, but overlays retain scene priority.
    if result.get("scene") not in BOARD_SCENES | {"unknown"}:
        return result
    # Two independent positive clues: a specific enemy phase label and the
    # board's action heading. player_turn=False alone proves nothing.
    if (probes["enemy_turn_title"][0] >= .85
            and probes["board_actions_heading"][0] >= .85
            and not result.get("player_turn") and result.get("scene") != "rotate_preview"):
        result.update(scene="enemy_turn", enemy_turn=True, event_board=True,
                      player_turn=False, options=[],
                      enemy_turn_evidence={"title": probes["enemy_turn_title"][0],
                                           "board_heading": probes["board_actions_heading"][0]})
        result.pop("click", None)
        return result
    result["rotate_cancel_point"] = single._match(gray, "rotate_cancel")["point"]
    move_cyan = int(cv2.countNonZero(single._crop(cyan, single.MOVE_BUTTON)))
    rot_cyan = int(cv2.countNonZero(single._crop(cyan, single.ROTATE_BUTTON)))
    result["cyan_pixels"] = {"move": move_cyan, "rotate": rot_cyan}
    result["rotate_done"] = cv2.countNonZero(single._crop(yellow, single.ROTATE_BUTTON)) >= 500
    result["rotation_mode_evidence"] = {
        "active_button": rot_cyan >= 2000 and move_cyan < 2000,
        "player_turn": bool(result.get("player_turn")),
        "cyan_arrow_count": len(result["raw_rotation_options"]),
        "arrow_roi": list(BOARD_ROI),
    }
    if result.get("scene") != "rotate_preview" and result.get("player_turn"):
        if move_cyan >= 2000 and rot_cyan < 2000 and result["raw_move_options"]:
            result.update(scene="choose_move", options=result["raw_move_options"])
        elif rot_cyan >= 2000 and move_cyan < 2000 and result["raw_rotation_options"]:
            # The legacy arrow ROI only covers the high rows of the board.
            # A lower-row pawn is still in rotation mode when the active button,
            # player phase and freshly parsed cyan arrows independently agree.
            result.update(scene="choose_rotate", options=result["raw_rotation_options"])
    result["fresh_scan_ready"] = bool(result.get("scene") == "board" and result.get("player_turn"))
    result["single_action_flags"] = {
        "move_pending": bool(result.get("move_pending")),
        "move_done": bool(result.get("move_done")),
        "rotate_pending": bool(result.get("rotate_pending")),
        "rotate_done": bool(result.get("rotate_done")),
        "requires_known_single_action_baseline": True,
    }
    return result


@lru_cache(maxsize=None)
def _cleanup_template(name: str) -> np.ndarray:
    image = cv2.imread(str(ROOT.parent / "deep_dive_cleanup" / (name + ".png")), 0)
    if image is None:
        raise FileNotFoundError(name)
    return image


def _legacy_cleanup_match(gray, name, region):
    reference = _cleanup_template(name)
    x1, y1, x2, y2 = region
    roi = gray[y1:y2, x1:x2]
    if roi.shape[0] < reference.shape[0] or roi.shape[1] < reference.shape[1]:
        return 0., None
    response = cv2.matchTemplate(roi, reference, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(response)
    return (float(score) if math.isfinite(score) else 0.,
            [x1+x+reference.shape[1]//2, y1+y+reference.shape[0]//2])


def _ocr_text(rgb: np.ndarray, ocr: Any, region: tuple[int, int, int, int]) -> str:
    x1, y1, x2, y2 = region
    crop = cv2.resize(rgb[y1:y2, x1:x2], None, fx=3, fy=3,
                      interpolation=cv2.INTER_CUBIC)
    response = ocr.recognize_all(source_image=crop)
    items = response.get("results", ()) if isinstance(response, dict) else getattr(response, "results", ())
    words = []
    for item in items or ():
        text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        confidence = item.get("confidence") if isinstance(item, dict) else getattr(item, "confidence", None)
        if confidence is not None and isinstance(confidence, (int, float)) and confidence < .65:
            continue
        words.append(str(text or ""))
    return " ".join(words)


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("／", "/").replace("（", "(").replace("）", ")")


@lru_cache(maxsize=31)
def _round_template(value: int) -> np.ndarray:
    source = cv2.imread(str(ROOT / "rounds" / f"{value}.png"), cv2.IMREAD_GRAYSCALE)
    if source is None:
        raise FileNotFoundError(f"native_round_template:{value}")
    points = cv2.findNonZero((source >= 96).astype(np.uint8))
    x, y, width, height = cv2.boundingRect(points)
    return source[y:y+height, x:x+width]


def read_rounds_template(rgb: np.ndarray) -> dict:
    """Match the entire cyan numeral against native BebasNeue glyphs 0..30.

    Width/height gating prevents a single glyph from matching a substring of a
    two-digit budget. A missing or ambiguous glyph remains unknown; no OCR.
    """
    x1, y1, x2, y2 = HUD_REGIONS["rounds"]
    roi = rgb[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, np.array([75, 80, 180], np.uint8),
                       np.array([105, 255, 255], np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    components = [i for i in range(1, count)
                  if 16 <= stats[i, cv2.CC_STAT_HEIGHT] <= 34
                  and 3 <= stats[i, cv2.CC_STAT_WIDTH] <= 35
                  and stats[i, cv2.CC_STAT_AREA] >= 30]
    clean = np.isin(labels, components).astype(np.uint8) * 255
    points = cv2.findNonZero(clean)
    result = {"value": None, "status": "unknown", "source": "native_round_glyph_templates",
              "roi": list(HUD_REGIONS["rounds"]), "scores": []}
    if points is None:
        result["reason"] = "cyan_numeral_absent"
        return result
    x, y, width, height = cv2.boundingRect(points)
    observed = clean[y:y+height, x:x+width]
    normalized = cv2.resize(observed, (32, 64), interpolation=cv2.INTER_AREA)
    scores = []
    for value in range(31):
        reference = _round_template(value)
        if abs(reference.shape[1]-width) > 3 or abs(reference.shape[0]-height) > 3:
            continue
        binary = (reference >= 96).astype(np.uint8) * 255
        comparable = cv2.resize(binary, (32, 64), interpolation=cv2.INTER_AREA)
        score = float(cv2.matchTemplate(normalized, comparable, cv2.TM_CCOEFF_NORMED)[0, 0])
        if math.isfinite(score):
            scores.append((score, value))
    scores.sort(reverse=True)
    result["scores"] = [{"value": value, "confidence": round(score, 4)} for score, value in scores[:3]]
    result["glyph_box"] = [x1+x, y1+y, width, height]
    margin = scores[0][0]-scores[1][0] if len(scores) > 1 else 1. if scores else 0.
    result["margin"] = round(margin, 4)
    if scores and scores[0][0] >= .72 and margin >= .10:
        result.update(value=scores[0][1], status="recognized", confidence=round(scores[0][0], 4))
    else:
        result["reason"] = "native_numeral_low_confidence_or_ambiguous"
    return result


def _pair(text: str) -> tuple[int, int] | None:
    pairs = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", _normal(text))
    if len(pairs) != 1:
        return None
    used, total = map(int, pairs[0])
    return (used, total) if 0 <= used <= total and 1 <= total <= 20 else None


def read_hud(rgb: np.ndarray, ocr: Any, *, observation: dict | None = None) -> dict:
    """Read fixed HUD ROIs. None means unknown; never derive a round budget.

    Numeric pairs are read directly from the UI. Colour flags are returned as
    a separate, explicitly conditional fallback and never invent numeric totals.
    OCR work is selected from the same-frame scene. Known battle/reward/event
    overlays need no numeric HUD read. Skipped fields remain explicitly unknown.
    """
    fields = ("plane_index", "rounds_remaining", "moves_used", "moves_total",
              "rotations_used", "rotations_total", "collected_count", "inspiration_total")
    result = {name: None for name in fields}
    result.update(schema="resonance_pc.deep_dive_hud.v1", plane_name=None,
                  raw_text={}, errors={}, known_fields=[], unknown_fields=list(fields),
                  insufficient_battle_roles=False, boss_entry_title_confirmed=False,
                  evidence_source={})
    observation = observation if observation is not None else observe(rgb)
    result["scene"] = observation.get("scene", "unknown")
    result["single_action_flags"] = observation.get("single_action_flags", {})
    if not observation.get("valid"):
        result["status"] = "invalid_frame"
        return result
    scene = observation.get("scene", "unknown")
    board = scene in BOARD_SCENES
    if board:
        icon = observation.get("plane_evidence", {}).get("plane_index")
        if icon is not None:
            result["plane_index"] = icon
            result["evidence_source"]["plane_index"] = "native_plane_icon"
        rounds_evidence = read_rounds_template(rgb)
        result["rounds_template_evidence"] = rounds_evidence
        result["rounds_remaining"] = rounds_evidence["value"]
        if rounds_evidence["value"] is not None:
            result["evidence_source"]["rounds_remaining"] = "native_round_glyph_templates"
        pairs = hud_templates.read_numeric_pairs(rgb)
        result["numeric_pair_template_evidence"] = pairs.get("evidence", {})
        for field in ("moves_used", "moves_total", "rotations_used", "rotations_total",
                      "collected_count", "inspiration_total"):
            if pairs.get(field) is not None:
                result[field] = pairs[field]
                result["evidence_source"][field] = "native_font_pair_templates"
        names = (["plane"] if icon is None else []) + [
            name for name, used, total in (
                ("moves", "moves_used", "moves_total"),
                ("rotations", "rotations_used", "rotations_total"),
                ("inspirations", "collected_count", "inspiration_total"))
            if result[used] is None or result[total] is None]
    elif scene == "ambiguous_event_entry":
        names = ["entry_title", "popup"]
    elif scene == "unknown":
        names = ["turn_label", "popup", "entry_title"]
    elif scene == "rest_area" and observation.get("unknown_modal_evidence"):
        names = ["popup"]
    else:
        names = []
    result["ocr_regions_requested"] = list(names)
    result["ocr_regions_skipped"] = {
        name: "not_applicable_for_scene" for name in HUD_REGIONS if name not in names}
    for name in names:
        if ocr is None:
            result["errors"][name] = "ocr_unavailable"
            continue
        try:
            result["raw_text"][name] = _ocr_text(rgb, ocr, HUD_REGIONS[name])
        except Exception as exc:
            result["errors"][name] = str(exc)
    popup = _normal(result["raw_text"].get("popup", ""))
    result["insufficient_battle_roles"] = (
        observation.get("scene") == "insufficient_battle_roles"
        or ("不足5人" in popup and "复苏" in popup and "招募" in popup))
    entry = _normal(result["raw_text"].get("entry_title", ""))
    result["boss_entry_title_confirmed"] = "位面奇点战斗" in entry
    result["enemy_turn"] = bool(observation.get("enemy_turn"))
    # The native prefab says "位面奇点行动中" (Group_BossMove/Txt_, font size
    # 28). An OCR result clipped at its left edge may retain the exact specific
    # suffix "奇点行动中". Neither form can match "玩家行动中"; both still
    # require independent board-heading evidence.
    turn = _normal(result["raw_text"].get("turn_label", ""))
    if (observation.get("scene") in BOARD_SCENES | {"unknown", "enemy_turn"}
            and turn in {"位面奇点行动中", "奇点行动中"}
            and observation.get("planned_scores", {}).get("board_actions_heading", 0.) >= .85
            and not observation.get("player_turn")):
        result.update(scene="enemy_turn", enemy_turn=True, event_board=True)
        board = False
    if result["insufficient_battle_roles"]:
        result["scene"] = "insufficient_battle_roles"
        board = False
    elif (result["boss_entry_title_confirmed"]
          and observation.get("scene") in {"event_entry", "ambiguous_event_entry"}
          and observation.get("scores", {}).get("event_enter", 0.) >= .80):
        result.update(scene="event_entry", entry_kind="boss_battle", event_type="boss_battle")
        board = False
    if board:
        plane = _normal(result["raw_text"].get("plane", ""))
        matched = [(name, index) for name, index in PLANE_NAMES.items() if name in plane]
        icon = observation.get("plane_evidence", {}).get("plane_index")
        if len(matched) == 1 and (icon is None or icon == matched[0][1]):
            result.update(plane_name=matched[0][0], plane_index=matched[0][1])
            result["evidence_source"]["plane_index"] = "ocr_name"
        elif not matched and icon is not None:
            result["plane_index"] = icon
            result["evidence_source"]["plane_index"] = "native_plane_icon"
        elif matched and icon is not None:
            result["errors"]["plane"] = "plane_name_icon_conflict"
        for roi, used_field, total_field in (("moves", "moves_used", "moves_total"),
                                            ("rotations", "rotations_used", "rotations_total"),
                                            ("inspirations", "collected_count", "inspiration_total")):
            pair = _pair(result["raw_text"].get(roi, ""))
            if pair:
                result[used_field], result[total_field] = pair
                result["evidence_source"][used_field] = "ocr_pair"
                result["evidence_source"][total_field] = "ocr_pair"
    result["known_fields"] = [name for name in fields if result[name] is not None]
    result["unknown_fields"] = [name for name in fields if result[name] is None]
    result["status"] = "complete" if not result["unknown_fields"] else "partial"
    return result


def enrich_observation(observation: dict, hud: dict) -> dict:
    """Merge same-frame OCR page evidence; never change a terminal observation.

    The caller must pass the HUD read from this observation's original RGB frame.
    This function never copies HUD numbers into page/control geometry.
    """
    result = dict(observation)
    if result.get("scene") in {"settlement", "uncertain_settlement"} or not result.get("valid"):
        return result
    if hud.get("insufficient_battle_roles") is True:
        result.update(scene="insufficient_battle_roles", options=[], fresh_scan_ready=False,
                      popup_evidence="exact_native_tips_text_or_native_font_pair")
        for name in tuple(result):
            if name == "click" or name.endswith("_point"):
                result.pop(name, None)
        return result
    if result.get("unknown_modal_evidence"):
        result.update(scene="unknown", options=[], fresh_scan_ready=False,
                      modal_unclassified=True)
        for name in tuple(result):
            if name == "click" or name.endswith("_point"):
                result.pop(name, None)
        return result
    if (hud.get("enemy_turn") is True
            and result.get("scene") in BOARD_SCENES | {"unknown", "enemy_turn"}
            and result.get("planned_scores", {}).get("board_actions_heading", 0.) >= .85
            and not result.get("player_turn")):
        result.update(scene="enemy_turn", enemy_turn=True, event_board=True,
                      fresh_scan_ready=False, options=[])
        result.pop("click", None)
        return result
    if (hud.get("boss_entry_title_confirmed") is True
            and result.get("scene") in {"event_entry", "ambiguous_event_entry"}
            and result.get("scores", {}).get("event_enter", 0.) >= .80):
        entry = result.get("entry_card") or {}
        point = entry.get("enter_point") or result.get("click")
        if isinstance(point, (tuple, list)) and len(point) == 2:
            result.update(scene="event_entry", event_type="boss_battle", entry_kind="boss_battle",
                          fresh_scan_ready=False, click=list(point),
                          entry_card={"status": "recognized", "kind": "boss_battle",
                                      "enter_point": list(point),
                                      "evidence_mode": "exact_ocr_boss_title_and_same_frame_enter"})
    return result


__all__ = ["observe", "read_hud", "read_rounds_template", "enrich_observation", "HUD_REGIONS", "NODE_EVENT_TYPES"]

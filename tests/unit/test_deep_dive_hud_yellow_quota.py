"""Real completed rotation glyphs, with strict unknown and colour boundaries."""
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_hud_templates as reader
from plans.resonance_pc.src.actions import _deep_dive_planned_run_vision as vision

FIXTURES = Path(__file__).resolve().parents[1]/"fixtures"/"deep_dive_hud"
METADATA = json.loads((FIXTURES/"yellow_rotation_actual_rois.json").read_text(encoding="utf8"))


def actual_rgb(index=1):
    # Only these exact source ROIs are needed by read_hud's numeric readers.
    rgb = np.zeros((720,1280,3), np.uint8)
    with np.load(FIXTURES/"yellow_rotation_actual_rois.npz") as data:
        for name, (x1,y1,x2,y2) in METADATA["regions"].items():
            rgb[y1:y2,x1:x2] = data[f"pre{index}_{name}"]
    return rgb


def rotation(rgb):
    values = reader.read_numeric_pairs(rgb)
    return (values["rotations_used"], values["rotations_total"]), values["evidence"]["rotations"]


@pytest.mark.parametrize("index", [1,2])
def test_real_pre_frames_read_all_eight_values_without_ocr(index):
    record = METADATA["frames"][index-1]
    hud = vision.read_hud(actual_rgb(index), None, observation=record["observation"])
    assert hud["status"] == "complete" and not hud["ocr_regions_requested"]
    assert {name:hud[name] for name in record["expected"]} == record["expected"]
    evidence = hud["numeric_pair_template_evidence"]["rotations"]
    assert evidence["text"] == record["ocr_rotation_text"] == "1/1"
    assert evidence["foreground"] == "yellow_rotation_text"
    assert [g["match"]["character"] for g in evidence["glyphs"]] == ["1","/","1"]
    assert [g["box"] for g in evidence["glyphs"]] == [[1195,471,6,16],[1206,471,8,16],[1217,471,6,16]]
    assert min(g["match"]["confidence"] for g in evidence["glyphs"]) >= .76
    assert min(g["match"]["margin"] for g in evidence["glyphs"]) >= .045


@pytest.mark.parametrize("mutation", ["empty", "missing_slash", "missing_right", "extra_slash",
                                      "ambiguous_extra", "conflicting_white", "filled", "shifted_prefix"])
def test_incomplete_extra_conflicting_or_non_glyph_is_unknown(mutation):
    rgb = actual_rgb()
    slash = rgb[471:487,1206:1214].copy()
    if mutation == "empty":
        rgb[465:493,1189:1250] = 0
    elif mutation == "missing_slash":
        rgb[471:487,1206:1214] = 0
    elif mutation == "missing_right":
        rgb[471:487,1217:1223] = 0
    elif mutation in ("extra_slash", "conflicting_white"):
        if mutation == "conflicting_white":
            hsv = cv2.cvtColor(slash, cv2.COLOR_RGB2HSV)
            mask = (hsv[:,:,0]>=20)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>=100)&(hsv[:,:,2]>=145)
            slash[:] = 0
            slash[mask] = 255
        rgb[471:487,1232:1240] = slash
    elif mutation == "ambiguous_extra":
        rgb[471:487,1232:1239] = [255,220,0]
    elif mutation == "filled":
        rgb[471:487,1195:1223] = [255,220,0]
    elif mutation == "shifted_prefix":
        one = rgb[471:487,1195:1201].copy()
        rgb[465:493,1165:1189] = 0
        rgb[471:487,1186:1192] = one
    pair,evidence = rotation(rgb)
    assert pair == (None,None), (mutation,evidence)
    assert evidence["status"] == "unknown"


def test_completed_circle_alone_cannot_supply_numbers():
    rgb = actual_rgb()
    rgb[457:505,1189:1250] = 0
    assert rotation(rgb)[0] == (None,None)


def test_yellow_enabled_for_rotation_only_and_does_not_assume_one_one():
    rgb = actual_rgb()
    # Actual movement 0/1 glyph pixels recoloured yellow, with no check icon.
    move = rgb[386:432,1165:1250].copy()
    hsv = cv2.cvtColor(move,cv2.COLOR_RGB2HSV)
    white = (hsv[:,:,1]<=80)&(hsv[:,:,2]>=145)
    yellow = np.zeros_like(move)
    yellow[white] = [255,220,0]
    rgb[457:505,1165:1250] = 0
    rgb[457:503,1165:1250] = yellow
    assert rotation(rgb)[0] == (0,1)
    rgb[386:432,1165:1250] = yellow
    values = reader.read_numeric_pairs(rgb)
    assert values["moves_used"] is None and values["moves_total"] is None


@pytest.mark.parametrize("style", ["white", "dark_on_cyan"])
def test_original_white_and_verified_cyan_paths_still_read_glyphs(style):
    rgb = actual_rgb()
    roi = rgb[457:505,1165:1250]
    hsv = cv2.cvtColor(roi,cv2.COLOR_RGB2HSV)
    glyph = (hsv[:,:,0]>=20)&(hsv[:,:,0]<=40)&(hsv[:,:,1]>=100)&(hsv[:,:,2]>=145)
    glyph[:,:24] = False
    # Use only the three audited components, excluding decorative border.
    numeric = np.zeros_like(glyph)
    numeric[14:30,30:58] = glyph[14:30,30:58]
    if style == "white":
        roi[:] = 0
        roi[numeric] = 255
    else:
        roi[:] = [0,220,255]
        roi[numeric] = 0
    pair,evidence = rotation(rgb)
    assert pair == (1,1)
    assert evidence["foreground"] == ("light" if style=="white" else "dark_on_verified_cyan")


@pytest.mark.parametrize("value", ["confidence", "margin"])
def test_existing_glyph_confidence_and_margin_thresholds_are_preserved(monkeypatch, value):
    original = reader._glyph
    def lowered(mask,font_size):
        match = original(mask,font_size)
        if match is not None:
            match[value] = .7599 if value == "confidence" else .0449
        return match
    monkeypatch.setattr(reader,"_glyph",lowered)
    pair,evidence = rotation(actual_rgb())
    assert pair == (None,None) and evidence["reason"] == "ambiguous_numeric_glyph"

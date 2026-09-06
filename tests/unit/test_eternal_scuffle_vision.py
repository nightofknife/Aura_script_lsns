"""Offline screenshot acceptance: the production VisionService, never inputs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions._eternal_scuffle_vision import ScuffleVision

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
FIXTURES = ROOT / "tests/fixtures/eternal_scuffle"
SCENES = ("home", "coin", "role_select", "initial_equipment", "role_select", "captain", "stage", "victory", "loot_select", "assign", "stage", "defeat", "abandon_confirm", "settlement", "settlement", "settlement")


def frame(number, *, normalized=False):
    image = np.array(Image.open(FIXTURES / f"{number:02}.png").convert("RGB"))
    if normalized and number <= 11:
        image = cv2.resize(image[:, :1268], (1280, 720))
    return image


def probe(image, **capture_fields):
    capture = NS(success=True, image=image, quality_flags=[], **capture_fields)
    # No click method: any accidental input from the vision layer fails loudly.
    app = NS(capture=lambda: capture)
    vision = VisionService()
    vision._loop = asyncio.get_running_loop()
    catalog = json.loads((PLAN / "data/meta/eternal_scuffle.json").read_text(encoding="utf-8"))
    return ScuffleVision(app, vision, catalog, PLAN)


@pytest.mark.parametrize("number", range(1, 17))
@pytest.mark.parametrize("normalized", (False, True))
def test_supplied_scenes_and_shared_reward_boxes(number, normalized):
    async def run():
        helper = probe(frame(number, normalized=normalized))
        observation = await asyncio.to_thread(helper.observe)
        assert observation["valid"] is True
        assert observation["scene"] == SCENES[number-1], observation["diagnostics"]
        if number in (14, 15, 16):
            assert len(observation["boxes"]) == {14: 4, 15: 0, 16: 7}[number]
            assert ("back" in observation["controls"]) == (number == 15)
        if number == 2:
            assert set(observation["controls"]) == {"min", "plus", "max", "confirm"}
            assert "play" not in observation["controls"]
        for match in [*observation["controls"].values(), *observation["boxes"]]:
            x, y = match["center"]
            assert 0 <= x < 1280 and 0 <= y < 720
    asyncio.run(run())


@pytest.mark.parametrize("number,kind,expected", (
    (3, "role", [10000890, 10000154, 10001356]),
    (5, "role", [10000468, 10000095, 10000139]),
    (4, "equipment", [11800292, 11800008, 11800084]),
    (9, "equipment", [11800061, 11800240, 11800001]),
))
@pytest.mark.parametrize("normalized", (False, True))
def test_all_catalog_identity_comparison(number, kind, expected, normalized):
    async def run():
        helper = probe(frame(number, normalized=normalized))
        observation = await asyncio.to_thread(helper.observe)
        rows = await asyncio.to_thread(helper.read_candidates, observation, kind)
        assert [r["id"] for r in rows] == expected, observation["diagnostics"]
        assert [r["index"] for r in rows] == [0, 1, 2]
        assert all(450 < row["select_point"][1] < 520 for row in rows)
    asyncio.run(run())


@pytest.mark.parametrize("number", (6, 10))
@pytest.mark.parametrize("normalized", (False, True))
def test_team_reordering_and_all_fifteen_slot_states(number, normalized):
    async def run():
        helper = probe(frame(number, normalized=normalized))
        observation = await asyncio.to_thread(helper.observe)
        rows = await asyncio.to_thread(helper.read_team, observation)
        expected = [10001356, 10000468, 10000072, 10000027, 10000389]
        if number == 10:
            expected[:2] = reversed(expected[:2])
        assert [r["id"] for r in rows] == expected, observation["diagnostics"]
        assert [r["screen_index"] for r in rows] == list(range(5))
        assert [r["screen_index"] for r in rows if r["selected"]] == [0]
        filled = {10001356: "attack", 10000468: "support", 10000072: "support", 10000027: "support", 10000389: "defense"}
        for row in rows:
            assert row["slots"] == {s: "occupied" if s == filled[row["id"]] else "empty" for s in ("attack", "defense", "support")}
    asyncio.run(run())


@pytest.mark.parametrize("problem", ("failed", "flags", "black", "wrong_size", "wrong_dtype", "exception"))
def test_invalid_capture_never_counts_as_disappeared_controls(problem):
    async def run():
        helper = probe(frame(2))
        capture = helper.app.capture()
        if problem == "failed":
            capture.success = False
        elif problem == "flags":
            capture.quality_flags = ["black_frame"]
        elif problem == "black":
            capture.image[:] = 0
        elif problem == "wrong_size":
            capture.image = capture.image[:, :1268]
        elif problem == "wrong_dtype":
            capture.image = capture.image.astype(float)
        else:
            def fail():
                raise RuntimeError("capture disconnected")
            helper.app.capture = fail
        observation = await asyncio.to_thread(helper.observe)
        assert observation["valid"] is False
        assert observation["scene"] == "unknown"
        assert observation["controls"] == {}
        assert observation["diagnostics"]["capture_error"]
    asyncio.run(run())


def test_reads_use_same_frame_and_do_not_capture_or_act_again():
    async def run():
        helper = probe(frame(3))
        observation = await asyncio.to_thread(helper.observe)
        helper.app.capture = lambda: pytest.fail("Identity reading must use original observation")
        rows = await asyncio.to_thread(helper.read_candidates, observation, "role")
        assert len(rows) == 3
    asyncio.run(run())


def test_unreadable_role_and_missing_select_are_not_random_choices():
    async def run():
        for erase in ("name", "select"):
            image = frame(3)
            if erase == "name":
                image[330:365, 350:530] = 0
            else:
                image[451:515] = 0
            helper = probe(image)
            observation = await asyncio.to_thread(helper.observe)
            assert observation["scene"] == "role_select"
            assert await asyncio.to_thread(helper.read_candidates, observation, "role") == []
            assert len(observation["diagnostics"]["candidates"]) == 3
    asyncio.run(run())


def test_blank_slot_is_unknown_not_assumed_empty():
    async def run():
        image = frame(6)
        image[42:115, 389:462] = 0
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        rows = await asyncio.to_thread(helper.read_team, observation)
        assert rows[0]["slots"]["attack"] == "unknown"
    asyncio.run(run())


def test_rank_collapses_scales_and_phases_before_runner_up(monkeypatch):
    helper = ScuffleVision(None, NS(find_templates_batch=lambda *a, **kw: [NS(confidence=.96), NS(confidence=.95), NS(confidence=.89)]), {}, PLAN)
    templates = [np.ones((3, 3), np.uint8)]*3
    monkeypatch.setattr(helper, "_identity_variants", lambda *a, **kw: (templates, [None]*3, [100, 100, 200]))
    ranked = helper._rank(np.ones((10, 10), np.uint8), "role", "stand")
    assert [r["id"] for r in ranked] == [100, 200]
    assert helper._accepted(ranked, .93, .05)


def test_same_art_different_equipment_ids_require_readable_full_name():
    async def run():
        helper = probe(frame(4))
        group = helper.catalog["image_alias_groups"][0]["ids"]
        rows = {r["id"]: r for r in helper.catalog["equipment"]}
        for identifier in group:
            glyphs = helper._image(rows[identifier]["name_template"])
            image = np.zeros((720, 1280, 3), np.uint8)
            if glyphs.ndim == 2:
                glyphs = np.repeat(glyphs[:, :, None], 3, axis=2)
            width = glyphs.shape[1]
            image[100:123, 100:100+width] = glyphs[:, :, :3]
            chosen, _ = await asyncio.to_thread(helper._equipment_name, image, [100, 100, width, 23])
            assert chosen["id"] == identifier
            image[100:123, 100:100+width] = 0
            chosen, _ = await asyncio.to_thread(helper._equipment_name, image, [100, 100, width, 23])
            assert chosen is None
    asyncio.run(run())


def test_real_spine_bank_recovers_three_roles_when_names_are_hidden():
    async def run():
        image = frame(3)
        image[330:365, 350:1015] = 0
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        assert await asyncio.to_thread(helper.read_candidates, observation, "role") == []
        recovered = await asyncio.to_thread(helper.read_candidates, observation, "role", True)
        assert [r["id"] for r in recovered] == [10000890, 10000154, 10001356]
        assert all(r["method"] == "stand" and r["margin"] >= .05 for r in recovered)
    asyncio.run(run())


def live_equipment_frame():
    return np.array(Image.open(FIXTURES / "live_751713507132575744_initial_equipment.png").convert("RGB"))


def test_live_equipment_names_identify_all_three_candidates_without_art_matching():
    async def run():
        helper = probe(live_equipment_frame())
        observation = await asyncio.to_thread(helper.observe)
        assert observation["scene"] == "initial_equipment"
        rows = await asyncio.to_thread(helper.read_candidates, observation, "equipment")
        assert [r["id"] for r in rows] == [11800310, 11800240, 11800094]
        assert all(r["method"] == "equipment_name" for r in rows)
        assert all(r["score"] >= .86 and r["margin"] >= .05 for r in rows)
    asyncio.run(run())


def test_erased_live_equipment_name_is_not_guessed_from_art_or_frame():
    async def run():
        image = live_equipment_frame()
        # Preserve artwork, rarity, frame and SELECT; erase only the name.
        image[358:383, 387:527] = 22
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        assert observation["scene"] == "initial_equipment"
        assert await asyncio.to_thread(helper.read_candidates, observation, "equipment") == []
    asyncio.run(run())


def test_replaced_live_weapon_art_does_not_override_unchanged_name():
    async def run():
        image = live_equipment_frame()
        # Copy only the independent MK0 artwork over the first weapon; leave
        # the first card's original 探宝枪 name and UR frame untouched.
        image[238:355, 396:525] = image[238:355, 638:767]
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        rows = await asyncio.to_thread(helper.read_candidates, observation, "equipment")
        assert len(rows) == 3, observation["diagnostics"]
        assert rows[0]["id"] == 11800310
        assert rows[0]["method"] == "equipment_name"
    asyncio.run(run())


@pytest.mark.parametrize("identifier", (11800017, 11800019, 11800042, 11800242))
def test_similar_equipment_name_suffixes_and_single_line_overflow(identifier):
    async def run():
        image = live_equipment_frame()
        helper = probe(image)
        row = next(r for r in helper.catalog["equipment"] if r["id"] == identifier)
        glyphs = helper._image(row["name_template"])
        assert glyphs.ndim == 2
        width = glyphs.shape[1]
        assert width == (164 if identifier == 11800242 else 138)
        # Model the actual Text component's centered, unmasked single-line
        # rendering on the existing card, including overflow onto its border.
        image[359:383, 388:526] = 22
        left, top = 457-width//2, 359
        alpha = glyphs[:, :, None].astype(np.float32)/255.
        background = image[top:top+23, left:left+width]
        image[top:top+23, left:left+width] = (255*alpha + background*(1-alpha)).astype(np.uint8)
        chosen, ranked = await asyncio.to_thread(helper._equipment_name, image, [375, top, 164, 23])
        assert chosen is not None, ranked[:3]
        assert chosen["id"] == identifier, ranked[:3]
        assert chosen["margin"] >= .05
    asyncio.run(run())


def live_assign_frame(name="live_751723822939377664_assign.png"):
    return np.array(Image.open(FIXTURES / name).convert("RGB"))


@pytest.mark.parametrize("source,expected,index", (("06.png", 10001356, 0), ("10.png", 10000468, 0),
    ("live_751723822939377664_assign.png", 10001357, 4), ("user_selected_character_20260906.png", 10001357, 4)))
def test_stable_select_banner_identifies_actual_card_without_slot_work(source, expected, index, monkeypatch):
    async def run():
        helper = probe(live_assign_frame(source))
        observation = await asyncio.to_thread(helper.observe)
        helper.app.capture = lambda: pytest.fail("Selection probe must use one existing frame")
        monkeypatch.setattr(helper, "_occupied", lambda *a: pytest.fail("Selection probe must not classify equipment"))
        original_rank = helper._rank
        def rank(*args, **kwargs):
            assert args[1:3] == ("role", "name_template")
            return original_rank(*args, **kwargs)
        monkeypatch.setattr(helper, "_rank", rank)
        selected = await asyncio.to_thread(helper.read_selected_character, observation)
        assert selected["id"] == expected
        assert selected["screen_index"] == index
        assert selected["selected"] is True
        assert selected["marker_score"] >= .90
    asyncio.run(run())


@pytest.mark.parametrize("change", ("erase_banner", "erase_name", "two_banners"))
def test_selection_rejects_missing_or_ambiguous_central_markers(change):
    async def run():
        image = live_assign_frame()
        if change == "erase_banner":
            image[392:430, 515:652] = 22  # Dynamic four corners remain intact.
        elif change == "erase_name":
            image[439:466, 513:655] = 22
        else:
            image[124:168, 246:391] = image[386:430, 511:656]
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        assert observation["scene"] == "assign"
        assert await asyncio.to_thread(helper.read_selected_character, observation) is None
    asyncio.run(run())


def test_selection_ignores_animated_corners_and_tracks_marker_on_another_card():
    async def run():
        image = live_assign_frame()
        for y in (280, 505):
            for x in (490, 641):
                image[y:y+35, x:x+36] = 22
        helper = probe(image)
        observation = await asyncio.to_thread(helper.observe)
        selected = await asyncio.to_thread(helper.read_selected_character, observation)
        assert selected["id"] == 10001357
        # Moving only the central marker to the first card must identify that
        # first card, regardless of old corners and the preview portrait.
        image[124:168, 246:391] = image[386:430, 511:656]
        image[392:430, 515:652] = 22
        observation = await asyncio.to_thread(helper.observe)
        selected = await asyncio.to_thread(helper.read_selected_character, observation)
        assert selected["id"] == 10000300
        assert selected["screen_index"] == 0
    asyncio.run(run())


def test_live_assignment_full_team_and_light_probe_agree_on_fifth_card():
    async def run():
        helper = probe(live_assign_frame())
        observation = await asyncio.to_thread(helper.observe)
        rows = await asyncio.to_thread(helper.read_team, observation)
        assert [r["id"] for r in rows] == [10000300, 10000157, 10001019, 10000468, 10001357]
        assert [r["screen_index"] for r in rows if r["selected"]] == [4]
        assert all(s in {"empty", "occupied"} for r in rows for s in r["slots"].values())
        light = await asyncio.to_thread(helper.read_selected_character, observation)
        assert light["id"] == rows[4]["id"]
    asyncio.run(run())

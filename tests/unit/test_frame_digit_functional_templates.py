"""Geometry resources share pixels without merging states, digits or scenes."""
from collections import Counter
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _trade_goods_investment_vision as investment


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def builder(monkeypatch, name):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return importlib.import_module(name)


def hashes(folder):
    return {p.relative_to(folder).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


def test_investment_preserves_all_states_and_geometry_with_shared_masks():
    manifest, pixels = investment._resources(investment.TEMPLATE_ROOT.resolve())
    cards = manifest["cards"]
    sizes = {(114, 128), (114, 129), (115, 128), (115, 129)}
    assert Counter((row["state"], tuple(row["size"])) for row in cards["frame_variants"]) == Counter(
        (state, size) for state in ("normal", "locked", "selected", "maximum") for size in sizes)
    assert cards["variants"] == [f"cards/{state}_115x129.png"
                                  for state in ("normal", "locked", "selected", "maximum")]
    for row in cards["frame_variants"]:
        w, h = row["size"]
        assert row["mask"] == f"cards/masks/frame_{w}x{h}.png"
        assert pixels[row["mask"]].shape == (h, w)
        assert pixels[row["file"]].shape == (h, w, 3)
    assert cards["frame_mask"] == "cards/masks/frame_115x129.png"
    assert cards["header_threshold"] == .88
    assert cards["frame_threshold"] == .8
    assert cards["method"] == "TM_CCOEFF_NORMED"
    assert cards["preprocess"] == "rgb_blur"
    assert {p.name for p in (investment.TEMPLATE_ROOT / "cards/masks").glob("*.png")} == {
        f"frame_{w}x{h}.png" for w, h in sizes}
    assert not list((investment.TEMPLATE_ROOT / "cards").glob("*frame_mask*.png"))


def test_mask_writer_shares_only_equal_geometry_pixels_and_keeps_changed_maximum(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_trade_goods_investment_templates")
    monkeypatch.setattr(module, "TEMPLATES", tmp_path)
    ordinary = np.zeros((129, 115), dtype=np.uint8)
    ordinary[:15] = 255
    first = module.save_frame_mask(ordinary, 115, 129)
    assert module.save_frame_mask(ordinary, 115, 129, maximum=True) == first
    changed = ordinary.copy()
    changed[15:20] = 255
    maximum = module.save_frame_mask(changed, 115, 129, maximum=True)
    assert maximum != first
    before = hashes(tmp_path)
    assert module.save_frame_mask(changed, 115, 129, maximum=True) == maximum
    assert hashes(tmp_path) == before
    with Image.open(tmp_path / first) as original:
        np.testing.assert_array_equal(np.array(original), ordinary)


def test_workshop_digit_generator_preserves_all_scene_style_character_rows(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_raw_workshop_digit_templates")
    payload = json.loads((PLAN / "data/meta/raw_workshop_digits_pc.json").read_text(encoding="utf-8"))
    rows = payload["assets"]
    assert len(rows) == 77
    glyphs = {(row["style"], row["font_size"], row["scale"], row["character"]):
              Image.open(PLAN / row["image"]).copy() for row in rows}
    assert len(glyphs) == 77
    plan = tmp_path / "plan"
    (plan / "data/meta").mkdir(parents=True)
    monkeypatch.setattr(module, "PLAN", plan)
    counter = tmp_path / f"{module.FONT_ID}.ttf"
    counter.write_bytes(b"counter")
    footer = tmp_path / "-3758921232364580136.ttf"
    footer.write_bytes(b"selection")
    monkeypatch.setattr(module, "render_glyph", lambda raw, char, size, scale:
                        glyphs[("list" if raw == b"counter" else "selection", size, scale, char)])
    # External font extraction is stubbed; real output routing and preview/catalog writing run twice.
    module.build(tmp_path, tmp_path / "preview", counter, footer)
    first = hashes(tmp_path)
    module.build(tmp_path, tmp_path / "preview", counter, footer)
    assert hashes(tmp_path) == first
    output = json.loads((plan / "data/meta/raw_workshop_digits_pc.json").read_text(encoding="utf-8"))
    assert output["assets"] == rows
    assert output["font_sha256"] == sha256(b"counter").hexdigest()
    assert output["selection_font_sha256"] == sha256(b"selection").hexdigest()
    assert {p.relative_to(plan).as_posix() for p in (plan / "templates").rglob("*.png")} == {
        row["image"] for row in rows}
    assert not (PLAN / "templates/reforming_center/raw_workshop/digits").exists()


def test_profile_digit_generator_owns_exact_functional_bank(tmp_path, monkeypatch):
    module = builder(monkeypatch, "generate_profile_status_digit_templates")
    bank = PLAN / "templates/navigation/profile/status_digits"
    manifest = json.loads((bank / "manifest.json").read_text(encoding="utf-8"))
    glyphs = {character: np.array(Image.open(bank / file)) for character, file in manifest["characters"].items()}
    font = tmp_path / "font.ttf"
    font.write_bytes(b"native-font-extraction-stub")
    monkeypatch.setattr(module, "FONT_SHA256", sha256(font.read_bytes()).hexdigest())
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "templates/navigation/profile/status_digits")
    monkeypatch.setattr(module.ImageFont, "truetype", lambda *args: object())
    monkeypatch.setattr(module, "render_glyph", lambda font, character: glyphs[character])
    monkeypatch.setattr(sys, "argv", [module.__file__, str(font)])
    module.main()
    before = hashes(tmp_path)
    module.main()
    assert hashes(tmp_path) == before
    produced = json.loads((module.OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    assert produced["characters"] == manifest["characters"]
    assert len(list(module.OUTPUT.glob("*.png"))) == 11
    assert not (PLAN / "templates/player_data/status_digits").exists()

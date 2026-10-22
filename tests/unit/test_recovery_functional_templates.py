"""Functional recovery ownership, producer coverage and stable screenshot banks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

from tools import generate_bento_count_templates as count_builder
from tools import generate_bento_recovery_digit_templates as digit_builder
from plans.resonance_pc.src.actions._navigation_templates import navigation_profile_layout


PLAN = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
BANK = PLAN / "templates/recovery/bento/recovery_digits"


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_recovery_catalogs_resolve_functional_assets_without_old_directories():
    for legacy in ("player_recovery", "bento_consumption", "sparkling_water"):
        assert not (PLAN / "templates" / legacy).exists()
    for name in ("player_recovery", "bento_consumption", "sparkling_water", "love_bento"):
        catalog = json.loads((PLAN / f"data/meta/{name}.json").read_text(encoding="utf8"))

        def check(value):
            if isinstance(value, dict):
                if isinstance(value.get("profile"), str):
                    check(navigation_profile_layout(value["profile"]))
                for key, child in value.items():
                    check(key)
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
            elif isinstance(value, str) and value.startswith("templates/"):
                assert value.startswith(("templates/recovery/", "templates/navigation/")), value
                assert (PLAN / value).exists(), value

        check(catalog)


def test_bento_digit_bank_covers_all_logical_digits_with_no_orphans_or_duplicate_variants():
    manifest = json.loads((BANK / "manifest.json").read_text())
    assert set(manifest["styles"]) == {"work", "love"}
    for style, config in manifest["styles"].items():
        assert set(config["variants"]) == {str(n) for n in range(10)}
        referenced = set()
        for paths in config["variants"].values():
            pixels = set()
            assert paths
            for path in paths:
                image = Image.open(BANK / style / path)
                assert image.mode == "L" and image.size == (12, 16)
                assert image.tobytes() not in pixels
                pixels.add(image.tobytes())
                referenced.add(path)
        assert referenced == {p.name for p in (BANK / style).glob("*.png")}


def test_recovery_digit_producer_replays_existing_font_glyphs_and_is_idempotent(tmp_path, monkeypatch):
    """Use checked-in font glyphs; external game font is deliberately not required."""
    font = tmp_path / "font"
    font.write_bytes(b"font-replay")
    monkeypatch.setattr(digit_builder, "FONT_SHA256", hashlib.sha256(font.read_bytes()).hexdigest())
    sizes = {cfg["font_size"]: style for style, cfg in digit_builder.STYLES.items()}
    monkeypatch.setattr(digit_builder.ImageFont, "truetype", lambda path, size: size)
    monkeypatch.setattr(digit_builder, "generated", lambda size, cutoff, digit:
                        np.asarray(Image.open(BANK / sizes[size] / f"{digit}_font.png")))
    output = tmp_path / "digits"
    actual = digit_builder.build(font, output=output)
    expected = json.loads((BANK / "manifest.json").read_text())
    expected["font_sha256"] = digit_builder.FONT_SHA256
    assert actual == expected
    assert {key: value for key, value in hashes(output).items() if key != "manifest.json"} == {
        key: value for key, value in hashes(BANK).items() if key != "manifest.json"}
    stale = output / "work/3_sample_99.png"
    stale.write_bytes(b"stale owned output")
    unrelated = output / "work/unrelated.txt"
    unrelated.write_bytes(b"not owned by generator")
    digit_builder.build(font, output=output)
    assert not stale.exists()
    assert unrelated.read_bytes() == b"not owned by generator"
    first = hashes(output)
    digit_builder.build(font, output=output)
    assert hashes(output) == first


def test_recovery_digit_producer_deduplicates_font_and_samples_within_each_style(tmp_path, monkeypatch):
    font = tmp_path / "font"
    font.write_bytes(b"font")
    monkeypatch.setattr(digit_builder, "FONT_SHA256", hashlib.sha256(font.read_bytes()).hexdigest())
    monkeypatch.setattr(digit_builder.ImageFont, "truetype", lambda path, size: size)
    glyph = np.zeros((16, 12), dtype=np.uint8)
    glyph[3:13, 5:7] = 255
    monkeypatch.setattr(digit_builder, "generated", lambda *args: glyph)
    samples = tmp_path / "samples"
    samples.mkdir()
    frame = np.concatenate((glyph[:, :11], glyph[:, :11]), axis=1)
    for style in ("work", "love"):
        Image.fromarray(frame).save(samples / f"{style}_11.png")
    output = tmp_path / "output"
    manifest = digit_builder.build(font, output=output, samples=samples)
    for style in ("work", "love"):
        assert manifest["styles"][style]["variants"]["1"] == ["1_font.png"]
        assert not list((output / style).glob("*_sample_*.png"))


def test_count_producer_covers_0_through_12_and_is_idempotent(tmp_path, monkeypatch):
    font = tmp_path / "font"
    font.write_bytes(b"font")
    monkeypatch.setattr(count_builder, "EXPECTED_FONT_SHA256", hashlib.sha256(font.read_bytes()).hexdigest())
    default = ImageFont.load_default()
    monkeypatch.setattr(count_builder.ImageFont, "truetype", lambda *args, **kwargs: default)
    output = tmp_path / "counts"
    count_builder.build(font, output=output)
    assert {p.stem for p in output.glob("*.png")} == {str(n) for n in range(13)}
    for path in output.glob("*.png"):
        image = Image.open(path)
        assert image.mode == "L" and image.size == (24, 22)
    first = hashes(output)
    count_builder.build(font, output=output)
    assert hashes(output) == first


@pytest.mark.parametrize("builder", [count_builder, digit_builder])
def test_producers_reject_unverified_font_before_writing(tmp_path, builder):
    font = tmp_path / "wrong-font"
    font.write_bytes(b"not the game font")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="font is not"):
        builder.build(font, output=output)
    assert not output.exists()

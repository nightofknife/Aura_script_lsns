"""The calibrated bank writes one shared mask without changing item identity."""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def generator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return importlib.import_module("build_black_moon_local_select_templates")


def client_frames(tmp_path):
    paths = []
    fixtures = ROOT / "tests/fixtures/black_moon_select"
    for name, fixture in (("normal", "current"), ("selected", "selected")):
        frame = Image.new("RGB", (1280, 720))
        for suffix, origin in (("list", (594, 143)), ("footer", (1000, 646))):
            with Image.open(fixtures / f"{fixture}_{suffix}.png") as crop:
                frame.paste(crop, origin)
        path = tmp_path / f"{name}.png"
        frame.save(path)
        paths.append(path)
    return paths


def configure(generator, monkeypatch, output, frames):
    monkeypatch.setattr(generator, "OUTPUT", output)
    monkeypatch.setattr(sys, "argv", [generator.__file__, "--normal-frame", str(frames[0]),
                                    "--selected-frame", str(frames[1])])


def test_full_catalog_writes_one_identical_identity_mask_and_keeps_confuser_profiles(
        tmp_path, monkeypatch, generator):
    original_root = generator.OUTPUT
    original = json.loads((original_root / "manifest.json").read_text(encoding="utf-8"))
    frames = client_frames(tmp_path)
    output = tmp_path / "calibrated"
    output.mkdir()
    sold = original["sold_out_boundary"]
    (output / sold["file"]).parent.mkdir(parents=True, exist_ok=True)
    (output / sold["file"]).write_bytes((original_root / sold["file"]).read_bytes())
    (output / "manifest.json").write_text(json.dumps({"sold_out_boundary": sold}), encoding="utf-8")
    configure(generator, monkeypatch, output, frames)
    original_save = Image.Image.save
    mask_writes = []

    def save(image, fp, *args, **kwargs):
        if Path(fp) == output / "masks/item_identity_96x96.png":
            mask_writes.append((image.mode, image.size))
        return original_save(image, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    generator.main()
    generated = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert len(generated["items"]) == 140
    assert mask_writes == [("L", (96, 96))]
    assert generated["items"] == original["items"]
    assert generated["confuser_audit"] == original["confuser_audit"]
    assert generated["sold_out_boundary"] == sold
    assert {row["mask_file"] for row in generated["items"]} == {"masks/item_identity_96x96.png"}
    for row in generated["items"]:
        assert (output / row["file"]).read_bytes() == (original_root / row["file"]).read_bytes()
    mask_path = output / "masks/item_identity_96x96.png"
    assert mask_path.read_bytes() == (original_root / "masks/item_identity_96x96.png").read_bytes()
    with Image.open(mask_path) as image:
        actual = np.asarray(image)
    expected = np.full((96, 96), 255, dtype=np.uint8)
    expected[:24] = 0
    expected[67:, 65:] = 0
    expected[92:] = 0
    assert np.array_equal(actual, expected)
    assert generated["source"]["normal_client_sha256"] == sha256(frames[0].read_bytes()).hexdigest()
    assert generated["source"]["selected_client_sha256"] == sha256(frames[1].read_bytes()).hexdigest()
    assert generated["source"]["calibration"] == original["source"]["calibration"]
    assert generated["files"] == {
        path.relative_to(output).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in output.rglob("*.png")}
    first = (output / "manifest.json").read_bytes()
    generator.main()
    assert len(mask_writes) == 2
    assert (output / "manifest.json").read_bytes() == first


@pytest.mark.parametrize("rows", [[], [{"state": "selected"}]])
def test_no_normal_items_keeps_existing_failure_instead_of_publishing_empty_bank(
        tmp_path, monkeypatch, generator, rows):
    source = tmp_path / "native"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"cards": {"items": rows}}), encoding="utf-8")
    monkeypatch.setattr(generator, "SOURCE", source)
    output = tmp_path / "calibrated"
    configure(generator, monkeypatch, output, client_frames(tmp_path))
    with pytest.raises(UnboundLocalError):
        generator.main()
    assert not (output / "manifest.json").exists()
    assert not list(output.rglob("*.png"))


@pytest.mark.parametrize("reference", ["../outside.png", "/outside.png", "absolute"])
def test_normal_source_still_rejects_paths_outside_plan_templates(
        tmp_path, monkeypatch, generator, reference):
    source = tmp_path / "native"
    source.mkdir()
    if reference == "absolute":
        reference = str((tmp_path / "outside.png").resolve())
    (source / "manifest.json").write_text(json.dumps({"cards": {"items": [
        {"state": "normal", "file": reference}]}}), encoding="utf-8")
    monkeypatch.setattr(generator, "SOURCE", source)
    output = tmp_path / "calibrated"
    configure(generator, monkeypatch, output, client_frames(tmp_path))
    with pytest.raises(ValueError, match="Native resource outside plan templates"):
        generator.main()
    assert not (output / "manifest.json").exists()
    assert not list((output / "items").glob("*.png"))
    assert not list(output.rglob("*.png"))


def test_source_validation_keeps_row_order_and_ignores_selected_state(
        tmp_path, monkeypatch, generator):
    original = json.loads((generator.SOURCE / "manifest.json").read_text(encoding="utf-8"))
    item = original["cards"]["items"][0]
    source = tmp_path / "native"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"cards": {"items": [
        {"state": "selected"}, item, {}]}}), encoding="utf-8")
    monkeypatch.setattr(generator, "SOURCE", source)
    output = tmp_path / "calibrated"
    configure(generator, monkeypatch, output, client_frames(tmp_path))
    with pytest.raises(KeyError, match="state"):
        generator.main()
    assert {path.relative_to(output).as_posix() for path in output.rglob("*.png")} == {
        f"items/{item['item_id']}.png", "masks/item_identity_96x96.png"}
    assert not (output / "manifest.json").exists()

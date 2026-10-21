"""Recovery asset primitives keep catalog-specific validation at the loaders."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _recovery_assets as assets
from plans.resonance_pc.src.actions import sparkling_water_pc_actions as water


class Vision:
    def __init__(self, root, *, resolved=None, images=None):
        self.root = root
        self.resolved = resolved
        self.images = images or {}
        self.calls = []

    def resolve_template(self, plan, reference, root):
        self.calls.append(("resolve", plan, reference, root))
        return self.resolved if self.resolved is not None else root / reference

    def load_image_file(self, path, flags):
        self.calls.append(("load", path, flags))
        return self.images.get(Path(path).name)


def test_json_reader_preserves_unicode_and_leaves_schema_to_caller(tmp_path):
    config = tmp_path / "恢复.json"
    config.write_text(json.dumps({"名称": "气泡水"}, ensure_ascii=False), encoding="utf-8")
    assert assets.read_layout_json(config) == {"名称": "气泡水"}
    config.write_text("[]", encoding="utf-8")
    assert assets.read_layout_json(config) == []
    config.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        assets.read_layout_json(config)


@pytest.mark.parametrize("reference", ["../outside.png", "../../outside.png"])
def test_asset_escape_is_rejected_before_loading(tmp_path, reference):
    plan = tmp_path / "plan"
    plan.mkdir()
    vision = Vision(plan)
    with pytest.raises(ValueError, match="^catalog-specific escape$"):
        assets.load_plan_image(vision, reference, plan, flags=cv2.IMREAD_UNCHANGED,
                               path_error="catalog-specific escape")
    assert all(call[0] != "load" for call in vision.calls)


def test_resolver_result_is_checked_against_directory_boundary(tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir()
    vision = Vision(plan, resolved=tmp_path / "plan-other" / "asset.png")
    with pytest.raises(ValueError, match="outside plan"):
        assets.resolve_plan_asset(vision, "templates/apparently-safe.png", plan, error="outside plan")


@pytest.mark.parametrize("flags", [cv2.IMREAD_COLOR, cv2.IMREAD_GRAYSCALE, cv2.IMREAD_UNCHANGED])
def test_image_loading_preserves_unicode_path_flags_and_image_identity(tmp_path, flags):
    image = np.zeros((2, 3), dtype=np.uint8)
    vision = Vision(tmp_path, images={"便当.png": image})
    path, actual = assets.load_plan_image(
        vision, "templates/便当.png", tmp_path, flags=flags, path_error="escape",
    )
    assert path == tmp_path / "templates/便当.png"
    assert actual is image
    assert vision.calls == [
        ("resolve", "resonance_pc", "templates/便当.png", tmp_path),
        ("load", path, flags),
    ]


def test_missing_image_is_returned_for_catalog_specific_validation(tmp_path):
    path, image = assets.load_plan_image(
        Vision(tmp_path), "missing.png", tmp_path, flags=cv2.IMREAD_COLOR, path_error="escape",
    )
    assert path == tmp_path / "missing.png"
    assert image is None


@pytest.mark.parametrize("roi,message", [
    ([0, 0, 1], "ROI must contain x, y, width, height"),
    ([False, 0, 1, 1], "ROI coordinates must be integers"),
    ([0.0, 0, 1, 1], "ROI coordinates must be integers"),
    ([-1, 0, 1, 1], "ROI is outside reference client"),
    ([0, 0, 0, 1], "ROI is outside reference client"),
    ([9, 0, 2, 1], "ROI is outside reference client"),
])
def test_roi_validation_keeps_original_error_messages(roi, message):
    with pytest.raises(ValueError, match="^test: " + message + "$"):
        assets.validated_roi(roi, (10, 10), "test")


def test_roi_accepts_exact_boundary_and_returns_tuple():
    assert assets.validated_roi([0, 0, 1280, 720], (1280, 720), "viewport") == (0, 0, 1280, 720)


def water_layout(tmp_path, monkeypatch, *, roi=None):
    config = tmp_path / "水.json"
    config.write_text(json.dumps({
        "reference_client": [1280, 720],
        "templates": {"drink": {
            "path": "templates/水.png", "mask": "templates/遮罩.png",
            "roi": roi or [0, 0, 20, 20], "threshold": 0.85,
        }},
    }), encoding="utf-8")
    monkeypatch.setattr(water, "PLAN_ROOT", tmp_path)
    monkeypatch.setattr(water, "CONFIG_PATH", config)
    return Vision(tmp_path, images={
        "水.png": np.zeros((10, 10, 3), dtype=np.uint8),
        "遮罩.png": np.full((10, 10), 255, dtype=np.uint8),
    })


def test_water_loader_keeps_color_mask_flags_and_its_own_roi_policy(tmp_path, monkeypatch):
    vision = water_layout(tmp_path, monkeypatch, roi=[0.0, 0.0, 20.0, 20.0])
    layout = water.load_sparkling_water_layout(vision)
    spec = layout["templates"]["drink"]
    assert spec["resolved_path"] == str(tmp_path / "templates/水.png")
    assert spec["resolved_mask"] == str(tmp_path / "templates/遮罩.png")
    assert [call[0] for call in vision.calls] == ["resolve", "resolve", "load", "load"]
    assert [(Path(call[1]).name, call[2]) for call in vision.calls if call[0] == "load"] == [
        ("水.png", cv2.IMREAD_COLOR), ("遮罩.png", cv2.IMREAD_UNCHANGED),
    ]


def test_water_loader_rejects_escaping_mask_before_any_image_load(tmp_path, monkeypatch):
    vision = water_layout(tmp_path, monkeypatch)
    original = vision.resolve_template

    def resolve(plan, reference, root):
        if "遮罩" in reference:
            return root.parent / "outside.png"
        return original(plan, reference, root)

    vision.resolve_template = resolve
    with pytest.raises(ValueError, match="^Invalid sparkling water template path: drink$"):
        water.load_sparkling_water_layout(vision)
    assert all(call[0] != "load" for call in vision.calls)


@pytest.mark.parametrize("bad_image,message", [
    (np.zeros((21, 10, 3), dtype=np.uint8), "Sparkling water template exceeds ROI: drink"),
    (np.zeros((10, 10, 3), dtype=np.uint8), "Invalid sparkling water mask: drink"),
])
def test_water_loader_preserves_extent_and_mask_validation(tmp_path, monkeypatch, bad_image, message):
    vision = water_layout(tmp_path, monkeypatch)
    if "exceeds" in message:
        vision.images["水.png"] = bad_image
    else:
        vision.images["遮罩.png"] = bad_image
    with pytest.raises(ValueError, match="^" + message + "$"):
        water.load_sparkling_water_layout(vision)


@pytest.mark.parametrize("field", ["template", "mask"])
@pytest.mark.parametrize("problem", ["missing", "empty", "dtype", "shape"])
def test_water_unreadable_assets_fail_with_named_validation_error(tmp_path, monkeypatch, field, problem):
    vision = water_layout(tmp_path, monkeypatch)
    name = "水.png" if field == "template" else "遮罩.png"
    if problem == "missing":
        vision.images[name] = None
    elif problem == "empty":
        vision.images[name] = vision.images[name][:0]
    elif problem == "dtype":
        vision.images[name] = vision.images[name].astype(np.float32)
    else:
        vision.images[name] = np.zeros((10,), dtype=np.uint8)
    with pytest.raises(ValueError, match=f"Invalid sparkling water {field}: drink"):
        water.load_sparkling_water_layout(vision)


@pytest.mark.parametrize("field", ["template", "mask"])
def test_water_decoder_failure_has_named_resource_error(tmp_path, monkeypatch, field):
    vision = water_layout(tmp_path, monkeypatch)
    original = vision.load_image_file
    target = "水.png" if field == "template" else "遮罩.png"
    def load(path, flags):
        if Path(path).name == target:
            raise OSError("decoder cannot read file")
        return original(path, flags)
    vision.load_image_file = load
    with pytest.raises(ValueError, match=f"Invalid sparkling water {field}: drink"):
        water.load_sparkling_water_layout(vision)


@pytest.mark.parametrize("field", ["template", "mask"])
def test_water_missing_resolved_file_has_named_resource_error(tmp_path, monkeypatch, field):
    vision = water_layout(tmp_path, monkeypatch)
    original = vision.resolve_template
    target = "水.png" if field == "template" else "遮罩.png"
    def resolve(plan, reference, root):
        if Path(reference).name == target:
            raise FileNotFoundError(reference)
        return original(plan, reference, root)
    vision.resolve_template = resolve
    with pytest.raises(ValueError, match=f"Invalid sparkling water {field}: drink"):
        water.load_sparkling_water_layout(vision)
    assert all(call[0] != "load" for call in vision.calls)

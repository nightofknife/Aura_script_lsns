"""Functional Black Moon banks retain identity/state profiles and own their output."""
from hashlib import sha256
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from PIL import Image, ImageDraw
import pytest

from plans.resonance_pc.src.actions import _black_moon_local_select_vision as select
from plans.resonance_pc.src.actions import _black_moon_local_shop_vision as shop


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def builder(monkeypatch, name):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return importlib.import_module(name)


def test_black_moon_banks_are_functional_and_have_no_orphan_images():
    assert shop.TEMPLATE_ROOT == PLAN / "templates/commerce/shop/black_moon/native"
    assert select.ROOT == PLAN / "templates/commerce/shop/black_moon/calibrated"
    native, pixels = shop.BlackMoonLocalShopVision()._data
    calibrated = select.BlackMoonLocalSelectVision()
    controls = {row["name"]: row for row in native["templates"]}
    assert controls["local_tab_selected"]["mask_file"] == controls["local_tab_unselected"]["mask_file"]
    assert controls["local_tab_selected"]["file"] != controls["local_tab_unselected"]["file"]
    for key in ("roi", "threshold", "color_mae_limit", "preprocess", "method"):
        assert controls["local_tab_selected"][key] == controls["local_tab_unselected"][key]
    for root, references in ((shop.TEMPLATE_ROOT, {PLAN / name for name in native["files"]}),
                             (select.ROOT, {select.ROOT / name for name in calibrated.meta["files"]})):
        assert set(root.rglob("*.png")) <= references
    for name, info in native["files"].items():
        assert sha256((PLAN / name).read_bytes()).hexdigest() == info["sha256"]
        assert name in pixels
    for name, digest in calibrated.meta["files"].items():
        assert sha256((select.ROOT / name).read_bytes()).hexdigest() == digest


def test_native_tab_mask_recipe_keeps_distinct_pixels_and_prunes_only_previous_outputs(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_shop_templates")
    monkeypatch.setattr(module, "PLAN_ROOT", tmp_path)
    output = tmp_path / "templates/commerce/shop/black_moon/native"
    monkeypatch.setattr(module, "OUTPUT", output)
    rows = [{"name": f"local_tab_{state}", "mask_file": module.save_image(
        f"local_tab_{state}.mask.png", Image.new("L", (4, 3), 255))}
            for state in ("selected", "unselected")]
    module.share_prepare_masks(rows)
    shared = rows[0]["mask_file"]
    assert rows[1]["mask_file"] == shared
    assert len(list(output.rglob("*.png"))) == 1
    before = {p: sha256(p.read_bytes()).hexdigest() for p in output.rglob("*.png")}
    module.share_prepare_masks(rows)
    assert {p: sha256(p.read_bytes()).hexdigest() for p in output.rglob("*.png")} == before
    changed = [{"name": f"local_tab_{state}", "mask_file": module.save_image(
        f"local_tab_{state}.mask.png", Image.new("L", (4, 3), level))}
            for state, level in (("selected", 255), ("unselected", 100))]
    module.share_prepare_masks(changed)
    assert changed[0]["mask_file"] != changed[1]["mask_file"]
    neighbour = output / "unrelated.png"
    Image.new("L", (1, 1)).save(neighbour)
    current = {"files": {row["mask_file"]: {} for row in changed}}
    module.cleanup_previous_resources({"files": {shared: {}}}, current)
    assert not (tmp_path / shared).exists()
    assert neighbour.exists()
    assert all((tmp_path / row["mask_file"]).exists() for row in changed)


@pytest.mark.parametrize("reference", ["../outside.png", "/outside.png", "absolute"])
def test_calibrated_loader_rejects_escaping_or_absolute_reference(tmp_path, reference):
    path = tmp_path / "inside.png"
    Image.new("RGB", (3, 4)).save(path)
    if reference == "absolute":
        reference = str(path.resolve())
    with pytest.raises(ValueError, match="escapes bank"):
        select._pixels(tmp_path, reference)


def test_native_cleanup_does_not_remove_calibrated_sibling_or_escaped_paths(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_shop_templates")
    monkeypatch.setattr(module, "PLAN_ROOT", tmp_path)
    output = tmp_path / "templates/commerce/shop/black_moon/native"
    monkeypatch.setattr(module, "OUTPUT", output)
    names = ["templates/commerce/shop/black_moon/native/items/obsolete.png",
             "templates/commerce/shop/black_moon/calibrated/items/keep.png", "outside.png"]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("L", (1, 1)).save(path)
    module.cleanup_previous_resources({"files": dict.fromkeys(names)}, {"files": {}})
    assert not (tmp_path / names[0]).exists()
    assert all((tmp_path / name).exists() for name in names[1:])


def test_calibrated_cleanup_only_removes_previous_manifest_outputs(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_select_templates")
    monkeypatch.setattr(module, "OUTPUT", tmp_path)
    for name in ("obsolete.png", "keep.png", "neighbour.png"):
        Image.new("L", (1, 1)).save(tmp_path / name)
    module.cleanup_previous_resources({"files": {"obsolete.png": "", "keep.png": ""}},
                                      {"files": {"keep.png": ""}})
    assert not (tmp_path / "obsolete.png").exists()
    assert (tmp_path / "keep.png").exists() and (tmp_path / "neighbour.png").exists()
    with pytest.raises(ValueError, match="outside bank"):
        module.cleanup_previous_resources({"files": {"../escape.png": ""}}, {"files": {}})


def test_sold_out_generator_routes_to_controls_and_is_idempotent(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_sold_out_template")
    bank = tmp_path / "calibrated"
    bank.mkdir()
    (bank / "manifest.json").write_bytes((select.ROOT / "manifest.json").read_bytes())
    frame = tmp_path / "client.png"
    Image.new("RGB", (1280, 720), (80, 130, 200)).save(frame)
    monkeypatch.setattr(module, "BANK", bank)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", [module.__file__, "--frame", str(frame),
                                    "--client-box", "0", "0", "1280", "720",
                                    "--badge-box", "620", "230", "70", "20",
                                    "--card-origin", "600", "210"])
    module.main()
    manifest = json.loads((bank / "manifest.json").read_text(encoding="utf-8"))
    row = manifest["sold_out_boundary"]
    assert row["file"] == "controls/sold_out_badge.png"
    assert row["card_relative_roi"] == [20, 20, 70, 20]
    assert row["preprocess"] == "gray" and row["threshold"] == .90
    with Image.open(bank / row["file"]) as image:
        assert image.mode == "L" and image.size == (70, 20)
    before = {p.relative_to(bank): sha256(p.read_bytes()).hexdigest() for p in bank.rglob("*") if p.is_file()}
    module.main()
    assert {p.relative_to(bank): sha256(p.read_bytes()).hexdigest() for p in bank.rglob("*") if p.is_file()} == before


def test_native_preparation_producer_owns_functional_controls_with_shared_tab_mask(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_shop_templates")
    monkeypatch.setattr(module, "PLAN_ROOT", tmp_path)
    bank = tmp_path / "templates/commerce/shop/black_moon/native"
    monkeypatch.setattr(module, "OUTPUT", bank)

    class Native:
        def nodes(self, path):
            return [{"path": path}]

        def node(self, path):
            return {"path": path, "rect_client": [950, 90, 30, 20]}

        def layer(self, canvas, node):
            # Native extraction is stubbed; real crop, mask, routing and metadata run.
            ImageDraw.Draw(canvas).rectangle((0, 0, 1279, 719), fill=(80, 130, 200, 255))
            return {"path": node["path"]}

    rows = module.manufacture_prepare_controls(Native())
    files = module.resource_files({"templates": rows})
    assert len(rows) == 6 and len(files) == 10
    assert all(name.startswith("templates/commerce/shop/black_moon/native/controls/") for name in files)
    assert {p.relative_to(tmp_path).as_posix() for p in bank.rglob("*.png")} == set(files)
    assert module.manufacture_prepare_controls(Native()) == rows
    assert module.resource_files({"templates": rows}) == files


def test_prepare_only_rebuild_retires_scanner_outputs_and_preserves_normal_sources(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_shop_templates")
    original = json.loads((shop.TEMPLATE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    plan = tmp_path / "plan"
    output = plan / "templates/commerce/shop/black_moon/native"
    output.mkdir(parents=True)
    monkeypatch.setattr(module, "PLAN_ROOT", plan)
    monkeypatch.setattr(module, "OUTPUT", output)
    normal = deepcopy(original["cards"]["items"][:2])
    retired = ["templates/commerce/shop/black_moon/native/items/obsolete_selected.png",
               "templates/commerce/shop/black_moon/native/controls/reward.png",
               "templates/commerce/shop/black_moon/cards/normal.png",
               "templates/commerce/shop/black_moon/masks/item_identity_120x119.png"]
    neighbour = "templates/commerce/shop/black_moon/masks/item_identity_96x96.png"
    for name in [*(row["file"] for row in normal), *retired, neighbour]:
        path = plan / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 119), (80, 130, 200)).save(path)
    source_hashes = {row["file"]: sha256((plan / row["file"]).read_bytes()).hexdigest() for row in normal}
    selected = {**deepcopy(normal[0]), "state": "selected", "file": retired[0], "mask_file": retired[-1]}
    original["templates"].append({"name": "reward", "file": retired[1]})
    original["cards"].update(items=normal + [selected], anchors=[{"file": retired[2]}],
                             frame_variants=[{"file": retired[2]}], fingerprint_box=[0, 0, 5, 5])
    original["files"] = {name: {} for name in [*source_hashes, *retired]}
    (output / "manifest.json").write_text(json.dumps(original), encoding="utf-8")
    controls = deepcopy(original["templates"][:6])

    def manufacture(native):
        for row in controls:
            for key in ("file", "mask_file"):
                if row.get(key):
                    path = plan / row[key]
                    module.save_image(path.relative_to(output).as_posix(),
                                      Image.new("RGB", (5, 5), (70, 100, 150)))
        return controls

    monkeypatch.setattr(module, "Native", lambda *args: object())
    monkeypatch.setattr(module, "manufacture_prepare_controls", manufacture)
    records = {module.STORE_PREFAB: {"bundle_sha256": "native-prefab"}}
    module.update_prepare_controls(None, None, records)
    result = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {row["name"] for row in result["templates"]} == set(module.PREPARE_NAMES)
    assert result["cards"]["items"] == normal
    assert set(result["cards"]) == {"items", "icon_rect", "native_icon_rect", "quality", "catalog_products"}
    assert len(result["files"]) == 12
    assert all(not (plan / name).exists() for name in retired)
    assert (plan / neighbour).is_file()
    assert {name: sha256((plan / name).read_bytes()).hexdigest() for name in source_hashes} == source_hashes
    first = (output / "manifest.json").read_bytes()
    module.update_prepare_controls(None, None, records)
    assert (output / "manifest.json").read_bytes() == first

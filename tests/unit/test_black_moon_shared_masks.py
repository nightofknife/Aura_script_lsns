"""Preparation masks and calibrated identity sources keep explicit ownership."""
from collections import Counter
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _black_moon_local_shop_vision as shop
from plans.resonance_pc.src.actions._black_moon_local_shop_policy import load_black_moon_catalog


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
RETIRED_MASK = "templates/commerce/shop/black_moon/masks/item_identity_120x119.png"


def builder(monkeypatch, name="build_black_moon_local_shop_templates"):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return importlib.import_module(name)


def test_native_bank_keeps_only_preparation_controls_and_normal_calibration_sources():
    reader = shop.BlackMoonLocalShopVision()
    manifest, pixels = reader._data
    catalog = load_black_moon_catalog()
    products = {row["item_id"] for row in catalog["products"]}
    rows = manifest["cards"]["items"]
    assert Counter((row["item_id"], row["state"]) for row in rows) == Counter(
        (item_id, "normal") for item_id in products)
    assert len(rows) == len(products)
    expected_files = {row[key] for row in [*rows, *manifest["templates"]]
                      for key in ("file", "mask_file") if row.get(key)}
    assert set(manifest["files"]) == expected_files
    assert all("mask_file" not in row for row in rows)
    assert not (PLAN / RETIRED_MASK).exists()
    assert all(pixels[row["file"]].shape == (119, 120, 3) for row in rows)
    assert all(not value.flags.writeable for value in pixels.values())
    assert all(row["source"]["catalog_product"]["item_id"] == row["item_id"] for row in rows)
    assert not list((shop.TEMPLATE_ROOT / "items").glob("*.mask.png"))
    assert not (PLAN / "templates/commerce/shop/black_moon/cards").exists()
    assert set(manifest["cards"]) == {"items", "icon_rect", "native_icon_rect", "quality", "catalog_products"}
    assert {row["name"] for row in manifest["templates"]} == {
        "shop_entry", "shop_page", "local_tab_selected", "local_tab_unselected", "batch_on", "batch_off"}


def test_manifest_hashes_only_declared_resources_without_orphan_copies(monkeypatch):
    module = builder(monkeypatch)
    manifest = json.loads((shop.TEMPLATE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == module.resource_files(manifest)
    physical = {path.relative_to(PLAN).as_posix() for path in shop.TEMPLATE_ROOT.rglob("*.png")}
    assert set(manifest["files"]) == physical


@pytest.mark.parametrize("reference", ["legacy.png", "../escape.png", "templates/../data/escape.png", "absolute"])
def test_native_loader_rejects_legacy_and_escaping_paths(tmp_path, reference):
    plan = tmp_path / "plan"
    bank = plan / "templates/native"
    bank.mkdir(parents=True)
    legacy = bank / "legacy.png"
    Image.new("RGB", (4, 4), "white").save(legacy)
    if reference == "absolute":
        reference = str(legacy.resolve())
    (bank / "manifest.json").write_text(json.dumps({
        "schema_version": 2, "reference_client": [1280, 720],
        "templates": [{"name": "control", "file": reference}],
    }), encoding="utf-8")
    with pytest.raises(shop.BlackMoonLocalShopRecognitionError, match="outside plan templates"):
        shop._resources(bank, ("control",), plan)


def test_native_loader_resolves_shared_sibling_with_explicit_plan_root(tmp_path):
    plan = tmp_path / "plan"
    bank = plan / "templates/native"
    bank.mkdir(parents=True)
    image = plan / "templates/shared.png"
    Image.new("L", (5, 4), 128).save(image)
    (bank / "manifest.json").write_text(json.dumps({
        "schema_version": 2, "reference_client": [1280, 720],
        "templates": [{"name": "control", "file": "templates/shared.png"}],
    }), encoding="utf-8")
    reader = shop.BlackMoonLocalShopVision(template_root=bank, plan_root=plan)
    manifest, pixels = shop._resources(reader.template_root, ("control",), reader.plan_root)
    assert pixels[manifest["templates"][0]["file"]].shape == (4, 5)
    assert not pixels["templates/shared.png"].flags.writeable


def test_native_item_generator_only_emits_normal_calibration_sources_and_cleans_owned_stale_outputs(tmp_path, monkeypatch):
    module = builder(monkeypatch)
    plan = tmp_path / "plan"
    output = plan / "templates/commerce/shop/black_moon/native"
    monkeypatch.setattr(module, "PLAN_ROOT", plan)
    monkeypatch.setattr(module, "OUTPUT", output)
    # Asset extraction is external to the recipe; supply deterministic native pixels.
    catalog_module = ModuleType("aura_resonance_devtools.item_catalog")
    catalog_module.QUALITY_NUMBERS = {"green": 2}
    monkeypatch.setitem(sys.modules, "aura_resonance_devtools.item_catalog", catalog_module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"products": [
        {"item_id": item, "name": item, "quality": "green", "category": "material",
         "icon_resource_url": f"ui/items/{item}"} for item in ("first", "second")
    ]}), encoding="utf-8")

    class Native:
        blockers = []

        def node(self, path):
            return {"rect_client": [0, 0, 120, 119]}

        def named(self, relative, name):
            return (relative, name), name

        def image(self, key, obj):
            return Image.new("RGBA", (120, 119), (83, 155, 211, 255))

        def provenance(self, key, obj):
            return {"bundle": key[0], "name": obj}

        def layer(self, canvas, node, origin):
            pytest.fail("Native selected-state artwork is retired")

    def generate():
        cards = {"items": [], "icon_rect": [0, 0, 120, 119], "native_icon_rect": [0, 0, 120, 119]}
        module.build_items(Native(), catalog, cards, Image.new("RGBA", (120, 119)), (0, 0))
        files = module.resource_files({"templates": [], "cards": cards})
        return cards, files

    cards, files = generate()
    assert len(cards["items"]) == 2
    assert all("mask_file" not in row and row["state"] == "normal" for row in cards["items"])
    assert all(row["source"]["icon"]["name"] == row["item_id"] for row in cards["items"])
    assert {row["threshold"] for row in cards["items"]} == {.86}
    assert {row["preprocess"] for row in cards["items"]} == {"rgb"}
    assert generate() == (cards, files)
    assert len(list(plan.rglob("*.png"))) == 2
    assert not list(output.rglob("*.mask.png"))
    previous = {"files": files}
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    payload["products"] = payload["products"][:1]
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    changed, current_files = generate()
    module.cleanup_previous_resources(previous, {"files": current_files})
    assert len(changed["items"]) == 1
    assert {path.relative_to(plan).as_posix() for path in plan.rglob("*.png")} == set(current_files)
    assert generate() == (changed, current_files)


def test_calibrated_generator_reads_native_plan_references_without_enrolling_neighbours(tmp_path, monkeypatch):
    module = builder(monkeypatch, "build_black_moon_local_select_templates")
    source_manifest = json.loads((module.SOURCE / "manifest.json").read_text(encoding="utf-8"))
    output = tmp_path / "select"
    output.mkdir()
    original = json.loads((module.OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    sold = original["sold_out_boundary"]
    (output / sold["file"]).parent.mkdir(parents=True, exist_ok=True)
    (output / sold["file"]).write_bytes((module.OUTPUT / sold["file"]).read_bytes())
    (output / "manifest.json").write_text(json.dumps({"sold_out_boundary": sold}), encoding="utf-8")
    Image.new("L", (1, 1), 20).save(output / "neighbour_function.png")
    fixture_root = ROOT / "tests/fixtures/black_moon_select"
    paths = []
    for name, fixture in (("normal", "current"), ("selected", "selected")):
        frame = Image.new("RGB", (1280, 720))
        with Image.open(fixture_root / f"{fixture}_list.png") as image:
            frame.paste(image, (594, 143))
        with Image.open(fixture_root / f"{fixture}_footer.png") as image:
            frame.paste(image, (1000, 646))
        path = tmp_path / f"{name}.png"
        frame.save(path)
        paths.append(path)
    monkeypatch.setattr(module, "OUTPUT", output)
    monkeypatch.setattr(sys, "argv", [module.__file__, "--normal-frame", str(paths[0]),
                                    "--selected-frame", str(paths[1])])
    module.main()
    manifest_path = output / "manifest.json"
    first = manifest_path.read_bytes()
    generated = json.loads(first)
    expected_ids = {row["item_id"] for row in source_manifest["cards"]["items"]}
    assert {row["item_id"] for row in generated["items"]} == expected_ids
    assert "neighbour_function.png" not in generated["files"]
    assert generated["sold_out_boundary"] == sold
    assert generated["files"]["masks/item_identity_96x96.png"] == original["files"]["masks/item_identity_96x96.png"]
    for item in generated["items"]:
        assert generated["files"][item["file"]] == original["files"][item["file"]]
    module.main()
    assert manifest_path.read_bytes() == first
    # A changed canonical catalog must retire previously declared item PNGs.
    removed = source_manifest["cards"]["items"][0]["item_id"]
    source_manifest["cards"]["items"] = [row for row in source_manifest["cards"]["items"]
                                         if row["item_id"] != removed]
    source = tmp_path / "native"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    monkeypatch.setattr(module, "SOURCE", source)
    module.main()
    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {row["item_id"] for row in changed["items"]} == expected_ids - {removed}
    assert not (output / f"items/{removed}.png").exists()
    assert (output / "neighbour_function.png").exists()
    assert {path.relative_to(output).as_posix() for path in output.rglob("*.png")} == (
        set(changed["files"]) | {"neighbour_function.png"})

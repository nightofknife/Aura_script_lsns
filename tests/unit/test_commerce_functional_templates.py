"""Functional commerce banks preserve identities and the generators' ownership."""
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from plans.resonance_pc.src.actions import _trade_buy_selection as selection
from plans.resonance_pc.src.actions import _trade_goods_investment_vision as investment
from tools import build_trade_product_templates as products


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def hashes(folder):
    return {path.relative_to(folder).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


def test_retired_commerce_banks_have_no_old_directory_or_root_assets():
    assert not (PLAN / "templates/trade_products").exists()
    assert not (PLAN / "templates/trade_goods_investment").exists()
    for pattern in ("trade_*.png", "purchase_book_*.png", "*_settlement_scale_badge.png"):
        assert not list((PLAN / "templates").glob(pattern))


def test_investment_resources_remain_contained_immutable_and_cover_runtime_assets():
    root = (PLAN / "templates/commerce/investment").resolve()
    assert investment.TEMPLATE_ROOT.resolve() == root
    manifest, pixels = investment._resources(root)
    files = {path.relative_to(root).as_posix() for path in root.rglob("*.png")}
    # Native full-card variants belong to the review catalog, not the runtime cache.
    reviewed = set(manifest["cards"]["variants"]) | {manifest["cards"]["frame_mask"]}
    assert set(pixels) | reviewed <= files
    assert files == set(pixels) | reviewed
    assert all(not array.flags.writeable for array in pixels.values())
    assert {entry["level"] for entry in manifest["levels"]} == set(range(21))
    for key in ("digits", "max_digits"):
        if key in manifest:
            assert {entry["digit"] for entry in manifest[key]} == set(range(10))


def test_product_catalog_rejects_escape_from_the_functional_bank(tmp_path):
    plan = tmp_path / "plan"
    catalog = plan / "data/meta/trade_product_templates.json"
    catalog.parent.mkdir(parents=True)
    payload = json.loads((PLAN / "data/meta/trade_product_templates.json").read_text(encoding="utf-8"))
    next(entry for entry in payload["products"].values() if entry["supported"])["available"] = "templates/commerce/investment/entry.png"
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(selection.TradeBuySelectionError, match="outside product assets"):
        selection.load_product_templates(str(catalog))


def test_investment_catalog_rejects_escape_from_the_functional_bank(tmp_path):
    root = tmp_path / "investment"
    root.mkdir()
    payload = json.loads((PLAN / "templates/commerce/investment/manifest.json").read_text(encoding="utf-8"))
    # Every lookup fails containment before any image read, regardless of set order.
    for key in ("templates", "levels", "digits", "max_levels", "max_digits"):
        for row in payload.get(key, []):
            row["file"] = "../outside.png"
            if row.get("mask_file"):
                row["mask_file"] = "../outside.png"
    cards = payload["cards"]
    cards["header_variants"] = ["../outside.png"]
    cards["header_mask"] = "../outside.png"
    cards["frame_variants"] = [{"file": "../outside.png", "mask": "../outside.png"}]
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(investment.InvestmentRecognitionError, match="outside resource root"):
        investment._resources(root.resolve())


def test_product_generator_writes_functional_identity_state_paths_idempotently(tmp_path, monkeypatch):
    """Stub native extraction only: exercise the real catalog/output routing twice."""
    plan = tmp_path / "plan"
    metadata_dir = plan / "data/meta"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "products.json").write_text(json.dumps({"12": "test goods"}))
    game = tmp_path / "game"
    files = ("il2cpp_data/Metadata/global-metadata.dat", "Patch/BinaryConfig/HomeGoodsFactory.bin",
             "Patch/Asset/item/goods/small.asset", "Patch/Asset/item/goods/wulinyuan.asset",
             "Patch/Asset/ui/common.asset", "Patch/Asset/ui/font/originpack.asset")
    for filename in files:
        path = game / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"native fixture")
    image = Image.new("RGB", (80, 56), (27, 100, 200))
    selected = Image.new("RGB", (80, 56), (10, 40, 80))
    source = "Item/Goods/Small/test"
    records = [{"fields": [{"name": "name", "value": "test goods"},
                           {"name": "imagePath", "value": source}]}]
    font_objects = [SimpleNamespace(type=SimpleNamespace(name="Font"), path_id=font_id,
                                   read_typetree=lambda: {"m_FontData": b"font fixture"})
                    for font_id in products.FONT_IDS.values()]

    def bundle(path, metadata):
        if path.name == "originpack.asset":
            return SimpleNamespace(objects=font_objects)
        names = (["common_bottom_rarity01", "common_mask_rarity01"]
                 if path.name == "common.asset" else ["test"])
        return SimpleNamespace(objects=[SimpleNamespace(type=SimpleNamespace(name="Sprite"),
                                read=lambda name=name: SimpleNamespace(m_Name=name), version=1)
                                        for name in names])

    monkeypatch.setitem(sys.modules, "aura_resonance_devtools.full_config_export",
                        SimpleNamespace(decode_binary_config=lambda path: {"records": records}))
    monkeypatch.setitem(sys.modules, "aura_resonance_devtools.unity_bundle",
                        SimpleNamespace(load_bundle=bundle, _logical_sprite_image=lambda *args: image))
    monkeypatch.setattr(products, "render_states", lambda *args: {"available": image, "selected": selected})
    before_path = list(sys.path)
    try:
        first = products.build(game, tmp_path / "devtools", plan)
        first_hashes = hashes(plan)
        assert products.build(game, tmp_path / "devtools", plan) == first
        assert hashes(plan) == first_hashes
    finally:
        sys.path[:] = before_path
    assert set(first["assets"]) == {f"templates/commerce/trade/products/12/{state}.png"
                                    for state in ("available", "selected")}
    assert set(first["assets"]) == {path.relative_to(plan).as_posix()
                                    for path in (plan / "templates").rglob("*.png")}
    assert not (plan / "templates/trade_products").exists()


@pytest.mark.parametrize("filename", ["entry.png", "cards/masks/frame_115x129.png"])
def test_investment_writer_preserves_pixels_mode_and_is_idempotent(tmp_path, monkeypatch, filename):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    generator = importlib.import_module("tools.build_trade_goods_investment_templates")
    assert generator.TEMPLATES == PLAN / "templates/commerce/investment"
    with Image.open(generator.TEMPLATES / filename) as image:
        pixels = np.array(image)
        mode = image.mode
    target = tmp_path / "templates/commerce/investment"
    monkeypatch.setattr(generator, "TEMPLATES", target)
    generator.save(filename, pixels)
    first = hashes(tmp_path)
    generator.save(filename, pixels)
    assert hashes(tmp_path) == first
    with Image.open(target / filename) as output:
        assert output.mode == mode
        np.testing.assert_array_equal(np.array(output), pixels)

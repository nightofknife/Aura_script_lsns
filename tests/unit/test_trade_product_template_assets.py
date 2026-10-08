"""Self-contained trade templates must not need an installed game or masks."""
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import cv2
from PIL import Image
import pytest

from tools.build_trade_product_templates import CROP, ICON_SIZE, product_sources


PLAN = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
CATALOG = json.loads((PLAN / "data/meta/trade_product_templates.json").read_text(encoding="utf-8"))
REAL_CROPS = Path(__file__).resolve().parents[1] / "fixtures/trade_product_templates"


def test_catalog_covers_every_repo_product_by_exact_identity():
    products = json.loads((PLAN / "data/meta/products.json").read_text(encoding="utf-8"))
    assert CATALOG["schema_version"] == 1
    assert set(CATALOG["products"]) == set(products)
    for product_id, name in products.items():
        assert CATALOG["products"][product_id]["name"] == name
    unsupported = {key: item for key, item in CATALOG["products"].items() if not item["supported"]}
    assert set(unsupported) == {"67"}
    assert unsupported["67"]["reason"] == "configured_sprite_not_found"
    assert unsupported["67"]["source"].endswith("/baoshigongyiping")
    assert "available" not in unsupported["67"]
    assert "selected" not in unsupported["67"]


def test_fixed_rgb_crop_and_matching_contract_without_masks():
    assert CATALOG["match"] == {
        "method": "TM_SQDIFF_NORMED", "threshold": .92,
        "use_grayscale": False, "preprocess": "none",
    }
    assert CATALOG["icon_size"] == list(ICON_SIZE) == [96, 96]
    assert CATALOG["crop"] == list(CROP) == [8, 24, 80, 56]
    assert CATALOG["template_size"] == [80, 56]
    assert "mask" not in json.dumps(CATALOG["products"])
    assert all("mask" not in path for path in CATALOG["assets"])


def test_every_asset_is_opaque_png_with_verified_content_and_two_distinct_states():
    referenced = set()
    for entry in CATALOG["products"].values():
        if not entry["supported"]:
            continue
        state_bytes = []
        for state in ("available", "selected"):
            relative = entry[state]
            path = PLAN / relative
            assert path.resolve().is_relative_to((PLAN / "templates/trade_products").resolve())
            assert path.suffix == ".png"
            raw = path.read_bytes()
            metadata = CATALOG["assets"][relative]
            assert sha256(raw).hexdigest() == metadata["sha256"]
            with Image.open(path) as image:
                assert image.format == "PNG"
                assert image.mode == metadata["mode"] == "RGB"
                assert list(image.size) == metadata["size"] == [80, 56]
                assert np.asarray(image).std() > 1
            referenced.add(relative)
            state_bytes.append(raw)
        assert state_bytes[0] != state_bytes[1]
    assert referenced == set(CATALOG["assets"])
    assert len(referenced) == 456
    assert {path.relative_to(PLAN).as_posix() for path in (PLAN / "templates/trade_products").glob("*")} == referenced


def _record(**values):
    return {"fields": [{"name": key, "value": value} for key, value in values.items()]}


def test_builder_ignores_reserved_records_and_preserves_exact_names():
    records = [
        _record(name="toy", idCN="reserved: \u6682\u65e0", imagePath="wrong"),
        _record(name="toy", idCN="trade", imagePath="Item/Goods/Small/toy"),
        _record(name="toy", imagePath="wrong", isInformalData=True),
        _record(name="unrelated", imagePath="one"),
        _record(name="unrelated", imagePath="two"),
    ]
    assert product_sources(records, {"toy"}) == {"toy": "Item/Goods/Small/toy"}


def test_builder_rejects_conflicting_real_product_mapping():
    with pytest.raises(ValueError, match="Conflicting configured icon paths"):
        product_sources([_record(name="toy", imagePath="one"), _record(name="toy", imagePath="two")])


def test_source_provenance_contains_hashes_without_local_paths_or_font_payload():
    expected = {
        "il2cpp_data/Metadata/global-metadata.dat", "Patch/BinaryConfig/HomeGoodsFactory.bin",
        "Patch/Asset/item/goods/small.asset", "Patch/Asset/item/goods/wulinyuan.asset",
        "Patch/Asset/ui/common.asset", "Patch/Asset/ui/font/originpack.asset", "plan/products.json",
    }
    assert set(CATALOG["source_hashes"]) == expected
    for value in CATALOG["source_hashes"].values():
        assert len(value) == 64 and int(value, 16) >= 0
    assert CATALOG["source_hashes"]["plan/products.json"] == sha256((PLAN / "data/meta/products.json").read_bytes()).hexdigest()


def _score(source, template):
    return 1 - cv2.minMaxLoc(cv2.matchTemplate(source, template, cv2.TM_SQDIFF_NORMED))[0]


def _all_templates(state):
    return {product_id: np.asarray(Image.open(PLAN / entry[state]))
            for product_id, entry in CATALOG["products"].items() if entry["supported"]}


def test_real_available_crops_choose_correct_full_catalog_winner_and_pass_threshold():
    templates = _all_templates("available")
    for source_path in sorted(REAL_CROPS.glob("*_available*.png")):
        product_id = source_path.stem.split("_")[0]
        source = np.asarray(Image.open(source_path))
        ranking = sorted(((key, _score(source, value)) for key, value in templates.items()),
                         key=lambda item: -item[1])
        assert ranking[0][0] == product_id
        assert ranking[0][1] >= CATALOG["match"]["threshold"]
        assert ranking[0][1] - ranking[1][1] > .05
    # A pending-only match could misbuy quilt as paraffin at the approved .92.
    # Full-catalog winner selection must therefore precede pending-ID filtering.
    quilt = np.asarray(Image.open(REAL_CROPS / "176_available.png"))
    paraffin_id = next(key for key, entry in CATALOG["products"].items() if entry["name"] == "\u77f3\u8721")
    assert _score(quilt, templates[paraffin_id]) >= .92
    assert _score(quilt, templates["176"]) > _score(quilt, templates[paraffin_id])


def test_real_selected_crops_cannot_pass_any_available_template():
    templates = _all_templates("available")
    for source_path in REAL_CROPS.glob("*_selected.png"):
        source = np.asarray(Image.open(source_path))
        assert max(_score(source, template) for template in templates.values()) < .92


def test_same_row_selected_confirmation_accepts_real_click_but_not_unclicked_state():
    templates = _all_templates("selected")
    for source_path in REAL_CROPS.glob("*_selected.png"):
        product_id = source_path.stem.split("_")[0]
        assert _score(np.asarray(Image.open(source_path)), templates[product_id]) >= .92
    for source_path in REAL_CROPS.glob("*_available*.png"):
        product_id = source_path.stem.split("_")[0]
        assert _score(np.asarray(Image.open(source_path)), templates[product_id]) < .92


def test_synthetic_locked_dark_crops_do_not_pass_any_available_template():
    templates = _all_templates("available")
    for source_path in REAL_CROPS.glob("*_available*.png"):
        dark = np.rint(np.asarray(Image.open(source_path), dtype=float) * .4).astype(np.uint8)
        assert max(_score(dark, template) for template in templates.values()) < .92

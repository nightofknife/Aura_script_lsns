"""Inventory identities and native digit samples survive functional ownership moves."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory


PLAN = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
VISION = SimpleNamespace(
    load_image_file=VisionService.load_image_file,
    resolve_template=lambda _key, ref, root: str(root / ref),
)

# Fingerprints of the distinct native pixels before migration, grouped by digit.
# Repeated copies do not contribute a new visual sample.
COUNT_PIXELS = {
    "0": "5f37b7e609225f5815d7494a2b81a06c2b9a240044015b710020c35b1a11b5ac",
    "1": "d409a170a84b0e29b2ecb40ddf76ec1a00bc2b612c4d8f120298b76179597d07",
    "2": "5fb3439f442190cfc41a64b074a6392ebcc111de927c83839fda9dd629e987ac",
    "3": "85328f27be515d9c4fcf97f3dc8769733e7d72e2127e89cfa449ce1e1068270f",
    "4": "11824781c0dabc71bdefbe37b34bedbc87f2c87c52e1c1c4df98caf42d0293be",
    "5": "399c702a4a59cce513c32d8a1cfda0c07ebf5bd531bf176739c9c9da162cfef5",
    "6": "820f82c107c9d08f7fb7542f582a07c9ecc72925fbe5f909a3a77398bcd69324",
    "7": "b5659d071a6531dcb76a8cbd34ff6ce5f4d6024dd7729a3dea293bdc867dad88",
    "8": "907ebcada22e8094fd5de33fe7d0208ec0e1bd6b054d430e707df4bbb8041897",
    "9": "08fb6da53d592bc755a7278194f0c98cd9dff346811213adae6d1fb7441c4f7d",
}
EXPIRY_PIXELS = {
    "3": "7fd2b4f905c4f9fef00321ff079c4cb4093dc318f773e83b9f1393e75ed08273",
    "4": "16dbd85bd4135018c44c3fe027a6dabdc00f389579e05715e22c32dd122da41f",
    "6": "f428c5b2f29c4b5726ff73fd80b69b48b9277d639e65b2a64af3e05eb3f6e1e7",
    "7": "dd24920ec69210f7e95520fc6a3b8dc6eedd947bb9908afc3e4f32d22f9881fc",
}


@pytest.mark.parametrize("digit,expected", COUNT_PIXELS.items())
def test_count_bank_keeps_every_distinct_native_digit_sample(digit, expected):
    reader = inventory.load_inventory_digit_catalog(vision=VISION)
    assert reader["template_root"] == "templates/inventory/digits/count"
    paths = reader["_template_paths"][digit]
    images = [VISION.load_image_file(Path(path), cv2.IMREAD_UNCHANGED) for path in paths]
    hashes = [hashlib.sha256(image.tobytes()).hexdigest() for image in images]
    assert len(hashes) == len(set(hashes))
    assert hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest() == expected
    assert all(image.shape == (22, 18) for image in images)


@pytest.mark.parametrize("digit,expected", EXPIRY_PIXELS.items())
def test_expiry_bank_keeps_its_own_native_style(digit, expected):
    reader = inventory.load_inventory_expiry_digit_catalog(vision=VISION)
    assert reader["template_root"] == "templates/inventory/digits/expiry"
    paths = reader["_template_paths"][digit]
    assert len(paths) == 1
    image = VISION.load_image_file(Path(paths[0]), cv2.IMREAD_UNCHANGED)
    digest = hashlib.sha256(image.tobytes()).hexdigest()
    assert hashlib.sha256(digest.encode()).hexdigest() == expected
    assert image.shape == (18, 14)


@pytest.mark.parametrize(
    "catalog_name,load",
    [
        ("inventory_digits.json", inventory.load_inventory_digit_catalog),
        ("inventory_expiry_digits.json", inventory.load_inventory_expiry_digit_catalog),
    ],
)
def test_digit_functional_roots_still_reject_escape(tmp_path, catalog_name, load):
    payload = json.loads((PLAN / "data/meta" / catalog_name).read_text(encoding="utf-8"))
    payload["template_root"] = "../outside"
    plan = tmp_path / "plan"
    plan.mkdir()
    (tmp_path / "outside").mkdir()
    catalog = plan / catalog_name
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="root is unavailable"):
        load(catalog, plan_root=plan, vision=VISION)


def test_equipment_shared_art_preserves_business_identities_and_order():
    catalog = inventory.prepare_inventory_catalog("equipment", VISION)
    entries = catalog["items"]
    by_id = {entry["source_record_id"]: entry for entry in entries}
    assert [entry["source_record_id"] for entry in entries] == sorted(by_id)
    for canonical, other in [(11800009, 11800010), (11800053, 11800278), (11800057, 11800101)]:
        assert by_id[canonical]["item_id"] != by_id[other]["item_id"]
        assert by_id[canonical]["name"] != by_id[other]["name"]
        expected = f"templates/inventory/equipment/art/{canonical}/grid_100x60.png"
        assert by_id[canonical]["template"] == by_id[other]["template"] == expected
    # The historical name/art disagreement retains its original business record.
    assert by_id[11800101]["name"] == "宏观形而上材料"
    assert len(entries) == 175
    assert len(set(catalog["_template_paths"])) == 172
    assert catalog["layout"]["template_size"] == (100, 60)
    assert catalog["layout"]["template_offset_from_card"] == (10, 35)
    assert catalog["layout"]["match_threshold"] == 0.82


def test_equipment_matching_retains_each_logical_candidate_and_native_profile():
    catalog = inventory.prepare_inventory_catalog("equipment", VISION)
    calls = []

    class Matcher:
        def find_all_templates_batch(self, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(matches=[]) for _ in catalog["items"]]

    assert inventory.scan_inventory_page(
        np.zeros((626, 680, 3), dtype=np.uint8), catalog, object(), Matcher()
    ) == []
    call = calls[0]
    assert len(call["template_images"]) == 175
    assert call["threshold"] == 0.82
    assert call["use_grayscale"] is False
    assert call["match_method"] == cv2.TM_CCOEFF_NORMED
    assert call["preprocess"] == "none"


def test_inventory_digit_recognition_profiles_remain_distinct():
    count = inventory.load_inventory_digit_catalog(vision=VISION)
    expiry = inventory.load_inventory_expiry_digit_catalog(vision=VISION)
    assert count["digits"] == list("0123456789")
    assert count["count_band_from_card"] == (0, 80, 122, 41)
    assert (count["min_digit_score"], count["min_digit_margin"]) == (0.75, 0.02)
    assert count["white_min_candidates"] == [165, 180, 195]
    assert expiry["available_digits"] == list("3467")
    assert expiry["similarity_mode"] == "gaussian_cosine"
    assert expiry["gaussian_sigma"] == 0.9
    assert (expiry["min_digit_score"], expiry["min_digit_margin"]) == (0.9, 0.05)
    assert inventory._EXPIRY_RECOGNITION_ENABLED is False

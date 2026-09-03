from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory


class ResolvingVision:
    load_image_file = staticmethod(VisionService.load_image_file)

    def resolve_template(self, _plan_key: str, template_ref: str, plan_root: Path) -> str:
        return str(Path(plan_root) / template_ref)


def _prepared_material_catalog() -> dict:
    materials = [
        {"material_id": "a", "name": "甲", "template": "a.png"},
        {"material_id": "b", "name": "乙", "template": "b.png"},
        {"material_id": "c", "name": "丙", "template": "c.png"},
    ]
    return {
        "schema_version": 2,
        "category": "materials",
        "default_stack_policy": inventory.STACK_POLICY_MERGE,
        "layout": {
            "template_size": (50, 35),
            "source_template_size": (100, 70),
            "template_offset_from_card": (10, 23),
            "card_size": (122, 121),
            "grid_region": (397, 94, 680, 626),
            "recognition_scale": 0.5,
            "blur_kernel": (3, 3),
            "blur_sigma": 0.8,
            "match_threshold": 0.85,
        },
        "materials": materials,
        "items": [
            {
                "item_id": entry["material_id"],
                "material_id": entry["material_id"],
                "name": entry["name"],
                "template": entry["template"],
                "stack_policy": inventory.STACK_POLICY_MERGE,
            }
            for entry in materials
        ],
        "_template_paths": [entry["template"] for entry in materials],
        "_template_images": [
            np.zeros((35, 50, 3), dtype=np.uint8) for _entry in materials
        ],
        "_count_mode": inventory._COUNT_MODE_DIGIT_TEMPLATE,
        "_supports_expiry": False,
        "_digit_reader": {},
        "_expiry_digit_reader": None,
    }


def test_actual_material_catalog_contains_native_visual_classes() -> None:
    catalog = inventory.prepare_inventory_catalog("materials", ResolvingVision())

    assert catalog["schema_version"] == 2
    assert catalog["layout"]["template_size"] == (50, 35)
    assert catalog["layout"]["source_template_size"] == (100, 70)
    assert catalog["layout"]["recognition_scale"] == 0.5
    assert catalog["layout"]["blur_kernel"] == (3, 3)
    assert catalog["layout"]["blur_sigma"] == 0.8
    assert catalog["layout"]["match_threshold"] == 0.85
    assert len(catalog["items"]) == 201
    assert len(catalog["_template_paths"]) == 201
    assert len({item["item_id"] for item in catalog["items"]}) == 201
    for item, template_path in zip(catalog["items"], catalog["_template_paths"], strict=True):
        assert item["quality"] in {"White", "Blue", "Purple", "Golden", "Orange"}
        assert item["visual_key"]
        assert item["source_record_ids"]
        image = cv2.imread(str(template_path), cv2.IMREAD_UNCHANGED)
        assert image is not None
        assert image.shape[:2] == (35, 50)


def test_material_page_uses_one_half_scale_pass_and_keeps_highest_match(
    monkeypatch,
) -> None:
    catalog = _prepared_material_catalog()
    page = np.zeros((626, 680, 3), dtype=np.uint8)
    page[100:130, 200:230] = (20, 80, 200)
    expected = cv2.resize(
        cv2.GaussianBlur(page, (3, 3), sigmaX=0.8, sigmaY=0.8),
        (340, 313),
        interpolation=cv2.INTER_AREA,
    )

    class BatchVision:
        def find_all_templates_batch(self, **kwargs):
            assert kwargs["source_image"].shape == (313, 340, 3)
            assert np.array_equal(kwargs["source_image"], expected)
            assert kwargs["threshold"] == 0.85
            assert kwargs["preprocess"] == "none"
            return [
                SimpleNamespace(
                    matches=[
                        SimpleNamespace(top_left=(5, 12), confidence=0.90),
                        SimpleNamespace(top_left=(71, 12), confidence=0.91),
                    ]
                ),
                SimpleNamespace(
                    matches=[SimpleNamespace(top_left=(5, 12), confidence=0.95)]
                ),
                SimpleNamespace(
                    matches=[SimpleNamespace(top_left=(5, 12), confidence=0.84)]
                ),
            ]

    monkeypatch.setattr(inventory, "read_inventory_count", lambda *_args, **_kwargs: 7)
    observations = inventory.scan_inventory_page(page, catalog, object(), BatchVision())

    assert observations == [
        {
            "item_id": "b",
            "name": "乙",
            "count": 7,
            "confidence": 0.95,
            "card_top_left": [0, 1],
        },
        {
            "item_id": "a",
            "name": "甲",
            "count": 7,
            "confidence": 0.91,
            "card_top_left": [132, 1],
        },
    ]


def test_material_overlap_tie_is_resolved_by_item_id() -> None:
    candidates = [
        {
            "confidence": 0.9,
            "card_top_left": (0, 0),
            "item": {"item_id": "b"},
        },
        {
            "confidence": 0.9,
            "card_top_left": (0, 0),
            "item": {"item_id": "a"},
        },
    ]

    assert inventory._suppress_cross_template_overlaps(candidates) == [candidates[1]]


def test_vision_nms_uses_xywh_rectangles(monkeypatch) -> None:
    service = VisionService()
    score_map = np.zeros((6, 7), dtype=np.float32)
    score_map[4, 3] = 0.9
    captured = {}

    monkeypatch.setattr(cv2, "matchTemplate", lambda *_args, **_kwargs: score_map)

    def nms_boxes(rects, scores, score_threshold, nms_threshold):
        captured["rects"] = rects
        captured["scores"] = scores
        captured["thresholds"] = (score_threshold, nms_threshold)
        return np.asarray([0], dtype=np.int32)

    monkeypatch.setattr(cv2.dnn, "NMSBoxes", nms_boxes)
    matches = service._match_all_templates_prepared(
        np.zeros((10, 11, 3), dtype=np.uint8),
        np.zeros((6, 5, 3), dtype=np.uint8),
        None,
        0.85,
        0.5,
        cv2.TM_CCOEFF_NORMED,
        False,
        "none",
    )

    assert captured["rects"] == [[3, 4, 5, 6]]
    assert captured["thresholds"] == (0.85, 0.5)
    assert matches[0].top_left == (3, 4)

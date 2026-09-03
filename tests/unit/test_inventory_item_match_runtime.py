from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory


def test_lab_chroma_blur_uses_rgb_lab_float32_and_weighted_l() -> None:
    service = VisionService()
    image = np.array(
        [
            [[10, 40, 220], [25, 80, 190], [40, 120, 160]],
            [[55, 160, 130], [70, 200, 100], [85, 240, 70]],
            [[100, 210, 40], [115, 170, 20], [130, 130, 0]],
        ],
        dtype=np.uint8,
    )
    expected = cv2.cvtColor(
        cv2.GaussianBlur(image, (3, 3), 0),
        cv2.COLOR_RGB2LAB,
    ).astype(np.float32)
    expected[:, :, 0] *= 0.2

    actual = service._apply_preprocess(image, "lab_chroma_blur")

    assert actual.dtype == np.float32
    assert np.array_equal(actual, expected)


def test_raw_sqdiff_score_scale_converts_sum_to_mse_confidence() -> None:
    service = VisionService()
    template_shape = (50, 120, 3)
    raw_sqdiff = 25.0 * np.prod(template_shape)

    confidence = service._normalize_match_score(
        raw_sqdiff,
        cv2.TM_SQDIFF,
        template_shape,
        100.0,
    )
    confidence_map = service._normalize_match_map(
        np.asarray([[raw_sqdiff]], dtype=np.float32),
        cv2.TM_SQDIFF,
        template_shape,
        100.0,
    )

    assert np.isclose(confidence, 0.8)
    assert np.isclose(float(confidence_map[0, 0]), 0.8)
    assert service._normalize_match_score(4.0, cv2.TM_SQDIFF) == 0.2


def test_item_scan_reads_native_match_options_from_layout(monkeypatch) -> None:
    captured: dict = {}

    class BatchVision:
        def find_all_templates_batch(self, **kwargs):
            captured.update(kwargs)
            return [
                SimpleNamespace(
                    matches=[SimpleNamespace(top_left=(0, 44), confidence=0.9)]
                )
            ]

    catalog = {
        "category": "items",
        "layout": {
            "template_size": (120, 50),
            "template_offset_from_card": (0, 44),
            "card_size": (122, 121),
            "match_threshold": 0.82,
            "preprocess": "lab_chroma_blur",
            "match_method": "sqdiff",
            "score_scale": 100.0,
        },
        "items": [
            {
                "item_id": "native_item",
                "name": "原生道具",
                "stack_policy": inventory.STACK_POLICY_MERGE,
            }
        ],
        "_template_paths": ["native_item.png"],
        "_count_mode": inventory._COUNT_MODE_DIGIT_TEMPLATE,
        "_supports_expiry": False,
        "_digit_reader": {},
        "_expiry_digit_reader": None,
    }
    monkeypatch.setattr(inventory, "read_inventory_count", lambda *_args, **_kwargs: 3)

    observations = inventory.scan_inventory_page(
        np.zeros((121, 122, 3), dtype=np.uint8),
        catalog,
        object(),
        BatchVision(),
    )

    assert captured["preprocess"] == "lab_chroma_blur"
    assert captured["match_method"] == cv2.TM_SQDIFF
    assert captured["score_scale"] == 100.0
    assert observations[0]["item_id"] == "native_item"
    assert observations[0]["count"] == 3


def test_material_scan_keeps_legacy_match_options(monkeypatch) -> None:
    captured: dict = {}

    class BatchVision:
        def find_all_templates_batch(self, **kwargs):
            captured.update(kwargs)
            return [SimpleNamespace(matches=[])]

    catalog = {
        "category": "materials",
        "layout": {
            "template_size": (50, 35),
            "source_template_size": (100, 70),
            "template_offset_from_card": (10, 23),
            "card_size": (122, 121),
            "match_threshold": 0.85,
            "recognition_scale": 0.5,
            "blur_kernel": (3, 3),
            "blur_sigma": 0.8,
            "preprocess": "lab_chroma_blur",
            "match_method": "sqdiff",
            "score_scale": 100.0,
        },
        "items": [
            {
                "item_id": "material",
                "name": "材料",
                "stack_policy": inventory.STACK_POLICY_MERGE,
            }
        ],
        "_template_paths": ["material.png"],
        "_template_images": [np.zeros((35, 50, 3), dtype=np.uint8)],
        "_count_mode": inventory._COUNT_MODE_DIGIT_TEMPLATE,
        "_supports_expiry": False,
        "_digit_reader": {},
        "_expiry_digit_reader": None,
    }
    monkeypatch.setattr(inventory, "read_inventory_count", lambda *_args, **_kwargs: 1)

    inventory.scan_inventory_page(
        np.zeros((121, 122, 3), dtype=np.uint8),
        catalog,
        object(),
        BatchVision(),
    )

    assert captured["preprocess"] == "none"
    assert captured["match_method"] == cv2.TM_CCOEFF_NORMED
    assert "score_scale" not in captured

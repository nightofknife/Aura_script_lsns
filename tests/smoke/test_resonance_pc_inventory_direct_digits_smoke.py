from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.vision_service import MultiMatchResult, VisionService
from plans.resonance_pc.src.actions import inventory_pc_actions as inventory


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_ROOT = REPO_ROOT / "plans" / "resonance_pc"
DIGIT_CATALOG = PLAN_ROOT / "data" / "meta" / "inventory_digits.json"


class SynchronousFrameworkVision:
    """Use the framework matcher core without requiring a scheduler event loop."""

    def __init__(self) -> None:
        self.service = VisionService()
        self.calls: list[dict] = []

    def find_all_templates_batch(self, **kwargs):
        self.calls.append(kwargs)
        source = self.service._prepare_image(
            kwargs["source_image"],
            use_grayscale=kwargs["use_grayscale"],
            preprocess=kwargs["preprocess"],
        )
        results = []
        for template in kwargs["template_images"]:
            prepared = self.service._prepare_image(
                template,
                use_grayscale=kwargs["use_grayscale"],
                preprocess=kwargs["preprocess"],
            )
            matches = self.service._match_all_templates_prepared(
                source,
                prepared,
                None,
                kwargs["threshold"],
                kwargs["nms_threshold"],
                kwargs["match_method"],
                kwargs["use_grayscale"],
                kwargs["preprocess"],
            )
            results.append(MultiMatchResult(count=len(matches), matches=matches))
        return results


class ResolvingVision:
    def resolve_template(self, _plan_key: str, template_ref: str, plan_root: Path) -> str:
        return str(Path(plan_root) / template_ref)


def _template_for(
    reader: dict,
    digit: str,
    *,
    scale: float = 0.64,
) -> np.ndarray:
    for template, metadata in zip(
        reader["_direct_templates"],
        reader["_direct_template_meta"],
        strict=True,
    ):
        if metadata["digit"] == digit and metadata["scale"] == scale:
            return template
    raise AssertionError(f"missing direct template for {digit}@{scale}")


def _raw_count_card(
    digits: str,
    reader: dict,
    *,
    background: str,
) -> np.ndarray:
    card = np.zeros((121, 122, 3), dtype=np.uint8)
    band_height, band_width = 41, 122
    x_gradient = np.linspace(0, 1, band_width, dtype=np.float32)[None, :]
    y_gradient = np.linspace(0, 1, band_height, dtype=np.float32)[:, None]
    if background == "white":
        gray = 215 + 25 * x_gradient + 8 * y_gradient
    elif background == "blue":
        gray = 45 + 90 * x_gradient + 18 * y_gradient
    else:
        raise ValueError(background)
    checker = ((np.indices((band_height, band_width)).sum(axis=0) % 2) * 3).astype(
        np.float32
    )
    band_gray = np.clip(gray + checker, 0, 255).astype(np.uint8)
    band = np.repeat(band_gray[:, :, None], 3, axis=2)

    right_x = 105
    step = 11
    start_x = right_x - step * (len(digits) - 1)
    for index, digit in enumerate(digits):
        glyph = _template_for(reader, digit)
        glyph_height, glyph_width = glyph.shape
        x = start_x + step * index
        y = 19
        support = glyph > 12
        outline = cv2.dilate(
            support.astype(np.uint8),
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        ).astype(bool) & ~support
        target = band[y : y + glyph_height, x : x + glyph_width]
        target[outline] = 60
        alpha = glyph.astype(np.float32) / 255.0
        for channel in range(3):
            source = target[:, :, channel].astype(np.float32)
            target[:, :, channel] = np.clip(
                source * (1.0 - alpha) + 255.0 * alpha,
                0,
                255,
            ).astype(np.uint8)
    card[80:121] = band
    return card


def test_digit_catalog_prepares_binary_glyphs_for_direct_multiscale_matching() -> None:
    reader = inventory.load_inventory_digit_catalog()

    assert reader["schema_version"] == 2
    assert reader["recognition_mode"] == "raw_grayscale_multiscale_template"
    assert reader["preprocess"] == "none"
    assert reader["template_scales"] == (0.64, 0.70)
    assert len(reader["_direct_templates"]) == 96
    assert len(reader["_direct_template_meta"]) == 96
    assert {template.shape for template in reader["_direct_templates"]} == {
        (14, 12),
        (15, 13),
    }
    assert all(
        set(int(value) for value in np.unique(sample)).issubset({0, 255})
        for samples in reader["_templates"].values()
        for sample in samples
    )


def test_digit_catalog_rejects_source_image_preprocessing(tmp_path: Path) -> None:
    payload = json.loads(DIGIT_CATALOG.read_text(encoding="utf-8"))
    payload["preprocess"] = "normalize"
    catalog_path = tmp_path / "inventory_digits.json"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="preprocess=none"):
        inventory.load_inventory_digit_catalog(
            catalog_path,
            plan_root=PLAN_ROOT,
        )


@pytest.mark.parametrize("category", ("items", "materials"))
def test_all_stack_count_categories_share_the_direct_digit_reader(category: str) -> None:
    catalog = inventory.prepare_inventory_catalog(category, ResolvingVision())

    assert catalog["_count_mode"] == inventory._COUNT_MODE_DIGIT_TEMPLATE
    assert catalog["_digit_reader"]["recognition_mode"] == (
        "raw_grayscale_multiscale_template"
    )
    assert catalog["_digit_reader"]["preprocess"] == "none"


def test_raw_direct_matching_reads_white_and_blue_counts_in_one_framework_call(
    monkeypatch,
) -> None:
    reader = inventory.load_inventory_digit_catalog()
    cards = [
        _raw_count_card("35", reader, background="white"),
        _raw_count_card("62", reader, background="white"),
        _raw_count_card("2908", reader, background="blue"),
    ]
    vision = SynchronousFrameworkVision()

    monkeypatch.setattr(
        inventory,
        "build_quantity_white_mask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bottom count recognition must not build a binary mask")
        ),
    )
    counts = inventory.read_inventory_counts(
        cards,
        reader,
        vision,
        item_ids=["white_35", "white_62", "blue_2908"],
    )

    assert counts == [35, 62, 2908]
    assert len(vision.calls) == 1
    call = vision.calls[0]
    assert call["preprocess"] == "none"
    assert call["use_grayscale"] is True
    assert call["match_method"] == cv2.TM_CCOEFF_NORMED
    assert call["nms_threshold"] == 1.0
    assert len(call["template_images"]) == 96


def test_direct_digit_run_prefers_complete_right_aligned_sequence() -> None:
    reader = inventory.load_inventory_digit_catalog()
    candidates = [
        {"digit": "3", "score": 0.600, "rect": [83, 19, 12, 14]},
        {"digit": "5", "score": 0.537, "rect": [94, 19, 12, 14]},
        {"digit": "4", "score": 0.597, "rect": [105, 19, 12, 14]},
        {"digit": "8", "score": 0.910, "rect": [45, 4, 12, 14]},
    ]

    run, average = inventory._select_quantity_digit_run(
        candidates,
        reader,
        band_width=122,
    )

    assert "".join(candidate["digit"] for candidate in run) == "354"
    assert average == pytest.approx((0.600 + 0.537 + 0.597) / 3)


def test_direct_templates_cover_all_ten_digit_classes() -> None:
    reader = inventory.load_inventory_digit_catalog()
    values = ["10", "21", "32", "43", "54", "65", "76", "87", "98"]
    cards = [
        _raw_count_card(value, reader, background="blue")
        for value in values
    ]

    counts = inventory.read_inventory_counts(
        cards,
        reader,
        SynchronousFrameworkVision(),
        item_ids=[f"digits_{value}" for value in values],
    )

    assert counts == [int(value) for value in values]

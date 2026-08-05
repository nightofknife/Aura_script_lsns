from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from plans.aura_base.src.actions.vision_actions import (
    find_best_template_in_set,
    find_templates_in_set,
)
from plans.aura_base.src.services.vision_service import MatchResult


class _App:
    def capture(self, rect=None):
        del rect
        return SimpleNamespace(success=True, image=np.zeros((100, 100, 3), dtype=np.uint8))


class _Vision:
    def __init__(self, root: Path):
        self.root = root
        self.batch_calls = []

    def expand_templates(self, plan_name, templates_ref, plan_path):
        del plan_name, templates_ref, plan_path
        return [self.root / "bronze.png", self.root / "silver.png"]

    def resolve_template(self, plan_name, template, plan_path):
        del plan_name, plan_path
        return self.root / Path(template).name

    def find_templates_batch(self, **kwargs):
        self.batch_calls.append(kwargs)
        return [
            MatchResult(found=True, top_left=(1, 2), center_point=(6, 7), rect=(1, 2, 10, 10), confidence=0.91),
            MatchResult(found=True, top_left=(3, 4), center_point=(8, 9), rect=(3, 4, 10, 10), confidence=0.97),
        ]


def _engine(root: Path):
    return SimpleNamespace(
        orchestrator=SimpleNamespace(current_plan_path=root, plan_name="test_plan")
    )


def test_template_set_matching_resolves_and_reuses_one_mask(tmp_path):
    vision = _Vision(tmp_path)

    result = find_templates_in_set(
        app=_App(),
        vision=vision,
        engine=_engine(tmp_path),
        templates_ref="templates/cards/*.png",
        region=(100, 200, 300, 400),
        threshold=0.84,
        use_grayscale=False,
        match_method=cv2.TM_SQDIFF_NORMED,
        mask="templates/card_mask.png",
    )

    assert result["count"] == 2
    assert result["matches"][0]["match"].center_point == (106, 207)
    call = vision.batch_calls[0]
    assert call["mask_images"] == [
        str(tmp_path / "card_mask.png"),
        str(tmp_path / "card_mask.png"),
    ]
    assert call["match_method"] == cv2.TM_SQDIFF_NORMED
    assert call["use_grayscale"] is False


def test_best_template_in_set_preserves_masked_batch_result(tmp_path):
    vision = _Vision(tmp_path)

    result = find_best_template_in_set(
        app=_App(),
        vision=vision,
        engine=_engine(tmp_path),
        templates_ref="templates/cards/*.png",
        threshold=0.84,
        use_grayscale=False,
        match_method=cv2.TM_SQDIFF_NORMED,
        mask="templates/card_mask.png",
    )

    assert Path(result["template"]).name == "silver.png"
    assert result["match"].confidence == 0.97
    assert vision.batch_calls[0]["mask_images"] == [
        str(tmp_path / "card_mask.png"),
        str(tmp_path / "card_mask.png"),
    ]

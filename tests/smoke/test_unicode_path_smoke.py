"""Runtime smoke coverage for template files below Unicode filesystem paths."""

from __future__ import annotations

import asyncio

import cv2
import numpy as np

from plans.aura_base.src.services.vision_service import VisionService


def test_vision_template_matching_supports_unicode_path(tmp_path) -> None:
    template = np.zeros((16, 16), dtype=np.uint8)
    cv2.rectangle(template, (2, 2), (13, 13), 180, 2)
    cv2.line(template, (3, 12), (12, 3), 255, 1)
    success, encoded = cv2.imencode(".png", template)
    assert success

    template_path = tmp_path / "中文路径" / "交易所模板.png"
    template_path.parent.mkdir(parents=True)
    template_path.write_bytes(encoded.tobytes())

    source = np.zeros((32, 32), dtype=np.uint8)
    source[8:24, 10:26] = template
    vision = VisionService()

    loaded = vision.load_image_file(template_path, cv2.IMREAD_GRAYSCALE)
    assert np.array_equal(loaded, template)

    match = asyncio.run(
        vision.find_template_async(
            source_image=source,
            template_image=str(template_path),
            threshold=0.99,
            use_grayscale=True,
        )
    )
    assert match.found is True
    assert match.top_left == (10, 8)

"""Supplied screenshot replay through the real vision service, with fake inputs."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import trailer_service_pc_actions as m
from plans.resonance_pc.src.actions import _trailer_service_vision as v


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/trailer_service"


def frame(name):
    return np.array(Image.open(FIXTURES / f"{name}_client.png").convert("RGB"))


class Vision:
    def __init__(self):
        self.service = VisionService()

    def find_template(self, **kwargs):
        return asyncio.run(self.service.find_template_async(**kwargs))


@pytest.mark.parametrize("name", ["map", "gold", "mileage"])
def test_supplied_screens_never_prove_departure(name):
    reader = v.TrailerVision(vision=Vision())
    assert m._departure_observation(reader, frame(name)) is None


@pytest.mark.parametrize("payment", ["gold", "mileage"])
def test_real_template_coordinates_drive_only_expected_screenshot_controls(monkeypatch, payment):
    images = {name: frame(name) for name in ("map", "gold", "mileage")}
    state = {"page": "map", "events": []}
    def capture():
        return SimpleNamespace(success=True, image=images.get(state["page"], images["map"]))
    def click(*, x, y):
        if state["page"] == "map":
            assert 728 <= x <= 930 and 611 <= y <= 671
            state["page"] = "gold"
            state["events"].append("open")
        elif (x, y) == v.PAYMENT_POINTS["mileage"]:
            assert state["page"] == "gold" and payment == "mileage"
            state["page"] = "mileage"
            state["events"].append("mileage")
        else:
            assert state["page"] == payment
            assert 806 <= x <= 1000 and 498 <= y <= 545
            state["events"].append("confirm")
            state["page"] = "traveling"
    def recognize_all(*, source_image):
        assert source_image.shape == (28, 80, 3)
        return SimpleNamespace(results=[SimpleNamespace(text="7/8", confidence=.995, center_point=(41, 15))])
    reader = v.TrailerVision(vision=Vision())
    monkeypatch.setattr(m, "TrailerVision", lambda **kwargs: reader)
    # No travel screenshot was supplied. Only the post-submit travel observation is simulated.
    monkeypatch.setattr(m, "_departure_observation", lambda *args:
                        {"state": "traveling"} if state["page"] == "traveling" else None)
    result = m.resonance_pc_use_trailer_service_from_map(
        daily_first_n=2, payment=payment, app=SimpleNamespace(capture=capture, click=click),
        ocr=SimpleNamespace(recognize_all=recognize_all), vision=object(),
    )
    assert result["success"] and result["submitted"] and result["departed"]
    assert state["events"] == (["open", "mileage", "confirm"] if payment == "mileage"
                               else ["open", "confirm"])

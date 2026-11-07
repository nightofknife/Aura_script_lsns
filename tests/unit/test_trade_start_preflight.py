from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import yaml

from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _depart_button_vision as departure
from plans.resonance_pc.src.actions import runtime_preflight_pc_actions as preflight


@pytest.mark.parametrize(("layout", "expected_error"), [
    ("standard", None),
    ("oversized", "trade_start_hud_oversized"),
    ("unknown", "trade_start_screen_unconfirmed"),
])
def test_trade_start_classifies_the_departure_button_with_one_capture(layout, expected_error):
    image = np.full((100, 160, 3), 17, np.uint8)
    if layout == "standard":
        fixture = Path(__file__).resolve().parents[1] / "fixtures/depart_button/actual-matched-crop.png"
        with Image.open(fixture) as template:
            image[30:72, 20:126] = np.asarray(template.convert("RGB"))
    elif layout == "oversized":
        image[20:71, 17:147] = departure._load_oversized_template()

    captures = []

    def capture(*, rect):
        captures.append(rect)
        return SimpleNamespace(success=True, image=image)

    app = SimpleNamespace(capture=capture)
    vision = VisionService()
    if expected_error is None:
        result = preflight.resonance_pc_require_trade_start_screen(app=app, vision=vision)
        assert result["ok"] is True and result["page_state"] == "city_main"
    else:
        with pytest.raises(departure.DepartButtonError) as failure:
            preflight.resonance_pc_require_trade_start_screen(app=app, vision=vision)
        assert failure.value.code == expected_error
    assert captures == [departure.REGION]


def test_freight_waits_for_preflight_and_preflight_requires_resolution_first():
    tasks = Path(__file__).resolve().parents[2] / "plans/resonance_pc/tasks"
    freight = yaml.safe_load((tasks / "auto_cycle_trade_pc.yaml").read_text(encoding="utf-8"))
    steps = freight["auto_cycle_trade_pc"]["steps"]
    assert steps["run"]["depends_on"] == "preflight"
    assert steps["preflight"] == {
        "action": "aura.run_task",
        "params": {"task_ref": "tasks:trade_start_preflight_pc.yaml:trade_start_preflight_pc", "inputs": {}},
    }
    guard = yaml.safe_load((tasks / "trade_start_preflight_pc.yaml").read_text(encoding="utf-8"))
    checks = guard["trade_start_preflight_pc"]["steps"]
    assert checks["require_resolution"]["action"] == "resonance_pc.require_client_resolution"
    assert checks["require_start_screen"]["depends_on"] == "require_resolution"
    assert checks["require_start_screen"]["action"] == "resonance_pc.require_trade_start_screen"

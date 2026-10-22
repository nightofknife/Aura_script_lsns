from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from packages.aura_core.packaging.manifest.generator import ManifestGenerator
from plans.aura_base.src.actions import interaction_actions, ocr_actions, wait_actions
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService


class _Session:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[None, 3, 48, None])]

    def run(self, _outputs, _inputs):
        self.calls += 1
        return [self.output]


def _service():
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 64},
        "rec_postprocess": {"character_dict": ["A", "B"]},
    })
    # CTC sequence A, A, blank, B decodes to AB.
    rec_output = np.asarray([[[0, 1, 0], [0, 1, 0], [1, 0, 0], [0, 0, 1]]], dtype=np.float32)
    sessions = OcrOnnxSessions(
        det=_Session(np.ones((1, 1, 64, 64), dtype=np.float32)),
        rec=_Session(rec_output),
        textline_orientation=_Session(np.asarray([[1, 0]], dtype=np.float32)),
    )
    service = OcrService()
    service._engine = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider="CPUExecutionProvider")
    service._engine_provider = "CPUExecutionProvider"
    return service, sessions


@pytest.mark.parametrize("recognition_only", [False, True])
def test_ocr_service_runs_only_requested_stages(recognition_only):
    service, sessions = _service()
    result = asyncio.run(service._recognize_all_async(
        np.zeros((32, 80, 3), dtype=np.uint8), recognition_only=recognition_only,
    ))

    assert result.count == 1
    assert result.results[0].text == "AB"
    assert sessions.rec.calls == 1
    assert sessions.det.calls == (0 if recognition_only else 1)
    assert sessions.textline_orientation.calls == (0 if recognition_only else 1)
    if recognition_only:
        assert result.results[0].rect == (0, 0, 80, 32)
        assert result.results[0].center_point == (40, 16)
        assert result.results[0].debug_info["recognition_only"] is True


class _App:
    def __init__(self):
        self.clicks = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=np.zeros((32, 80, 3), dtype=np.uint8))

    def move_to(self, *_args, **_kwargs):
        pass

    def click(self, x, y, button="left"):
        self.clicks.append((x, y, button))


@pytest.mark.parametrize("entry", [
    "find_text", "normalized_find_text", "recognize_all_text", "get_text_in_region",
    "check_text_exists", "assert_text_exists", "assert_text_not_exists", "assert_text_equals",
    "find_text_and_click", "wait_for_text", "wait_for_text_to_disappear",
])
def test_ocr_actions_forward_recognition_only_and_region(monkeypatch, entry):
    service, sessions = _service()
    # Keep the public sync bridge in this test independent of the scheduler.
    monkeypatch.setattr(service, "_submit_to_loop_and_wait", asyncio.run)
    app = _App()
    engine = SimpleNamespace(root_context=SimpleNamespace(data={}))
    kwargs = {"app": app, "ocr": service, "region": (100, 200, 80, 32), "recognition_only": True}

    if entry in {"recognize_all_text", "get_text_in_region"}:
        result = getattr(ocr_actions, entry)(**kwargs)
        if entry == "recognize_all_text":
            assert result.results[0].center_point == (140, 216)
            assert result.results[0].rect == (100, 200, 80, 32)
        else:
            assert result == "AB"
    else:
        kwargs["engine"] = engine
        kwargs["text_to_find"] = "AB"
        if entry == "normalized_find_text":
            result = ocr_actions.find_text(**kwargs, normalize=True)
        elif entry.startswith("wait_for_text"):
            kwargs["timeout"] = 0.0
            if entry.endswith("to_disappear"):
                kwargs["text_to_monitor"] = "missing"
                del kwargs["text_to_find"]
            result = asyncio.run(getattr(wait_actions, entry)(**kwargs))
        elif entry == "find_text_and_click":
            result = interaction_actions.find_text_and_click(**kwargs)
            assert app.clicks == [(140, 216, "left")]
        else:
            if entry == "assert_text_equals":
                kwargs["expected_value"] = "AB"
            elif entry == "assert_text_not_exists":
                kwargs["text_to_find"] = "missing"
            result = getattr(ocr_actions, entry)(**kwargs)
        if entry in {"find_text", "normalized_find_text", "wait_for_text"}:
            assert result.found is True
            assert result.center_point == (140, 216)
        else:
            assert result is True

    assert sessions.det.calls == 0
    assert sessions.textline_orientation.calls == 0
    assert sessions.rec.calls == 1


def test_framework_manifest_removes_composite_service_and_keeps_ocr_exports():
    root = Path(__file__).resolve().parents[2]
    plan = root / "plans" / "aura_base"
    saved = yaml.safe_load((plan / "manifest.yaml").read_text(encoding="utf-8"))["exports"]
    generated = ManifestGenerator(plan).generate()["exports"]

    assert saved["services"] == generated["services"]
    assert saved["actions"] == generated["actions"]
    assert not (plan / "src/services/composite_interaction_service.py").exists()
    assert "composite-interaction" not in {item["name"] for item in saved["services"]}
    assert "ocr" in {item["name"] for item in saved["services"]}
    for name in ("find_text", "recognize_all_text", "get_text_in_region", "check_text_exists",
                 "assert_text_exists", "assert_text_not_exists", "assert_text_equals",
                 "find_text_and_click", "wait_for_text", "wait_for_text_to_disappear"):
        action = next(item for item in saved["actions"] if item["name"] == name)
        parameter = next(item for item in action["parameters"] if item["name"] == "recognition_only")
        assert parameter["default"] is False
        assert parameter["required"] is False
        resize_parameter = next(item for item in action["parameters"] if item["name"] == "det_resize_long")
        assert resize_parameter["default"] is None
        assert resize_parameter["required"] is False

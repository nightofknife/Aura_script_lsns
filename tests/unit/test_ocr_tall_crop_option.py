"""Per-call tall-crop rotation without changing detection or 180-degree classification."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from plans.aura_base.src.actions import interaction_actions, ocr_actions, wait_actions
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService
from plans.aura_base.src.services.observation_service import ObservationService


_BOX = np.asarray([[10, 2], [26, 2], [26, 30], [10, 30]], dtype=np.float32)
_ACTIONS = (
    "find_text", "recognize_all_text", "get_text_in_region", "check_text_exists",
    "assert_text_exists", "assert_text_not_exists", "assert_text_equals",
    "find_text_and_click", "wait_for_text", "wait_for_text_to_disappear",
)


class _Session:
    def __init__(self, stage):
        self.stage = stage
        self.tensors = []

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[None, 3, None, None])]

    def run(self, _outputs, inputs):
        tensor = inputs["image"]
        self.tensors.append(tensor.copy())
        if self.stage == "det":
            return [np.zeros((len(tensor), 1, *tensor.shape[2:]), dtype=np.float32)]
        if self.stage == "ori":
            return [np.tile(np.asarray([[1., 0.]], dtype=np.float32), (len(tensor), 1))]
        prediction = np.asarray([[[0, 1, 0], [1, 0, 0], [0, 0, 1]]], dtype=np.float32)
        return [np.tile(prediction, (len(tensor), 1, 1))]


def _image():
    return np.arange(32 * 80 * 3, dtype=np.uint8).reshape(32, 80, 3)


def _service(monkeypatch, box=_BOX):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 64},
        "rec_postprocess": {"character_dict": ["A", "B"]},
    })
    sessions = OcrOnnxSessions(det=_Session("det"), rec=_Session("rec"),
                               textline_orientation=_Session("ori"))
    pipeline = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider="CPUExecutionProvider")
    monkeypatch.setattr(pipeline, "_postprocess_det", lambda *_args: [box.copy()])
    service = OcrService()
    service._engine = pipeline
    service._engine_provider = "CPUExecutionProvider"
    monkeypatch.setattr(service, "_submit_to_loop_and_wait", asyncio.run)
    seen = []
    original_recognize = pipeline._recognize_crops

    def recognize(crops, **kwargs):
        seen.extend(crop.copy() for crop in crops)
        return original_recognize(crops, **kwargs)

    monkeypatch.setattr(pipeline, "_recognize_crops", recognize)
    return service, sessions, seen


def _options(value):
    return {} if value is None else {"rotate_tall_crops": value}


@pytest.mark.parametrize("rotate", [None, False, True])
@pytest.mark.parametrize("width,height", [(16, 28), (16, 24), (16, 23), (40, 16)])
def test_detected_crop_rotation_has_explicit_default_and_exact_threshold(monkeypatch, rotate, width, height):
    box = np.asarray([[10, 2], [10 + width, 2], [10 + width, 2 + height], [10, 2 + height]],
                     dtype=np.float32)
    service, sessions, seen = _service(monkeypatch, box)
    image = _image()
    raw = service._engine.predict(image, use_textline_orientation=False, **_options(rotate))[0]
    # This integer-aligned perspective transform must preserve the original pixels.
    expected = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)[2:2 + height, 10:10 + width]
    if rotate is True and height / width >= 1.5:
        expected = cv2.rotate(expected, cv2.ROTATE_90_CLOCKWISE)
    np.testing.assert_array_equal(seen[0], expected)
    np.testing.assert_array_equal(raw["rec_polys"][0], box)
    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 1
    assert not sessions.textline_orientation.tensors


def test_skewed_box_still_uses_perspective_rectification_when_rotation_is_disabled(monkeypatch):
    box = np.asarray([[10, 2], [27, 4], [24, 30], [8, 28]], dtype=np.float32)
    service, _, seen = _service(monkeypatch, box)
    service._engine.predict(_image(), rotate_tall_crops=False, use_textline_orientation=False)
    width = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    height = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    destination = np.asarray([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    expected = cv2.warpPerspective(cv2.cvtColor(_image(), cv2.COLOR_RGB2BGR),
                                  cv2.getPerspectiveTransform(box, destination), (width, height),
                                  borderMode=cv2.BORDER_REPLICATE)
    np.testing.assert_array_equal(seen[0], expected)
    assert seen[0].shape[0] > seen[0].shape[1]


@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("orientation", [False, True])
def test_90_degree_geometry_and_180_degree_classifier_are_independent(monkeypatch, rotate, orientation):
    service, sessions, seen = _service(monkeypatch)

    def inverted(_outputs, inputs):
        tensor = inputs["image"]
        sessions.textline_orientation.tensors.append(tensor.copy())
        return [np.tile(np.asarray([[0., 1.]], dtype=np.float32), (len(tensor), 1))]

    monkeypatch.setattr(sessions.textline_orientation, "run", inverted)
    raw = service._engine.predict(_image(), rotate_tall_crops=rotate,
                                  use_textline_orientation=orientation)[0]
    expected = cv2.cvtColor(_image(), cv2.COLOR_RGB2BGR)[2:30, 10:26]
    if rotate:
        expected = cv2.rotate(expected, cv2.ROTATE_90_CLOCKWISE)
    if orientation:
        expected = cv2.rotate(expected, cv2.ROTATE_180)
    np.testing.assert_array_equal(seen[0], expected)
    np.testing.assert_array_equal(raw["rec_polys"][0], _BOX)
    assert len(sessions.textline_orientation.tensors) == int(orientation)


@pytest.mark.parametrize("rotate", [False, True])
def test_recognition_only_bypasses_crop_and_both_rotation_steps(monkeypatch, rotate):
    service, sessions, seen = _service(monkeypatch)

    def unexpected_crop(*_args, **_kwargs):
        pytest.fail("recognition_only must not create a detection crop")

    monkeypatch.setattr(service._engine, "_get_rotate_crop_image", unexpected_crop)
    image = _image()[:, :12]
    raw = service._engine.predict(image, recognition_only=True, rotate_tall_crops=rotate,
                                  use_textline_orientation=True)[0]
    np.testing.assert_array_equal(seen[0], cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    assert raw["rec_texts"] == ["AB"]
    assert not sessions.det.tensors and not sessions.textline_orientation.tensors
    assert len(sessions.rec.tensors) == 1


@pytest.mark.parametrize("method", ["find_text", "find_all_text", "recognize_text", "recognize_all"])
@pytest.mark.parametrize("rotate", [None, False, True])
def test_all_public_service_routes_preserve_geometry_and_forward_rotation(monkeypatch, method, rotate):
    service, sessions, seen = _service(monkeypatch)
    args = ("AB", _image()) if method.startswith("find") else (_image(),)
    result = getattr(service, method)(*args, **_options(rotate))
    values = result.results if hasattr(result, "results") else [result]
    assert [v.text for v in values] == ["AB"]
    assert values[0].rect == (10, 2, 16, 28)
    assert values[0].center_point == (18, 16)
    assert seen[0].shape[:2] == ((16, 28) if rotate is True else (28, 16))
    assert len(sessions.textline_orientation.tensors) == 1


class _App:
    def __init__(self):
        self.clicks = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=_image(), relative_rect=rect)

    def move_to(self, *_args, **_kwargs):
        pass

    def click(self, x, y, button="left"):
        self.clicks.append((x, y, button))


@pytest.mark.parametrize("entry", (*_ACTIONS, "normalized_find_text", "normalized_wait_for_text",
                                   "normalized_wait_for_text_to_disappear"))
@pytest.mark.parametrize("rotate", [None, False, True])
def test_action_and_observation_routes_forward_rotation_without_moving_results(monkeypatch, entry, rotate):
    service, sessions, seen = _service(monkeypatch)
    app = _App()
    engine = SimpleNamespace(root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": True}}))
    kwargs = dict(app=app, ocr=service, region=(100, 200, 80, 32), **_options(rotate))
    name = entry.removeprefix("normalized_")
    if entry.startswith("normalized_"):
        kwargs["normalize"] = True
    if name in {"recognize_all_text", "get_text_in_region"}:
        result = getattr(ocr_actions, name)(**kwargs)
        if name == "recognize_all_text":
            assert result.results[0].rect == (110, 202, 16, 28)
            assert result.results[0].center_point == (118, 216)
        else:
            assert result == "AB"
    else:
        kwargs.update(engine=engine, text_to_find="AB")
        if name.startswith("wait_for_text"):
            # Exercise the injected current ObservationService as well as its
            # normalized recognize_all route, rather than replacing it with a stub.
            kwargs.update(timeout=0., observation=ObservationService(app, None, service))
            if name.endswith("to_disappear"):
                kwargs["text_to_monitor"] = "missing"
                del kwargs["text_to_find"]
            result = asyncio.run(getattr(wait_actions, name)(**kwargs))
        elif name == "find_text_and_click":
            result = interaction_actions.find_text_and_click(**kwargs)
            assert app.clicks == [(118, 216, "left")]
        else:
            if name == "assert_text_equals":
                kwargs["expected_value"] = "AB"
            elif name == "assert_text_not_exists":
                kwargs["text_to_find"] = "missing"
            result = getattr(ocr_actions, name)(**kwargs)
        if name in {"find_text", "wait_for_text"}:
            assert result.found is True
            assert result.center_point == (118, 216)
            assert result.rect == (110, 202, 16, 28)
            assert result.debug_info["params"]["rotate_tall_crops"] is (rotate is True)
        else:
            assert result is True
    assert len(seen) == 1
    assert seen[0].shape[:2] == ((16, 28) if rotate is True else (28, 16))
    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 1
    assert len(sessions.textline_orientation.tensors) == 1


def test_queued_requests_keep_their_own_rotation_and_leave_default_unchanged(monkeypatch):
    service, sessions, _ = _service(monkeypatch)
    pipeline = service._engine
    metadata = pipeline.metadata.model_dump()
    options = vars(pipeline.recognition_options).copy()

    def recognize(crops, **_kwargs):
        return [f"{int(c[0, 0, 0])}:{c.shape[0]}x{c.shape[1]}" for c in crops], [1.] * len(crops)

    monkeypatch.setattr(pipeline, "_recognize_crops", recognize)

    async def run():
        flags = (True, False, True, False)
        results = await asyncio.gather(*(
            service._recognize_all_async(np.full((32, 80, 3), 10 + index, np.uint8),
                                         rotate_tall_crops=flag)
            for index, flag in enumerate(flags)
        ))
        assert [r.results[0].text for r in results] == ["10:16x28", "11:28x16", "12:16x28", "13:28x16"]
        default = await service._recognize_all_async(np.full((32, 80, 3), 20, np.uint8))
        assert default.results[0].text == "20:28x16"

    asyncio.run(run())
    assert pipeline.metadata.model_dump() == metadata
    assert vars(pipeline.recognition_options) == options
    assert service._engine is pipeline and pipeline.sessions is sessions


def test_call_rotation_choices_do_not_change_full_startup_warmup(monkeypatch):
    service, sessions, _ = _service(monkeypatch)

    async def run():
        for flag in (True, False):
            await service._recognize_all_async(_image(), rotate_tall_crops=flag,
                                               use_textline_orientation=False)
        for session in (sessions.det, sessions.rec, sessions.textline_orientation):
            session.tensors.clear()
        assert await service._warmup_engine_async() is True
        assert service._warmed_engine is service._engine
        assert len(sessions.det.tensors) == 1
        assert [t.shape[0] for t in sessions.rec.tensors] == [1, 8]
        assert [t.shape[0] for t in sessions.textline_orientation.tensors] == [1, 8]
        await service._recognize_all_async(_image())
        assert len(sessions.textline_orientation.tensors) == 3

    asyncio.run(run())
    assert service._engine.sessions is sessions


def test_public_defaults_and_action_manifest_expose_independent_options():
    root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / "plans/aura_base/manifest.yaml").read_text(encoding="utf-8"))
    exports = {a["name"]: a for a in manifest["exports"]["actions"]}
    for name in _ACTIONS:
        module = (wait_actions if name.startswith("wait_for") else interaction_actions
                  if name == "find_text_and_click" else ocr_actions)
        params = inspect.signature(getattr(module, name)).parameters
        assert params["rotate_tall_crops"].default is False
        assert params["use_textline_orientation"].default is True
        setting = next(p for p in exports[name]["parameters"] if p["name"] == "rotate_tall_crops")
        assert setting["default"] is False and setting["required"] is False
    for method in (OcrService.find_text, OcrService.find_all_text, OcrService.recognize_text,
                   OcrService.recognize_all, ObservationService.find_text, OnnxOcrPipeline.predict):
        params = inspect.signature(method).parameters
        assert params["rotate_tall_crops"].default is False
        assert params["rotate_tall_crops"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["use_textline_orientation"].default is True

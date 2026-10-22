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
from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService


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
            return [np.tile(np.asarray([[1, 0]], dtype=np.float32), (len(tensor), 1))]
        # A, blank, B, blank -> AB, independently for every crop in the batch.
        return [np.tile(np.asarray([[[0, 1, 0], [1, 0, 0], [0, 0, 1], [1, 0, 0]]],
                                   dtype=np.float32), (len(tensor), 1, 1))]


def _service(monkeypatch, *, pipeline_enabled=True, metadata_enabled=True, session_available=True):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 64},
        "rec_postprocess": {"character_dict": ["A", "B"]},
        "pipeline": {"use_textline_orientation": pipeline_enabled},
        "textline_orientation": {"enabled": metadata_enabled},
    })
    sessions = OcrOnnxSessions(
        det=_Session("det"), rec=_Session("rec"),
        textline_orientation=_Session("ori") if session_available else None,
    )
    pipeline = OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider="CPUExecutionProvider")
    # A deterministic detected box smaller than the input distinguishes full
    # detection/cropping from recognition_only, including click coordinates.
    box = np.asarray([[10, 4], [50, 4], [50, 20], [10, 20]], dtype=np.float32)
    monkeypatch.setattr(pipeline, "_postprocess_det", lambda *_args: [box.copy()])
    service = OcrService()
    service._engine = pipeline
    service._engine_provider = "CPUExecutionProvider"
    monkeypatch.setattr(service, "_submit_to_loop_and_wait", asyncio.run)
    return service, sessions


def _image():
    return np.arange(32 * 80 * 3, dtype=np.uint8).reshape(32, 80, 3)


@pytest.mark.parametrize("orientation", [None, True, False])
@pytest.mark.parametrize("recognition_only", [False, True])
def test_pipeline_classification_toggle_preserves_detection_and_crop(monkeypatch, orientation, recognition_only):
    service, sessions = _service(monkeypatch)
    pipeline = service._engine
    crops = []
    original_crop = pipeline._get_rotate_crop_image

    def crop(image, box, **kwargs):
        value = original_crop(image, box, **kwargs)
        crops.append(value)
        return value

    monkeypatch.setattr(pipeline, "_get_rotate_crop_image", crop)
    options = {"recognition_only": recognition_only}
    if orientation is not None:
        options["use_textline_orientation"] = orientation
    timing = OcrTiming()
    result = pipeline.predict(_image(), timing=timing, **options)[0]

    assert result["rec_texts"] == ["AB"]
    assert len(sessions.det.tensors) == int(not recognition_only)
    assert len(crops) == int(not recognition_only)
    if crops:
        assert crops[0].shape[:2] == (16, 40)
    should_classify = not recognition_only and orientation is not False
    assert len(sessions.textline_orientation.tensors) == int(should_classify)
    assert len(sessions.rec.tensors) == 1
    assert timing.snapshot()["stage_calls"]["ori_inference"] == int(should_classify)
    expected_box = ([[0, 0], [80, 0], [80, 32], [0, 32]] if recognition_only
                    else [[10, 4], [50, 4], [50, 20], [10, 20]])
    np.testing.assert_array_equal(result["rec_polys"][0], expected_box)


@pytest.mark.parametrize("disabled", ["pipeline", "metadata", "session"])
def test_call_option_cannot_enable_unavailable_or_disabled_orientation(monkeypatch, disabled):
    service, sessions = _service(
        monkeypatch, pipeline_enabled=disabled != "pipeline",
        metadata_enabled=disabled != "metadata", session_available=disabled != "session",
    )
    metadata_before = service._engine.metadata.model_dump()
    result = service._engine.predict(_image(), use_textline_orientation=True)
    assert result[0]["rec_texts"] == ["AB"]
    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 1
    if sessions.textline_orientation is not None:
        assert not sessions.textline_orientation.tensors
    assert service._engine.metadata.model_dump() == metadata_before


@pytest.mark.parametrize("method", ["find_text", "find_all_text", "recognize_text", "recognize_all"])
@pytest.mark.parametrize("orientation", [None, False])
def test_public_service_methods_forward_per_call_option(monkeypatch, method, orientation):
    service, sessions = _service(monkeypatch)
    options = {} if orientation is None else {"use_textline_orientation": orientation}
    args = ("AB", _image()) if method.startswith("find") else (_image(),)
    result = getattr(service, method)(*args, **options)
    values = result.results if hasattr(result, "results") else [result]
    assert [value.text for value in values] == ["AB"]
    assert values[0].rect == (10, 4, 40, 16)
    assert values[0].center_point == (30, 12)
    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 1
    assert len(sessions.textline_orientation.tensors) == int(orientation is not False)


def test_concurrent_requests_do_not_mutate_shared_engine_options(monkeypatch):
    service, sessions = _service(monkeypatch)
    pipeline = service._engine
    original_metadata = pipeline.metadata.model_dump()

    async def run():
        results = await asyncio.gather(*(
            service._recognize_all_async(_image(), use_textline_orientation=flag)
            for flag in (False, True, False, True)
        ))
        assert all(result.results[0].text == "AB" for result in results)

    asyncio.run(run())
    assert service._engine is pipeline
    assert pipeline.metadata.model_dump() == original_metadata
    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 4
    assert len(sessions.textline_orientation.tensors) == 2


def test_disabling_classification_does_not_change_warmup_or_loaded_sessions(monkeypatch):
    service, sessions = _service(monkeypatch)
    pipeline = service._engine

    async def run():
        await service._recognize_all_async(_image(), use_textline_orientation=False)
        assert not sessions.textline_orientation.tensors
        assert await service._warmup_engine_async() is True
        assert service._warmed_engine is pipeline
        assert len(sessions.textline_orientation.tensors) == 2
        await service._recognize_all_async(_image(), use_textline_orientation=False)
        assert len(sessions.textline_orientation.tensors) == 2
        # The next call uses the default and the very same classifier session.
        await service._recognize_all_async(_image())
        assert len(sessions.textline_orientation.tensors) == 3

    asyncio.run(run())
    assert service._engine.sessions is sessions


def test_false_skips_180_degree_flip_without_changing_the_detected_polygon(monkeypatch):
    service, sessions = _service(monkeypatch)
    pipeline = service._engine
    crops_seen = []

    def classify_as_inverted(_outputs, inputs):
        sessions.textline_orientation.tensors.append(inputs["image"].copy())
        return [np.asarray([[0, 1]], dtype=np.float32)]

    def recognize(crops, **_kwargs):
        crops_seen.append(crops[0].copy())
        return ["AB"], [1.0]

    monkeypatch.setattr(sessions.textline_orientation, "run", classify_as_inverted)
    monkeypatch.setattr(pipeline, "_recognize_crops", recognize)
    result_off = pipeline.predict(_image(), use_textline_orientation=False)
    result_on = pipeline.predict(_image())

    np.testing.assert_array_equal(crops_seen[1], cv2.rotate(crops_seen[0], cv2.ROTATE_180))
    np.testing.assert_array_equal(result_off[0]["rec_polys"][0], result_on[0]["rec_polys"][0])
    assert len(sessions.textline_orientation.tensors) == 1


def test_false_preserves_perspective_crop_and_automatic_90_degree_rotation(monkeypatch):
    service, sessions = _service(monkeypatch)
    pipeline = service._engine
    box = np.asarray([[10, 2], [26, 2], [26, 30], [10, 30]], dtype=np.float32)
    monkeypatch.setattr(pipeline, "_postprocess_det", lambda *_args: [box.copy()])
    crops_seen = []

    def recognize(crops, **_kwargs):
        crops_seen.extend(crops)
        return ["AB"], [1.0]

    monkeypatch.setattr(pipeline, "_recognize_crops", recognize)
    result = pipeline.predict(_image(), use_textline_orientation=False, rotate_tall_crops=True)

    # A tall 16x28 detection crop is still rotated to a horizontal 28x16 image.
    assert crops_seen[0].shape[:2] == (16, 28)
    np.testing.assert_array_equal(
        crops_seen[0], pipeline._get_rotate_crop_image(
            pipeline._ensure_bgr_image(_image()), box, rotate_tall_crops=True,
        ),
    )
    np.testing.assert_array_equal(result[0]["rec_polys"][0], box)
    assert not sessions.textline_orientation.tensors


class _App:
    def __init__(self):
        self.clicks = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=_image())

    def move_to(self, *_args, **_kwargs):
        pass

    def click(self, x, y, button="left"):
        self.clicks.append((x, y, button))


_ACTION_NAMES = (
    "find_text", "recognize_all_text", "get_text_in_region", "check_text_exists",
    "assert_text_exists", "assert_text_not_exists", "assert_text_equals",
    "find_text_and_click", "wait_for_text", "wait_for_text_to_disappear",
)


@pytest.mark.parametrize("entry", (*_ACTION_NAMES, "normalized_find_text"))
@pytest.mark.parametrize("orientation", [None, False])
def test_actions_forward_orientation_option_and_keep_detected_coordinates(monkeypatch, entry, orientation):
    service, sessions = _service(monkeypatch)
    app = _App()
    engine = SimpleNamespace(root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": True}}))
    kwargs = {"app": app, "ocr": service, "region": (100, 200, 80, 32)}
    if orientation is not None:
        kwargs["use_textline_orientation"] = orientation

    if entry in {"recognize_all_text", "get_text_in_region"}:
        result = getattr(ocr_actions, entry)(**kwargs)
        if entry == "recognize_all_text":
            assert result.results[0].rect == (110, 204, 40, 16)
            assert result.results[0].center_point == (130, 212)
        else:
            assert result == "AB"
    else:
        kwargs.update(engine=engine, text_to_find="AB")
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
            assert app.clicks == [(130, 212, "left")]
        else:
            if entry == "assert_text_equals":
                kwargs["expected_value"] = "AB"
            elif entry == "assert_text_not_exists":
                kwargs["text_to_find"] = "missing"
            result = getattr(ocr_actions, entry)(**kwargs)
        if entry in {"find_text", "normalized_find_text", "wait_for_text"}:
            assert result.found is True
            assert result.center_point == (130, 212)
            assert result.rect == (110, 204, 40, 16)
            assert result.debug_info["params"]["use_textline_orientation"] is (orientation is not False)
        else:
            assert result is True

    assert len(sessions.det.tensors) == len(sessions.rec.tensors) == 1
    assert len(sessions.textline_orientation.tensors) == int(orientation is not False)


def test_public_signatures_and_manifest_keep_orientation_enabled_by_default():
    root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / "plans/aura_base/manifest.yaml").read_text(encoding="utf-8"))
    actions = {action["name"]: action for action in manifest["exports"]["actions"]}
    for name in _ACTION_NAMES:
        module = (wait_actions if name.startswith("wait_for") else interaction_actions
                  if name == "find_text_and_click" else ocr_actions)
        function = getattr(module, name)
        signature = inspect.signature(function)
        assert signature.parameters["use_textline_orientation"].default is True
        injected = getattr(function, "_service_dependencies", {})
        business_parameters = [parameter for parameter in signature.parameters if parameter not in injected]
        assert business_parameters[-2:] == ["use_textline_orientation", "rotate_tall_crops"]
        parameter = next(p for p in actions[name]["parameters"] if p["name"] == "use_textline_orientation")
        assert parameter["default"] is True
        assert parameter["required"] is False
    for name in ("find_text", "find_all_text", "recognize_text", "recognize_all"):
        parameter = inspect.signature(getattr(OcrService, name)).parameters["use_textline_orientation"]
        assert parameter.default is True
        assert parameter.kind == inspect.Parameter.KEYWORD_ONLY

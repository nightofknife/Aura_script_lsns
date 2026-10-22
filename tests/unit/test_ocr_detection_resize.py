from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.actions import interaction_actions, ocr_actions, wait_actions
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult, OcrService


def _pipeline(*, det=None, limit=960):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": limit},
        "det_postprocess": {"unclip_ratio": 1.0},
        "rec_postprocess": {"character_dict": ["A", "B"]},
    })
    return OnnxOcrPipeline(
        metadata=metadata,
        sessions=OcrOnnxSessions(det=det, rec=None),
        provider="CPUExecutionProvider",
    )


@pytest.mark.parametrize("source_size,expected_size", [
    ((200, 100), (192, 96)),
    ((640, 320), (640, 320)),
    ((1280, 720), (960, 544)),
    ((1920, 1080), (960, 544)),
])
def test_default_detection_size_caps_large_images_without_upscaling_small_images(source_size, expected_size):
    width, height = source_size
    expected_width, expected_height = expected_size
    image = np.random.default_rng(4).integers(0, 256, (height, width, 3), dtype=np.uint8)
    before = image.copy()

    tensor, shape = _pipeline()._preprocess_det(image)

    assert tensor.shape == (1, 3, expected_height, expected_width)
    assert tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    assert shape == {
        "src_h": float(height), "src_w": float(width),
        "resize_h": float(expected_height), "resize_w": float(expected_width),
        "ratio_h": expected_height / height, "ratio_w": expected_width / width,
    }
    np.testing.assert_array_equal(image, before)


def test_explicit_long_side_can_upscale_without_leaking_to_following_calls():
    pipeline = _pipeline()
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    metadata_before = pipeline.metadata.model_dump()

    first, _ = pipeline._preprocess_det(image)
    enlarged, _ = pipeline._preprocess_det(image, det_resize_long=960)
    smaller, _ = pipeline._preprocess_det(image, det_resize_long=128)
    following, _ = pipeline._preprocess_det(image)

    assert first.shape == following.shape == (1, 3, 96, 192)
    assert enlarged.shape == (1, 3, 480, 960)
    assert smaller.shape == (1, 3, 64, 128)
    assert pipeline.metadata.model_dump() == metadata_before


def test_default_limit_comes_from_model_metadata():
    tensor, _ = _pipeline(limit=320)._preprocess_det(np.zeros((320, 640, 3), dtype=np.uint8))
    assert tensor.shape == (1, 3, 160, 320)


@pytest.mark.parametrize("det_resize_long", [0, -32])
def test_invalid_explicit_long_side_is_rejected(det_resize_long):
    with pytest.raises(ValueError, match="det_resize_long"):
        _pipeline()._preprocess_det(np.zeros((32, 64, 3), dtype=np.uint8), det_resize_long=det_resize_long)


def test_aligned_original_size_skips_resize_and_preserves_normalization(monkeypatch):
    image = np.random.default_rng(5).integers(0, 256, (64, 128, 3), dtype=np.uint8)
    before = image.copy()
    pipeline = _pipeline()
    expected = image.astype(np.float32) * pipeline.metadata.det_preprocess.scale
    expected -= np.asarray(pipeline.metadata.det_preprocess.mean, dtype=np.float32)
    expected /= np.asarray(pipeline.metadata.det_preprocess.std, dtype=np.float32)

    def unexpected_resize(*_args, **_kwargs):
        pytest.fail("Already aligned input must not be copied through cv2.resize")

    monkeypatch.setattr(cv2, "resize", unexpected_resize)
    tensor, _ = pipeline._preprocess_det(image)

    np.testing.assert_array_equal(tensor, np.transpose(expected, (2, 0, 1))[None])
    np.testing.assert_array_equal(image, before)


@pytest.mark.parametrize("det_resize_long", [None, 160, 960])
def test_detection_boxes_are_restored_to_source_coordinates(det_resize_long):
    source_width, source_height = 320, 160
    expected_box = np.asarray([[40, 40], [240, 40], [240, 120], [40, 120]], dtype=np.float32)
    inputs = []

    class DetectionSession:
        def get_inputs(self):
            return [SimpleNamespace(name="image")]

        def run(self, _outputs, feed):
            tensor = feed["image"]
            inputs.append(tensor)
            height, width = tensor.shape[2:]
            scale = np.asarray([width / source_width, height / source_height])
            scaled_box = np.rint(expected_box * scale).astype(np.int32)
            mask = np.zeros((height, width), dtype=np.float32)
            cv2.fillConvexPoly(mask, scaled_box, 1.0)
            return [mask[None, None]]

    pipeline = _pipeline(det=DetectionSession())
    boxes = pipeline._detect_text_boxes(
        np.zeros((source_height, source_width, 3), dtype=np.uint8),
        det_resize_long=det_resize_long,
    )

    assert len(inputs) == len(boxes) == 1
    np.testing.assert_allclose(boxes[0], expected_box, atol=0.01)


def test_prediction_forwards_detection_override_and_recognition_only_bypasses_it(monkeypatch):
    pipeline = _pipeline()
    requests = []

    def detect(image, *, det_resize_long=None):
        requests.append(det_resize_long)
        return []

    monkeypatch.setattr(pipeline, "_detect_text_boxes", detect)
    monkeypatch.setattr(pipeline, "_recognize_crops", lambda _crops: (["AB"], [0.9]))
    image = np.zeros((32, 80, 3), dtype=np.uint8)

    pipeline.predict(image, det_resize_long=960)
    result = pipeline.predict(image, recognition_only=True, det_resize_long=960)
    pipeline.predict(image)

    assert requests == [960, None]
    assert result[0]["rec_texts"] == ["AB"]
    np.testing.assert_array_equal(result[0]["rec_polys"][0], [[0, 0], [80, 0], [80, 32], [0, 32]])


@pytest.mark.parametrize("entry", ["find_text", "find_all_text", "recognize_text", "recognize_all"])
@pytest.mark.parametrize("det_resize_long", [None, 960])
def test_all_public_service_entries_forward_detection_override(monkeypatch, entry, det_resize_long):
    calls = []

    class Pipeline:
        def predict(self, image, *, recognition_only=False, det_resize_long=None,
                    use_textline_orientation=True, rotate_tall_crops=False):
            assert use_textline_orientation is True
            assert rotate_tall_crops is False
            calls.append((image, recognition_only, det_resize_long))
            return [{
                "rec_texts": ["AB"], "rec_scores": [0.9],
                "rec_polys": [np.asarray([[0, 0], [80, 0], [80, 32], [0, 32]])],
            }]

    service = OcrService()
    service._engine = Pipeline()
    monkeypatch.setattr(service, "_submit_to_loop_and_wait", asyncio.run)
    image = np.zeros((32, 80, 3), dtype=np.uint8)
    kwargs = {"source_image": image, "det_resize_long": det_resize_long}
    if entry.startswith("find"):
        kwargs["text_to_find"] = "AB"

    result = getattr(service, entry)(**kwargs)

    assert getattr(result, "count", 1) == 1
    assert len(calls) == 1
    assert calls[0][0] is image
    assert calls[0][1:] == (False, det_resize_long)


@pytest.mark.parametrize("entry", [
    "find_text", "normalized_find_text", "recognize_all_text", "get_text_in_region",
    "check_text_exists", "assert_text_exists", "assert_text_not_exists", "assert_text_equals",
    "find_text_and_click", "wait_for_text", "wait_for_text_to_disappear",
])
@pytest.mark.parametrize("det_resize_long", [None, 960])
def test_all_action_entries_forward_detection_override_and_keep_region_offsets(entry, det_resize_long):
    calls = []
    clicks = []

    def find_text(*, source_image, recognition_only=False, det_resize_long=None, **kwargs):
        calls.append((recognition_only, det_resize_long))
        if kwargs.get("text_to_find") == "missing":
            return OcrResult(found=False)
        return OcrResult(found=True, text="AB", confidence=0.9,
                         rect=(10, 4, 40, 20), center_point=(30, 14))

    def recognize_all(**kwargs):
        return MultiOcrResult(count=1, results=[find_text(**kwargs)])

    service = SimpleNamespace(find_text=find_text, recognize_all=recognize_all)
    app = SimpleNamespace(
        capture=lambda rect: SimpleNamespace(success=True, image=np.zeros((32, 80, 3), dtype=np.uint8)),
        move_to=lambda *_args, **_kwargs: None,
        click=lambda x, y, button="left": clicks.append((x, y, button)),
    )
    engine = SimpleNamespace(root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": True}}))
    kwargs = {"app": app, "ocr": service, "region": (100, 200, 80, 32),
              "det_resize_long": det_resize_long}
    if entry in {"recognize_all_text", "get_text_in_region"}:
        result = getattr(ocr_actions, entry)(**kwargs)
        if entry == "recognize_all_text":
            assert result.results[0].rect == (110, 204, 40, 20)
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
            assert clicks == [(130, 214, "left")]
        else:
            if entry == "assert_text_equals":
                kwargs["expected_value"] = "AB"
            elif entry == "assert_text_not_exists":
                kwargs["text_to_find"] = "missing"
            result = getattr(ocr_actions, entry)(**kwargs)
        if entry in {"find_text", "normalized_find_text", "wait_for_text"}:
            assert result.center_point == (130, 214)
            assert result.debug_info["params"]["det_resize_long"] == det_resize_long
        else:
            assert result is True

    assert calls == [(False, det_resize_long)]

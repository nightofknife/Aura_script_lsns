from __future__ import annotations

import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.actions import ocr_actions
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult


class _Session:
    def __init__(self, width=None):
        self.width = width
        self.inputs = []

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[None, 3, 8, self.width])]

    def run(self, _outputs, inputs):
        self.inputs.append(inputs["image"])
        return [np.zeros((len(inputs["image"]), 1, 6), dtype=np.float32)]


def _pipeline(*, session=None, **overrides):
    payload = {
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 96},
        "rec_preprocess": {"image_shape": [3, 8, 32], "max_width": 256},
        "rec_postprocess": {"character_dict": ["A", "B", "C", "D", "E"]},
        "textline_orientation": {"image_size": [20, 12]},
        "pipeline": {"batch_size": 2},
    }
    payload.update(overrides)
    session = session if session is not None else _Session()
    return OnnxOcrPipeline(
        metadata=OcrModelMetadata.model_validate(payload),
        sessions=OcrOnnxSessions(det=_Session(), rec=session, textline_orientation=_Session()),
        provider="CPUExecutionProvider",
    )


def _legacy_normalized_image(image, width, height, metadata):
    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    image = resized.astype(np.float32) * float(metadata.scale)
    mean = np.asarray(metadata.mean, dtype=np.float32).reshape(1, 1, 3)
    std = np.asarray(metadata.std, dtype=np.float32).reshape(1, 1, 3)
    return np.transpose((image - mean) / std, (2, 0, 1)).astype(np.float32)


def _legacy_rec_image(image, width, height, base_width):
    width = max(width, base_width)
    resized_width = min(width, max(1, int(math.ceil(height * image.shape[1] / float(image.shape[0])))))
    resized = cv2.resize(image, (resized_width, height), interpolation=cv2.INTER_LINEAR)
    image = resized.astype(np.float32) / 255.0
    image = (image - 0.5) / 0.5
    output = np.zeros((3, height, width), dtype=np.float32)
    output[:, :, :resized_width] = np.transpose(image, (2, 0, 1))
    return output


def test_detection_preprocessing_preserves_values_shape_and_source():
    pipeline = _pipeline()
    source = np.random.default_rng(12).integers(0, 256, (31, 77, 3), dtype=np.uint8)
    before = source.copy()

    tensor, shape = pipeline._preprocess_det(source, det_resize_long=96)
    expected = _legacy_normalized_image(source, 96, 32, pipeline.metadata.det_preprocess)[None]

    np.testing.assert_array_equal(tensor, expected)
    np.testing.assert_array_equal(source, before)
    assert tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    assert shape == {"src_h": 31.0, "src_w": 77.0, "resize_h": 32.0, "resize_w": 96.0,
                     "ratio_h": 32 / 31, "ratio_w": 96 / 77}


@pytest.mark.parametrize("shape,target_width", [((13, 17, 3), 64), ((8, 144, 3), 64), ((17, 2, 3), 16)])
def test_recognition_preprocessing_preserves_values_and_fills_reused_output(shape, target_width):
    pipeline = _pipeline()
    source = np.random.default_rng(23).integers(0, 256, shape, dtype=np.uint8)
    before = source.copy()
    width = max(target_width, 32)
    expected = _legacy_rec_image(source, target_width, 8, 32)
    batch = np.full((2, 3, 8, width), np.nan, dtype=np.float32)

    allocated = pipeline._preprocess_rec_image(source, target_width)
    returned = pipeline._preprocess_rec_image(source, target_width, out=batch[1])

    np.testing.assert_array_equal(allocated, expected)
    np.testing.assert_array_equal(returned, expected)
    np.testing.assert_array_equal(batch[1], expected)
    assert np.shares_memory(returned, batch[1])
    assert np.isnan(batch[0]).all()
    np.testing.assert_array_equal(source, before)


def test_orientation_preprocessing_preserves_values_and_fills_reused_output():
    pipeline = _pipeline()
    source = np.random.default_rng(34).integers(0, 256, (21, 47, 3), dtype=np.uint8)
    before = source.copy()
    expected = _legacy_normalized_image(source, 20, 12, pipeline.metadata.textline_orientation)
    batch = np.full((2, 3, 12, 20), np.nan, dtype=np.float32)

    allocated = pipeline._preprocess_textline_orientation(source)
    returned = pipeline._preprocess_textline_orientation(source, out=batch[1])

    np.testing.assert_array_equal(allocated, expected)
    np.testing.assert_array_equal(returned, expected)
    assert np.shares_memory(returned, batch[1])
    assert np.isnan(batch[0]).all()
    np.testing.assert_array_equal(source, before)


def test_session_reuses_contiguous_float32_input():
    session = _Session()
    tensor = np.arange(24, dtype=np.float32).reshape(1, 3, 2, 4)

    OnnxOcrPipeline._run_session(session, tensor)

    assert session.inputs[0] is tensor


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.uint8])
def test_session_converts_noncontiguous_input_without_changing_values(dtype):
    session = _Session()
    tensor = np.arange(48, dtype=dtype).reshape(1, 3, 2, 8)[:, :, :, ::2]
    before = tensor.copy()
    assert not tensor.flags.c_contiguous

    OnnxOcrPipeline._run_session(session, tensor)

    received = session.inputs[0]
    assert received.dtype == np.float32
    assert received.flags.c_contiguous
    np.testing.assert_array_equal(received, before.astype(np.float32))
    np.testing.assert_array_equal(tensor, before)


class _IdentifyingRecognitionSession(_Session):
    """Each constant crop carries an identity in its first pixel, independent of padding."""

    def run(self, outputs, inputs):
        super().run(outputs, inputs)
        tensor = inputs["image"]
        markers = np.rint((tensor[:, 0, 0, 0] * 0.5 + 0.5) * 255).astype(int)
        predictions = np.zeros((len(tensor), 1, 6), dtype=np.float32)
        for row, marker in enumerate(markers):
            predictions[row, 0, marker // 25] = 0.6 + marker / 1000
        return [predictions]


@pytest.mark.parametrize("static_width", [None, 64])
def test_width_grouping_preserves_crop_order_and_scores_and_reduces_dynamic_padding(static_width):
    session = _IdentifyingRecognitionSession(width=static_width)
    pipeline = _pipeline(session=session)
    widths = [160, 16, 128, 24, 96]
    crops = [np.full((8, width, 3), 25 * (index + 1), dtype=np.uint8)
             for index, width in enumerate(widths)]
    before = [crop.copy() for crop in crops]
    old_padding_cost = sum(
        len(chunk) * pipeline._resolve_recognition_width(chunk)
        for chunk in pipeline._chunks(crops, 2)
    )

    texts, scores = pipeline._recognize_crops(crops)

    assert texts == ["A", "B", "C", "D", "E"]
    assert scores == pytest.approx([0.625, 0.650, 0.675, 0.700, 0.725])
    assert [len(batch) for batch in session.inputs] == [2, 2, 1]
    assert all(batch.dtype == np.float32 and batch.flags.c_contiguous for batch in session.inputs)
    if static_width is None:
        assert [batch.shape[3] for batch in session.inputs] == [32, 128, 160]
        assert sum(batch.shape[0] * batch.shape[3] for batch in session.inputs) < old_padding_cost
    else:
        assert all(batch.shape[3] == static_width for batch in session.inputs)
    for crop, original in zip(crops, before):
        np.testing.assert_array_equal(crop, original)


def test_empty_crop_list_does_not_run_recognition():
    session = _Session()
    pipeline = _pipeline(session=session)

    assert pipeline._recognize_crops([]) == ([], [])
    assert session.inputs == []


def test_ctc_decode_keeps_blank_repeat_tie_and_confidence_semantics():
    pipeline = _pipeline(rec_postprocess={"character_dict": ["A", "B"]})
    predictions = np.zeros((2, 9, 4), dtype=np.float32)
    indices = [1, 1, 0, 1, 2, 2, 0, 2, 3]
    probabilities = [0.6, 0.95, 0.99, 0.7, 0.8, 0.98, 0.99, 0.9, 0.99]
    predictions[0, np.arange(9), indices] = probabilities
    # Equal maxima pick the first class, as in numpy.argmax. All-zero rows pick blank.
    predictions[1, 0, 1:3] = 0.5

    texts, scores = pipeline._ctc_decode(predictions)

    assert texts == ["AABB", "A"]
    assert scores == pytest.approx([0.75, 0.5])
    assert pipeline._ctc_decode(np.zeros((1, 0, 3), dtype=np.float32)) == ([""], [0.0])
    assert pipeline._ctc_decode(np.zeros((0, 2, 3), dtype=np.float32)) == ([], [])
    assert pipeline._ctc_decode(np.zeros((1, 2, 3), dtype=np.float32)) == ([""], [0.0])


def test_ctc_decode_supports_nonzero_blank_index():
    pipeline = _pipeline(rec_postprocess={"character_dict": ["A", "B"], "blank_index": 2})
    predictions = np.zeros((1, 5, 3), dtype=np.float32)
    predictions[0, np.arange(5), [0, 0, 2, 0, 1]] = [0.6, 0.9, 0.99, 0.7, 0.8]

    texts, scores = pipeline._ctc_decode(predictions)

    assert texts == ["AAB"]
    assert scores == pytest.approx([0.7])


@pytest.mark.parametrize("inspect_mode", [False, True])
@pytest.mark.parametrize("normalize", [False, True])
def test_find_text_reuses_capture_for_ocr_and_copies_only_inspect_snapshot(inspect_mode, normalize):
    class TrackedImage(np.ndarray):
        copies = 0

        def copy(self, *args, **kwargs):
            self.copies += 1
            return super().copy(*args, **kwargs)

    image = np.zeros((4, 8, 3), dtype=np.uint8).view(TrackedImage)
    captured_inputs = []

    def find_text(**kwargs):
        captured_inputs.append(kwargs["source_image"])
        return OcrResult(found=True, text="A", confidence=0.9, rect=(0, 0, 8, 4), center_point=(4, 2))

    def recognize_all(**kwargs):
        return MultiOcrResult(count=1, results=[find_text(**kwargs)])

    app = SimpleNamespace(capture=lambda rect: SimpleNamespace(success=True, image=image))
    service = SimpleNamespace(find_text=find_text, recognize_all=recognize_all)
    engine = SimpleNamespace(root_context=SimpleNamespace(data={
        "initial": {"__is_inspect_mode__": inspect_mode},
    }))

    result = ocr_actions.find_text(app, service, engine, "A", normalize=normalize)

    assert result.found
    assert captured_inputs[0] is image
    assert image.copies == int(inspect_mode)
    if inspect_mode:
        snapshot = result.debug_info["source_image"]
        assert not np.shares_memory(snapshot, image)
        image[:] = 255
        assert np.count_nonzero(snapshot) == 0
    else:
        assert "source_image" not in result.debug_info

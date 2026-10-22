from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from plans.aura_base.src.services._ocr_recognition import OcrRecognitionOptions
from plans.aura_base.src.services._ocr_timing import OcrTiming
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services import ocr_service


class _Session:
    def __init__(self, stage="rec", width=None):
        self.stage = stage
        self.width = width
        self.tensors = []

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[None, 3, 48, self.width])]

    def run(self, _outputs, inputs):
        tensor = inputs["image"]
        self.tensors.append(tensor.copy())
        if self.stage == "det":
            return [np.zeros((len(tensor), 1, *tensor.shape[2:]), dtype=np.float32)]
        predictions = np.zeros((len(tensor), 1, 13), dtype=np.float32)
        markers = np.rint((tensor[:, 0, 0, 0] * 0.5 + 0.5) * 255 / 20).astype(int)
        for row, marker in enumerate(markers):
            predictions[row, 0, marker] = 0.9
        return [predictions]


def _pipeline(*, options=None, static_width=None, batch_size=8):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 96},
        "rec_preprocess": {"image_shape": [3, 48, 320], "max_width": 3200},
        "rec_postprocess": {"character_dict": list("ABCDEFGHIJKL")},
        "pipeline": {"batch_size": batch_size, "use_textline_orientation": False},
    })
    return OnnxOcrPipeline(
        metadata=metadata,
        sessions=OcrOnnxSessions(det=_Session("det"), rec=_Session(width=static_width)),
        provider="CPUExecutionProvider",
        recognition_options=options,
    )


def _crop(width, *, height=48, marker=20):
    return np.full((height, width, 3), marker, dtype=np.uint8)


def _area(batches):
    return sum(len(indices) * width for indices, width in batches)


def test_defaults_keep_legacy_width_enable_adaptive_batching_and_are_immutable():
    options = OcrRecognitionOptions()
    assert options.width_mode == "legacy"
    assert options.batch_mode == "adaptive"
    assert options.batch_size is None
    with pytest.raises(FrozenInstanceError):
        options.width_mode = "adaptive"


@pytest.mark.parametrize("overrides", [
    {"width_mode": "typo"}, {"batch_mode": "typo"},
    {"min_width": 0}, {"min_width": -1},
    {"min_width": 160.5}, {"min_width": True},
    {"batch_size": 0}, {"batch_size": -1},
    {"batch_size": 1.5}, {"batch_size": True},
    {"split_min_saving": -0.1}, {"split_min_saving": 1.1},
    {"split_min_saving": 0}, {"split_min_saving": 1},
    {"split_min_saving": float("nan")},
])
def test_invalid_options_fail_before_inference(overrides):
    with pytest.raises((ValueError, TypeError)):
        OcrRecognitionOptions(**overrides)


def test_dotted_configuration_preserves_independent_strategy_switches():
    options = OcrRecognitionOptions.from_config({
        "ocr.recognition.width_mode": "adaptive",
        "ocr.recognition.min_width": "192",
        "ocr.recognition.batch_mode": "fixed",
        "ocr.recognition.batch_size": "4",
        "ocr.recognition.split_min_saving": "0.4",
    })
    assert options == OcrRecognitionOptions(
        width_mode="adaptive", min_width=192, batch_mode="fixed", batch_size=4,
        split_min_saving=0.4,
    )
    assert OcrRecognitionOptions.from_config({}) == OcrRecognitionOptions()


def test_service_passes_configured_options_to_loaded_pipeline(monkeypatch):
    service = ocr_service.OcrService(config={
        "ocr.recognition.width_mode": "adaptive",
        "ocr.recognition.batch_mode": "adaptive",
        "ocr.recognition.batch_size": 4,
    })
    original = _pipeline()
    monkeypatch.setattr(service, "_resolve_bundle_paths", lambda: (Path("models/test"), Path("meta.json")))
    monkeypatch.setattr(ocr_service, "load_ocr_metadata", lambda _: original.metadata)
    monkeypatch.setattr(service, "_create_pipeline_sessions", lambda *_: (original.sessions, original.provider))

    asyncio.run(service._initialize_engine_async())

    assert service._engine.recognition_options == OcrRecognitionOptions(
        width_mode="adaptive", batch_mode="adaptive", batch_size=4,
    )


@pytest.mark.parametrize("width,height,expected", [(99, 25, 192), (280, 44, 320), (4, 48, 160)])
def test_adaptive_width_uses_content_aspect_ratio_without_base_width_floor(width, height, expected):
    adaptive = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive"))
    legacy = _pipeline()
    crop = _crop(width, height=height)

    assert adaptive._resolve_recognition_width([crop]) == expected
    assert legacy._resolve_recognition_width([crop]) == 320
    adaptive._recognize_crops([crop])
    assert adaptive.sessions.rec.tensors[0].shape == (1, 3, 48, expected)


@pytest.mark.parametrize("mode", ["legacy", "adaptive"])
def test_dynamic_width_remains_capped_for_extremely_long_text(mode):
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode=mode))
    assert pipeline._resolve_recognition_width([_crop(5000)]) == 3200
    pipeline._recognize_crops([_crop(5000)])
    assert pipeline.sessions.rec.tensors[0].shape == (1, 3, 48, 3200)


def test_configured_minimum_width_is_rounded_up_to_model_alignment():
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive", min_width=161))
    assert pipeline._resolve_recognition_width([_crop(32)]) == 192


def test_narrower_padding_keeps_resized_glyph_pixels_and_source_unchanged():
    source = np.random.default_rng(10).integers(0, 256, (25, 99, 3), dtype=np.uint8)
    before = source.copy()
    legacy = _pipeline()._preprocess_rec_image(source, 320)
    adaptive = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive"))._preprocess_rec_image(source, 192)

    assert adaptive.shape == (3, 48, 192)
    np.testing.assert_array_equal(adaptive[:, :, :191], legacy[:, :, :191])
    np.testing.assert_array_equal(adaptive[:, :, 191:], 0)
    np.testing.assert_array_equal(legacy[:, :, 191:], 0)
    np.testing.assert_array_equal(source, before)


def test_padding_split_saves_area_without_enabling_adaptive_width():
    crops = [_crop(320) for _ in range(7)] + [_crop(1280)]
    fixed = _pipeline(options=OcrRecognitionOptions(batch_mode="fixed"))._plan_recognition_batches(crops)
    adaptive = _pipeline()._plan_recognition_batches(crops)

    assert fixed == [(list(range(8)), 1280)]
    assert adaptive == [(list(range(7)), 320), ([7], 1280)]
    assert _area(adaptive) == 3520
    assert _area(fixed) == 10240


def test_batch_override_keeps_width_default_and_width_override_keeps_batch_default():
    fixed = OcrRecognitionOptions.from_config({"ocr.recognition.batch_mode": "fixed"})
    assert (fixed.width_mode, fixed.batch_mode) == ("legacy", "fixed")
    adaptive_width = OcrRecognitionOptions.from_config({"ocr.recognition.width_mode": "adaptive"})
    assert (adaptive_width.width_mode, adaptive_width.batch_mode) == ("adaptive", "adaptive")


def test_release_template_matches_runtime_strategy_defaults():
    import yaml

    template = yaml.safe_load(Path("packaging/templates/config.yaml").read_text(encoding="utf-8"))
    options = OcrRecognitionOptions.from_config({
        f"ocr.recognition.{key}": value for key, value in template["ocr"]["recognition"].items()
    })
    assert (options.width_mode, options.batch_mode) == (
        OcrRecognitionOptions().width_mode, OcrRecognitionOptions().batch_mode,
    ) == ("legacy", "adaptive")


def test_split_threshold_requires_enough_pixel_savings():
    crops = [_crop(320), _crop(640)]
    at_threshold = _pipeline(options=OcrRecognitionOptions(batch_mode="adaptive", split_min_saving=0.25))
    above_threshold = _pipeline(options=OcrRecognitionOptions(batch_mode="adaptive", split_min_saving=0.251))

    assert at_threshold._plan_recognition_batches(crops) == [([0], 320), ([1], 640)]
    assert above_threshold._plan_recognition_batches(crops) == [([0, 1], 640)]


@pytest.mark.parametrize("batch_mode", ["fixed", "adaptive"])
def test_uniform_widths_keep_full_batches_and_respect_configured_maximum(batch_mode):
    crops = [_crop(320) for _ in range(11)]
    pipeline = _pipeline(options=OcrRecognitionOptions(batch_mode=batch_mode, batch_size=4))
    assert pipeline._plan_recognition_batches(crops) == [
        ([0, 1, 2, 3], 320), ([4, 5, 6, 7], 320), ([8, 9, 10], 320),
    ]
    assert _pipeline()._plan_recognition_batches(crops) == [(list(range(8)), 320), ([8, 9, 10], 320)]


def test_sorting_and_splitting_restore_original_text_and_score_order():
    widths = [1280, 160, 320, 96, 192, 320, 256, 320, 640, 64, 320]
    crops = [_crop(width, marker=(index + 1) * 20) for index, width in enumerate(widths)]
    before = [crop.copy() for crop in crops]
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive", batch_mode="adaptive"))

    texts, scores = pipeline._recognize_crops(crops)

    assert texts == list("ABCDEFGHIJK")
    assert scores == pytest.approx([0.9] * len(crops))
    assert sum(len(tensor) for tensor in pipeline.sessions.rec.tensors) == len(crops)
    assert all(len(tensor) <= 8 for tensor in pipeline.sessions.rec.tensors)
    for source, unchanged in zip(crops, before):
        np.testing.assert_array_equal(source, unchanged)


@pytest.mark.parametrize("width_mode", ["legacy", "adaptive"])
def test_static_model_width_is_honored_and_never_split_for_padding(width_mode):
    pipeline = _pipeline(
        options=OcrRecognitionOptions(width_mode=width_mode, batch_mode="adaptive"), static_width=160,
    )
    crops = [_crop(32), _crop(144)]

    assert pipeline._plan_recognition_batches(crops) == [([0, 1], 160)]
    pipeline._recognize_crops(crops)
    assert pipeline.sessions.rec.tensors[0].shape == (2, 3, 48, 160)


def test_adaptive_warmup_exercises_short_base_and_full_configured_batches():
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive", batch_size=4))

    pipeline.warmup()

    shapes = {tensor.shape for tensor in pipeline.sessions.rec.tensors}
    assert {(batch, 3, 48, width) for batch in (1, 4) for width in (160, 192, 320)} <= shapes


def test_static_width_warmup_avoids_duplicate_dynamic_width_profiles():
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive"), static_width=640)
    pipeline.warmup()
    assert [tensor.shape for tensor in pipeline.sessions.rec.tensors] == [
        (1, 3, 48, 640), (8, 3, 48, 640),
    ]


def test_timing_records_actual_batch_shapes_content_widths_and_independent_snapshots():
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive", batch_mode="adaptive"))
    timing = OcrTiming()
    crops = [_crop(144) for _ in range(7)] + [_crop(1280)]

    pipeline._recognize_crops(crops, timing=timing)

    report = timing.snapshot()
    batches = report["recognition_batches"]
    assert batches == [
        {"input_shape": [7, 3, 48, 160], "content_widths": [144] * 7},
        {"input_shape": [1, 3, 48, 1280], "content_widths": [1280]},
    ]
    assert [batch["input_shape"] for batch in batches] == [list(tensor.shape) for tensor in pipeline.sessions.rec.tensors]
    assert report["stage_calls"]["rec_inference"] == 2
    batches[0]["input_shape"][0] = -1
    batches[0]["content_widths"][0] = -1
    assert timing.snapshot()["recognition_batches"][0] == {
        "input_shape": [7, 3, 48, 160], "content_widths": [144] * 7,
    }


def test_empty_crop_list_produces_no_batches_or_inference():
    pipeline = _pipeline(options=OcrRecognitionOptions(width_mode="adaptive", batch_mode="adaptive"))
    timing = OcrTiming()
    assert pipeline._plan_recognition_batches([]) == []
    assert pipeline._recognize_crops([], timing=timing) == ([], [])
    assert pipeline.sessions.rec.tensors == []
    assert timing.snapshot()["recognition_batches"] == []

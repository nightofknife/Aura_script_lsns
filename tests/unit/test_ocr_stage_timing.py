from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from plans.aura_base.src.services import _ocr_timing
from plans.aura_base.src.services._ocr_timing import OcrTiming, STAGE_NAMES
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService


class _Session:
    def __init__(self, stage):
        self.stage = stage
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[None, 3, None, None])]

    def run(self, _outputs, inputs):
        self.calls += 1
        tensor = inputs["image"]
        if self.stage == "det":
            return [np.zeros((len(tensor), 1, *tensor.shape[2:]), dtype=np.float32)]
        if self.stage == "rec":
            return [np.tile(np.asarray([[[0.0, 0.9, 0.1]]], dtype=np.float32), (len(tensor), 1, 1))]
        return [np.tile(np.asarray([[0.9, 0.1]], dtype=np.float32), (len(tensor), 1))]


def _pipeline(monkeypatch, *, box_count=1):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "det_preprocess": {"resize_long": 96},
        "rec_preprocess": {"image_shape": [3, 8, 32], "max_width": 256},
        "rec_postprocess": {"character_dict": ["A", "B"]},
        "textline_orientation": {"image_size": [20, 12]},
        "pipeline": {"batch_size": 2},
    })
    pipeline = OnnxOcrPipeline(
        metadata=metadata,
        sessions=OcrOnnxSessions(det=_Session("det"), rec=_Session("rec"),
                                 textline_orientation=_Session("ori")),
        provider="CPUExecutionProvider",
    )
    # Keep the actual preprocessing, inference and decoding paths, but provide
    # deterministic text boxes independent of OpenCV contour heuristics.
    boxes = [np.asarray([[2, 2 + 10 * i], [42, 2 + 10 * i],
                         [42, 10 + 10 * i], [2, 10 + 10 * i]], dtype=np.float32)
             for i in range(box_count)]
    monkeypatch.setattr(pipeline, "_postprocess_det", lambda *_: boxes)
    return pipeline


def _image():
    return np.random.default_rng(20).integers(0, 256, (64, 96, 3), dtype=np.uint8)


def test_collector_accumulates_stages_and_returns_independent_snapshots(monkeypatch):
    ticks = iter([0, 10, 30, 40, 90])
    monkeypatch.setattr(_ocr_timing, "perf_counter_ns", lambda: next(ticks))
    timing = OcrTiming()
    with timing.measure("det_preprocess"):
        pass
    with timing.measure("det_preprocess"):
        pass

    report = timing.snapshot(total_ms=0.0001)

    assert set(report["stages_ms"]) == set(STAGE_NAMES)
    assert report["stages_ms"]["det_preprocess"] == pytest.approx(70 / 1e6)
    assert report["stage_calls"]["det_preprocess"] == 2
    assert report["other_ms"] == pytest.approx(30 / 1e6)
    assert all(report["stages_ms"][name] == 0 for name in STAGE_NAMES if name != "det_preprocess")
    report["stages_ms"]["det_preprocess"] = -1
    report["stage_calls"]["det_preprocess"] = -1
    again = timing.snapshot(total_ms=0.0001)
    assert again["stages_ms"]["det_preprocess"] == pytest.approx(70 / 1e6)
    assert again["stage_calls"]["det_preprocess"] == 2


def test_collector_accounts_for_exception_and_rejects_unknown_stage(monkeypatch):
    ticks = iter([0, 10, 60])
    monkeypatch.setattr(_ocr_timing, "perf_counter_ns", lambda: next(ticks))
    timing = OcrTiming()
    with pytest.raises(RuntimeError, match="inference failure"):
        with timing.measure("rec_inference"):
            raise RuntimeError("inference failure")
    with pytest.raises(ValueError, match="Unknown OCR timing stage"):
        with timing.measure("typo"):
            pass
    report = timing.snapshot(total_ms=0.001)
    assert report["stages_ms"]["rec_inference"] == pytest.approx(50 / 1e6)
    assert report["stage_calls"]["rec_inference"] == 1


def test_pipeline_timing_preserves_results_and_counts_every_batch(monkeypatch):
    pipeline = _pipeline(monkeypatch, box_count=5)
    image = _image()
    before = image.copy()
    expected = pipeline.predict(image)
    timing = OcrTiming()

    actual = pipeline.predict(image, timing=timing)

    assert len(actual) == len(expected) == 1
    for key in expected[0]:
        if key == "rec_polys":
            for actual_poly, expected_poly in zip(actual[0][key], expected[0][key]):
                np.testing.assert_array_equal(actual_poly, expected_poly)
        else:
            assert actual[0][key] == expected[0][key]
    assert actual[0]["rec_texts"] == ["A"] * 5
    np.testing.assert_array_equal(image, before)
    report = timing.snapshot()
    calls = report["stage_calls"]
    assert calls["det_preprocess"] == calls["det_inference"] == calls["det_postprocess"] == 1
    assert calls["ori_preprocess"] == calls["ori_inference"] == calls["ori_postprocess"] == 3
    assert calls["rec_inference"] == calls["rec_decode"] == 3
    assert calls["rec_preprocess"] == 4  # One sort/allocation setup plus three batches.
    assert calls["crop"] == calls["result_pack"] == 1
    assert calls["result_parse"] == 0  # Parsing belongs to the service.
    assert sum(report["stages_ms"].values()) <= report["total_ms"]


def test_empty_detection_does_not_time_orientation_or_recognition(monkeypatch):
    pipeline = _pipeline(monkeypatch, box_count=0)
    timing = OcrTiming()

    assert pipeline.predict(_image(), timing=timing) == [{"rec_texts": [], "rec_scores": [], "rec_polys": []}]

    report = timing.snapshot()
    for stage in STAGE_NAMES:
        if stage.startswith(("ori_", "rec_")) or stage == "crop":
            assert report["stages_ms"][stage] == report["stage_calls"][stage] == 0
    assert pipeline.sessions.det.calls == 1
    assert pipeline.sessions.rec.calls == pipeline.sessions.textline_orientation.calls == 0


def test_recognition_only_times_no_detection_or_orientation(monkeypatch):
    pipeline = _pipeline(monkeypatch)
    timing = OcrTiming()

    result = pipeline.predict(_image(), recognition_only=True, timing=timing)

    assert result[0]["rec_texts"] == ["A"]
    report = timing.snapshot()
    for stage in STAGE_NAMES:
        if stage.startswith(("det_", "ori_")) or stage == "crop":
            assert report["stages_ms"][stage] == report["stage_calls"][stage] == 0
    assert report["stage_calls"]["rec_inference"] == 1
    assert pipeline.sessions.det.calls == pipeline.sessions.textline_orientation.calls == 0


def test_default_pipeline_does_not_read_profiling_clock(monkeypatch):
    pipeline = _pipeline(monkeypatch)

    def fail_clock():
        raise AssertionError("Profiling clock read with profiling disabled")

    monkeypatch.setattr(_ocr_timing, "perf_counter_ns", fail_clock)
    assert pipeline.predict(_image())[0]["rec_texts"] == ["A"]


@pytest.mark.parametrize("enabled", [True, "true", "1", "on"])
def test_service_profiling_adds_report_and_one_summary_log(monkeypatch, enabled):
    service = OcrService(SimpleNamespace(get=lambda key, default=None:
                         enabled if key == "ocr.profiling.enabled" else default))
    service._engine = _pipeline(monkeypatch)
    logs = []
    monkeypatch.setattr("plans.aura_base.src.services.ocr_service.logger.info", lambda *args: logs.append(args))

    results = asyncio.run(service._recognize_all_and_parse_async(_image()))

    assert [result.text for result in results] == ["A"]
    report = results[0].debug_info["timing"]
    assert report["stage_calls"]["result_parse"] == 1
    assert report["stage_calls"]["det_inference"] == 1
    assert len(logs) == 1
    assert "OCR timing" in logs[0][0]
    assert logs[0][1] == report


@pytest.mark.parametrize("enabled", [None, False, "false", "0", "off"])
def test_service_profiling_disabled_creates_no_report_or_clock_reads(monkeypatch, enabled):
    service = OcrService() if enabled is None else OcrService(SimpleNamespace(
        get=lambda key, default=None: enabled if key == "ocr.profiling.enabled" else default))
    service._engine = _pipeline(monkeypatch)
    logs = []
    monkeypatch.setattr("plans.aura_base.src.services.ocr_service.logger.info", lambda *args: logs.append(args))

    def fail_clock():
        raise AssertionError("Profiling clock read with profiling disabled")

    monkeypatch.setattr(_ocr_timing, "perf_counter_ns", fail_clock)
    results = asyncio.run(service._recognize_all_and_parse_async(_image()))

    assert [result.text for result in results] == ["A"]
    assert "timing" not in results[0].debug_info
    assert logs == []


def test_service_logs_empty_result_timings(monkeypatch):
    service = OcrService(SimpleNamespace(get=lambda key, default=None:
                         True if key == "ocr.profiling.enabled" else default))
    service._engine = _pipeline(monkeypatch, box_count=0)
    logs = []
    monkeypatch.setattr("plans.aura_base.src.services.ocr_service.logger.info", lambda *args: logs.append(args))

    assert asyncio.run(service._recognize_all_and_parse_async(_image())) == []

    assert len(logs) == 1
    assert logs[0][1]["stage_calls"]["result_parse"] == 1
    assert logs[0][1]["stage_calls"]["rec_inference"] == 0


def test_profiling_does_not_add_public_service_parameters():
    for method_name in ("find_text", "find_all_text", "recognize_text", "recognize_all"):
        parameters = inspect.signature(getattr(OcrService, method_name)).parameters
        assert "timing" not in parameters
        assert "profiling" not in parameters
        assert {"recognition_only", "det_resize_long"} <= set(parameters)

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from plans.aura_base.src.services.ocr_service import OcrService


class _Session:
    def __init__(self, stage: str, shape=None):
        self.stage = stage
        self.shape = shape or [None, 3, None, None]
        self.tensors = []
        self.fail = False

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=self.shape, type="tensor(float)")]

    def run(self, _outputs, inputs):
        if self.fail:
            raise ValueError("broken model")
        tensor = inputs["image"]
        self.tensors.append(tensor)
        if self.stage == "det":
            return [np.zeros((len(tensor), 1, *tensor.shape[2:]), dtype=np.float32)]
        if self.stage == "rec":
            return [np.zeros((len(tensor), 1, 3), dtype=np.float32)]
        return [np.tile(np.asarray([[1, 0]], dtype=np.float32), (len(tensor), 1))]


def _pipeline(*, orientation=True, batch_size=8, doc_shape=None):
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "rec_postprocess": {"character_dict": ["A", "B"]},
        "pipeline": {
            "batch_size": batch_size,
            "use_textline_orientation": orientation,
            "use_doc_orientation": doc_shape is not None,
        },
    })
    sessions = OcrOnnxSessions(
        det=_Session("det"),
        rec=_Session("rec"),
        textline_orientation=_Session("ori") if orientation else None,
        doc_orientation=_Session("doc", doc_shape) if doc_shape is not None else None,
    )
    return OnnxOcrPipeline(metadata=metadata, sessions=sessions, provider="CPUExecutionProvider")


def _service(pipeline):
    service = OcrService()
    service._engine = pipeline
    return service


def test_warmup_runs_all_stages_even_with_no_detected_text():
    pipeline = _pipeline()

    stages = pipeline.warmup()

    assert stages == ("det", "textline_orientation(batch=1)", "textline_orientation(batch=8)",
                      "rec(batch=1)", "rec(batch=8)")
    assert [tensor.shape for tensor in pipeline.sessions.det.tensors] == [(1, 3, 544, 960)]
    assert [tensor.shape for tensor in pipeline.sessions.textline_orientation.tensors] == [
        (1, 3, 80, 160), (8, 3, 80, 160),
    ]
    assert [tensor.shape for tensor in pipeline.sessions.rec.tensors] == [
        (1, 3, 48, 320), (8, 3, 48, 320),
    ]
    for session in (pipeline.sessions.det, pipeline.sessions.textline_orientation, pipeline.sessions.rec):
        assert all(tensor.dtype == np.float32 and tensor.flags.c_contiguous for tensor in session.tensors)


def test_disabled_orientation_and_single_batch_are_respected():
    pipeline = _pipeline(orientation=False, batch_size=1)

    assert pipeline.warmup() == ("det", "rec(batch=1)")
    assert len(pipeline.sessions.rec.tensors) == 1


def test_warmup_honors_static_recognition_batch_and_width():
    pipeline = _pipeline(orientation=False)
    pipeline.sessions.rec.shape = [1, 3, 48, 640]

    assert pipeline.warmup() == ("det", "rec(batch=1)")
    assert pipeline.sessions.rec.tensors[0].shape == (1, 3, 48, 640)


def test_optional_document_orientation_is_warmed_using_declared_shape():
    pipeline = _pipeline(doc_shape=[None, 3, 224, 224])

    assert pipeline.warmup()[-1] == "doc_orientation"
    assert pipeline.sessions.doc_orientation.tensors[0].shape == (1, 3, 224, 224)


def test_unknown_document_orientation_shape_fails_explicitly():
    pipeline = _pipeline(doc_shape=[None, 3, "height", "width"])

    with pytest.raises(RuntimeError, match="doc_orientation warmup failed.*concrete"):
        pipeline.warmup()


def test_concurrent_preloads_reuse_loaded_sessions_and_successful_warmup():
    pipeline = _pipeline()
    service = _service(pipeline)

    async def run():
        assert await asyncio.gather(*(service._preload_engine_async(True) for _ in range(3))) == [None] * 3

    asyncio.run(run())

    assert service._engine is pipeline
    assert service._warmed_engine is pipeline
    assert len(pipeline.sessions.det.tensors) == 1
    assert len(pipeline.sessions.rec.tensors) == 2
    assert len(pipeline.sessions.textline_orientation.tensors) == 2


def test_failed_warmup_is_not_cached_and_can_be_retried():
    pipeline = _pipeline()
    service = _service(pipeline)
    pipeline.sessions.rec.fail = True

    async def run():
        with pytest.raises(RuntimeError, match=r"rec\(batch=1\) warmup failed.*broken model"):
            await service._warmup_engine_async()
        assert service._warmed_engine is None
        pipeline.sessions.rec.fail = False
        assert await service._warmup_engine_async() is True

    asyncio.run(run())

    assert service._warmed_engine is pipeline
    assert len(pipeline.sessions.det.tensors) == 2
    assert len(pipeline.sessions.rec.tensors) == 2


def test_self_check_revalidates_all_stages_after_warmup_and_clears_failed_state():
    pipeline = _pipeline()
    service = _service(pipeline)

    async def run():
        await service._warmup_engine_async()
        await service._self_check_async()
        assert len(pipeline.sessions.rec.tensors) == 4
        pipeline.sessions.textline_orientation.fail = True
        with pytest.raises(RuntimeError, match="textline_orientation"):
            await service._self_check_async()
        assert service._warmed_engine is None

    asyncio.run(run())


def test_task_ocr_waits_for_warmup_semaphore(monkeypatch):
    pipeline = _pipeline()
    service = _service(pipeline)
    started = threading.Event()
    release = threading.Event()
    prediction_started = threading.Event()
    original_warmup = pipeline.warmup
    original_predict = pipeline.predict

    def warmup():
        started.set()
        assert release.wait(timeout=5)
        return original_warmup()

    def predict(*args, **kwargs):
        prediction_started.set()
        return original_predict(*args, **kwargs)

    monkeypatch.setattr(pipeline, "warmup", warmup)
    monkeypatch.setattr(pipeline, "predict", predict)

    async def run():
        warmup_task = asyncio.create_task(service._warmup_engine_async())
        assert await asyncio.to_thread(started.wait, 5)
        prediction_task = asyncio.create_task(service._recognize_all_async(
            np.zeros((32, 80, 3), dtype=np.uint8), recognition_only=True,
        ))
        try:
            await asyncio.sleep(0)
            assert not prediction_started.is_set()
        finally:
            release.set()
        await warmup_task
        await prediction_task

    asyncio.run(run())
    assert prediction_started.is_set()

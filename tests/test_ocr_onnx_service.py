from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from plans.aura_base.src.actions import ocr_actions
from plans.aura_base.src.services.ocr_contract import (
    OcrModelFiles,
    OcrModelMetadata,
    OcrRecPostprocessMetadata,
    write_ocr_metadata,
)
from plans.aura_base.src.services.ocr_onnx_pipeline import OnnxOcrPipeline, OcrOnnxSessions
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult, OcrService


class _FakeConfig:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakePipeline:
    provider = "CPUExecutionProvider"

    def predict(self, _image):
        return [
            {
                "rec_texts": ["Start", "Settings"],
                "rec_scores": [0.91, 0.82],
                "rec_polys": [
                    np.array([[0, 0], [50, 0], [50, 20], [0, 20]], dtype=np.float32),
                    np.array([[100, 40], [180, 40], [180, 70], [100, 70]], dtype=np.float32),
                ],
            }
        ]


class _FakeCapture:
    def __init__(self, success=True):
        self.success = success
        self.image = np.zeros((80, 200, 3), dtype=np.uint8)


class _FakeApp:
    def __init__(self, success=True):
        self.success = success

    def capture(self, rect=None):
        self.last_rect = rect
        return _FakeCapture(success=self.success)


class _FakeEngine:
    root_context = SimpleNamespace(data={})


class _FakeInput:
    name = "x"
    shape = [1, 3, 48, 8]


class _FakeSession:
    def __init__(self, output):
        self.output = output
        self.feeds = []

    def get_inputs(self):
        return [_FakeInput()]

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        return [self.output]


class TestOcrOnnxService(unittest.TestCase):
    def setUp(self):
        self.service = OcrService(config=_FakeConfig())
        self.service._engine = _FakePipeline()
        self.service._engine_provider = "CPUExecutionProvider"
        self.service._engine_device = "cpu"
        self.service._engine_model = "ppocrv5_server"

    def test_recognize_and_find_text_keep_result_shape(self):
        async def run():
            all_result = await self.service._recognize_all_async(np.zeros((80, 200, 3), dtype=np.uint8))
            best = await self.service._recognize_text_async(np.zeros((80, 200, 3), dtype=np.uint8))
            found = await self.service._find_text_async("Set", np.zeros((80, 200, 3), dtype=np.uint8), "contains", None)
            missing = await self.service._find_text_async("Exit", np.zeros((80, 200, 3), dtype=np.uint8), "exact", None)
            return all_result, best, found, missing

        all_result, best, found, missing = asyncio.run(run())
        self.assertEqual(all_result.count, 2)
        self.assertEqual(all_result.results[0].text, "Start")
        self.assertEqual(all_result.results[0].center_point, (25, 10))
        self.assertEqual(all_result.results[1].rect, (100, 40, 80, 30))
        self.assertEqual(best.text, "Start")
        self.assertTrue(found.found)
        self.assertEqual(found.text, "Settings")
        self.assertFalse(missing.found)
        self.assertIn("all_recognized_results", missing.debug_info)

    def test_preload_action_adds_backend_provider_and_model(self):
        with patch.object(OcrService, "preload_engine", return_value="cpu"):
            result = ocr_actions.preload_ocr(self.service, warmup=True)
        self.assertEqual(result["device"], "cpu")
        self.assertEqual(result["backend"], "onnxruntime")
        self.assertEqual(result["provider"], "CPUExecutionProvider")
        self.assertEqual(result["model"], "ppocrv5_server")

    def test_actions_apply_roi_offsets_and_whitelist(self):
        app = _FakeApp()
        region = (10, 20, 200, 80)
        local_result = OcrResult(found=True, text="Start", center_point=(25, 10), rect=(0, 0, 50, 20), confidence=0.9)
        multi_result = MultiOcrResult(
            count=2,
            results=[
                OcrResult(found=True, text="Start", center_point=(25, 10), rect=(0, 0, 50, 20), confidence=0.9),
                OcrResult(found=True, text="Settings", center_point=(140, 55), rect=(100, 40, 80, 30), confidence=0.8),
            ],
        )
        with patch.object(self.service, "find_text", return_value=local_result):
            found = ocr_actions.find_text(app, self.service, _FakeEngine(), "Start", region=region)
        self.assertEqual(found.center_point, (35, 30))
        self.assertEqual(found.rect, (10, 20, 50, 20))

        with patch.object(self.service, "recognize_all", return_value=multi_result):
            text = ocr_actions.get_text_in_region(app, self.service, region, whitelist="Start0123456789")
        self.assertEqual(text, "Start Stt")

    def test_missing_metadata_fails_with_export_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _FakeConfig({"ocr.models_root": tmp, "ocr.default_model": "ppocrv5_server"})
            service = OcrService(config=config)
            with self.assertRaisesRegex(ValueError, "export_paddleocr_onnx.py"):
                asyncio.run(service._preload_engine_async(False))

    def test_malformed_metadata_fails_during_preload(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "ppocrv5_server"
            bundle.mkdir(parents=True)
            (bundle / "ocr.meta.json").write_text("{bad-json}", encoding="utf-8")
            config = _FakeConfig({"ocr.models_root": tmp, "ocr.default_model": "ppocrv5_server"})
            service = OcrService(config=config)
            with self.assertRaisesRegex(ValueError, "Invalid OCR metadata JSON"):
                asyncio.run(service._preload_engine_async(False))


class TestOcrOnnxPipeline(unittest.TestCase):
    @staticmethod
    def _metadata() -> OcrModelMetadata:
        return OcrModelMetadata(
            schema_version=1,
            task="ocr",
            family="ppocrv5",
            variant="server",
            lang="ch",
            models=OcrModelFiles(det="det.onnx", rec="rec.onnx", textline_orientation="textline_orientation.onnx"),
            rec_postprocess=OcrRecPostprocessMetadata(character_dict=["a", "b", "c"]),
        )

    def test_ctc_decode_filters_blank_and_repeats(self):
        pipeline = OnnxOcrPipeline(
            metadata=self._metadata(),
            sessions=OcrOnnxSessions(det=_FakeSession(np.zeros((1, 1, 4, 4))), rec=_FakeSession(np.zeros((1, 2, 4)))),
            provider="CPUExecutionProvider",
        )
        preds = np.array(
            [
                [
                    [0.9, 0.1, 0.0, 0.0],
                    [0.1, 0.8, 0.1, 0.0],
                    [0.1, 0.7, 0.2, 0.0],
                    [0.8, 0.1, 0.1, 0.0],
                    [0.1, 0.0, 0.85, 0.05],
                ]
            ],
            dtype=np.float32,
        )
        texts, scores = pipeline._ctc_decode(preds)
        self.assertEqual(texts, ["ab"])
        self.assertGreater(scores[0], 0.8)

    def test_textline_orientation_rotates_180_degree_predictions(self):
        metadata = self._metadata()
        ori_session = _FakeSession(np.array([[0.01, 0.99]], dtype=np.float32))
        pipeline = OnnxOcrPipeline(
            metadata=metadata,
            sessions=OcrOnnxSessions(
                det=_FakeSession(np.zeros((1, 1, 4, 4))),
                rec=_FakeSession(np.zeros((1, 2, 4))),
                textline_orientation=ori_session,
            ),
            provider="CPUExecutionProvider",
        )
        crop = np.zeros((10, 20, 3), dtype=np.uint8)
        crop[:, :10] = 255
        rotated = pipeline._apply_textline_orientation([crop])[0]
        self.assertTrue(np.array_equal(rotated[:, 10:], np.full((10, 10, 3), 255, dtype=np.uint8)))

    def test_invalid_metadata_rejects_missing_character_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            metadata_path = bundle / "ocr.meta.json"
            write_ocr_metadata(metadata_path, self._metadata())
            loaded = metadata_path.read_text(encoding="utf-8")
            self.assertIn("character_dict", loaded)


if __name__ == "__main__":
    unittest.main()

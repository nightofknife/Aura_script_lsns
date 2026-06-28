from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plans.aura_base.src.services.ocr_contract import load_ocr_metadata
from tools import export_paddleocr_onnx as export_tool


class _FakeOnnxModule:
    def __init__(self):
        self.checker = SimpleNamespace(check_model=self._check_model)
        self.loaded = []
        self.checked = []

    def load(self, path):
        self.loaded.append(path)
        return SimpleNamespace(path=path)

    def _check_model(self, model):
        self.checked.append(model)


class _FakeOrtModule:
    def __init__(self):
        self.sessions = []

    def get_available_providers(self):
        return ["CPUExecutionProvider"]

    def InferenceSession(self, path, providers=None):
        session = SimpleNamespace(get_providers=lambda: list(providers or ["CPUExecutionProvider"]))
        self.sessions.append((path, providers))
        return session


class TestOcrExportTool(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.model_root = self.root / "ocr_model"
        self._write_model_dir(
            "PP-OCRv5_server_det",
            """
Global:
  model_name: PP-OCRv5_server_det
PreProcess:
  transform_ops:
  - DetResizeForTest:
      resize_long: 960
  - NormalizeImage:
      mean: [0.485, 0.456, 0.406]
      scale: 1./255.
      std: [0.229, 0.224, 0.225]
PostProcess:
  thresh: 0.3
  box_thresh: 0.6
  max_candidates: 1000
  unclip_ratio: 1.5
""",
        )
        self._write_model_dir(
            "PP-OCRv5_server_rec",
            """
Global:
  model_name: PP-OCRv5_server_rec
Hpi:
  backend_configs:
    paddle_infer:
      trt_dynamic_shapes:
        x:
        - [1, 3, 48, 160]
        - [1, 3, 48, 320]
        - [8, 3, 48, 3200]
PreProcess:
  transform_ops:
  - RecResizeImg:
      image_shape: [3, 48, 320]
PostProcess:
  character_dict: ["你", "好"]
""",
        )
        self._write_model_dir(
            "PP-LCNet_x1_0_textline_ori",
            """
Global:
  model_name: PP-LCNet_x1_0_textline_ori
PreProcess:
  transform_ops:
  - ResizeImage:
      size: [160, 80]
  - NormalizeImage:
      mean: [0.485, 0.456, 0.406]
      scale: 0.00392156862745098
      std: [0.229, 0.224, 0.225]
PostProcess:
  Topk:
    label_list: ["0_degree", "180_degree"]
""",
        )
        doc_dir = self.model_root / "PP-LCNet_x1_0_doc_ori"
        doc_dir.mkdir(parents=True)
        (doc_dir / "inference.yml").write_text("Global:\n  model_name: PP-LCNet_x1_0_doc_ori\n", encoding="utf-8")

    def _write_model_dir(self, name: str, yml: str):
        path = self.model_root / name
        path.mkdir(parents=True)
        (path / "inference.pdmodel").write_bytes(b"fake-model")
        (path / "inference.pdiparams").write_bytes(b"fake-params")
        (path / "inference.yml").write_text(yml.strip() + "\n", encoding="utf-8")

    def test_export_generates_bundle_and_metadata(self):
        fake_onnx = _FakeOnnxModule()
        fake_ort = _FakeOrtModule()

        def fake_convert(_source_dir, target_path, *, opset):
            self.assertEqual(opset, 13)
            target_path.write_bytes(b"fake-onnx")

        with patch.object(export_tool, "_convert_paddle_model_to_onnx", side_effect=fake_convert), patch.object(export_tool, "_load_onnx_module", return_value=fake_onnx), patch.object(export_tool, "_load_onnxruntime_module", return_value=fake_ort):
            result = export_tool.export_paddleocr_onnx(
                model_root=self.model_root,
                out_dir=self.root / "exports",
                name="ppocrv5_server",
                opset=13,
            )

        bundle = self.root / "exports" / "ppocrv5_server"
        self.assertTrue((bundle / "det.onnx").is_file())
        self.assertTrue((bundle / "rec.onnx").is_file())
        self.assertTrue((bundle / "textline_orientation.onnx").is_file())
        self.assertFalse((bundle / "doc_orientation.onnx").exists())
        metadata = load_ocr_metadata(bundle / "ocr.meta.json")
        self.assertEqual(metadata.family, "ppocrv5")
        self.assertEqual(metadata.variant, "server")
        self.assertEqual(metadata.models.det, "det.onnx")
        self.assertEqual(metadata.rec_preprocess.max_width, 3200)
        self.assertEqual(metadata.rec_postprocess.character_dict, ["你", "好"])
        self.assertEqual(result["providers"]["det"], "CPUExecutionProvider")
        self.assertEqual(len(fake_ort.sessions), 3)

    def test_export_rejects_missing_required_model(self):
        (self.model_root / "PP-OCRv5_server_rec" / "inference.pdmodel").unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing Paddle inference files"):
            export_tool.export_paddleocr_onnx(
                model_root=self.model_root,
                out_dir=self.root / "exports",
            )

    def test_export_allows_missing_optional_doc_orientation_dir(self):
        doc_dir = self.model_root / "PP-LCNet_x1_0_doc_ori"
        (doc_dir / "inference.yml").unlink()
        doc_dir.rmdir()

        def fake_convert(_source_dir, target_path, *, opset):
            target_path.write_bytes(b"fake-onnx")

        with patch.object(export_tool, "_convert_paddle_model_to_onnx", side_effect=fake_convert), patch.object(export_tool, "_load_onnx_module", return_value=_FakeOnnxModule()), patch.object(export_tool, "_load_onnxruntime_module", return_value=_FakeOrtModule()):
            result = export_tool.export_paddleocr_onnx(
                model_root=self.model_root,
                out_dir=self.root / "exports",
            )

        self.assertNotIn("doc_orientation", result["models"])
        self.assertTrue((self.root / "exports" / "ppocrv5_server" / "ocr.meta.json").is_file())


if __name__ == "__main__":
    unittest.main()

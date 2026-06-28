from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.services.yolo_contract import YoloModelMetadata, YoloPreprocessMetadata, write_model_metadata
from packages.aura_core.services.yolo_service import YoloService


class _FakeConfig:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakeCapture:
    def __init__(self):
        self.success = True
        self.image = np.zeros((240, 120, 3), dtype=np.uint8)
        self.window_rect = (100, 200, 640, 480)
        self.relative_rect = (5, 6, 120, 240)
        self.error_message = ""


class _FakeScreen:
    @staticmethod
    def get_client_rect():
        return (10, 20, 640, 480)


class _FakeApp:
    def __init__(self):
        self.screen = _FakeScreen()
        self.capture_calls = []

    def capture(self, rect=None):
        self.capture_calls.append(rect)
        return _FakeCapture()


class _FakeSession:
    def __init__(self, outputs=None, providers=None):
        self._outputs = outputs or []
        self._providers = providers or ["CPUExecutionProvider"]
        self.feeds = []

    def get_inputs(self):
        return [SimpleNamespace(name="images")]

    def get_providers(self):
        return list(self._providers)

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        return self._outputs


class _FakeGraphOptimizationLevel:
    ORT_DISABLE_ALL = "disabled"
    ORT_ENABLE_BASIC = "basic"
    ORT_ENABLE_EXTENDED = "extended"
    ORT_ENABLE_ALL = "all"


class _FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None
        self.graph_optimization_level = None


class _FakeOrtModule:
    GraphOptimizationLevel = _FakeGraphOptimizationLevel
    SessionOptions = _FakeSessionOptions

    def __init__(self, available_providers):
        self._available_providers = list(available_providers)
        self.created = []

    def get_available_providers(self):
        return list(self._available_providers)

    def InferenceSession(self, path, sess_options=None, providers=None):
        session = _FakeSession(providers=providers)
        self.created.append(
            {
                "path": path,
                "sess_options": sess_options,
                "providers": list(providers or []),
                "session": session,
            }
        )
        return session


class TestCoreYoloService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name)
        self.models_root = self.repo_root / "models"
        self.models_root.mkdir(parents=True, exist_ok=True)
        self.service = YoloService(
            config=_FakeConfig(
                {
                    "yolo.default_variant": "n",
                    "yolo.models_root": str(self.models_root),
                    "yolo.execution_provider": "auto",
                }
            )
        )

    def _write_model(
        self,
        path: Path,
        *,
        family: str,
        variant: str | None = None,
        input_size: tuple[int, int] = (120, 240),
        output_layout: str = "bcn",
        class_names: list[str] | None = None,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        write_model_metadata(
            path.with_suffix(".meta.json"),
            YoloModelMetadata(
                schema_version=1,
                task="detect",
                family=family,
                variant=variant,
                input_size=input_size,
                input_format="rgb",
                input_layout="nchw",
                preprocess=YoloPreprocessMetadata(letterbox=True, pad_value=114, normalize="divide_255"),
                output_format="ultralytics_detect_raw_v1",
                output_layout=output_layout,
                class_names=class_names or ["person", "vehicle"],
                default_conf=0.25,
                default_iou=0.45,
            ),
        )
        return path

    @staticmethod
    def _single_detection_bcn() -> np.ndarray:
        return np.array([[[60.0], [120.0], [100.0], [200.0], [0.1], [0.93]]], dtype=np.float32)

    def test_supported_generations_cover_requested_families(self):
        self.assertEqual(self.service.supported_generations(), ["yolo8", "yolo11", "yolo26"])

    def test_resolve_known_family_aliases(self):
        cases = {
            "yolo8": "yolov8n.onnx",
            "yolo11l": "yolo11l.onnx",
            "yolo26x": "yolo26x.onnx",
        }
        for raw, expected_name in cases.items():
            with self.subTest(raw=raw):
                ref = self.service.resolve_model_reference(raw)
                self.assertTrue(ref.source.endswith(expected_name))
                self.assertTrue(ref.metadata_source.endswith(expected_name.replace(".onnx", ".meta.json")))
                self.assertFalse(ref.is_path)

    def test_named_model_prefers_valid_aura_base_path_for_relative_models_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release_root = Path(tmp_dir)
            (release_root / "plans").mkdir()
            (release_root / "models" / "yolo").mkdir(parents=True)
            (release_root / "config.yaml").write_text("runtime: {}\n", encoding="utf-8")
            model_path = self._write_model(
                release_root / "models" / "yolo" / "yolo11n.onnx",
                family="yolo11",
                variant="n",
            )
            service = YoloService(
                config=_FakeConfig(
                    {
                        "yolo.default_variant": "n",
                        "yolo.models_root": "models/yolo",
                        "yolo.execution_provider": "auto",
                    }
                )
            )

            with patch.dict(os.environ, {"AURA_BASE_PATH": str(release_root)}):
                ref = service.resolve_model_reference("yolo11")

            self.assertEqual(Path(ref.source), model_path.resolve())
            self.assertEqual(Path(ref.metadata_source), model_path.with_suffix(".meta.json").resolve())

    def test_resolve_relative_path_prefers_current_plan_when_file_exists(self):
        plan_file = self.repo_root / "plans" / "aura_benchmark" / "models" / "demo.onnx"
        self._write_model(plan_file, family="yolo11", variant="n")
        token = current_plan_name.set("aura_benchmark")
        try:
            with patch.object(self.service, "_repo_root", return_value=self.repo_root):
                ref = self.service.resolve_model_reference("models/demo.onnx")
            self.assertTrue(ref.is_path)
            self.assertTrue(str(ref.source).endswith("plans\\aura_benchmark\\models\\demo.onnx") or str(ref.source).endswith("plans/aura_benchmark/models/demo.onnx"))
        finally:
            current_plan_name.reset(token)

    def test_rejects_pt_model_paths_with_export_hint(self):
        with self.assertRaisesRegex(ValueError, "exported ONNX model"):
            self.service.resolve_model_reference("models/demo.pt")

    def test_missing_metadata_fails_preload(self):
        model_path = self.models_root / "yolo11n.onnx"
        model_path.write_bytes(b"fake")
        with patch.object(self.service, "_create_session", return_value=(_FakeSession(), "CPUExecutionProvider")):
            with self.assertRaisesRegex(ValueError, "Missing YOLO metadata"):
                self.service.preload_model("yolo11")

    def test_missing_named_model_error_reports_aura_base_path_and_candidates(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            release_root = Path(tmp_dir)
            (release_root / "plans").mkdir()
            (release_root / "models" / "yolo").mkdir(parents=True)
            (release_root / "config.yaml").write_text("runtime: {}\n", encoding="utf-8")
            service = YoloService(
                config=_FakeConfig(
                    {
                        "yolo.default_variant": "n",
                        "yolo.models_root": "models/yolo",
                        "yolo.execution_provider": "auto",
                    }
                )
            )

            with patch.dict(os.environ, {"AURA_BASE_PATH": str(release_root)}):
                with self.assertRaisesRegex(ValueError, "AURA_BASE_PATH=.*candidates="):
                    service.preload_model("yolo11")

    def test_malformed_metadata_fails_preload(self):
        model_path = self.models_root / "yolo11n.onnx"
        model_path.write_bytes(b"fake")
        model_path.with_suffix(".meta.json").write_text("{not-json}", encoding="utf-8")
        with patch.object(self.service, "_create_session", return_value=(_FakeSession(), "CPUExecutionProvider")):
            with self.assertRaisesRegex(ValueError, "Invalid YOLO metadata JSON"):
                self.service.preload_model("yolo11")

    def test_preload_and_detect_image(self):
        model_path = self._write_model(self.models_root / "yolo11n.onnx", family="yolo11", variant="n")
        fake_session = _FakeSession(outputs=[self._single_detection_bcn()], providers=["CPUExecutionProvider"])
        with patch.object(self.service, "_create_session", return_value=(fake_session, "CPUExecutionProvider")):
            info = self.service.preload_model("yolo11")
            result = self.service.detect_image(np.zeros((240, 120, 3), dtype=np.uint8), model_name="yolo11")

        self.assertTrue(info["loaded"])
        self.assertEqual(info["family"], "yolo11")
        self.assertEqual(Path(info["source"]), model_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "yolo11n")
        self.assertEqual(result["backend"], "onnxruntime")
        self.assertEqual(result["provider"], "CPUExecutionProvider")
        self.assertEqual(result["image_size"], [120, 240])
        self.assertEqual(len(result["detections"]), 1)
        self.assertEqual(result["detections"][0]["label"], "vehicle")
        self.assertEqual(result["detections"][0]["bbox_xywh"], [10.0, 20.0, 100.0, 200.0])
        self.assertEqual(result["detections"][0]["bbox_global"], [10, 20, 100, 200])
        self.assertEqual(self.service.list_loaded_models(), ["yolo11n"])
        feed = fake_session.feeds[0]["images"]
        self.assertEqual(feed.shape, (1, 3, 240, 120))
        self.assertEqual(feed.dtype, np.float32)

    def test_detect_on_screen_maps_global_bbox(self):
        self._write_model(self.models_root / "yolov8n.onnx", family="yolo8", variant="n")
        fake_session = _FakeSession(outputs=[self._single_detection_bcn()], providers=["CPUExecutionProvider"])
        fake_app = _FakeApp()
        with patch.object(self.service, "_create_session", return_value=(fake_session, "CPUExecutionProvider")):
            self.service.preload_model("yolo8")
            result = self.service.detect_on_screen(app=fake_app, roi=(1, 2, 3, 4), model_name="yolo8")

        self.assertTrue(result["ok"])
        self.assertEqual(fake_app.capture_calls, [(1, 2, 3, 4)])
        self.assertEqual(result["detections"][0]["bbox_global"], [25, 46, 100, 200])

    def test_provider_selection_honors_auto_cpu_and_cuda(self):
        model_path = self.models_root / "demo.onnx"
        model_path.write_bytes(b"fake")

        fake_ort = _FakeOrtModule(["CUDAExecutionProvider", "CPUExecutionProvider"])
        with patch.object(self.service._onnx_backend, "load_onnxruntime_module", return_value=fake_ort), patch.object(self.service._onnx_backend, "prepare_cuda_execution_provider_environment") as prepare_cuda:
            _session, provider = self.service._create_session(model_path)
        self.assertEqual(provider, "CUDAExecutionProvider")
        self.assertEqual(fake_ort.created[0]["providers"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        prepare_cuda.assert_called_once()

        cpu_service = YoloService(config=_FakeConfig({"yolo.execution_provider": "cpu"}))
        with patch.object(cpu_service._onnx_backend, "load_onnxruntime_module", return_value=fake_ort):
            _session, provider = cpu_service._create_session(model_path)
        self.assertEqual(provider, "CPUExecutionProvider")
        self.assertEqual(fake_ort.created[1]["providers"], ["CPUExecutionProvider"])

        cuda_only_service = YoloService(config=_FakeConfig({"yolo.execution_provider": "cuda"}))
        cpu_only_ort = _FakeOrtModule(["CPUExecutionProvider"])
        with patch.object(cuda_only_service._onnx_backend, "load_onnxruntime_module", return_value=cpu_only_ort):
            with self.assertRaisesRegex(RuntimeError, "CUDAExecutionProvider is not available"):
                cuda_only_service._create_session(model_path)

        fallback_service = YoloService(config=_FakeConfig({"yolo.execution_provider": "cuda"}))
        fallback_ort = _FakeOrtModule(["CUDAExecutionProvider", "CPUExecutionProvider"])

        def _cpu_fallback_session(path, sess_options=None, providers=None):
            session = _FakeSession(providers=["CPUExecutionProvider"])
            fallback_ort.created.append(
                {
                    "path": path,
                    "sess_options": sess_options,
                    "providers": list(providers or []),
                    "session": session,
                }
            )
            return session

        fallback_ort.InferenceSession = _cpu_fallback_session
        with patch.object(fallback_service._onnx_backend, "load_onnxruntime_module", return_value=fallback_ort), patch.object(fallback_service._onnx_backend, "prepare_cuda_execution_provider_environment"):
            with self.assertRaisesRegex(RuntimeError, "fell back to CPUExecutionProvider"):
                fallback_service._create_session(model_path)

    def test_prepare_cuda_environment_runs_once(self):
        fake_ort = SimpleNamespace(preload_dlls=lambda: None)
        with patch.object(self.service._onnx_backend, "register_windows_cuda_dll_directories") as register_dirs, patch.object(self.service._onnx_backend, "preload_torch_cuda_runtime") as preload_torch, patch("packages.aura_core.services.onnx_runtime_backend.os.name", "nt"):
            self.service._onnx_backend.prepare_cuda_execution_provider_environment(fake_ort)
            self.service._onnx_backend.prepare_cuda_execution_provider_environment(fake_ort)

        register_dirs.assert_called_once_with(fake_ort)
        preload_torch.assert_called_once()

    def test_warns_and_ignores_legacy_runtime_options(self):
        metadata = YoloModelMetadata(
            schema_version=1,
            task="detect",
            family="yolo11",
            variant="n",
            input_size=(640, 640),
            input_format="rgb",
            input_layout="nchw",
            preprocess=YoloPreprocessMetadata(letterbox=True, pad_value=114, normalize="divide_255"),
            output_format="ultralytics_detect_raw_v1",
            output_layout="bcn",
            class_names=["enemy"],
            default_conf=0.33,
            default_iou=0.55,
        )
        with patch("packages.aura_core.services.yolo_service.logger.warning") as warning_mock:
            settings = self.service._build_infer_settings(
                {"device": "cuda:0", "half": True, "imgsz": 960},
                metadata,
            )
        self.assertEqual(settings["conf"], 0.33)
        self.assertEqual(settings["iou"], 0.55)
        self.assertEqual(warning_mock.call_count, 3)

    def test_applies_class_filter_and_nms(self):
        self._write_model(
            self.models_root / "yolo11n.onnx",
            family="yolo11",
            variant="n",
            input_size=(120, 240),
            class_names=["enemy", "ally"],
        )
        outputs = [
            np.array(
                [
                    [
                        [60.0, 60.0, 100.0],
                        [120.0, 120.0, 40.0],
                        [100.0, 100.0, 20.0],
                        [200.0, 200.0, 40.0],
                        [0.91, 0.1, 0.05],
                        [0.1, 0.88, 0.87],
                    ]
                ],
                dtype=np.float32,
            )
        ]
        fake_session = _FakeSession(outputs=outputs, providers=["CPUExecutionProvider"])
        with patch.object(self.service, "_create_session", return_value=(fake_session, "CPUExecutionProvider")):
            self.service.preload_model("yolo11")
            filtered = self.service.detect_image(
                np.zeros((240, 120, 3), dtype=np.uint8),
                model_name="yolo11",
                options={"classes": [1]},
            )
            class_aware = self.service.detect_image(
                np.zeros((240, 120, 3), dtype=np.uint8),
                model_name="yolo11",
                options={"agnostic_nms": False},
            )
            agnostic = self.service.detect_image(
                np.zeros((240, 120, 3), dtype=np.uint8),
                model_name="yolo11",
                options={"agnostic_nms": True},
            )

        self.assertEqual(len(filtered["detections"]), 2)
        self.assertTrue(all(det["class_id"] == 1 for det in filtered["detections"]))
        self.assertEqual(len(class_aware["detections"]), 3)
        self.assertEqual(len(agnostic["detections"]), 2)


if __name__ == "__main__":
    unittest.main()

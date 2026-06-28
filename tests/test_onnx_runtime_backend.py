from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packages.aura_core.services.onnx_runtime_backend import OnnxRuntimeBackend


class _FakeConfig:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


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


class _FakeSession:
    def __init__(self, providers):
        self._providers = list(providers)

    def get_providers(self):
        return list(self._providers)


class _FakeOrtModule:
    GraphOptimizationLevel = _FakeGraphOptimizationLevel
    SessionOptions = _FakeSessionOptions

    def __init__(self, available_providers, active_providers=None):
        self._available_providers = list(available_providers)
        self._active_providers = active_providers
        self.created = []

    def get_available_providers(self):
        return list(self._available_providers)

    def InferenceSession(self, path, sess_options=None, providers=None):
        self.created.append({"path": path, "sess_options": sess_options, "providers": list(providers or [])})
        return _FakeSession(self._active_providers or providers or [])


class TestOnnxRuntimeBackend(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.model_path = Path(self.temp_dir.name) / "model.onnx"
        self.model_path.write_bytes(b"fake")

    def _backend(self, values=None):
        return OnnxRuntimeBackend(
            config=_FakeConfig(values or {}),
            config_prefix="ocr",
            runtime_name="test runtime",
            install_hint="requirements/optional-vision-onnx-cuda.txt",
        )

    def test_auto_prefers_cuda_then_cpu(self):
        backend = self._backend({"ocr.execution_provider": "auto"})
        fake_ort = _FakeOrtModule(["CUDAExecutionProvider", "CPUExecutionProvider"])
        with patch.object(backend, "load_onnxruntime_module", return_value=fake_ort), patch.object(backend, "prepare_cuda_execution_provider_environment") as prepare_cuda:
            _session, provider = backend.create_session(self.model_path)

        self.assertEqual(provider, "CUDAExecutionProvider")
        self.assertEqual(fake_ort.created[0]["providers"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        prepare_cuda.assert_called_once()

    def test_cpu_uses_cpu_only_and_session_options(self):
        backend = self._backend(
            {
                "ocr.execution_provider": "cpu",
                "ocr.session.intra_op_num_threads": 2,
                "ocr.session.inter_op_num_threads": 1,
                "ocr.session.graph_optimization_level": "extended",
            }
        )
        fake_ort = _FakeOrtModule(["CUDAExecutionProvider", "CPUExecutionProvider"])
        with patch.object(backend, "load_onnxruntime_module", return_value=fake_ort):
            _session, provider = backend.create_session(self.model_path)

        options = fake_ort.created[0]["sess_options"]
        self.assertEqual(provider, "CPUExecutionProvider")
        self.assertEqual(fake_ort.created[0]["providers"], ["CPUExecutionProvider"])
        self.assertEqual(options.intra_op_num_threads, 2)
        self.assertEqual(options.inter_op_num_threads, 1)
        self.assertEqual(options.graph_optimization_level, "extended")

    def test_cuda_requires_cuda_provider(self):
        backend = self._backend({"ocr.execution_provider": "cuda"})
        fake_ort = _FakeOrtModule(["CPUExecutionProvider"])
        with patch.object(backend, "load_onnxruntime_module", return_value=fake_ort):
            with self.assertRaisesRegex(RuntimeError, "CUDAExecutionProvider is not available"):
                backend.create_session(self.model_path)

    def test_cuda_fallback_is_error(self):
        backend = self._backend({"ocr.execution_provider": "cuda"})
        fake_ort = _FakeOrtModule(
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
            active_providers=["CPUExecutionProvider"],
        )
        with patch.object(backend, "load_onnxruntime_module", return_value=fake_ort), patch.object(backend, "prepare_cuda_execution_provider_environment"):
            with self.assertRaisesRegex(RuntimeError, "fell back to CPUExecutionProvider"):
                backend.create_session(self.model_path)

    def test_prepare_cuda_environment_runs_once(self):
        backend = self._backend()
        fake_ort = SimpleNamespace(preload_dlls=lambda: None)
        with patch.object(backend, "register_windows_cuda_dll_directories") as register_dirs, patch.object(backend, "preload_torch_cuda_runtime") as preload_torch, patch("packages.aura_core.services.onnx_runtime_backend.os.name", "nt"):
            backend.prepare_cuda_execution_provider_environment(fake_ort)
            backend.prepare_cuda_execution_provider_environment(fake_ort)

        register_dirs.assert_called_once_with(fake_ort)
        preload_torch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.metadata
import os
import site
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from packages.aura_core.observability.logging.core_logger import logger


@dataclass(frozen=True)
class OnnxRuntimeSession:
    session: Any
    provider: str


class OnnxRuntimeBackend:
    """Shared ONNX Runtime session/provider helper for Aura vision services."""

    def __init__(
        self,
        *,
        config: Any,
        config_prefix: str,
        runtime_name: str,
        install_hint: str,
    ):
        self._config = config
        self._config_prefix = str(config_prefix).strip(".")
        self._runtime_name = runtime_name
        self._install_hint = install_hint
        self._lock = threading.RLock()
        self._cuda_runtime_prepared = False
        self._dll_directory_handles: list[Any] = []
        self._registered_dll_directories: set[str] = set()

    def create_session(self, model_path: Path) -> Tuple[Any, str]:
        ort = self.load_onnxruntime_module()
        available_providers = list(getattr(ort, "get_available_providers")())
        provider_mode = self._provider_mode()
        dist_versions = self.onnxruntime_distribution_versions()

        if provider_mode == "auto":
            providers = [
                provider
                for provider in ("CUDAExecutionProvider", "CPUExecutionProvider")
                if provider in available_providers
            ]
        elif provider_mode == "cpu":
            providers = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available_providers else []
        elif provider_mode == "cuda":
            providers = ["CUDAExecutionProvider"] if "CUDAExecutionProvider" in available_providers else []
        else:
            raise ValueError(f"{self._config_prefix}.execution_provider must be one of: auto, cpu, cuda.")

        if not providers:
            if provider_mode == "cuda":
                if "onnxruntime" in dist_versions and "onnxruntime-gpu" in dist_versions:
                    raise RuntimeError(
                        "CUDAExecutionProvider is not available. Detected both onnxruntime and onnxruntime-gpu; "
                        f"remove onnxruntime and reinstall {self._install_hint}."
                    )
                raise RuntimeError(
                    f"CUDAExecutionProvider is not available. Install the CUDA ONNX Runtime package: {self._install_hint}."
                )
            raise RuntimeError("No compatible ONNX Runtime execution provider is available.")

        if provider_mode in {"auto", "cuda"} and "CUDAExecutionProvider" not in available_providers:
            if "onnxruntime" in dist_versions and "onnxruntime-gpu" in dist_versions:
                logger.warning(
                    "Both onnxruntime and onnxruntime-gpu are installed; CUDAExecutionProvider may stay hidden until "
                    "the CPU-only onnxruntime package is removed."
                )

        if "CUDAExecutionProvider" in providers:
            self.prepare_cuda_execution_provider_environment(ort)

        session_options = self.create_session_options(ort)
        session = ort.InferenceSession(str(model_path), sess_options=session_options, providers=providers)
        active_providers = list(session.get_providers())
        active_provider = active_providers[0] if active_providers else providers[0]
        if provider_mode == "cuda" and active_provider != "CUDAExecutionProvider":
            raise RuntimeError(
                "CUDAExecutionProvider was requested but ONNX Runtime fell back to "
                f"{active_provider}. Check CUDA 12 runtime dependencies and cuDNN visibility."
            )
        if provider_mode == "auto" and providers[0] == "CUDAExecutionProvider" and active_provider != "CUDAExecutionProvider":
            logger.warning(
                "CUDAExecutionProvider is available but could not be activated; falling back to %s.",
                active_provider,
            )
        return session, active_provider

    def create_session_options(self, ort: Any) -> Any:
        session_options = ort.SessionOptions()
        intra_threads = int(self._config_get("session.intra_op_num_threads", 0) or 0)
        inter_threads = int(self._config_get("session.inter_op_num_threads", 0) or 0)
        if intra_threads > 0:
            session_options.intra_op_num_threads = intra_threads
        if inter_threads > 0:
            session_options.inter_op_num_threads = inter_threads
        session_options.graph_optimization_level = self.resolve_graph_optimization_level(
            ort=ort,
            raw_value=self._config_get("session.graph_optimization_level", "all"),
        )
        return session_options

    def load_onnxruntime_module(self) -> Any:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                f"onnxruntime is required for {self._runtime_name}. Install {self._install_hint}."
            ) from exc
        if not hasattr(ort, "InferenceSession"):
            raise RuntimeError(
                f"onnxruntime import is incomplete. Reinstall {self._install_hint}, and do not install both "
                "onnxruntime and onnxruntime-gpu."
            )
        return ort

    @staticmethod
    def onnxruntime_distribution_versions() -> Dict[str, str]:
        versions: Dict[str, str] = {}
        for dist_name in ("onnxruntime", "onnxruntime-gpu"):
            try:
                versions[dist_name] = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                continue
        return versions

    @staticmethod
    def resolve_graph_optimization_level(*, ort: Any, raw_value: Any) -> Any:
        normalized = str(raw_value or "all").strip().lower()
        graph_levels = getattr(ort, "GraphOptimizationLevel")
        mapping = {
            "disabled": graph_levels.ORT_DISABLE_ALL,
            "basic": graph_levels.ORT_ENABLE_BASIC,
            "extended": graph_levels.ORT_ENABLE_EXTENDED,
            "all": graph_levels.ORT_ENABLE_ALL,
        }
        if normalized not in mapping:
            raise ValueError("session.graph_optimization_level must be one of: disabled, basic, extended, all.")
        return mapping[normalized]

    def prepare_cuda_execution_provider_environment(self, ort: Any) -> None:
        with self._lock:
            if self._cuda_runtime_prepared:
                return
            if os.name == "nt":
                self.register_windows_cuda_dll_directories(ort)
            self.preload_torch_cuda_runtime()
            preload = getattr(ort, "preload_dlls", None)
            if callable(preload):
                try:
                    preload()
                except TypeError:
                    try:
                        preload(cuda=True, cudnn=True, msvc=True)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("ONNX Runtime CUDA DLL preload failed: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ONNX Runtime CUDA DLL preload failed: %s", exc)
            self._cuda_runtime_prepared = True

    def register_windows_cuda_dll_directories(self, ort: Any) -> None:
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if not callable(add_dll_directory):
            return

        for candidate in self.candidate_windows_cuda_directories(ort):
            resolved = str(candidate.resolve())
            if resolved in self._registered_dll_directories:
                continue
            try:
                handle = add_dll_directory(resolved)
            except OSError:
                continue
            self._dll_directory_handles.append(handle)
            self._registered_dll_directories.add(resolved)

    def candidate_windows_cuda_directories(self, ort: Any) -> Iterable[Path]:
        seen: set[str] = set()

        def emit(path: Path) -> Iterable[Path]:
            resolved = str(path.resolve())
            if path.is_dir() and resolved not in seen:
                seen.add(resolved)
                yield path

        for env_name, env_value in os.environ.items():
            if env_name == "CUDA_PATH" or env_name.startswith("CUDA_PATH_V"):
                for subdir in ("bin", "libnvvp"):
                    yield from emit(Path(env_value) / subdir)

        site_roots: list[Path] = []
        try:
            site_roots.extend(Path(entry) for entry in site.getsitepackages())
        except Exception:
            pass
        try:
            usersite = site.getusersitepackages()
            if usersite:
                site_roots.append(Path(usersite))
        except Exception:
            pass
        ort_path = getattr(ort, "__file__", None)
        if ort_path:
            site_roots.append(Path(ort_path).resolve().parents[1])

        for site_root in site_roots:
            for relative in (
                "nvidia/cuda_runtime/bin",
                "nvidia/cudnn/bin",
                "nvidia/cublas/bin",
                "nvidia/cufft/bin",
                "nvidia/curand/bin",
                "nvidia/cusolver/bin",
                "nvidia/cusparse/bin",
                "torch/lib",
                "torchvision",
                "av.libs",
            ):
                yield from emit(site_root / relative)

    @staticmethod
    def preload_torch_cuda_runtime() -> None:
        try:
            import torch
        except Exception:
            return
        try:
            if getattr(torch.version, "cuda", None):
                torch.cuda.is_available()
        except Exception:
            return

    def _provider_mode(self) -> str:
        return str(self._config_get("execution_provider", "auto") or "auto").strip().lower()

    def _config_get(self, suffix: str, default: Any = None) -> Any:
        return self._config.get(f"{self._config_prefix}.{suffix}", default)

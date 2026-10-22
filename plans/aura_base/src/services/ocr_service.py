from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.services.onnx_runtime_backend import OnnxRuntimeBackend

from .ocr_contract import OcrModelMetadata, load_ocr_metadata, metadata_path_for_bundle
from .ocr_onnx_pipeline import OcrOnnxSessions, OnnxOcrPipeline
from ._ocr_timing import OcrTiming, timed
from ._ocr_recognition import OcrRecognitionOptions


@dataclass
class OcrResult:
    found: bool = False
    text: str = ""
    center_point: tuple[int, int] | None = None
    rect: tuple[int, int, int, int] | None = None
    confidence: float = 0.0
    debug_info: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiOcrResult:
    count: int = 0
    results: list[OcrResult] = field(default_factory=list)


class _DefaultConfig:
    @staticmethod
    def get(_key: str, default: Any = None) -> Any:
        return default


@service_info(alias="ocr", public=True)
class OcrService:
    """OCR service backed by PP-OCR ONNX Runtime artifacts."""

    def __init__(self, config: ConfigService | None = None):
        self._config = config or _DefaultConfig()
        self._onnx_backend = OnnxRuntimeBackend(
            config=self._config,
            config_prefix="ocr",
            runtime_name="Aura OCR service",
            install_hint="requirements/optional-vision-onnx-cpu.txt or requirements/optional-vision-onnx-cuda.txt",
            session_defaults={"intra_op_num_threads": 8, "allow_spinning": False},
        )

        self._engine: Optional[OnnxOcrPipeline] = None
        self._warmed_engine: Optional[OnnxOcrPipeline] = None
        self._engine_device: Optional[str] = None
        self._engine_provider: Optional[str] = None
        self._engine_model: Optional[str] = None
        self._engine_lock = asyncio.Lock()
        self._ocr_semaphore = asyncio.Semaphore(1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()

    def initialize_engine(self):
        logger.info("OCR initialize requested.")
        self._submit_to_loop_and_wait(self._initialize_engine_async())

    def preload_engine(self, warmup: bool = False) -> Optional[str]:
        logger.info("OCR preload requested (warmup=%s).", warmup)
        return self._submit_to_loop_and_wait(self._preload_engine_async(warmup))

    def warmup_engine(self) -> bool:
        logger.info("OCR warmup requested.")
        return self._submit_to_loop_and_wait(self._warmup_engine_async())

    def self_check(self) -> bool:
        try:
            try:
                loop = self._get_running_loop()
            except RuntimeError:
                # CLI diagnostics run before the scheduler owns an event loop.
                # Keep initialization and inference on one temporary loop so
                # asyncio primitives are never rebound between separate runs.
                asyncio.run(self._self_check_async())
            else:
                self._submit_to_loop_and_wait(self._self_check_async(), loop=loop)
            logger.info(
                "OCR self-check OK (backend=onnxruntime, provider=%s).",
                self._engine_provider or "unknown",
            )
            return True
        except Exception as e:
            logger.error("OCR self-check failed: %s", e, exc_info=True)
            return False

    async def _self_check_async(self) -> None:
        # A blank detection result must not skip recognition or orientation.
        await self._warmup_engine_async(force=True)

    def get_backend(self) -> str:
        return "onnxruntime"

    def get_provider(self) -> Optional[str]:
        return self._engine_provider

    def get_model(self) -> Optional[str]:
        return self._engine_model

    def find_text(
        self,
        text_to_find: str,
        source_image: np.ndarray,
        match_mode: str = "exact",
        synonyms: Optional[Dict[str, str]] = None,
        *,
        recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> OcrResult:
        return self._submit_to_loop_and_wait(
            self._find_text_async(
                text_to_find, source_image, match_mode, synonyms,
                recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation,
                rotate_tall_crops=rotate_tall_crops,
            )
        )

    def find_all_text(
        self,
        text_to_find: str,
        source_image: np.ndarray,
        match_mode: str = "exact",
        synonyms: Optional[Dict[str, str]] = None,
        *,
        recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> MultiOcrResult:
        return self._submit_to_loop_and_wait(
            self._find_all_text_async(
                text_to_find, source_image, match_mode, synonyms,
                recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation,
                rotate_tall_crops=rotate_tall_crops,
            )
        )

    def recognize_text(
        self, source_image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> OcrResult:
        """Recognize text; recognition_only treats the input as one upright line."""
        return self._submit_to_loop_and_wait(
            self._recognize_text_async(
                source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation,
                rotate_tall_crops=rotate_tall_crops,
            )
        )

    def recognize_all(
        self, source_image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> MultiOcrResult:
        """Recognize regions; recognition_only returns at most one input region."""
        return self._submit_to_loop_and_wait(
            self._recognize_all_async(
                source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation,
                rotate_tall_crops=rotate_tall_crops,
            )
        )

    async def _initialize_engine_async(self):
        async with self._engine_lock:
            if self._engine is not None:
                logger.info("OCR service: ONNX engine already initialized.")
                return

            bundle_dir, metadata_path = self._resolve_bundle_paths()
            metadata = await asyncio.to_thread(load_ocr_metadata, metadata_path)
            sessions, provider = await asyncio.to_thread(self._create_pipeline_sessions, metadata, bundle_dir)
            self._engine = OnnxOcrPipeline(
                metadata=metadata, sessions=sessions, provider=provider,
                recognition_options=OcrRecognitionOptions.from_config(self._config),
            )
            self._engine_provider = provider
            self._engine_device = "gpu" if provider == "CUDAExecutionProvider" else "cpu"
            self._engine_model = bundle_dir.name
            logger.info(
                "OCR service: ONNX engine initialized (model=%s, provider=%s).",
                self._engine_model,
                self._engine_provider,
            )

    async def _preload_engine_async(self, warmup: bool) -> Optional[str]:
        await self._initialize_engine_async()
        if warmup:
            await self._warmup_engine_async()
        return self._engine_device

    async def _warmup_engine_async(self, *, force: bool = False) -> bool:
        engine = await self._get_engine_async()
        async with self._ocr_semaphore:
            if not force and self._warmed_engine is engine:
                return True
            self._warmed_engine = None
            stages = await asyncio.to_thread(engine.warmup)
            self._warmed_engine = engine
            logger.info("OCR warmup completed (model=%s, stages=%s).", self._engine_model, ",".join(stages))
        return True

    async def _get_engine_async(self) -> OnnxOcrPipeline:
        if self._engine is None:
            await self._initialize_engine_async()
        if self._engine is None:
            raise RuntimeError("OCR engine failed to initialize.")
        return self._engine

    async def _find_text_async(
        self,
        text_to_find: str,
        source_image: np.ndarray,
        match_mode: str,
        synonyms: Optional[Dict[str, str]],
        *,
        recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> OcrResult:
        all_parsed_results = await self._recognize_all_and_parse_async(
            source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops,
        )
        for result in all_parsed_results:
            normalized_text = synonyms.get(result.text, result.text) if synonyms else result.text
            if self._is_match(normalized_text, text_to_find, match_mode):
                return result
        return OcrResult(found=False, debug_info={"all_recognized_results": all_parsed_results})

    async def _find_all_text_async(
        self,
        text_to_find: str,
        source_image: np.ndarray,
        match_mode: str,
        synonyms: Optional[Dict[str, str]],
        *,
        recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> MultiOcrResult:
        all_parsed_results = await self._recognize_all_and_parse_async(
            source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops,
        )
        found_matches = []
        for result in all_parsed_results:
            normalized_text = synonyms.get(result.text, result.text) if synonyms else result.text
            if self._is_match(normalized_text, text_to_find, match_mode):
                found_matches.append(result)
        return MultiOcrResult(count=len(found_matches), results=found_matches)

    async def _recognize_text_async(
        self, source_image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> OcrResult:
        all_parsed_results = await self._recognize_all_and_parse_async(
            source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops,
        )
        if not all_parsed_results:
            return OcrResult(found=False)
        best_result = max(all_parsed_results, key=lambda r: r.confidence)
        best_result.found = True
        return best_result

    async def _recognize_all_async(
        self, source_image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> MultiOcrResult:
        all_parsed_results = await self._recognize_all_and_parse_async(
            source_image, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops,
        )
        return MultiOcrResult(count=len(all_parsed_results), results=all_parsed_results)

    async def _recognize_all_and_parse_async(
        self, source_image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> List[OcrResult]:
        engine = await self._get_engine_async()
        async with self._ocr_semaphore:
            enabled = self._config.get("ocr.profiling.enabled", False)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
            timing = OcrTiming() if enabled else None
            timing_kwargs = {"timing": timing} if timing is not None else {}
            raw_results = await asyncio.to_thread(
                self._run_ocr_sync, engine, source_image,
                recognition_only=recognition_only, det_resize_long=det_resize_long,
                use_textline_orientation=use_textline_orientation,
                rotate_tall_crops=rotate_tall_crops,
                **timing_kwargs,
            )
        with timed(timing, "result_parse"):
            results = self._parse_results(raw_results)
        if timing is not None:
            report = timing.snapshot()
            logger.info("OCR timing (ms; excludes capture, engine loading and semaphore wait): %s", report)
            for result in results:
                result.debug_info["timing"] = report
        return results

    def _run_ocr_sync(
        self, engine: OnnxOcrPipeline, image: np.ndarray, *, recognition_only: bool = False,
        det_resize_long: int | None = None, timing: OcrTiming | None = None,
        use_textline_orientation: bool = True,
        rotate_tall_crops: bool = False,
    ) -> List[Dict[str, Any]]:
        timing_kwargs = {"timing": timing} if timing is not None else {}
        return engine.predict(
            image, recognition_only=recognition_only, det_resize_long=det_resize_long,
            use_textline_orientation=use_textline_orientation,
            rotate_tall_crops=rotate_tall_crops, **timing_kwargs,
        )

    def _parse_results(self, ocr_raw_results: List[Dict[str, Any]]) -> List[OcrResult]:
        parsed_list: list[OcrResult] = []
        if not ocr_raw_results or not ocr_raw_results[0]:
            return []
        data = ocr_raw_results[0]
        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        boxes = data.get("rec_polys", [])
        for text, score, box in zip(texts, scores, boxes):
            if not isinstance(box, np.ndarray) or box.ndim != 2 or box.shape[0] < 1:
                continue
            x_coords = box[:, 0]
            y_coords = box[:, 1]
            x, y = int(np.min(x_coords)), int(np.min(y_coords))
            w, h = int(np.max(x_coords) - x), int(np.max(y_coords) - y)
            center_x, center_y = x + w // 2, y + h // 2
            parsed_list.append(
                OcrResult(
                    found=True,
                    text=str(text),
                    center_point=(center_x, center_y),
                    rect=(x, y, w, h),
                    confidence=float(score),
                    debug_info={
                        "backend": "onnxruntime",
                        "provider": self._engine_provider,
                        "polygon": box.astype(float).tolist(),
                        **({"recognition_only": True} if data.get("recognition_only") else {}),
                    },
                )
            )
        return parsed_list

    def _create_pipeline_sessions(self, metadata: OcrModelMetadata, bundle_dir: Path) -> tuple[OcrOnnxSessions, str]:
        providers: list[str] = []

        def create(relative_path: str, label: str) -> Any:
            model_path = self._resolve_model_path(bundle_dir, relative_path, label)
            session, provider = self._onnx_backend.create_session(model_path)
            providers.append(provider)
            return session

        det = create(metadata.models.det, "det")
        rec = create(metadata.models.rec, "rec")
        textline_orientation = None
        if metadata.pipeline.use_textline_orientation and metadata.textline_orientation.enabled:
            textline_orientation = create(metadata.models.textline_orientation, "textline_orientation")

        doc_orientation = None
        if metadata.pipeline.use_doc_orientation and metadata.models.doc_orientation:
            doc_orientation = create(metadata.models.doc_orientation, "doc_orientation")

        provider = providers[0] if providers else "unknown"
        if any(item != provider for item in providers):
            logger.warning("OCR ONNX sessions are using mixed providers: %s", providers)
            provider = ",".join(sorted(set(providers)))
        return OcrOnnxSessions(
            det=det,
            rec=rec,
            textline_orientation=textline_orientation,
            doc_orientation=doc_orientation,
        ), provider

    @staticmethod
    def _resolve_model_path(bundle_dir: Path, relative_path: str, label: str) -> Path:
        path = (bundle_dir / str(relative_path)).resolve()
        if path.suffix.lower() != ".onnx":
            raise ValueError(
                f"OCR {label} model must be an .onnx file. Run tools/export_paddleocr_onnx.py first."
            )
        if not path.is_file():
            raise ValueError(f"Missing OCR {label} ONNX model: {path}. Run tools/export_paddleocr_onnx.py first.")
        return path

    def _resolve_bundle_paths(self) -> tuple[Path, Path]:
        models_root_value = self._config.get("ocr.models_root", "models/ocr")
        model_name = str(self._config.get("ocr.default_model", "ppocrv6_small") or "ppocrv6_small").strip()
        if not model_name:
            raise ValueError("ocr.default_model must not be empty.")

        models_root = Path(str(models_root_value))
        if not models_root.is_absolute():
            models_root = (self._repo_root() / models_root).resolve()
        bundle_dir = (models_root / model_name).resolve()
        metadata_path = metadata_path_for_bundle(bundle_dir)
        return bundle_dir, metadata_path

    @staticmethod
    def _is_match(text_to_check: str, text_to_find: str, match_mode: str) -> bool:
        if match_mode == "exact":
            return text_to_check == text_to_find
        if match_mode == "contains":
            return text_to_find in text_to_check
        if match_mode == "regex":
            try:
                return bool(re.search(text_to_find, text_to_check))
            except re.error:
                logger.warning("Invalid OCR regex %r; falling back to contains matching.", text_to_find)
                return text_to_find in text_to_check
        return False

    def _get_running_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    from packages.aura_core.api import service_registry

                    scheduler = service_registry.get_service_instance("scheduler")
                    if scheduler and scheduler._loop and scheduler._loop.is_running():
                        self._loop = scheduler._loop
                    else:
                        raise RuntimeError("OcrService could not find a running asyncio event loop.")
            return self._loop

    def _submit_to_loop_and_wait(
        self,
        coro: asyncio.Future,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> Any:
        try:
            loop = loop or self._get_running_loop()
        except Exception:
            if hasattr(coro, "close"):
                coro.close()
            raise
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            if hasattr(coro, "close"):
                coro.close()
            raise RuntimeError("OcrService sync API called from event loop thread; use async internals instead.")

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[4]

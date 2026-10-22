"""Template matching with synchronous kernels and asynchronous offloading."""

import asyncio
import glob
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Tuple, Optional, Dict, List, Iterable

import cv2
import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger

from ._vision_timing import VisionTiming, start_vision_timing
from ._template_image_cache import TemplateImageCache
from .prepared_template import PreparedTemplate


# --- 返回值数据结构 (保持不变) ---

@dataclass
class MatchResult:
    found: bool = False
    top_left: tuple[int, int] | None = None
    center_point: tuple[int, int] | None = None
    rect: tuple[int, int, int, int] | None = None
    confidence: float = 0.0
    debug_info: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiMatchResult:
    count: int = 0
    matches: list[MatchResult] = field(default_factory=list)

@dataclass(frozen=True)
class TemplateLibrary:
    name: str
    root: Path
    recursive: bool
    extensions: tuple[str, ...]


@service_info(alias="vision", public=True)
class VisionService:
    """
    同步模板接口在调用线程计算；异步接口完整卸载到工作线程。
    匹配算法、分数和结果结构由两种入口共享。
    """

    _BATCH_MATCH_MIN_PARALLEL_SIZE = 4
    _BATCH_MATCH_MAX_WORKERS = 8

    def __init__(self):
        logger.info("视觉服务 (异步核心版) 已初始化。")
        # --- 桥接器组件 ---
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self._template_libraries: Dict[str, Dict[str, TemplateLibrary]] = {}
        self._template_lock = threading.RLock()
        self._template_image_cache = TemplateImageCache()

    # =========================================================================
    # Section 0: Template Library
    # =========================================================================

    def register_template_library(
            self,
            plan_key: str,
            name: str,
            root: Path,
            recursive: bool = False,
            extensions: Optional[Iterable[str]] = None,
    ):
        if not plan_key or not name:
            raise ValueError("plan_key and name are required for template library registration.")
        root_path = Path(root)
        normalized_exts = self._normalize_extensions(extensions)
        if not root_path.exists():
            logger.warning("Template library '%s' root not found: %s", name, root_path)
        with self._template_lock:
            plan_libs = self._template_libraries.setdefault(plan_key, {})
            plan_libs[name] = TemplateLibrary(
                name=name,
                root=root_path,
                recursive=bool(recursive),
                extensions=normalized_exts,
            )

    def unregister_template_library(self, plan_key: str, name: str):
        if not plan_key or not name:
            return
        with self._template_lock:
            libs = self._template_libraries.get(plan_key, {})
            libs.pop(name, None)

    def list_template_libraries(self, plan_key: str) -> Dict[str, Dict[str, Any]]:
        with self._template_lock:
            libs = self._template_libraries.get(plan_key, {})
            return {
                k: {
                    "root": str(v.root),
                    "recursive": v.recursive,
                    "extensions": list(v.extensions),
                }
                for k, v in libs.items()
            }

    def resolve_template(self, plan_key: str, ref: str, plan_path: Path) -> Path:
        paths = self.expand_templates(plan_key, ref, plan_path, expect_single=True)
        if len(paths) != 1:
            raise FileNotFoundError(f"Template reference '{ref}' resolved to {len(paths)} files.")
        return paths[0]

    def expand_templates(self, plan_key: str, ref: str, plan_path: Path, expect_single: bool = False) -> List[Path]:
        if not ref:
            raise ValueError("Template reference is empty.")
        plan_path = Path(plan_path)
        base_path, rel_path, library = self._resolve_template_base(plan_key, ref, plan_path)
        target = base_path / rel_path if rel_path else base_path
        recursive = library.recursive if library else False
        extensions = library.extensions if library else self._normalize_extensions(None)

        if self._contains_glob(str(target)):
            matches = [
                Path(p) for p in glob.glob(str(target), recursive=recursive)
                if Path(p).is_file()
            ]
            return self._filter_by_extensions(matches, extensions)

        if target.is_dir():
            files = target.rglob("*") if recursive else target.iterdir()
            return self._filter_by_extensions([p for p in files if p.is_file()], extensions)

        if not target.is_file():
            if expect_single:
                raise FileNotFoundError(f"Template file not found: {target}")
            return []
        return [target]

    def _resolve_template_base(self, plan_key: str, ref: str, plan_path: Path) -> tuple[Path, Path, Optional[TemplateLibrary]]:
        ref = ref.strip()
        if not ref:
            return plan_path, Path(), None

        if ref.startswith("./") or ref.startswith(".\\"):
            rel = Path(ref)
            return plan_path, rel, None

        if ref.startswith("@"):
            lib_name, rel = self._split_library_ref(ref[1:])
            library = self._get_library(plan_key, lib_name)
            if not library:
                raise KeyError(f"Template library '{lib_name}' is not registered.")
            return library.root, rel, library

        if Path(ref).is_absolute():
            return Path(ref), Path(), None

        lib_name, rel = self._split_library_ref(ref)
        library = self._get_library(plan_key, lib_name)
        if library:
            return library.root, rel, library

        return plan_path, Path(ref), None

    def _get_library(self, plan_key: str, name: str) -> Optional[TemplateLibrary]:
        if not plan_key:
            return None
        with self._template_lock:
            return self._template_libraries.get(plan_key, {}).get(name)

    def _split_library_ref(self, ref: str) -> tuple[str, Path]:
        normalized = ref.replace("\\", "/")
        parts = normalized.split("/", 1)
        if len(parts) == 1:
            return parts[0], Path()
        return parts[0], Path(parts[1])

    def _normalize_extensions(self, extensions: Optional[Iterable[str]]) -> tuple[str, ...]:
        if not extensions:
            return (".png", ".jpg", ".jpeg", ".bmp")
        normalized = []
        for ext in extensions:
            if not ext:
                continue
            ext_str = str(ext).lower()
            if not ext_str.startswith("."):
                ext_str = f".{ext_str}"
            normalized.append(ext_str)
        return tuple(dict.fromkeys(normalized))

    def _filter_by_extensions(self, paths: Iterable[Path], extensions: Iterable[str]) -> List[Path]:
        ext_set = {e.lower() for e in extensions}
        return sorted([p for p in paths if p.suffix.lower() in ext_set])

    def _contains_glob(self, value: str) -> bool:
        return any(ch in value for ch in "*?[]")

    # =========================================================================
    # Section 1: 公共同步接口
    # =========================================================================

    def find_template(self,
                      source_image: np.ndarray | str,
                      template_image: np.ndarray | str | PreparedTemplate,
                      mask_image: Optional[np.ndarray | str] = None,
                      threshold: float = 0.8,
                      use_grayscale: bool = True,
                      match_method: int = cv2.TM_CCOEFF_NORMED,
                      preprocess: str = "none",
                      score_scale: Optional[float] = None) -> MatchResult:
        """【同步接口】在源图像中查找最匹配的单个模板。"""
        self._ensure_template_sync_call_allowed()
        timing = self._start_template_timing(
            "find_template", source_image, [template_image], use_grayscale, match_method, preprocess, "sync",
        )
        return self._run_template_request(
            self._find_template_sync,
            (
                source_image,
                template_image,
                mask_image,
                threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
            ),
            timing,
        )

    def find_all_templates(self,
                           source_image: np.ndarray,
                           template_image: np.ndarray | str | PreparedTemplate,
                           mask_image: Optional[np.ndarray | str] = None,
                           threshold: float = 0.8,
                           nms_threshold: float = 0.5,
                           use_grayscale: bool = True,
                           match_method: int = cv2.TM_CCOEFF_NORMED,
                           preprocess: str = "none",
                           score_scale: Optional[float] = None) -> MultiMatchResult:
        """【同步接口】在源图像中查找所有匹配的模板实例。"""
        self._ensure_template_sync_call_allowed()
        timing = self._start_template_timing(
            "find_all_templates", source_image, [template_image], use_grayscale, match_method, preprocess, "sync",
        )
        return self._run_template_request(
            self._find_all_templates_sync,
            (
                source_image,
                template_image,
                mask_image,
                threshold,
                nms_threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
            ),
            timing,
        )

    def find_templates_batch(self,
                             source_image: np.ndarray,
                             template_images: List[np.ndarray | str | PreparedTemplate],
                             mask_images: Optional[List[np.ndarray | str]] = None,
                             threshold: float = 0.8,
                             use_grayscale: bool = True,
                             match_method: int = cv2.TM_CCOEFF_NORMED,
                             preprocess: str = "none",
                             score_scale: Optional[float] = None) -> List[MatchResult]:
        """【同步接口】在源图像中批量查找多个模板的最佳匹配。"""
        self._ensure_template_sync_call_allowed()
        timing = self._start_template_timing(
            "find_templates_batch", source_image, template_images, use_grayscale, match_method, preprocess, "sync",
        )
        return self._run_template_request(
            self._find_templates_batch_request_sync,
            (
                source_image,
                template_images,
                mask_images,
                threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
            ),
            timing,
        )

    def find_all_templates_batch(self,
                                 source_image: np.ndarray,
                                 template_images: List[np.ndarray | str | PreparedTemplate],
                                 mask_images: Optional[List[np.ndarray | str]] = None,
                                 threshold: float = 0.8,
                                 nms_threshold: float = 0.5,
                                 use_grayscale: bool = True,
                                 match_method: int = cv2.TM_CCOEFF_NORMED,
                                 preprocess: str = "none",
                                 score_scale: Optional[float] = None) -> List[MultiMatchResult]:
        """【同步接口】在源图像中批量查找多个模板的所有匹配。"""
        self._ensure_template_sync_call_allowed()
        timing = self._start_template_timing(
            "find_all_templates_batch", source_image, template_images, use_grayscale, match_method, preprocess, "sync",
        )
        return self._run_template_request(
            self._find_all_templates_batch_request_sync,
            (
                source_image,
                template_images,
                mask_images,
                threshold,
                nms_threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
            ),
            timing,
        )

    def find_color(self,
                   source_image: np.ndarray,
                   lower_hsv: Tuple[int, int, int],
                   upper_hsv: Tuple[int, int, int],
                   min_area: int = 50) -> MatchResult:
        """【同步接口】在源图像中查找指定HSV颜色范围内的最大区域。"""
        return self._submit_to_loop_and_wait(
            self.find_color_async(source_image, lower_hsv, upper_hsv, min_area)
        )

    # =========================================================================
    # Section 2: 异步入口（包含准备阶段的完整卸载）
    # =========================================================================

    async def find_template_async(self,
                                  source_image: np.ndarray | str,
                                  template_image: np.ndarray | str | PreparedTemplate,
                                  mask_image: Optional[np.ndarray | str] = None,
                                  threshold: float = 0.8,
                                  use_grayscale: bool = True,
                                  match_method: int = cv2.TM_CCOEFF_NORMED,
                                  preprocess: str = "none",
                                  score_scale: Optional[float] = None) -> MatchResult:
        """将准备、匹配和结果包装完整调度到后台线程。"""
        timing = self._start_template_timing(
            "find_template", source_image, [template_image], use_grayscale, match_method, preprocess, "async",
        )
        return await asyncio.to_thread(
            self._run_template_request,
            self._find_template_sync,
            (source_image, template_image, mask_image, threshold, use_grayscale, match_method, preprocess, score_scale),
            timing,
        )

    async def find_all_templates_async(self,
                                       source_image: np.ndarray,
                                       template_image: np.ndarray | str | PreparedTemplate,
                                       mask_image: Optional[np.ndarray | str] = None,
                                       threshold: float = 0.8,
                                       nms_threshold: float = 0.5,
                                       use_grayscale: bool = True,
                                       match_method: int = cv2.TM_CCOEFF_NORMED,
                                       preprocess: str = "none",
                                       score_scale: Optional[float] = None) -> MultiMatchResult:
        """将多目标模板请求完整调度到后台线程。"""
        timing = self._start_template_timing(
            "find_all_templates", source_image, [template_image], use_grayscale, match_method, preprocess, "async",
        )
        return await asyncio.to_thread(
            self._run_template_request,
            self._find_all_templates_sync,
            (source_image, template_image, mask_image, threshold, nms_threshold,
             use_grayscale, match_method, preprocess, score_scale),
            timing,
        )

    async def find_templates_batch_async(self,
                                         source_image: np.ndarray,
                                         template_images: List[np.ndarray | str | PreparedTemplate],
                                         mask_images: Optional[List[np.ndarray | str]] = None,
                                         threshold: float = 0.8,
                                         use_grayscale: bool = True,
                                         match_method: int = cv2.TM_CCOEFF_NORMED,
                                         preprocess: str = "none",
                                         score_scale: Optional[float] = None) -> List[MatchResult]:
        """将批量模板请求完整调度到后台线程。"""
        timing = self._start_template_timing(
            "find_templates_batch", source_image, template_images, use_grayscale, match_method, preprocess, "async",
        )
        return await asyncio.to_thread(
            self._run_template_request,
            self._find_templates_batch_request_sync,
            (source_image, template_images, mask_images, threshold, use_grayscale, match_method, preprocess, score_scale),
            timing,
        )

    async def find_all_templates_batch_async(self,
                                             source_image: np.ndarray,
                                             template_images: List[np.ndarray | str | PreparedTemplate],
                                             mask_images: Optional[List[np.ndarray | str]] = None,
                                             threshold: float = 0.8,
                                             nms_threshold: float = 0.5,
                                             use_grayscale: bool = True,
                                             match_method: int = cv2.TM_CCOEFF_NORMED,
                                             preprocess: str = "none",
                                             score_scale: Optional[float] = None) -> List[MultiMatchResult]:
        """将批量多目标模板请求完整调度到后台线程。"""
        timing = self._start_template_timing(
            "find_all_templates_batch", source_image, template_images, use_grayscale, match_method, preprocess, "async",
        )
        return await asyncio.to_thread(
            self._run_template_request,
            self._find_all_templates_batch_request_sync,
            (source_image, template_images, mask_images, threshold, nms_threshold,
             use_grayscale, match_method, preprocess, score_scale),
            timing,
        )

    # =========================================================================
    # 完整同步请求内核；异常契约由同步和异步入口共享。
    # =========================================================================

    def _find_template_sync(self,
                            source_image: np.ndarray | str,
                            template_image: np.ndarray | str | PreparedTemplate,
                            mask_image: Optional[np.ndarray | str],
                            threshold: float,
                            use_grayscale: bool,
                            match_method: int,
                            preprocess: str,
                            score_scale: Optional[float],
                            *, _timing: VisionTiming | None = None) -> MatchResult:
        try:
            source_prepared = self._prepare_match_image(
                source_image,
                use_grayscale=use_grayscale,
                preprocess=preprocess,
                stage="source_prepare", timing=_timing,
            )
            template_prepared, mask = self._prepare_template_image(
                template_image, mask_image,
                use_grayscale=use_grayscale,
                preprocess=preprocess,
                timing=_timing,
            )

            if mask is not None and mask.shape[:2] != template_prepared.shape[:2]:
                raise ValueError(
                    f"蒙版尺寸 {mask.shape} 必须与模板尺寸 {template_prepared.shape} 完全一致。"
                )

            return self._match_template_prepared(
                source_prepared,
                template_prepared,
                mask,
                threshold,
                match_method,
                use_grayscale,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )
        except (FileNotFoundError, TypeError, ValueError) as e:
            if _timing is not None:
                _timing.handled_error(e)
            logger.error(f"模板匹配预处理失败: {e}")
            return MatchResult(found=False, debug_info={"error": str(e)})

    def _find_all_templates_sync(self,
                                       source_image: np.ndarray,
                                       template_image: np.ndarray | str | PreparedTemplate,
                                       mask_image: Optional[np.ndarray | str] = None,
                                       threshold: float = 0.8,
                                       nms_threshold: float = 0.5,
                                       use_grayscale: bool = True,
                                       match_method: int = cv2.TM_CCOEFF_NORMED,
                                       preprocess: str = "none",
                                       score_scale: Optional[float] = None,
                                       *, _timing: VisionTiming | None = None) -> MultiMatchResult:
        try:
            source_prepared = self._prepare_match_image(
                source_image,
                use_grayscale=use_grayscale,
                preprocess=preprocess,
                stage="source_prepare", timing=_timing,
            )
            template_prepared, mask = self._prepare_template_image(
                template_image, mask_image,
                use_grayscale=use_grayscale,
                preprocess=preprocess,
                timing=_timing,
            )

            if mask is not None and mask.shape[:2] != template_prepared.shape[:2]:
                raise ValueError(
                    f"蒙版尺寸 {mask.shape} 必须与模板尺寸 {template_prepared.shape} 完全一致。"
                )

            matches = self._match_all_templates_prepared(
                source_prepared,
                template_prepared,
                mask,
                threshold,
                nms_threshold,
                match_method,
                use_grayscale,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )
            return MultiMatchResult(count=len(matches), matches=matches)

        except (FileNotFoundError, TypeError, ValueError) as e:
            if _timing is not None:
                _timing.handled_error(e)
            logger.error(f"查找所有模板预处理失败: {e}")
            return MultiMatchResult()

    def _find_templates_batch_request_sync(self,
                                         source_image: np.ndarray,
                                         template_images: List[np.ndarray | str | PreparedTemplate],
                                         mask_images: Optional[List[np.ndarray | str]] = None,
                                         threshold: float = 0.8,
                                         use_grayscale: bool = True,
                                         match_method: int = cv2.TM_CCOEFF_NORMED,
                                         preprocess: str = "none",
                                         score_scale: Optional[float] = None,
                                         *, _timing: VisionTiming | None = None) -> List[MatchResult]:
        if mask_images is not None and len(mask_images) != len(template_images):
            raise ValueError("mask_images length must match template_images length.")
        try:
            return self._find_templates_batch_sync(
                source_image,
                template_images,
                mask_images,
                threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )
        except (FileNotFoundError, TypeError, ValueError) as e:
            if _timing is not None:
                _timing.handled_error(e)
            logger.error(f"批量模板匹配预处理失败: {e}")
            return [MatchResult(found=False, debug_info={"error": str(e)}) for _ in template_images]

    def _find_all_templates_batch_request_sync(self,
                                             source_image: np.ndarray,
                                             template_images: List[np.ndarray | str | PreparedTemplate],
                                             mask_images: Optional[List[np.ndarray | str]] = None,
                                             threshold: float = 0.8,
                                             nms_threshold: float = 0.5,
                                             use_grayscale: bool = True,
                                             match_method: int = cv2.TM_CCOEFF_NORMED,
                                             preprocess: str = "none",
                                             score_scale: Optional[float] = None,
                                             *, _timing: VisionTiming | None = None) -> List[MultiMatchResult]:
        if mask_images is not None and len(mask_images) != len(template_images):
            raise ValueError("mask_images length must match template_images length.")
        try:
            return self._find_all_templates_batch_sync(
                source_image,
                template_images,
                mask_images,
                threshold,
                nms_threshold,
                use_grayscale,
                match_method,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )
        except (FileNotFoundError, TypeError, ValueError) as e:
            if _timing is not None:
                _timing.handled_error(e)
            logger.error(f"批量模板查找预处理失败: {e}")
            return [MultiMatchResult() for _ in template_images]

    async def find_color_async(self,
                               source_image: np.ndarray,
                               lower_hsv: Tuple[int, int, int],
                               upper_hsv: Tuple[int, int, int],
                               min_area: int = 50) -> MatchResult:
        """【异步内核】将颜色查找计算调度到后台线程。"""
        if not isinstance(source_image, np.ndarray) or len(source_image.shape) != 3:
            return MatchResult(found=False, debug_info={"error": "输入图像必须是BGR或RGB格式的NumPy数组。"})

        def _find_largest_contour():
            hsv_image = cv2.cvtColor(source_image, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv_image, np.array(lower_hsv), np.array(upper_hsv))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not contours:
                return None, 0

            best_contour = max(contours, key=cv2.contourArea)
            return best_contour, cv2.contourArea(best_contour)

        best_contour, area = await asyncio.to_thread(_find_largest_contour)

        if best_contour is not None and area >= min_area:
            x, y, w, h = cv2.boundingRect(best_contour)
            return MatchResult(
                found=True,
                top_left=(x, y),
                center_point=(x + w // 2, y + h // 2),
                rect=(x, y, w, h),
                confidence=area
            )

        return MatchResult(found=False, confidence=area)

    # =========================================================================
    # Section 3: 内部辅助工具 (同步)
    # =========================================================================

    @staticmethod
    def load_image_file(
            image_path: str | Path,
            flags: int = cv2.IMREAD_UNCHANGED,
    ) -> np.ndarray:
        """Load an OpenCV image from a filesystem path, including Unicode paths."""
        path = Path(image_path)
        try:
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        except OSError as exc:
            raise FileNotFoundError(f"无法从路径加载图像: {image_path}") from exc
        image = cv2.imdecode(encoded, int(flags))
        if image is None:
            raise FileNotFoundError(f"无法从路径加载图像: {image_path}")
        return image

    def load_template_image(self, image_path: str | Path) -> np.ndarray:
        """Return a versioned, read-only RGB/gray template snapshot for previews.

        This shares the matching cache and does not prepare or retain captures.
        Unlike load_image_file(), the returned color pixels are already RGB.
        """
        return self._prepare_match_image(
            str(image_path), use_grayscale=False, preprocess="none",
            stage="template_prepare_sum", timing=None,
        )

    def _prepare_image(self,
                       image: np.ndarray | str | None,
                       use_grayscale: bool = True,
                       preprocess: str = "none") -> np.ndarray | None:
        """
        Prepare image for template matching.
        - File inputs are converted from BGR/BGRA to RGB.
        - ndarray inputs are assumed to be RGB.
        """
        if image is None:
            return None

        input_is_bgr = isinstance(image, str)
        if isinstance(image, str):
            img = self.load_image_file(image, cv2.IMREAD_UNCHANGED)
        elif isinstance(image, np.ndarray):
            img = image
        else:
            raise TypeError(f"不支持的图像类型，需要str(路径)或np.ndarray。实际类型为{type(image)}")

        img = self._normalize_color_space(img, input_is_bgr=input_is_bgr)

        if (preprocess or "none").lower() == "lab_chroma_blur" and use_grayscale:
            raise ValueError("lab_chroma_blur requires use_grayscale=False")

        if use_grayscale:
            if img.ndim == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            elif img.ndim != 2:
                raise TypeError(f"不支持的图像形状: {img.shape}")

        img = self._apply_preprocess(img, preprocess)
        return img

    def _normalize_color_space(self, img: np.ndarray, input_is_bgr: bool) -> np.ndarray:
        if img.ndim == 2:
            return img
        if img.ndim == 3:
            channels = img.shape[2]
            if channels == 4:
                return cv2.cvtColor(img, cv2.COLOR_BGRA2RGB) if input_is_bgr else cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
            if channels == 3:
                return cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if input_is_bgr else img
            if channels == 1:
                return img.squeeze(axis=2)
            raise TypeError(f"不支持的3D图像通道数: {channels}")
        raise TypeError(f"不支持的图像形状: {img.shape}")

    def _apply_preprocess(self, img: np.ndarray, preprocess: str) -> np.ndarray:
        preprocess = (preprocess or "none").lower()
        if preprocess == "none":
            return img
        if preprocess == "blur":
            return cv2.GaussianBlur(img, (3, 3), 0)
        if preprocess == "lab_chroma_blur":
            if img.ndim != 3 or img.shape[2] != 3:
                raise ValueError("lab_chroma_blur requires a three-channel RGB image")
            blurred = cv2.GaussianBlur(img, (3, 3), 0)
            lab = cv2.cvtColor(blurred, cv2.COLOR_RGB2LAB).astype(np.float32)
            lab[:, :, 0] *= 0.2
            return lab
        if preprocess == "normalize":
            normalized = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
            return normalized.astype(np.uint8) if normalized.dtype != np.uint8 else normalized
        if preprocess == "edge":
            if img.ndim == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            return cv2.Canny(img, 50, 150)
        raise ValueError(f"Unsupported preprocess mode: {preprocess}")

    def _prepare_template_batch(self,
                                source_image: np.ndarray,
                                template_images: List[np.ndarray | str | PreparedTemplate],
                                mask_images: Optional[List[np.ndarray | str]],
                                use_grayscale: bool,
                                preprocess: str,
                                *, _timing: VisionTiming | None = None) -> tuple[np.ndarray, list[tuple[np.ndarray, Optional[np.ndarray]]]]:
        source_prepared = self._prepare_match_image(
            source_image,
            use_grayscale=use_grayscale,
            preprocess=preprocess,
            stage="source_prepare", timing=_timing,
        )
        prepared_templates: list[tuple[np.ndarray, Optional[np.ndarray]]] = []
        for index, template_image in enumerate(template_images):
            if isinstance(template_image, PreparedTemplate):
                template_prepared, mask = self._prepare_template_image(
                    template_image, mask_images[index] if mask_images is not None else None,
                    use_grayscale=use_grayscale, preprocess=preprocess, timing=_timing,
                )
            else:
                # Preserve legacy failure precedence: prepare the template before
                # indexing its mask, including malformed mask containers.
                template_prepared = self._prepare_match_image(
                    template_image, use_grayscale=use_grayscale, preprocess=preprocess,
                    stage="template_prepare_sum", timing=_timing,
                )
                mask = None
                if mask_images is not None:
                    mask = self._prepare_match_image(
                        mask_images[index], use_grayscale=True, preprocess="none",
                        stage="mask_prepare_sum", timing=_timing,
                    )
            prepared_templates.append((template_prepared, mask))
        return source_prepared, prepared_templates

    def _find_templates_batch_sync(self,
                                   source_image: np.ndarray,
                                   template_images: List[np.ndarray | str | PreparedTemplate],
                                   mask_images: Optional[List[np.ndarray | str]],
                                   threshold: float,
                                   use_grayscale: bool,
                                   match_method: int,
                                   preprocess: str,
                                   score_scale: Optional[float] = None,
                                   *, _timing: VisionTiming | None = None) -> List[MatchResult]:
        source_prepared, prepared_templates = self._prepare_template_batch(
            source_image,
            template_images,
            mask_images,
            use_grayscale,
            preprocess,
            **({"_timing": _timing} if _timing is not None else {}),
        )

        def match_one(item: tuple[np.ndarray, Optional[np.ndarray]]) -> MatchResult:
            template_prepared, mask = item
            return self._match_template_prepared(
                source_prepared,
                template_prepared,
                mask,
                threshold,
                match_method,
                use_grayscale,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )

        started_at = time.perf_counter() if _timing is not None else None
        try:
            if len(prepared_templates) < self._BATCH_MATCH_MIN_PARALLEL_SIZE:
                return [match_one(item) for item in prepared_templates]
            worker_count = min(self._BATCH_MATCH_MAX_WORKERS, len(prepared_templates))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="vision-match") as executor:
                return list(executor.map(match_one, prepared_templates))
        finally:
            if _timing is not None:
                _timing.record("batch_compute_wall", started_at)

    def _find_all_templates_batch_sync(self,
                                       source_image: np.ndarray,
                                       template_images: List[np.ndarray | str | PreparedTemplate],
                                       mask_images: Optional[List[np.ndarray | str]],
                                       threshold: float,
                                       nms_threshold: float,
                                       use_grayscale: bool,
                                       match_method: int,
                                       preprocess: str,
                                       score_scale: Optional[float] = None,
                                       *, _timing: VisionTiming | None = None) -> List[MultiMatchResult]:
        source_prepared, prepared_templates = self._prepare_template_batch(
            source_image,
            template_images,
            mask_images,
            use_grayscale,
            preprocess,
            **({"_timing": _timing} if _timing is not None else {}),
        )

        def match_one(item: tuple[np.ndarray, Optional[np.ndarray]]) -> MultiMatchResult:
            template_prepared, mask = item
            matches = self._match_all_templates_prepared(
                source_prepared,
                template_prepared,
                mask,
                threshold,
                nms_threshold,
                match_method,
                use_grayscale,
                preprocess,
                score_scale,
                **({"_timing": _timing} if _timing is not None else {}),
            )
            return MultiMatchResult(count=len(matches), matches=matches)

        started_at = time.perf_counter() if _timing is not None else None
        try:
            if len(prepared_templates) < self._BATCH_MATCH_MIN_PARALLEL_SIZE:
                return [match_one(item) for item in prepared_templates]
            worker_count = min(self._BATCH_MATCH_MAX_WORKERS, len(prepared_templates))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="vision-match-all") as executor:
                return list(executor.map(match_one, prepared_templates))
        finally:
            if _timing is not None:
                _timing.record("batch_compute_wall", started_at)

    def _select_best_match(self, match_method: int, min_val: float, max_val: float,
                           min_loc: tuple[int, int], max_loc: tuple[int, int]) -> tuple[float, tuple[int, int]]:
        if match_method in (cv2.TM_SQDIFF, cv2.TM_SQDIFF_NORMED):
            return min_val, min_loc
        return max_val, max_loc

    def _normalize_match_score(
            self,
            score: float,
            match_method: int,
            template_shape: Optional[tuple[int, ...]] = None,
            score_scale: Optional[float] = None,
    ) -> float:
        if match_method == cv2.TM_SQDIFF_NORMED:
            return 1.0 - float(score)
        if match_method == cv2.TM_SQDIFF:
            if score_scale is not None:
                if score_scale <= 0:
                    raise ValueError("score_scale must be greater than zero")
                if not template_shape:
                    raise ValueError("template_shape is required when score_scale is set")
                mse = float(score) / float(np.prod(template_shape))
                return 1.0 / (1.0 + mse / float(score_scale))
            return 1.0 / (1.0 + float(score))
        return float(score)

    def _normalize_match_map(
            self,
            score_map: np.ndarray,
            match_method: int,
            template_shape: Optional[tuple[int, ...]] = None,
            score_scale: Optional[float] = None,
    ) -> np.ndarray:
        if match_method == cv2.TM_SQDIFF_NORMED:
            return 1.0 - score_map
        if match_method == cv2.TM_SQDIFF:
            if score_scale is not None:
                if score_scale <= 0:
                    raise ValueError("score_scale must be greater than zero")
                if not template_shape:
                    raise ValueError("template_shape is required when score_scale is set")
                mse_map = score_map / float(np.prod(template_shape))
                return 1.0 / (1.0 + mse_map / float(score_scale))
            return 1.0 / (1.0 + score_map)
        return score_map

    def _match_template_prepared(self,
                                 source_prepared: np.ndarray,
                                 template_prepared: np.ndarray,
                                 mask: Optional[np.ndarray],
                                 threshold: float,
                                 match_method: int,
                                 use_grayscale: bool,
                                 preprocess: str,
                                 score_scale: Optional[float] = None,
                                 *, _timing: VisionTiming | None = None) -> MatchResult:
        if mask is not None and mask.shape[:2] != template_prepared.shape[:2]:
            raise ValueError(
                f"蒙版尺寸 {mask.shape} 必须与模板尺寸 {template_prepared.shape} 完全一致。"
            )
        started_at = time.perf_counter() if _timing is not None else None
        try:
            result = cv2.matchTemplate(source_prepared, template_prepared, match_method, mask=mask)
        finally:
            if _timing is not None:
                _timing.record("match_sum", started_at)
        postprocess_started_at = time.perf_counter() if _timing is not None else None
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        best_val, best_loc = self._select_best_match(match_method, min_val, max_val, min_loc, max_loc)
        best_confidence = self._normalize_match_score(
            best_val,
            match_method,
            template_prepared.shape,
            score_scale,
        )
        h, w = template_prepared.shape[:2]

        debug_info = {
            "match_method": match_method,
            "use_grayscale": use_grayscale,
            "preprocess": preprocess,
        }
        if score_scale is not None:
            debug_info["score_scale"] = score_scale

        if best_confidence >= threshold:
            match = MatchResult(
                found=True,
                top_left=best_loc,
                center_point=(best_loc[0] + w // 2, best_loc[1] + h // 2),
                rect=(best_loc[0], best_loc[1], w, h),
                confidence=best_confidence,
                debug_info=debug_info,
            )
            if _timing is not None:
                _timing.record("postprocess_sum", postprocess_started_at)
            return match
        debug_info["best_match_rect_on_fail"] = (best_loc[0], best_loc[1], w, h)
        match = MatchResult(
            found=False,
            confidence=best_confidence,
            debug_info=debug_info,
        )
        if _timing is not None:
            _timing.record("postprocess_sum", postprocess_started_at)
        return match

    def _match_all_templates_prepared(self,
                                      source_prepared: np.ndarray,
                                      template_prepared: np.ndarray,
                                      mask: Optional[np.ndarray],
                                      threshold: float,
                                      nms_threshold: float,
                                      match_method: int,
                                      use_grayscale: bool,
                                      preprocess: str,
                                      score_scale: Optional[float] = None,
                                      *, _timing: VisionTiming | None = None) -> List[MatchResult]:
        if mask is not None and mask.shape[:2] != template_prepared.shape[:2]:
            raise ValueError(
                f"蒙版尺寸 {mask.shape} 必须与模板尺寸 {template_prepared.shape} 完全一致。"
            )
        started_at = time.perf_counter() if _timing is not None else None
        try:
            result = cv2.matchTemplate(source_prepared, template_prepared, match_method, mask=mask)
        finally:
            if _timing is not None:
                _timing.record("match_sum", started_at)
        postprocess_started_at = time.perf_counter() if _timing is not None else None
        score_map = self._normalize_match_map(
            result,
            match_method,
            template_prepared.shape,
            score_scale,
        )
        locations = np.where(score_map >= threshold)
        if _timing is not None:
            _timing.add_candidates(len(locations[0]))

        h, w = template_prepared.shape[:2]
        rects = [[pt[0], pt[1], w, h] for pt in zip(*locations[::-1])]
        scores = [score_map[pt[1], pt[0]] for pt in zip(*locations[::-1])]

        if not rects:
            if _timing is not None:
                _timing.record("postprocess_sum", postprocess_started_at)
            return []

        indices = cv2.dnn.NMSBoxes(rects, np.array(scores, dtype=np.float32), threshold, nms_threshold)

        final_matches = []
        if len(indices) > 0:
            for i in indices.flatten():
                box = rects[i]
                top_left = (box[0], box[1])
                final_matches.append(MatchResult(
                    found=True,
                    top_left=top_left,
                    center_point=(top_left[0] + w // 2, top_left[1] + h // 2),
                    rect=(top_left[0], top_left[1], w, h),
                    confidence=float(scores[i]),
                    debug_info={
                        "match_method": match_method,
                        "use_grayscale": use_grayscale,
                        "preprocess": preprocess,
                        **({"score_scale": score_scale} if score_scale is not None else {}),
                    },
                ))
        if _timing is not None:
            _timing.record("postprocess_sum", postprocess_started_at)
        return final_matches

    # =========================================================================
    # Section 4: 同步/异步桥接器
    # =========================================================================

    def _get_running_loop(self) -> asyncio.AbstractEventLoop:
        """线程安全地获取正在运行的事件循环。"""
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                from packages.aura_core.api import service_registry
                scheduler = service_registry.get_service_instance('scheduler')
                if scheduler and scheduler._loop and scheduler._loop.is_running():
                    self._loop = scheduler._loop
                else:
                    raise RuntimeError("VisionService无法找到正在运行的asyncio事件循环。")
            return self._loop

    def _submit_to_loop_and_wait(self, coro: asyncio.Future) -> Any:
        """将一个协程从同步代码提交到事件循环，并阻塞等待其结果。"""
        loop = self._get_running_loop()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            raise RuntimeError("VisionService sync API called from event loop thread; use *_async to avoid deadlock.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def _ensure_template_sync_call_allowed(self) -> None:
        """Keep the existing scheduler-loop guard without scheduling any work."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running_loop is self._get_running_loop():
            raise RuntimeError("VisionService sync API called from event loop thread; use *_async to avoid deadlock.")

    def _start_template_timing(self, operation, source_image, template_images,
                               use_grayscale, match_method, preprocess, execution):
        timing = start_vision_timing(operation, execution=execution)
        if timing is None:
            return None
        timing.metadata.update(
            source_kind="path" if isinstance(source_image, str) else type(source_image).__name__,
            source_shape=list(source_image.shape) if isinstance(source_image, np.ndarray) else None,
            template_count=len(template_images) if isinstance(template_images, (list, tuple)) else None,
            path_template_count=sum(isinstance(image, str) for image in template_images)
            if isinstance(template_images, (list, tuple)) else None,
            prepared_template_count=sum(isinstance(image, PreparedTemplate) for image in template_images)
            if isinstance(template_images, (list, tuple)) else None,
            use_grayscale=use_grayscale,
            match_method=match_method,
            preprocess=preprocess,
        )
        timing.submitted_at = time.perf_counter()
        return timing

    def _run_template_request(self, kernel, args, timing: VisionTiming | None):
        if timing is None:
            return kernel(*args)
        timing.start_work()
        status = "completed"
        try:
            return kernel(*args, _timing=timing)
        except BaseException as exc:
            status = "failed"
            timing.error_type = type(exc).__name__
            raise
        finally:
            timing.finish(status=status)

    def _prepare_match_image(self, image, *, use_grayscale, preprocess,
                             stage, timing: VisionTiming | None):
        started_at = time.perf_counter() if timing is not None else None
        try:
            if isinstance(image, str) and stage in ("template_prepare_sum", "mask_prepare_sum"):
                return self._template_image_cache.get(
                    image, use_grayscale=use_grayscale, preprocess=preprocess,
                    load=lambda: self._normalize_color_space(
                        self.load_image_file(image, cv2.IMREAD_UNCHANGED), input_is_bgr=True,
                    ),
                    prepare=lambda pixels: self._prepare_image(
                        pixels, use_grayscale=use_grayscale, preprocess=preprocess,
                    ),
                )
            return self._prepare_image(image, use_grayscale=use_grayscale, preprocess=preprocess)
        finally:
            if timing is not None:
                timing.record(stage, started_at)

    def _prepare_template_image(self, template_image, mask_image, *, use_grayscale,
                                preprocess, timing: VisionTiming | None):
        if isinstance(template_image, PreparedTemplate):
            return template_image._resolve(
                self._prepare_match_image, use_grayscale=use_grayscale,
                preprocess=preprocess, mask_image=mask_image, timing=timing,
            )
        image = self._prepare_match_image(
            template_image, use_grayscale=use_grayscale, preprocess=preprocess,
            stage="template_prepare_sum", timing=timing,
        )
        mask = None
        if mask_image is not None:
            mask = self._prepare_match_image(
                mask_image, use_grayscale=True, preprocess="none",
                stage="mask_prepare_sum", timing=timing,
            )
        return image, mask

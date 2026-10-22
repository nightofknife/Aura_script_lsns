"""Explicit, caller-owned template snapshots with lazy preparation."""
from __future__ import annotations

import threading

import numpy as np


def _snapshot(image: np.ndarray) -> np.ndarray:
    # A bytes-backed array cannot be made writable again with setflags().
    return np.frombuffer(image.tobytes(order="C"), dtype=image.dtype).reshape(image.shape)


class PreparedTemplate:
    """Own pixels and reuse their prepared form for one preparation profile.

    Preparation happens inside the first matching request, after its source has
    been prepared. Failures are not cached, so request error handling and retry
    behavior stay with VisionService. Discard this object when assets change.
    """

    __slots__ = ("_template_image", "_mask_image", "_use_grayscale", "_preprocess", "_prepared", "_lock")

    def __init__(self, template_image: np.ndarray, mask_image: np.ndarray | None = None,
                 *, use_grayscale: bool = True, preprocess: str = "none"):
        if not isinstance(template_image, np.ndarray):
            raise TypeError("PreparedTemplate requires an ndarray template_image")
        if mask_image is not None and not isinstance(mask_image, np.ndarray):
            raise TypeError("PreparedTemplate requires an ndarray mask_image or None")
        self._template_image = _snapshot(template_image)
        self._mask_image = _snapshot(mask_image) if mask_image is not None else None
        self._use_grayscale = use_grayscale
        self._preprocess = (preprocess or "none").lower()
        self._prepared: tuple[np.ndarray, np.ndarray | None] | None = None
        self._lock = threading.Lock()

    @property
    def template_image(self) -> np.ndarray:
        return self._template_image

    @property
    def mask_image(self) -> np.ndarray | None:
        return self._mask_image

    @property
    def use_grayscale(self) -> bool:
        return self._use_grayscale

    @property
    def preprocess(self) -> str:
        return self._preprocess

    @property
    def is_prepared(self) -> bool:
        return self._prepared is not None

    def _resolve(self, prepare_image, *, use_grayscale, preprocess, mask_image, timing):
        if use_grayscale != self.use_grayscale or (preprocess or "none").lower() != self.preprocess:
            raise ValueError("PreparedTemplate preparation parameters must match the request")
        if mask_image is not None:
            raise ValueError("PreparedTemplate owns its mask; mask_image must be None")
        prepared = self._prepared
        if prepared is not None:
            return prepared
        with self._lock:
            if self._prepared is None:
                image = prepare_image(
                    self.template_image, use_grayscale=use_grayscale, preprocess=preprocess,
                    stage="template_prepare_sum", timing=timing,
                )
                mask = None
                if self.mask_image is not None:
                    mask = prepare_image(
                        self.mask_image, use_grayscale=True, preprocess="none",
                        stage="mask_prepare_sum", timing=timing,
                    )
                # Publish only after both preparations and ownership copies succeed.
                self._prepared = (_snapshot(image), _snapshot(mask) if mask is not None else None)
            return self._prepared

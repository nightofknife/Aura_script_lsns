"""Recognition input policies, independent of detection and orientation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OcrRecognitionOptions:
    width_mode: str = "legacy"
    min_width: int = 160
    batch_mode: str = "adaptive"
    batch_size: int | None = None
    split_min_saving: float = 0.25

    def __post_init__(self):
        if self.width_mode not in {"legacy", "adaptive"}:
            raise ValueError("ocr.recognition.width_mode must be legacy or adaptive.")
        if self.batch_mode not in {"fixed", "adaptive"}:
            raise ValueError("ocr.recognition.batch_mode must be fixed or adaptive.")
        if not isinstance(self.min_width, int) or isinstance(self.min_width, bool) or self.min_width <= 0:
            raise ValueError("ocr.recognition.min_width must be a positive integer.")
        if self.batch_size is not None and (
            not isinstance(self.batch_size, int) or isinstance(self.batch_size, bool) or self.batch_size <= 0
        ):
            raise ValueError("ocr.recognition.batch_size must be a positive integer.")
        if not 0 < self.split_min_saving < 1:
            raise ValueError("ocr.recognition.split_min_saving must be between 0 and 1 exclusively.")

    @classmethod
    def from_config(cls, config: Any) -> OcrRecognitionOptions:
        defaults = cls()
        get = lambda key, default: config.get(f"ocr.recognition.{key}", default)
        batch_size = get("batch_size", defaults.batch_size)
        return cls(
            width_mode=str(get("width_mode", defaults.width_mode)).strip().lower(),
            min_width=int(get("min_width", defaults.min_width)),
            batch_mode=str(get("batch_mode", defaults.batch_mode)).strip().lower(),
            batch_size=int(batch_size) if batch_size is not None else None,
            split_min_saving=float(get("split_min_saving", defaults.split_min_saving)),
        )

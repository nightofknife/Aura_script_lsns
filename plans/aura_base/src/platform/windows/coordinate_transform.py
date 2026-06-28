# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CoordinateTransformConfig:
    mode: str = "client_pixels"
    reference_resolution: tuple[int, int] | None = None


class ReferenceCoordinateTransformer:
    def __init__(
        self,
        *,
        client_size: tuple[int, int],
        config: CoordinateTransformConfig,
    ):
        self.client_width = max(int(client_size[0]), 1)
        self.client_height = max(int(client_size[1]), 1)
        self.config = config

    def point_to_client(self, x: int, y: int) -> tuple[int, int]:
        if self.config.mode == "client_pixels" or self.config.reference_resolution is None:
            return int(x), int(y)
        ref_width, ref_height = self.config.reference_resolution
        scale_x = float(self.client_width) / float(ref_width)
        scale_y = float(self.client_height) / float(ref_height)
        return int(round(float(x) * scale_x)), int(round(float(y) * scale_y))

    def rect_to_client(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = [int(value) for value in rect]
        if self.config.mode == "client_pixels" or self.config.reference_resolution is None:
            return x, y, width, height
        ref_width, ref_height = self.config.reference_resolution
        scale_x = float(self.client_width) / float(ref_width)
        scale_y = float(self.client_height) / float(ref_height)
        return (
            int(round(float(x) * scale_x)),
            int(round(float(y) * scale_y)),
            int(round(float(width) * scale_x)),
            int(round(float(height) * scale_y)),
        )

    def point_to_reference(self, x: int, y: int) -> tuple[int, int]:
        if self.config.mode == "client_pixels" or self.config.reference_resolution is None:
            return int(x), int(y)
        ref_width, ref_height = self.config.reference_resolution
        scale_x = float(ref_width) / float(self.client_width)
        scale_y = float(ref_height) / float(self.client_height)
        return int(round(float(x) * scale_x)), int(round(float(y) * scale_y))

    def rect_to_reference(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = [int(value) for value in rect]
        if self.config.mode == "client_pixels" or self.config.reference_resolution is None:
            return x, y, width, height
        ref_width, ref_height = self.config.reference_resolution
        scale_x = float(ref_width) / float(self.client_width)
        scale_y = float(ref_height) / float(self.client_height)
        return (
            int(round(float(x) * scale_x)),
            int(round(float(y) * scale_y)),
            int(round(float(width) * scale_x)),
            int(round(float(height) * scale_y)),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "reference_resolution": list(self.config.reference_resolution) if self.config.reference_resolution else None,
            "client_size": [self.client_width, self.client_height],
        }

"""File and geometry primitives for the PC recovery catalogs.

Catalogs retain their own schemas and image requirements. In particular, this
module does not infer color channels, masks, matching flags or usable thresholds.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence


def read_layout_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_plan_asset(vision: Any, reference: str, plan_root: Path, *, error: str) -> Path:
    path = Path(vision.resolve_template("resonance_pc", reference, plan_root)).resolve()
    if not path.is_relative_to(plan_root):
        raise ValueError(error)
    return path


def load_plan_image(vision: Any, reference: str, plan_root: Path, *, flags: int,
                    path_error: str) -> tuple[Path, Any]:
    path = resolve_plan_asset(vision, reference, plan_root, error=path_error)
    return path, vision.load_image_file(path, flags)


def validated_roi(raw: Any, reference: Sequence[int], label: str) -> tuple[int, int, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError(f"{label}: ROI must contain x, y, width, height")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        raise ValueError(f"{label}: ROI coordinates must be integers")
    x, y, w, h = raw
    if min(x, y) < 0 or min(w, h) <= 0 or x+w > reference[0] or y+h > reference[1]:
        raise ValueError(f"{label}: ROI is outside reference client")
    return x, y, w, h

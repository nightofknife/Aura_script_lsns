"""Native LOCAL-store observations; no OCR, input, or runtime asset writes."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


PLAN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PLAN_ROOT / "templates/commerce/shop/black_moon/native"


class BlackMoonLocalShopRecognitionError(RuntimeError):
    """A frame or native resource cannot support a safe LOCAL observation."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _entries(value):
    if isinstance(value, dict):
        return [dict(entry, name=name) for name, entry in value.items()]
    return list(value)


@lru_cache(maxsize=4)
def _resources(root: Path, template_names: tuple[str, ...] | None = None, plan_root: Path = PLAN_ROOT):
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["schema_version"] != 2:
            raise ValueError("Native shop resources require schema 2 plan-relative references")
        if manifest["reference_client"] != [1280, 720]:
            raise ValueError("Only RGB 1280x720 is supported")
        entries = _entries(manifest["templates"])
        if template_names is None:
            entries += manifest["cards"]["items"]
        else:
            entries = [entry for entry in entries if entry["name"] in template_names]
            if {entry["name"] for entry in entries} != set(template_names):
                raise ValueError("Preparation control templates are incomplete")
            manifest = {**manifest, "templates": entries}
        files = {entry["file"] for entry in entries}
        files.update(entry["mask_file"] for entry in entries if entry.get("mask_file"))
        pixels = {}
        for filename in files:
            reference = Path(filename)
            path = (plan_root / reference).resolve()
            if reference.is_absolute() or not path.is_relative_to((plan_root / "templates").resolve()):
                raise ValueError(f"Resource outside plan templates: {filename}")
            with Image.open(path) as asset:
                mode = "RGBA" if asset.mode == "RGBA" else "L" if asset.mode == "L" else "RGB"
                array = np.array(asset.convert(mode))
            array.setflags(write=False)
            pixels[filename] = array
        return manifest, pixels
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", str(exc)) from exc


def _image(frame):
    if not isinstance(frame, np.ndarray) or frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        raise BlackMoonLocalShopRecognitionError("invalid_image", "Expected uint8 RGB 1280x720")
    return frame


def _clip(box, bounds=(0, 0, 1280, 720)):
    x, y, w, h = map(int, box)
    bx, by, bw, bh = map(int, bounds)
    left, top = max(x, bx), max(y, by)
    return left, top, max(0, min(x + w, bx + bw) - left), max(0, min(y + h, by + bh) - top)


def _crop(frame, box):
    x, y, w, h = _clip(box)
    return frame[y:y + h, x:x + w]


def _prepare(pixels, mode="rgb"):
    if pixels.ndim == 2:
        return pixels
    pixels = pixels[:, :, :3]
    if mode == "gray":
        return cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    if mode == "bright":
        spread = np.ptp(pixels.astype(np.int16), axis=2)
        return np.where((pixels.min(axis=2) >= 205) & (spread < 38), 255, 0).astype(np.uint8)
    if mode == "dark":
        return np.where(pixels.max(axis=2) < 90, 255, 0).astype(np.uint8)
    if mode == "yellow":
        return cv2.inRange(cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV), (16, 160, 170), (40, 255, 255))
    if mode not in (None, "rgb"):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", f"Unsupported preprocessing {mode}")
    return pixels


def _response(source, template, mask=None, method=None):
    if source.shape[0] < template.shape[0] or source.shape[1] < template.shape[1]:
        return np.empty((0, 0), dtype=np.float32)
    if source.ndim != template.ndim:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Source/template channel mismatch")
    # Squared difference retains color and brightness, unlike correlation alone.
    squared = method not in ("TM_CCOEFF_NORMED", "ccoeff")
    algorithm = cv2.TM_SQDIFF_NORMED if squared else cv2.TM_CCOEFF_NORMED
    if mask is not None and (mask.shape != template.shape[:2] or not np.any(mask > 0)):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Empty or incompatible native mask")
    valid = template[mask > 0] if mask is not None else template.reshape((-1,) + template.shape[2:])
    if not np.any(valid):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Native template has zero matching energy")
    if not squared and np.max(np.std(valid.astype(np.float32), axis=0)) < 1e-5:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Constant native template cannot establish correlation")
    try:
        result = cv2.matchTemplate(np.ascontiguousarray(source), np.ascontiguousarray(template), algorithm, mask=mask)
    except cv2.error as exc:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", str(exc)) from exc
    if squared:
        result = 1. - result
    return np.nan_to_num(result, nan=-1., posinf=-1., neginf=-1.)


class BlackMoonLocalShopVision:
    """Cached native controls used by preparation; client RGB coordinates.

    Resource loading also validates the complete retained native asset bank.
    """

    def __init__(self, template_root: Path | None = None,
                 plan_root: Path | None = None):
        self.template_root = (TEMPLATE_ROOT if template_root is None else Path(template_root)).resolve()
        self.plan_root = (PLAN_ROOT if plan_root is None else Path(plan_root)).resolve()

    @property
    def _data(self):
        return _resources(self.template_root, plan_root=self.plan_root)

    @property
    def _templates(self):
        return {entry["name"]: entry for entry in _entries(self._data[0]["templates"])}

    def _pixels(self, entry):
        arrays = self._data[1]
        raw = arrays[entry["file"]]
        mask = arrays[entry["mask_file"]] if entry.get("mask_file") else None
        if raw.ndim == 3 and raw.shape[2] == 4:
            if mask is None:
                mask = raw[:, :, 3]
            raw = raw[:, :, :3]
        if mask is not None and mask.ndim == 3:
            mask = cv2.cvtColor(mask[:, :, :3], cv2.COLOR_RGB2GRAY)
        return _prepare(raw, entry.get("preprocess", "rgb")), mask

    def _entry_response(self, frame, entry, box):
        template, mask = self._pixels(entry)
        source = _prepare(_crop(frame, box), entry.get("preprocess", "rgb"))
        return _response(source, template, mask, entry.get("method"))

    def _match_entry(self, frame, entry, box):
        box = _clip(box)
        response = self._entry_response(frame, entry, box)
        if not response.size:
            return {"found": False, "score": 0., "center": None, "rect": None}
        _, score, _, point = cv2.minMaxLoc(response)
        raw = self._data[1][entry["file"]]
        rect = (box[0] + point[0], box[1] + point[1], raw.shape[1], raw.shape[0])
        found = score >= entry.get("threshold", .88)
        limit = entry.get("color_mae_limit")
        if found and limit is not None and raw.ndim == 3:
            observed = _crop(frame, rect).astype(np.float32)
            expected = raw[:, :, :3].astype(np.float32)
            _, mask = self._pixels(entry)
            delta = np.abs(observed - expected)
            found = float(delta[mask > 0].mean() if mask is not None else delta.mean()) <= limit
        return {"found": bool(found), "score": float(score), "rect": rect if found else None,
                "center": (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2) if found else None}

    def match(self, frame, name):
        frame = _image(frame)
        entry = self._templates.get(name)
        if entry is None:
            raise BlackMoonLocalShopRecognitionError("unknown_template", name)
        return self._match_entry(frame, entry, entry["roi"])

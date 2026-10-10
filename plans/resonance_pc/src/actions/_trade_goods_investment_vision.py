"""Native-template observations only; no OCR, input, or runtime resource writes."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates/trade_goods_investment"


class InvestmentRecognitionError(RuntimeError):
    """A frame or resource cannot support a confident investment observation."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@lru_cache(maxsize=4)
def _resources(root: Path) -> tuple[dict, dict[str, np.ndarray]]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        files = {entry["file"] for key in ("templates", "levels", "digits", "max_levels", "max_digits")
                 for entry in manifest.get(key, [])}
        files.update(entry["mask_file"] for entry in manifest.get("templates", []) if entry.get("mask_file"))
        cards = manifest["cards"]
        files.update(cards["header_variants"])
        files.add(cards["header_mask"])
        for entry in cards["frame_variants"]:
            files.update((entry["file"], entry["mask"]))
        pixels = {}
        for filename in files:
            path = (root / filename).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Template outside resource root: {filename}")
            with Image.open(path) as asset:
                array = np.array(asset.convert("RGB" if asset.mode == "RGB" else "L"))
            array.setflags(write=False)
            pixels[filename] = array
        if manifest["reference_client"] != [1280, 720]:
            raise ValueError("Only RGB 1280x720 is supported")
        if {entry["level"] for entry in manifest["levels"]} != set(range(21)):
            raise ValueError("Full-token templates must cover levels 0-20")
        style = manifest.get("max_level_style", {})
        digit_banks = {"digits", style.get("digits_key", "digits")}
        if "max_digits" in manifest:
            digit_banks.add("max_digits")
        for key in digit_banks:
            if {entry["digit"] for entry in manifest.get(key, [])} != set(range(10)):
                raise ValueError(f"Digit bank {key} must cover digits 0-9")
        return manifest, pixels
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InvestmentRecognitionError("template_load_failed", str(exc)) from exc


def __getattr__(name: str):
    # Keep the default manifest-derived constant available without import-time IO.
    if name == "VIEWPORT":
        return tuple(_resources(TEMPLATE_ROOT.resolve())[0]["cards"]["viewport"])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _image(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3) or image.dtype != np.uint8:
        raise InvestmentRecognitionError("invalid_image", "Expected uint8 RGB array with shape (720, 1280, 3)")
    return image


def _clip(box, bounds=(0, 0, 1280, 720)) -> tuple[int, int, int, int]:
    x, y, w, h = map(int, box)
    bx, by, bw, bh = bounds
    left, top = max(x, bx), max(y, by)
    return left, top, max(0, min(x + w, bx + bw) - left), max(0, min(y + h, by + bh) - top)


def _crop(image, box) -> np.ndarray:
    x, y, w, h = _clip(box)
    return image[y:y + h, x:x + w]


def _intersects(a, b) -> bool:
    return (max(a[0], b[0]) < min(a[0] + a[2], b[0] + b[2])
            and max(a[1], b[1]) < min(a[1] + a[3], b[1] + b[3]))


def _response(source, template, mask=None, *, squared_difference=False) -> np.ndarray:
    if any(a < b for a, b in zip(source.shape[:2], template.shape[:2])):
        return np.empty((0, 0), dtype=np.float32)
    method = cv2.TM_SQDIFF_NORMED if squared_difference else cv2.TM_CCOEFF_NORMED
    result = cv2.matchTemplate(np.ascontiguousarray(source), template, method, mask=mask)
    if squared_difference:
        result = 1. - result
    return np.nan_to_num(result, nan=-1., posinf=-1., neginf=-1.)


def _best(source, template, mask=None, *, squared_difference=False) -> tuple[float, tuple[int, int]]:
    response = _response(source, template, mask, squared_difference=squared_difference)
    if not response.size:
        return 0., (0, 0)
    _, score, _, point = cv2.minMaxLoc(response)
    return float(score), point


class InvestmentVision:
    """Recognize investment controls, dual-core levels, and viewport card bounds.

    VIEWPORT is loaded from this instance's manifest, never from fixed slots.
    Resource arrays are shared read-only across instances; imports perform no IO.
    """

    CONFIRM_COLOR_MAE_LIMIT = 28.
    CONFIRM_COLOR_MEAN_LIMIT = 24.
    CONFIRM_COLOR_FILL_MIN_SPREAD = 40

    def __init__(self, template_root: Path | None = None):
        self.template_root = (TEMPLATE_ROOT if template_root is None else Path(template_root)).resolve()
        self._manifest, self._pixels = _resources(self.template_root)
        self._templates = {entry["name"]: entry for entry in self._manifest["templates"]}
        self.VIEWPORT = tuple(self._manifest["cards"]["viewport"])

    @property
    def metadata(self) -> dict:
        """Return manifest metadata without exposing the shared cache to mutation."""
        return deepcopy(self._manifest)

    def _prepare(self, image, mode) -> np.ndarray:
        if mode in ("bright", "bright_blur"):
            spec = self._manifest["rasterization"]
            minimum = image.min(axis=2)
            spread = image.max(axis=2).astype(np.int16) - minimum.astype(np.int16)
            image = np.where((minimum >= spec["foreground_min_rgb"])
                             & (spread < spec["foreground_max_chroma_spread"]), 255, 0).astype(np.uint8)
        elif mode == "yellow":
            return cv2.inRange(cv2.cvtColor(image, cv2.COLOR_RGB2HSV), (16, 160, 170), (40, 255, 255))
        elif mode in ("dark", "dark_blur"):
            image = np.where(image.max(axis=2) < 90, 255, 0).astype(np.uint8)
        elif mode not in ("rgb", "rgb_blur", None):
            raise InvestmentRecognitionError("template_load_failed", f"Unsupported preprocessing: {mode}")
        return cv2.GaussianBlur(image, (3, 3), 0) if mode and mode.endswith("_blur") else image

    def _confirm_color_metrics(self, observed, template) -> dict:
        result = {"color_mae": None, "color_mean_difference": None, "color_consistent": False}
        if observed.shape != template.shape or template.ndim != 3:
            return result
        # NCC is insensitive to tint changes; compare only the native colored fill.
        fill = np.ptp(template.astype(np.int16), axis=2) >= self.CONFIRM_COLOR_FILL_MIN_SPREAD
        if not fill.any():
            return result
        expected = template[fill].astype(np.float32)
        actual = observed[fill].astype(np.float32)
        mae = float(np.abs(actual - expected).mean())
        mean_difference = float(np.abs(actual.mean(axis=0) - expected.mean(axis=0)).max())
        return {"color_mae": mae, "color_mean_difference": mean_difference,
                "color_consistent": mae <= self.CONFIRM_COLOR_MAE_LIMIT
                and mean_difference <= self.CONFIRM_COLOR_MEAN_LIMIT}

    def _match_box(self, image, name, box) -> dict:
        if name not in self._templates:
            raise InvestmentRecognitionError("unknown_template", name)
        entry = self._templates[name]
        template = self._pixels[entry["file"]]
        mask = self._pixels[entry["mask_file"]] if entry.get("mask_file") else None
        x, y, w, h = _clip(box)
        color_metrics = ({"color_mae": None, "color_mean_difference": None, "color_consistent": False}
                         if name == "confirm_enabled" else {})
        if w < template.shape[1] or h < template.shape[0]:
            return {"found": False, "score": 0., "center": None, "rect": None, **color_metrics}
        source = self._prepare(image[y:y + h, x:x + w], entry["preprocess"])
        # Flat native button fills and lock glyphs have no reliable NCC variance.
        score, point = _best(source, template, mask, squared_difference=mask is not None)
        found = score >= entry["threshold"]
        if entry.get("color_mae_limit") is not None:
            px, py = point
            observed = source[py:py + template.shape[0], px:px + template.shape[1]]
            selected = mask > 0 if mask is not None else np.ones(template.shape[:2], dtype=bool)
            mae = float(np.abs(observed[selected].astype(float) - template[selected].astype(float)).mean())
            color_metrics["color_mae"] = mae
            color_metrics["color_consistent"] = mae <= entry["color_mae_limit"]
            found = found and color_metrics["color_consistent"]
        if name == "confirm_enabled":
            px, py = point
            observed = source[py:py + template.shape[0], px:px + template.shape[1]]
            color_metrics = self._confirm_color_metrics(observed, template)
            found = found and color_metrics["color_consistent"]
        rect = (x + point[0], y + point[1], template.shape[1], template.shape[0]) if found else None
        return {"found": bool(found), "score": score, "rect": rect,
                "center": (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2) if rect else None,
                **color_metrics}

    def match(self, imageRGBnp: np.ndarray, name: str) -> dict:
        image = _image(imageRGBnp)
        if name not in self._templates:
            raise InvestmentRecognitionError("unknown_template", name)
        return self._match_box(image, name, self._templates[name]["roi"])

    def read_entry_state(self, imageRGBnp: np.ndarray) -> dict:
        """Confirm an open entry or a native restriction overlay before any click."""
        image = _image(imageRGBnp)
        entry = self.match(image, "entry")
        opened = self.match(image, "entry_available")
        restricted = self.match(image, "entry_unavailable")
        lock = self.match(image, "entry_lock")
        condition = self.match(image, "entry_restriction")
        evidence = {"entry": entry, "available": opened, "unavailable": restricted,
                    "lock": lock, "restriction": condition}
        if not entry["found"]:
            raise InvestmentRecognitionError("investment_entry_unknown", "Exchange investment label is unconfirmed")
        if restricted["found"] and not opened["found"] and (lock["found"] or condition["found"]):
            return {"availability": "unavailable", "rect": restricted["rect"], "center": None,
                    "evidence": evidence}
        if opened["found"] and not restricted["found"] and not lock["found"] and not condition["found"]:
            return {"availability": "available", "rect": opened["rect"], "center": entry["center"],
                    "evidence": evidence}
        raise InvestmentRecognitionError("investment_entry_unknown", "Investment entry state is ambiguous")

    def _read_level(self, image, roi, side, preprocess="bright", levels_key="levels", digits_key="digits") -> int:
        levels = self._manifest.get(levels_key, [])
        digits = self._manifest.get(digits_key, [])
        if not levels:
            raise InvestmentRecognitionError("template_load_failed", f"Empty level bank: {levels_key}")
        if {entry["digit"] for entry in digits} != set(range(10)):
            raise InvestmentRecognitionError("template_load_failed", f"Digit bank {digits_key} must cover digits 0-9")
        mask = self._prepare(_crop(image, roi), preprocess)
        points = cv2.findNonZero(mask)
        if points is None:
            raise InvestmentRecognitionError("level_uncertain", f"{side}: no full token foreground")
        x, y, w, h = cv2.boundingRect(points)
        spec = self._manifest["level_matching"]
        cw, ch = spec["canvas"]
        if w > cw - 2 or h > ch - 2:
            raise InvestmentRecognitionError("level_uncertain", f"{side}: token size {w}x{h} exceeds canvas")
        token = np.zeros((ch, cw), dtype=np.uint8)
        ox, oy = (cw - w) // 2, (ch - h) // 2
        token[oy:oy + h, ox:ox + w] = mask[y:y + h, x:x + w]
        scores = {}
        for entry in levels:
            score, _ = _best(np.pad(token, 2), self._pixels[entry["file"]])
            level = entry["level"]
            scores[level] = max(scores.get(level, -1.), score)
        ranked = sorted(scores.items(), key=lambda row: row[1], reverse=True)
        level, score = ranked[0]
        if len(ranked) == 1 and not (side == "maximum" and level == 20):
            raise InvestmentRecognitionError("template_load_failed", "Only the maximum-level bank may contain a single level 20")
        # A native level-20-only bank has no competing token; digits still compete against 0-9.
        margin = score - ranked[1][1] if len(ranked) > 1 else None
        if score < spec["threshold"] or (margin is not None and margin < spec["minimum_margin"]):
            raise InvestmentRecognitionError("level_uncertain", f"{side}: full token score={score:.4f}, margin={margin}")
        columns = np.flatnonzero(mask.any(axis=0))
        runs = np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1)
        if len(runs) not in (4, 5):
            raise InvestmentRecognitionError("level_uncertain", f"{side}: expected Lv. plus 1-2 digits, got {len(runs)} runs")
        values = []
        for run in runs[3:]:
            glyph = mask[:, run[0]:run[-1] + 1]
            gx, gy, gw, gh = cv2.boundingRect(cv2.findNonZero(glyph))
            glyph = cv2.resize(glyph[gy:gy + gh, gx:gx + gw], (14, 26), interpolation=cv2.INTER_AREA)
            observed = np.pad(np.where(glyph >= 128, 255, 0).astype(np.uint8), 3)
            digit_scores = {}
            for entry in digits:
                value, _ = _best(np.pad(observed, 1), self._pixels[entry["file"]])
                digit = entry["digit"]
                digit_scores[digit] = max(digit_scores.get(digit, -1.), value)
            digit_ranked = sorted(digit_scores.items(), key=lambda row: row[1], reverse=True)
            digit, value = digit_ranked[0]
            digit_margin = value - digit_ranked[1][1]
            if value < spec["digit_threshold"] or digit_margin < spec["digit_margin"]:
                raise InvestmentRecognitionError("level_uncertain", f"{side}: digit score={value:.4f}, margin={digit_margin:.4f}")
            values.append(str(digit))
        if int("".join(values)) != level or len(values) != len(str(level)):
            raise InvestmentRecognitionError("level_uncertain", f"{side}: full-token and digit cores disagree")
        return int(level)

    def read_levels(self, image: np.ndarray) -> dict:
        image = _image(image)
        if not self.match(image, "page_anchor")["found"]:
            raise InvestmentRecognitionError("layout_uncertain", "Investment page anchor not found")
        maximum = "max_layout" in self._templates and self.match(image, "max_layout")["found"]
        if maximum:
            if self.match(image, "plus")["found"] or self.match(image, "minus")["found"]:
                raise InvestmentRecognitionError("layout_uncertain", "Maximum and upgrade layouts overlap")
            roi = self._manifest.get("max_level_roi")
            if roi is None:
                raise InvestmentRecognitionError("template_load_failed", "Missing native max_level_roi")
            style = self._manifest.get("max_level_style", {})
            current = self._read_level(image, roi, "maximum", style.get("preprocess", "bright"),
                                       style.get("levels_key", "levels"), style.get("digits_key", "digits"))
            if current != 20:
                raise InvestmentRecognitionError("level_uncertain", f"Maximum layout token is {current}, not 20")
            return {"current": current, "preview": None, "max_level": True}
        rois = self._manifest["level_rois"]
        current = self._read_level(image, rois["current"], "current")
        preview = self._read_level(image, rois["preview"], "preview")
        if current == 20:
            raise InvestmentRecognitionError("layout_uncertain", "Level 20 requires the native maximum-layout anchor")
        if preview < current:
            raise InvestmentRecognitionError("level_uncertain", f"Preview {preview} is below current {current}")
        return {"current": current, "preview": preview, "max_level": False}

    def _headers(self, image) -> list[dict]:
        spec = self._manifest["cards"]
        region = self._prepare(_crop(image, self.VIEWPORT), spec["preprocess"])
        candidates = []
        for filename in spec["header_variants"]:
            response = _response(region, self._pixels[filename], self._pixels[spec["header_mask"]])
            if not response.size:
                continue
            maxima = response == cv2.dilate(response, np.ones((9, 9), dtype=np.uint8))
            ys, xs = np.where(maxima & (response >= spec["header_threshold"]))
            for x, y in zip(xs, ys):
                candidates.append({"x": int(self.VIEWPORT[0] + x - spec["header_box"][0]),
                                   "y": int(self.VIEWPORT[1] + y - spec["header_box"][1]),
                                   "header_score": float(response[y, x])})
        unique = []
        for item in sorted(candidates, key=lambda row: -row["header_score"]):
            if not any(abs(item["x"] - old["x"]) < 15 and abs(item["y"] - old["y"]) < 15 for old in unique):
                unique.append(item)
        return unique

    def _top_partials(self, image, candidates) -> list[dict]:
        # A clipped header has no pixels to match; locate the native lower frame instead.
        spec = self._manifest["cards"]
        vx, vy, vw, vh = self.VIEWPORT
        height = spec["size"][1]
        region = self._prepare(_crop(image, (vx, vy, vw, min(height, vh))), spec["preprocess"])
        found = []
        for entry in spec["frame_variants"]:
            template = self._pixels[entry["file"]]
            mask = self._pixels[entry["mask"]]
            th, tw = template.shape[:2]
            response = _response(region, template[-9:], mask[-9:])
            maxima = response == cv2.dilate(response, np.ones((9, 9), dtype=np.uint8))
            ys, xs = np.where(maxima & (response >= spec["header_threshold"]))
            for px, py in zip(xs, ys):
                x, y = int(vx + px), int(vy + py - th + 9)
                if y >= vy or y + th - vy < 24:
                    continue
                if any(abs(x - old["x"]) < 15 and abs(y - old["y"]) < 15 for old in candidates + found):
                    continue
                offset = vy - y
                visible = region[:th - offset, px:px + tw]
                score, _ = _best(visible, template[offset:], mask[offset:])
                if score >= spec["frame_threshold"]:
                    found.append({"x": x, "y": y, "header_score": 0.,
                                  "frame_score": score, "size": (tw, th)})
        return found

    def detect_cards(self, image: np.ndarray) -> list[dict]:
        image = _image(image)
        spec = self._manifest["cards"]
        vx, vy, vw, vh = self.VIEWPORT
        candidates = self._headers(image)
        candidates.extend(self._top_partials(image, candidates))
        success = self.match(image, "success")["found"]
        occlusion = spec.get("success_occlusion_roi", (0, 327, 1280, 66))
        cards = []
        for item in candidates:
            x, y = item["x"], item["y"]
            w, h = item.get("size", spec["size"])
            if x < vx - 2 or x + w > vx + vw + 2:
                continue
            rect = (x, y, w, h)
            partial = _clip(rect, self.VIEWPORT) != rect
            occluded = bool(success and _intersects(rect, occlusion))
            frame_score = item.get("frame_score")
            if not partial and not occluded:
                search_box = _clip((x - 3, y - 3, w + 6, h + 6), self.VIEWPORT)
                source = self._prepare(_crop(image, search_box), spec["preprocess"])
                rankings = []
                for entry in spec["frame_variants"]:
                    score, point = _best(source, self._pixels[entry["file"]], self._pixels[entry["mask"]])
                    rankings.append((score, point, entry["size"]))
                frame_score, point, size = max(rankings, key=lambda row: row[0])
                if frame_score < spec["frame_threshold"]:
                    raise InvestmentRecognitionError(
                        "card_frame_uncertain",
                        f"Full card header at ({x}, {y}) has unconfirmed frame score={frame_score:.4f}",
                    )
                x, y = search_box[0] + point[0], search_box[1] + point[1]
                w, h = size
                rect = (x, y, w, h)
                partial = _clip(rect, self.VIEWPORT) != rect
                occluded = bool(success and _intersects(rect, occlusion))
            selected_box = _clip((x - 3, y + 3, 28, 32), self.VIEWPORT)
            selected = False
            if not (success and _intersects(selected_box, occlusion)):
                selected = self._match_box(image, "selected_corner", selected_box)["found"]
            locked = None
            maximum = False
            if not partial and not occluded:
                locked = self._match_box(image, "lock", (x + 37, y + 38, 42, 50))["found"]
                badge = spec.get("maximum", {}).get("badge_box")
                if badge is not None:
                    bx, by, bw, bh = badge
                    maximum = self._match_box(image, "maximum_card_badge",
                                              (x + bx - 3, y + by - 3, bw + 6, bh + 6))["found"]
            cards.append({"rect": tuple(map(int, rect)), "partial": bool(partial), "occluded": occluded,
                          "selected": bool(selected), "locked": locked, "maximum": bool(maximum),
                          "header_score": item["header_score"], "frame_score": frame_score})
        return sorted(cards, key=lambda row: (round(row["rect"][1] / 20), row["rect"][0]))

    def fingerprint(self, image: np.ndarray, card: dict[str, Any]) -> np.ndarray:
        """A normalized internal commodity patch, not an item identity or action proof."""
        image = _image(image)
        try:
            rect = tuple(map(int, card["rect"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise InvestmentRecognitionError("invalid_card", "Missing card rectangle") from exc
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
            raise InvestmentRecognitionError("invalid_card", "Invalid card dimensions")
        if card.get("partial") or card.get("occluded") or _clip(rect, self.VIEWPORT) != rect:
            raise InvestmentRecognitionError("fingerprint_unavailable", "Partial or occluded commodity patch")
        nominal = tuple(self._manifest["cards"]["size"])
        patch = cv2.resize(_crop(image, rect), nominal, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)[20:100, 8:-8]
        small = cv2.resize(gray, (32, 28), interpolation=cv2.INTER_AREA).astype(np.float32)
        valid = np.ones(small.shape, dtype=bool)
        # Exclude the lock glyph and blur margin in nominal card coordinates.
        valid[5:25, 8:25] = False
        values = small[valid]
        deviation = float(values.std())
        if deviation < 2.:
            raise InvestmentRecognitionError("fingerprint_unavailable", "Commodity patch has insufficient contrast")
        small = (small - float(values.mean())) / deviation
        small[~valid] = 0.
        return small

    @staticmethod
    def same_fingerprint(a: np.ndarray, b: np.ndarray) -> bool:
        """Conservative scroll-overlap deduplication only, never item identification."""
        if not isinstance(a, np.ndarray) or not isinstance(b, np.ndarray) or a.shape != (28, 32) or b.shape != a.shape:
            return False
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return False
        valid = np.ones(a.shape, dtype=bool)
        valid[5:25, 8:25] = False
        av, bv = a[valid].astype(np.float64), b[valid].astype(np.float64)
        av, bv = av - av.mean(), bv - bv.mean()
        norm = float(np.linalg.norm(av) * np.linalg.norm(bv))
        if norm < 1e-8:
            return False
        correlation = float(np.dot(av, bv) / norm)
        return bool(correlation >= .97 and np.abs(av - bv).mean() <= .20)

"""Cached native affix-window templates; this module never invokes OCR.

Fixed labels and equipment names use the injected template service. Counters use
complete foreground segmentation and an independent bank of ten native glyphs.
No capture or input is performed here: callers supply a single RGB frame.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np


class TemplateError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class AffixTemplateReader:
    def __init__(self, vision, plan_root, catalog):
        self.vision = vision
        self.root = (Path(plan_root) / "templates" / "affix_refresh").resolve()
        self.catalog = catalog
        try:
            self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
            if (self.manifest.get("schema_version", self.manifest.get("schema")) != 1
                    or self.manifest.get("reference_client") != [1280, 720]):
                raise ValueError("Unsupported affix template manifest schema or client dimensions")
            self.tokens = self.manifest["tokens"]
            self.equipment = self.manifest["equipment"]
            self.digits = self.manifest["digits"]
            if not isinstance(self.tokens, dict) or not isinstance(self.digits, dict):
                raise ValueError("Template tokens and digit banks must be objects")
            expected = {str(row["id"]): row["name"] for row in catalog["equipment"]}
            actual = {str(row["id"]): row["name"] for row in self.equipment}
            if actual != expected or len(actual) != len(self.equipment):
                raise ValueError("Equipment templates must cover the complete production catalog")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TemplateError("affix_templates_invalid", str(exc)) from exc
        self._images, self._image_bounds, self._banks = {}, {}, {}
        self._frame = None
        self._matches, self._masks, self._scores = {}, {}, {}

    def begin_frame(self, rgb):
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
                or rgb.shape != (720, 1280, 3)):
            raise TemplateError("affix_template_frame_invalid", "Expected a complete 1280x720 RGB frame")
        # Keep the reference, not just id(rgb), so allocator id reuse cannot
        # cause recognition from an older frame to leak into a new capture.
        self._frame = rgb
        self._matches.clear()
        self._masks.clear()
        self._scores.clear()

    def _ensure_frame(self, rgb):
        if self._frame is not rgb:
            self.begin_frame(rgb)

    @staticmethod
    def _roi(image, roi):
        if (not isinstance(roi, (list, tuple)) or len(roi) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) for v in roi)):
            raise TemplateError("affix_template_roi_invalid", "Template ROI must contain four integers")
        x, y, width, height = roi
        if (x < 0 or y < 0 or width <= 0 or height <= 0
                or x+width > image.shape[1] or y+height > image.shape[0]):
            raise TemplateError("affix_template_roi_invalid", f"Template ROI is outside its frame: {roi}")
        return image[y:y+height, x:x+width]

    @staticmethod
    def _foreground(rgb, mode):
        values = rgb.astype(np.int16)
        low, high = values.min(axis=2), values.max(axis=2)
        if mode == "bright":
            selected = (low >= 180) & ((high-low) <= 65)
        elif mode == "dark":
            selected = (high <= 150) & ((high-low) <= 65)
        elif mode == "yellow":
            r, g, b = values[:, :, 0], values[:, :, 1], values[:, :, 2]
            selected = (r >= 170) & (g >= 110) & (b <= 130) & ((r-b) >= 65)
        else:
            raise TemplateError("affix_template_mode_invalid", f"Unknown foreground mode: {mode}")
        return selected.astype(np.uint8)*255

    def _mask(self, rgb, roi, mode):
        self._ensure_frame(rgb)
        crop = self._roi(rgb, roi)
        cache_key = (tuple(roi), mode)
        if cache_key not in self._masks:
            self._masks[cache_key] = self._foreground(crop, mode)
        return self._masks[cache_key]

    def _image(self, file):
        if not isinstance(file, str) or not file:
            raise TemplateError("affix_template_missing", "Missing template filename")
        if file not in self._images:
            path = (self.root / file).resolve()
            if not path.is_relative_to(self.root):
                raise TemplateError("affix_template_path_invalid", "Template path leaves its asset directory")
            try:
                image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
                if image is None or min(image.shape) < 1:
                    raise ValueError("Template cannot be decoded")
                image = (image >= 128).astype(np.uint8)*255
                if not np.any(image):
                    raise ValueError("Template contains no foreground")
                image.setflags(write=False)
                self._images[file] = image
                self._image_bounds[file] = self._bounds(image)
            except (OSError, ValueError, cv2.error) as exc:
                raise TemplateError("affix_template_missing", f"Invalid template {file}: {exc}") from exc
        return self._images[file]

    @staticmethod
    def _threshold(spec):
        value = spec.get("threshold", .9)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= 1):
            raise TemplateError("affix_template_threshold_invalid", "Invalid template confidence threshold")
        return float(value)

    def _match_spec(self, rgb, spec):
        roi = spec["roi"]
        source = self._mask(rgb, roi, spec["mode"])
        template = self._image(spec["file"])
        if source.shape[0] < template.shape[0] or source.shape[1] < template.shape[1]:
            raise TemplateError("affix_template_roi_invalid", "Template does not fit its search ROI")
        threshold = self._threshold(spec)
        hit = self.vision.find_template(
            source_image=source, template_image=template, threshold=threshold,
            use_grayscale=False, match_method=cv2.TM_CCOEFF_NORMED, preprocess="none")
        if (_get(hit, "debug_info", {}) or {}).get("error"):
            raise TemplateError("affix_template_match_failed", "Template service reported an error")
        try:
            score = float(_get(hit, "confidence", float("nan")))
        except (TypeError, ValueError) as exc:
            raise TemplateError("affix_template_match_failed", "Template service returned an invalid score") from exc
        if not math.isfinite(score):
            raise TemplateError("affix_template_match_failed", "Template service returned a nonfinite score")
        self._scores[(spec["file"], tuple(roi))] = score
        if not _get(hit, "found", False) or score < threshold:
            return None
        point = _get(hit, "center_point")
        rect = _get(hit, "rect")
        height, width = template.shape
        if rect is not None:
            if (not isinstance(rect, (list, tuple, np.ndarray)) or len(rect) != 4
                    or any(not math.isfinite(float(v)) or float(v) != int(v) for v in rect)
                    or list(rect[2:]) != [width, height]):
                raise TemplateError("affix_template_match_failed", "Template service returned an invalid rectangle")
            left, top = int(rect[0]), int(rect[1])
            point = [left+width//2, top+height//2]
        else:
            left = top = None
        if (not isinstance(point, (list, tuple, np.ndarray)) or len(point) != 2
                or any(not math.isfinite(float(v)) for v in point)):
            raise TemplateError("affix_template_match_failed", "Template service returned no usable location")
        if left is None:
            left, top = int(round(float(point[0])))-width//2, int(round(float(point[1])))-height//2
        if left < 0 or top < 0 or left+width > source.shape[1] or top+height > source.shape[0]:
            raise TemplateError("affix_template_match_failed", "Template service location leaves its ROI")
        return {"center": [roi[0]+int(round(float(point[0]))), roi[1]+int(round(float(point[1])))],
                "rect": [roi[0]+left, roi[1]+top, width, height], "score": score}

    def match(self, rgb, key):
        self._ensure_frame(rgb)
        if key not in self.tokens:
            raise TemplateError("affix_template_missing", f"Unknown fixed-label template: {key}")
        if key not in self._matches:
            self._matches[key] = self._match_spec(rgb, self.tokens[key])
        return self._matches[key]

    @staticmethod
    def _bounds(mask):
        ys, xs = np.nonzero(mask)
        if len(xs) == 0:
            return None
        return int(xs.min()), int(ys.min()), int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)

    def equipment_identity(self, rgb):
        self._ensure_frame(rgb)
        if ("equipment",) in self._matches:
            return dict(self._matches[("equipment",)])
        ranked = []
        competitors = []
        frame_bounds = {}
        for row in self.equipment:
            mask = self._mask(rgb, row["roi"], row["mode"])
            region_key = (tuple(row["roi"]), row["mode"])
            if region_key not in frame_bounds:
                frame_bounds[region_key] = self._bounds(mask)
            bounds = frame_bounds[region_key]
            template = self._image(row["file"])
            target = self._image_bounds[row["file"]]
            if bounds is None or target is None:
                continue
            # Entire ROI foreground must have the dimensions of the complete
            # name; a shorter name cannot match a substring of a longer name.
            if abs(bounds[2]-target[2]) > 2 or abs(bounds[3]-target[3]) > 2:
                continue
            hit = self._match_spec(rgb, row)
            competitors.append((self._scores[(row["file"], tuple(row["roi"]))], str(row["id"])))
            if hit is None:
                continue
            rect = hit["rect"]
            actual_left = row["roi"][0]+bounds[0]
            actual_top = row["roi"][1]+bounds[1]
            if abs(actual_left-(rect[0]+target[0])) > 2 or abs(actual_top-(rect[1]+target[1])) > 2:
                continue
            ranked.append((hit["score"], row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            raise TemplateError("affix_equipment_unreadable", "Equipment name is missing or ambiguous")
        next_score = max((score for score, identifier in competitors
                          if identifier != str(ranked[0][1]["id"])), default=-1.)
        if ranked[0][0]-next_score < .04:
            raise TemplateError("affix_equipment_unreadable", "Equipment name does not have a clear template margin")
        level = self.counter(rgb, "level")
        if level is None or not 1 <= level <= 999:
            raise TemplateError("affix_level_unreadable", "Equipment level is unreadable")
        row = ranked[0][1]
        result = {"id": row["id"], "name": row["name"], "level": level}
        self._matches[("equipment",)] = result
        return dict(result)

    @staticmethod
    def _normalize(mask):
        bounds = AffixTemplateReader._bounds(mask)
        if bounds is None:
            return None
        x, y, width, height = bounds
        target_width = max(1, round(width*24/height))
        if target_width > 28:
            return None
        ink = cv2.resize(mask[y:y+height, x:x+width].astype(np.float32)/255,
                         (target_width, 24), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((32, 32), np.float32)
        left = (32-target_width)//2
        canvas[4:28, left:left+target_width] = ink
        return canvas

    def _bank(self, kind):
        if kind not in self._banks:
            spec = self.digits[kind]
            files = spec["files"]
            if set(files) != set("0123456789"):
                raise TemplateError("affix_digit_bank_invalid", "Counter requires all ten native digit templates")
            bank = []
            for character in "0123456789":
                mask = self._image(files[character])
                bounds = self._bounds(mask)
                normalized = self._normalize(mask)
                if bounds is None or normalized is None:
                    raise TemplateError("affix_digit_bank_invalid", f"Invalid digit template: {kind}/{character}")
                bank.append((character, normalized, bounds[2], bounds[3]))
            self._banks[kind] = bank
        return self._banks[kind]

    def counter(self, rgb, kind):
        self._ensure_frame(rgb)
        cache_key = ("counter", kind)
        if cache_key not in self._matches:
            self._matches[cache_key] = self._counter(rgb, kind)
        return self._matches[cache_key]

    def _counter(self, rgb, kind):
        if kind not in self.digits:
            raise TemplateError("affix_digit_bank_invalid", f"Unknown counter kind: {kind}")
        spec = self.digits[kind]
        roi = list(spec["roi"])
        self._roi(rgb, roi)
        if kind in ("level", "film"):
            prefix = self.match(rgb, "level_prefix" if kind == "level" else "film_label")
            if prefix is None:
                return None
            prefix_key = "level_prefix" if kind == "level" else "film_label"
            prefix_file = self.tokens[prefix_key]["file"]
            self._image(prefix_file)
            glyph_x, glyph_y, glyph_width, glyph_height = self._image_bounds[prefix_file]
            right = prefix["rect"][0]+glyph_x+glyph_width
            end = roi[0]+roi[2]
            line_top = prefix["rect"][1]+glyph_y-2
            line_bottom = prefix["rect"][1]+glyph_y+glyph_height+2
            if (not roi[0] <= right < end or line_top < roi[1]
                    or line_bottom > roi[1]+roi[3]):
                return None
            # Restrict dark digits to their native text line. A pill's rounded
            # corners can expose dark background inside the broader balance ROI.
            roi = [right, line_top, end-right, line_bottom-line_top]
        mask = self._mask(rgb, roi, spec["mode"])
        occupied = np.any(mask > 0, axis=0)
        changes = np.diff(np.r_[False, occupied, False].astype(np.int8))
        runs = list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))
        if not int(spec.get("min_digits", 1)) <= len(runs) <= int(spec.get("max_digits", 9)):
            return None
        bank = self._bank(kind)
        minimum_score = self._threshold(spec) if "threshold" in spec else .80
        text, tops, bottoms = [], [], []
        for left, right in runs:
            ys = np.flatnonzero(np.any(mask[:, left:right] > 0, axis=1))
            if len(ys) == 0:
                return None
            top, bottom = int(ys[0]), int(ys[-1])+1
            width, height = int(right-left), bottom-top
            if (left == 0 or right == mask.shape[1] or top == 0 or bottom == mask.shape[0]
                    or width < 2 or height < 6):
                return None
            sample = self._normalize(mask[top:bottom, left:right])
            if sample is None:
                return None
            scores = []
            for character, target, target_width, target_height in bank:
                # Scale/aspect gates prevent the narrow "1" from winning by
                # treating the missing strokes of another glyph as blank area.
                if (abs(height-target_height) > 2
                        or abs(width/height-target_width/target_height) > .16):
                    scores.append((0., character))
                    continue
                score = 0.
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        shifted = np.roll(target, (dy, dx), axis=(0, 1))
                        overlap = float(np.minimum(sample, shifted).sum())
                        score = max(score, 2*overlap/max(float(sample.sum()+shifted.sum()), 1e-6))
                scores.append((score, character))
            scores.sort(reverse=True)
            if scores[0][0] < minimum_score or scores[0][0]-scores[1][0] < .04:
                return None
            text.append(scores[0][1])
            tops.append(top)
            bottoms.append(bottom)
        if not text or (len(text) > 1 and text[0] == "0") or max(tops)-min(tops) > 2 or max(bottoms)-min(bottoms) > 2:
            return None
        return int("".join(text))

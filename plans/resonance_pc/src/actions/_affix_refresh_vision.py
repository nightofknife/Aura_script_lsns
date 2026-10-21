"""Read-only, single-frame recognition for the native affix refresh window.

Only the pending affix uses OCR. Fixed UI and the film balance use bundled
templates; unchanged candidate crops reuse their exact-pixel OCR result. The
equipment and the independently scrollable existing-affix list are not read.
"""
from __future__ import annotations

import math
from collections import OrderedDict
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger

from ._affix_refresh_templates import AffixTemplateReader


_MIN_CONFIDENCE = .92


def _get(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _normal(text: str) -> str:
    # Typographical normalization only: never remove a decimal point or infer
    # characters, words, affix types, signs, or numbers that OCR did not read.
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", "", text).translate(str.maketrans({"，": ",", "。": ".", "：": ":", "？": "?"}))


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


class _Unreadable(ValueError):
    pass


class AffixRefreshVision:
    def __init__(self, app, vision, catalog, plan_root, *, ocr=None):
        self.app, self.vision, self.catalog = app, vision, catalog
        self.plan_root = Path(plan_root)
        self.ocr = ocr if ocr is not None else vision
        self.affixes = {int(row["id"]): row for row in catalog["affixes"]}
        self.templates = AffixTemplateReader(vision, self.plan_root, catalog)
        self._affix_cache: OrderedDict[tuple, dict] = OrderedDict()
        self._frame_ocr_calls = 0
        self._frame_cache_hits = 0
        self._ocr_sequence = 0

    def _text(self, image, roi, *, purpose="affix"):
        x, y, width, height = roi
        crop = cv2.resize(image[y:y+height, x:x+width], None, fx=3, fy=3,
                          interpolation=cv2.INTER_CUBIC)
        self._frame_ocr_calls += 1
        self._ocr_sequence += 1
        sequence = self._ocr_sequence
        try:
            response = self.ocr.recognize_all(source_image=crop)
        except Exception as exc:
            logger.warning("[AffixRefreshOCR] call=%s purpose=%s roi=%s status=service_exception error=%s",
                           sequence, purpose, roi, exc)
            raise
        # Persist the entire OCR response before any safety check can reject
        # it, including low-confidence/empty text and service-error results.
        raw_results = [{key: _get(item, key) for key in ("text", "confidence", "center_point", "rect")}
                       for item in _get(response, "results", ()) or ()]
        logger.info("[AffixRefreshOCR] call=%s purpose=%s roi=%s status=raw success=%s results=%s debug=%s",
                    sequence, purpose, roi, _get(response, "success", True),
                    json.dumps(raw_results, ensure_ascii=False, default=str),
                    json.dumps(_get(response, "debug_info", {}), ensure_ascii=False, default=str))
        if _get(response, "success", True) is False or (_get(response, "debug_info", {}) or {}).get("error"):
            raise _Unreadable("OCR service failed")
        rows = []
        for item in _get(response, "results", ()) or ():
            text = str(_get(item, "text", "") or "").strip()
            if not text:
                continue
            confidence = _get(item, "confidence")
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or confidence < _MIN_CONFIDENCE):
                raise _Unreadable("Text confidence below the affix safety threshold")
            center = _get(item, "center_point")
            rect = _get(item, "rect")
            if center is None and rect is not None and len(rect) == 4:
                center = [rect[0] + rect[2]/2, rect[1] + rect[3]/2]
            if center is None or len(center) != 2 or any(not math.isfinite(float(v)) for v in center):
                raise _Unreadable("OCR text has no usable location")
            point = [int(round(x + center[0]/3)), int(round(y + center[1]/3))]
            if not (x <= point[0] < x+width and y <= point[1] < y+height):
                raise _Unreadable("OCR location outside its region")
            rows.append({"text": text, "normal": _normal(text), "center": point})
        rows.sort(key=lambda row: (round(row["center"][1]/10), row["center"][0]))
        return rows

    def _required_template(self, image, key):
        match = self.templates.match(image, key)
        if match is None:
            raise _Unreadable(f"Missing or ambiguous UI template: {key}")
        return match

    def _read_affix(self, image, roi, allowed, *, purpose="affix"):
        """Only a changed affix crop is submitted to OCR; no stale-text fallback."""
        x, y, width, height = roi
        pixels = np.ascontiguousarray(image[y:y+height, x:x+width])
        key = (tuple(allowed), pixels.shape, hashlib.blake2s(pixels.tobytes(), digest_size=16).digest())
        cached = self._affix_cache.get(key)
        if cached is not None:
            self._affix_cache.move_to_end(key)
            self._frame_cache_hits += 1
            return dict(cached)
        try:
            text = "".join(row["normal"] for row in self._text(image, roi, purpose=purpose))
            result = self._affix(text, allowed)
        except Exception as exc:
            logger.warning("[AffixRefreshOCR] call=%s purpose=%s roi=%s status=rejected error=%s",
                           self._ocr_sequence, purpose, roi, exc)
            raise
        logger.info("[AffixRefreshOCR] call=%s purpose=%s roi=%s status=matched result=%s",
                    self._ocr_sequence, purpose, roi,
                    json.dumps(result, ensure_ascii=False, default=str))
        self._affix_cache[key] = dict(result)
        if len(self._affix_cache) > 256:
            self._affix_cache.popitem(last=False)
        return result

    def _affix(self, text, allowed):
        normalized = _normal(text)
        matches = []
        for affix_id in allowed:
            row = self.affixes[int(affix_id)]
            description = _normal(row["description"])
            if "%s" not in description:
                if normalized == description.replace("%%", "%"):
                    matches.append({"id": int(affix_id), "value": "0", "text": text})
                continue
            if description.count("%s") != 1:
                continue
            before, after = description.split("%s")
            pattern = re.escape(before) + r"([0-9]+(?:\.[0-9]+)?)" + re.escape(after.replace("%%", "%"))
            found = re.fullmatch(pattern, normalized)
            if found is None:
                continue
            try:
                value = Decimal(found[1])
                minimum, maximum = Decimal(row["min_value"]), Decimal(row["max_value"])
                step = Decimal(row["step"])
                if (minimum <= value <= maximum and (minimum == maximum or
                        (step > 0 and (value-minimum) % step == 0))):
                    matches.append({"id": int(affix_id), "value": _number(value), "text": text})
            except (InvalidOperation, ValueError):
                continue
        if len(matches) != 1:
            raise _Unreadable("Affix does not uniquely match a complete native effect and legal value")
        return matches[0]

    def _dialog(self, rgb):
        # Only the native refresh-spend prompt is supported. A replacement
        # confirmation is deliberately unknown and never yields input controls.
        # There is intentionally no OCR fallback for dialogs.
        if self.templates.match(rgb, "refresh_prompt") is None:
            return None
        confirm = self._required_template(rgb, "dialog_confirm")["center"]
        cancel = self._required_template(rgb, "dialog_cancel")["center"]
        return {"valid": True, "scene": "refresh_confirm", "replace_mode": False,
                "equipment": {}, "affixes": [], "candidate": None,
                "film_remaining": None, "replace_remaining": None,
                "selection_index": None, "confirm_enabled": False,
                "controls": {"dialog_confirm": confirm, "dialog_cancel": cancel}}

    def _main(self, rgb):
        # The undimmed blue list header is an occlusion guard only. Its contents
        # and the scrollable list below it do not determine which equipment or
        # existing affix slots are present, and are never read by this task.
        header = rgb[295:337, 225:1055].astype(np.int16)
        blue = ((header[:, :, 2] >= 210) & (header[:, :, 0] <= 135)
                & (header[:, :, 1] >= 80) & (header[:, :, 1] <= 190))
        if float(np.mean(blue)) < .55:
            raise _Unreadable("Affix window is dimmed, covered, or not present")
        self._required_template(rgb, "replacement_header")
        film_label = self.templates.match(rgb, "film_label")
        film_remaining = self.templates.counter(rgb, "film") if film_label is not None else None
        if film_label is not None and film_remaining is None:
            raise _Unreadable("Film digits are not unambiguously matched")
        refresh_match = self.templates.match(rgb, "refresh_label")
        refresh = refresh_match["center"] if refresh_match else None
        # The pending effect line is below its fixed title and excludes both
        # the resource pill and the equipment's fixed effect above this panel.
        no_candidate = self.templates.match(rgb, "no_candidate_label")
        if no_candidate is not None:
            self._required_template(rgb, "no_candidate_hint")
            candidate = None
        else:
            candidate = self._read_affix(rgb, (284, 232, 525, 27), tuple(self.affixes),
                                         purpose="candidate")
        if film_remaining is None:
            pill = rgb[205:250, 870:1030]
            visible_pill = float(np.mean(np.min(pill, axis=2) >= 190)) > .55
            if refresh is not None or candidate is None or visible_pill:
                raise _Unreadable("Film balance is unknown outside a hidden pending-only resource group")
        # Refresh-only observation intentionally supplies no slot, selection,
        # replacement-confirm control, or assertion about lower-list visibility.
        return {"valid": True, "scene": "affix", "replace_mode": True,
                "equipment": {}, "affixes": [], "candidate": candidate,
                "film_remaining": film_remaining, "replace_remaining": None,
                "selection_index": None, "confirm_enabled": False,
                "controls": {"refresh": refresh} if refresh is not None else {}}

    def observe(self) -> dict:
        """Capture once; all OCR regions and color checks use this same frame."""
        self._frame_ocr_calls = 0
        self._frame_cache_hits = 0
        try:
            capture = self.app.capture()
            rgb = _get(capture, "image")
            if (_get(capture, "success", False) is False or _get(capture, "quality_flags")
                    or not isinstance(rgb, np.ndarray)
                    or rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8):
                raise _Unreadable("Affix refresh requires a complete 1280x720 RGB client capture")
            # Capture providers may reuse an ndarray and update its pixels in
            # place. Template/counter caches must be fresh for EVERY capture.
            self.templates.begin_frame(rgb)
            header = rgb[295:337, 225:1055].astype(np.int16)
            undimmed = np.mean((header[:, :, 2] >= 210) & (header[:, :, 0] <= 135)
                               & (header[:, :, 1] >= 80) & (header[:, :, 1] <= 190)) > .55
            if undimmed:
                result = self._main(rgb)
                result["diagnostics"] = {"affix_ocr_calls": self._frame_ocr_calls,
                                         "affix_cache_hits": self._frame_cache_hits}
                return result
            # Modal parsing is isolated from main-window OCR. Its darkened
            # background is never treated as fresh resource evidence.
            result = self._dialog(rgb)
            if result is None:
                raise _Unreadable("Unknown, obstructed, or unsupported affix scene")
            result["diagnostics"] = {"affix_ocr_calls": self._frame_ocr_calls,
                                     "affix_cache_hits": self._frame_cache_hits}
            return result
        except Exception as exc:
            # Recognition is fail-closed. The coordinator reports the reason;
            # this helper performs no retries and no game-input operations.
            return {"valid": False, "scene": "unknown", "replace_mode": False, "error": str(exc),
                    "diagnostics": {"affix_ocr_calls": self._frame_ocr_calls,
                                    "affix_cache_hits": self._frame_cache_hits},
                    "equipment": {}, "affixes": [], "candidate": None,
                    "film_remaining": None, "replace_remaining": None,
                    "selection_index": None, "confirm_enabled": False, "controls": {}}

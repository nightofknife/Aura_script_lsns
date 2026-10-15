"""Small-region observations for the destination-map towing service."""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np


THRESHOLD = 0.90
TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates/trailer_service"
QUOTA_REGION = (790, 674, 80, 28)
PAYMENT_POINTS = {"gold": (715, 469), "mileage": (887, 469)}


class TrailerRecognitionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=1)
def _resources():
    try:
        manifest = json.loads((TEMPLATE_ROOT / "manifest.json").read_text(encoding="utf-8"))
        if manifest["reference_client"] != [1280, 720] or manifest["threshold"] != THRESHOLD:
            raise ValueError("Invalid towing template contract")
        resources = {}
        for entry in manifest["templates"]:
            path = (TEMPLATE_ROOT / entry["file"]).resolve()
            if not path.is_relative_to(TEMPLATE_ROOT.resolve()):
                raise ValueError("Template outside towing resource directory")
            encoded = path.read_bytes()
            if sha256(encoded).hexdigest() != entry["sha256"]:
                raise ValueError(f"Template checksum mismatch: {entry['name']}")
            pixels = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
            width, height = entry["size"]
            if pixels is None or pixels.shape != (height, width, 3):
                raise ValueError(f"Expected opaque RGB towing template: {entry['name']}")
            pixels = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)
            pixels.setflags(write=False)
            resources[entry["name"]] = (entry, pixels)
        if set(resources) != {"trailer_entry", "trailer_prompt", "gold_selected",
                              "mileage_selected", "confirm", "cancel"}:
            raise ValueError("Incomplete towing template set")
        return resources
    except (OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
        raise TrailerRecognitionError("trailer_assets_invalid", str(exc)) from exc


def validate_frame(frame):
    if not isinstance(frame, np.ndarray) or frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        raise TrailerRecognitionError("trailer_frame_invalid", "Expected 1280x720 uint8 RGB client")
    return frame


def parse_quota(text: str) -> dict:
    match = re.fullmatch(r"\s*([0-9]{1,3})\s*/\s*([0-9]{1,3})\s*", text)
    if match is None:
        raise TrailerRecognitionError("trailer_quota_unreadable", "Expected remaining/total towing quota")
    remaining, total = map(int, match.groups())
    if total < 1 or remaining > total:
        raise TrailerRecognitionError("trailer_quota_invalid", "Invalid remaining/total towing quota")
    return {"remaining": remaining, "total": total, "used_today": total - remaining}


class TrailerVision:
    def __init__(self, *, vision):
        self.vision = vision
        self.resources = _resources()

    def match(self, frame, name: str) -> dict:
        entry, pixels = self.resources[name]
        return self._match(frame, pixels, entry["roi"], THRESHOLD, cv2.TM_SQDIFF_NORMED, False)

    def match_existing(self, frame, template: str, region, threshold: float) -> dict:
        # Existing navigation evidence keeps its original grayscale/CCOEFF contract.
        return self._match(frame, str(TEMPLATE_ROOT.parents[1] / template), region,
                           threshold, cv2.TM_CCOEFF_NORMED, True)

    def _match(self, frame, template, region, threshold, method, grayscale):
        validate_frame(frame)
        x, y, width, height = region
        hit = self.vision.find_template(
            source_image=frame[y:y + height, x:x + width], template_image=template,
            threshold=threshold, use_grayscale=grayscale, match_method=method, preprocess="none",
        )
        try:
            score = float(hit.confidence)
            if not math.isfinite(score) or (getattr(hit, "debug_info", None) or {}).get("error"):
                raise ValueError("Invalid towing matching result")
            found = bool(hit.found) and score >= threshold
            center = None
            if found:
                point = hit.center_point
                if (not isinstance(point, (tuple, list)) or len(point) != 2
                        or not all(math.isfinite(float(value)) for value in point)
                        or not (0 <= point[0] < width and 0 <= point[1] < height)):
                    raise ValueError("Invalid towing match coordinates")
                center = [x + int(point[0]), y + int(point[1])]
            return {"found": found, "confidence": score, "center": center}
        except (AttributeError, ValueError, TypeError) as exc:
            raise TrailerRecognitionError("trailer_match_invalid", str(exc)) from exc

    def prompt(self, frame) -> dict | None:
        if not self.match(frame, "trailer_prompt")["found"]:
            return None
        confirm, cancel = self.match(frame, "confirm"), self.match(frame, "cancel")
        if not confirm["found"] or not cancel["found"]:
            return None
        gold = self.match(frame, "gold_selected")["found"]
        mileage = self.match(frame, "mileage_selected")["found"]
        if gold == mileage:
            return None
        return {"payment": "gold" if gold else "mileage", "confirm": confirm, "cancel": cancel}

    def read_quota(self, frame, ocr) -> dict:
        validate_frame(frame)
        x, y, width, height = QUOTA_REGION
        result = ocr.recognize_all(source_image=frame[y:y + height, x:x + width])
        if getattr(result, "success", True) is False:
            raise TrailerRecognitionError("trailer_quota_unreadable", "Quota OCR failed")
        tokens = []
        for item in getattr(result, "results", []) or []:
            text = str(getattr(item, "text", "") or "").strip()
            confidence = float(getattr(item, "confidence", 0.) or 0.)
            if not text or not math.isfinite(confidence) or confidence < .80:
                continue
            point = getattr(item, "center_point", None)
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise TrailerRecognitionError("trailer_quota_unreadable", "Quota OCR token lacks position")
            tokens.append((point[0], text))
        # Accept a whole fraction or horizontal numeric tokens, never guessed OCR substitutions.
        tokens.sort()
        return parse_quota("".join(text for _, text in tokens))

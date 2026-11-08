"""Daily-reward page evidence from one immutable, target-bound frame."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from ....aura_base.src.services.observation_service import Frame, Observation
from ....aura_base.src.services.prepared_template import PreparedTemplate
from ._depart_button_vision import load_depart_profile


PLAN = Path(__file__).resolve().parents[2]
REQUIRED = ("main_entry_with_red", "daily_tab", "claimable", "activity_gold",
            "activity_blue", "reward_title", "reward_exit", "city_main")


class DailyRewardError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = str(code), str(message), dict(detail or {})

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class RewardPage:
    frame: Frame
    page_state: str
    hits: dict[str, Observation]

    def present(self, key):
        return key in self.hits and self.hits[key].state == "present"

    def absent(self, key):
        return key in self.hits and self.hits[key].state == "absent"

    def summary(self):
        return {"page_state": self.page_state, "frame_id": self.frame.id,
                "matches": {key: {"state": hit.state,
                    "confidence": float(getattr(hit.value, "confidence", 0.)),
                    "center": list(hit.client_center) if hit.state == "present" else None,
                    "reason": hit.reason} for key, hit in self.hits.items()}}


class DailyRewardReader:
    def __init__(self, observation):
        self.observation = observation
        self.profiles = {}
        self.templates = {}
        try:
            catalog = json.loads((PLAN / "data/meta/daily_reward_templates_pc.json").read_text(encoding="utf-8"))
            if catalog["schema_version"] != 1 or catalog["client_size"] != [1280, 720]:
                raise ValueError("Unsupported daily-reward catalog")
            for key in REQUIRED:
                profile = dict(catalog["templates"][key])
                if profile.get("diagnostic_only"):
                    raise ValueError(f"Diagnostic template cannot authorize input: {key}")
                roi = profile["roi"]
                if (len(roi) != 4 or any(type(v) is not int for v in roi)
                        or min(roi[:2]) < 0 or min(roi[2:]) <= 0
                        or roi[0] + roi[2] > 1280 or roi[1] + roi[3] > 720
                        or profile["method"] != "TM_CCOEFF_NORMED"
                        or type(profile["use_grayscale"]) is not bool
                        or not 0 < profile["threshold"] <= 1
                        or profile["preprocess"] not in {"none", "blur"}):
                    raise ValueError(f"Invalid matching profile: {key}")
                image = self._image(profile["template"], profile.get("sha256"))
                mask = self._image(profile["mask"], profile.get("mask_sha256"), mask=True) if profile.get("mask") else None
                if (image.shape[1::-1] != tuple(profile["size"])
                        or image.shape[1] > roi[2] or image.shape[0] > roi[3]
                        or (mask is not None and (mask.shape != image.shape[:2] or not np.any(mask)))):
                    raise ValueError(f"Invalid template/mask dimensions: {key}")
                self.profiles[key] = profile
                self.templates[key] = PreparedTemplate(image, mask,
                    use_grayscale=profile["use_grayscale"], preprocess=profile["preprocess"])
            # The existing departure control confirms the main screen even
            # after its daily-reward reminder has disappeared.
            depart = load_depart_profile()
            self.profiles["main_screen"] = {"roi": depart.region, "threshold": depart.threshold,
                "use_grayscale": depart.use_grayscale, "preprocess": depart.preprocess,
                "method": depart.match_method}
            self.templates["main_screen"] = PreparedTemplate(self._image(depart.template),
                use_grayscale=depart.use_grayscale, preprocess=depart.preprocess)
        except Exception as exc:
            raise DailyRewardError("daily_reward_assets_invalid", "每日奖励素材或匹配配置不可用。",
                                   {"cause": str(exc)}) from exc

    def _image(self, reference, expected_hash=None, *, mask=False):
        path = (PLAN / reference).resolve()
        if not path.is_relative_to(PLAN / "templates") or not path.is_file():
            raise ValueError(f"Template must exist inside plan templates: {reference}")
        data = path.read_bytes()
        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError(f"Template fingerprint differs from its catalog: {reference}")
        pixels = self.observation.vision.load_image_file(str(path), cv2.IMREAD_UNCHANGED)
        if mask:
            if pixels.ndim != 2 or pixels.dtype != np.uint8:
                raise ValueError(f"Expected an 8-bit mask: {reference}")
            return pixels
        if pixels.ndim != 3 or pixels.shape[2] not in (3, 4) or pixels.dtype != np.uint8:
            raise ValueError(f"Expected RGB/RGBA template: {reference}")
        return cv2.cvtColor(pixels, cv2.COLOR_BGRA2RGB if pixels.shape[2] == 4 else cv2.COLOR_BGR2RGB)

    def observe(self, scope):
        frame = self.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
        if frame.image.shape != (720, 1280, 3):
            raise DailyRewardError("daily_reward_frame_invalid", "每日奖励需要 1280×720 RGB 客户区截图。")
        hits = {}

        def match(key):
            if key not in hits:
                profile = self.profiles[key]
                method = profile["method"]
                hits[key] = self.observation.match_template(frame, self.templates[key], scope=scope,
                    roi=tuple(profile["roi"]), threshold=profile["threshold"],
                    use_grayscale=profile["use_grayscale"], preprocess=profile["preprocess"],
                    match_method=getattr(cv2, method) if isinstance(method, str) else method)
                if hits[key].state == "unknown":
                    raise DailyRewardError("daily_reward_recognition_failed", "每日奖励识别发生技术错误，已停止。",
                        {"control": key, "reason": hits[key].reason})
            return hits[key].state == "present"

        # Never infer task state through an upper reward overlay. A lone reward
        # anchor is ambiguous and cannot authorize either input or absence.
        title, exit_hint = match("reward_title"), match("reward_exit")
        if title and exit_hint:
            return RewardPage(frame, "reward", hits)
        if title or exit_hint:
            return RewardPage(frame, "unknown", hits)
        if match("daily_tab"):
            for key in ("claimable", "activity_gold", "activity_blue", "city_main"):
                match(key)
            return RewardPage(frame, "daily", hits)
        if match("main_screen"):
            match("main_entry_with_red")
            return RewardPage(frame, "main", hits)
        return RewardPage(frame, "unknown", hits)

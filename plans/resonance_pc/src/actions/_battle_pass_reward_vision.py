"""Battle-pass controls from a single frame; no locked/incomplete heuristics."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import cv2
import numpy as np

from ....aura_base.src.services.prepared_template import PreparedTemplate
from ._daily_reward_vision import DailyRewardError, DailyRewardReader, PLAN, RewardPage
from ._depart_button_vision import load_depart_profile


REQUIRED = ("main_entry_with_red", "manual_on", "manual_off", "tasks_on", "tasks_off",
            "tasks_claim_all", "manual_claim_all_red", "reward_title", "reward_exit", "city_main")


class BattlePassRewardError(DailyRewardError):
    pass


@dataclass
class BattlePassPage(RewardPage):
    entries: tuple = ()
    selected_key: str | None = None
    selected_title: str | None = None

    def summary(self):
        result = super().summary()
        result.update(selected_key=self.selected_key, selected_title=self.selected_title,
                      entries=[entry.summary() for entry in self.entries])
        return result


class BattlePassRewardReader(DailyRewardReader):
    def __init__(self, observation):
        self.observation = observation
        self.profiles, self.templates = {}, {}
        try:
            catalog = json.loads((PLAN / "data/meta/battle_pass_templates_pc.json").read_text(encoding="utf-8"))
            if catalog["schema_version"] != 1 or catalog["client_size"] != [1280, 720]:
                raise ValueError("Unsupported battle-pass catalog")
            if set(catalog["templates"]) != set(REQUIRED):
                raise ValueError("Battle-pass catalog must contain only the approved flow controls")
            for key in REQUIRED:
                profile = dict(catalog["templates"][key])
                roi = profile["roi"]
                if (profile.get("diagnostic_only") or len(roi) != 4
                        or any(type(v) is not int for v in roi)
                        or min(roi[:2]) < 0 or min(roi[2:]) <= 0
                        or roi[0]+roi[2] > 1280 or roi[1]+roi[3] > 720
                        or profile["method"] != "TM_CCOEFF_NORMED"
                        or type(profile["use_grayscale"]) is not bool
                        or not 0 < profile["threshold"] <= 1
                        or profile["preprocess"] not in {"none", "blur"}):
                    raise ValueError(f"Invalid battle-pass matching profile: {key}")
                image = self._image(profile["template"], profile.get("sha256"))
                mask = self._mask(profile)
                if (image.shape[1::-1] != tuple(profile["size"])
                        or image.shape[1] > roi[2] or image.shape[0] > roi[3]
                        or (mask is not None and (mask.shape != image.shape[:2] or not np.any(mask)))):
                    raise ValueError(f"Invalid battle-pass image/mask dimensions: {key}")
                self.profiles[key] = profile
                self.templates[key] = PreparedTemplate(image, mask,
                    use_grayscale=profile["use_grayscale"], preprocess=profile["preprocess"])
            depart = load_depart_profile()
            self.profiles["main_screen"] = {"roi": depart.region, "threshold": depart.threshold,
                "use_grayscale": depart.use_grayscale, "preprocess": depart.preprocess, "method": depart.match_method}
            self.templates["main_screen"] = PreparedTemplate(self._image(depart.template),
                use_grayscale=depart.use_grayscale, preprocess=depart.preprocess)
        except Exception as exc:
            raise BattlePassRewardError("battle_pass_reward_assets_invalid", "通行证奖励素材或匹配配置不可用。",
                                        {"cause": str(exc)}) from exc

    def _mask(self, profile):
        reference = profile.get("mask")
        if not reference:
            return None
        encoding = profile.get("mask_encoding", "uint8_binary_png")
        if encoding == "uint8_binary_png":
            return self._image(reference, profile.get("mask_sha256"), mask=True)
        if encoding != "float32_npy":
            raise ValueError(f"Unsupported mask encoding: {encoding}")
        path = (PLAN / reference).resolve()
        if not path.is_relative_to(PLAN / "templates") or not path.is_file():
            raise ValueError("Weighted mask must exist inside plan templates")
        if hashlib.sha256(path.read_bytes()).hexdigest() != profile["mask_sha256"]:
            raise ValueError("Weighted mask fingerprint differs from catalog")
        mask = np.load(path, allow_pickle=False)
        if (mask.dtype != np.float32 or mask.ndim != 2 or not np.all(np.isfinite(mask))
                or np.min(mask) < 0 or not np.any(mask > 0)):
            raise ValueError("Weighted mask must contain finite nonnegative float32 weights")
        return mask

    def observe(self, scope):
        frame = self.observation.capture(scope=scope, roi=(0, 0, 1280, 720))
        if frame.image.shape != (720, 1280, 3):
            raise BattlePassRewardError("battle_pass_reward_frame_invalid", "通行证奖励需要 1280×720 RGB 客户区截图。")
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
                    raise BattlePassRewardError("battle_pass_reward_recognition_failed", "通行证奖励识别发生技术错误，已停止。",
                                                {"control": key, "reason": hits[key].reason})
            return hits[key].state == "present"

        title, exit_hint = match("reward_title"), match("reward_exit")
        if title and exit_hint:
            return BattlePassPage(frame, "reward", hits)
        if title or exit_hint:
            return BattlePassPage(frame, "unknown", hits)
        tasks_on, manual_on = match("tasks_on"), match("manual_on")
        tasks_off, manual_off = match("tasks_off"), match("manual_off")
        tasks = tasks_on and manual_off and not manual_on and not tasks_off
        manual = manual_on and tasks_off and not tasks_on and not manual_off
        if (tasks or manual) and match("city_main"):
            from ._battle_pass_entries import read_entries
            entries, selected_key, selected_title = read_entries(self.observation, frame, scope)
            page = BattlePassPage(frame, "tasks" if tasks else "manual", hits,
                                  entries, selected_key, selected_title)
            if not entries or not selected_key:
                page.page_state = "unknown"
                return page
            for entry in entries:
                hits[entry.control] = entry.observation(frame.id)
            if tasks:
                match("tasks_claim_all")
            else:
                match("manual_claim_all_red")
            return page
        if match("main_screen"):
            match("main_entry_with_red")
            return BattlePassPage(frame, "main", hits)
        return BattlePassPage(frame, "unknown", hits)

"""Read dynamic pass names and confirm selection using the same native title.

Both the list's Txt_Name and the selected pass's Txt_Title2 bind topicName.
No seasonal names, fixed pass count, reminder dots or reward locks are used.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ....aura_base.src.services.observation_service import Observation, normalize_match_text
from ._battle_pass_reward_vision import BattlePassRewardError


# Native ScrollGrid_Time: 300x640 at 2/3 scale, vertical/clamped, row 300x113.
LIST_ROI = (12, 75, 200, 427)
TITLE_ROI = (45, 485, 265, 75)


@dataclass(frozen=True)
class PassEntry:
    key: str
    title: str
    center: tuple[int, int]

    @property
    def control(self):
        return f"pass_entry:{self.key}"

    def observation(self, frame_id):
        return Observation("present", frame_id=frame_id, semantic_key=self.control, client_center=self.center)

    def summary(self):
        return {"title": self.title, "key": self.key, "center": list(self.center)}


def _recognize(observation, frame, roi, scope):
    scope.checkpoint()
    try:
        result = observation.ocr.recognize_all(frame.crop(roi).rgb_image(), use_textline_orientation=False)
    except Exception as exc:
        scope.checkpoint()
        raise BattlePassRewardError("battle_pass_reward_entry_ocr_failed", "读取通行证名称失败，已停止。",
                                    {"cause": str(exc)}) from exc
    scope.checkpoint()
    return result.results


def read_entries(observation, frame, scope):
    rows, uncertain = [], False
    for item in _recognize(observation, frame, LIST_ROI, scope):
        if item.rect is None or item.center_point is None:
            continue
        x, y, w, h = item.rect
        x, y = x+LIST_ROI[0], y+LIST_ROI[1]
        text = str(item.text or "").strip()
        key = normalize_match_text(text)
        # Txt_Name is left aligned at client x~31. The fixed label under the
        # list starts around x135 and must never become another pass entry.
        if not key or not 20 <= x < 195 or x+w > 212 or w <= 0 or not 6 <= h <= 32:
            continue
        if y <= LIST_ROI[1]+3 or y+h >= LIST_ROI[1]+LIST_ROI[3]-3:
            # Partial edge rows are revisited after the next overlapping drag.
            continue
        confidence = float(item.confidence)
        rows.append({"text": text, "key": key, "x": x, "cy": y+h/2,
                     "usable": math.isfinite(confidence) and confidence >= .75})
    # Join fragments of a single native text line, retaining layout rather than
    # assigning an arbitrary pass name to every OCR box.
    groups = []
    for row in sorted(rows, key=lambda value: (value["cy"], value["x"])):
        if groups and abs(groups[-1][0]["cy"]-row["cy"]) <= 8:
            groups[-1].append(row)
        else:
            groups.append([row])
    entries = []
    for group in groups:
        if not 20 <= min(row["x"] for row in group) <= 75:
            continue
        if not all(row["usable"] for row in group):
            uncertain = True
            continue
        title = "".join(row["text"] for row in sorted(group, key=lambda row: row["x"]))
        key = normalize_match_text(title)
        cy = round(sum(row["cy"] for row in group)/len(group))
        if len(key) < 2:
            uncertain = True
            continue
        entries.append(PassEntry(key, title, (110, cy)))
    if len({entry.key for entry in entries}) != len(entries):
        raise BattlePassRewardError("battle_pass_reward_entry_ambiguous", "通行证列表中出现同名或识别歧义，已停止。")
    # The title can wrap; concatenate lines in visual reading order. This ROI
    # excludes both the fixed 环游手册 label and the upgrade button below it.
    selected = []
    for item in _recognize(observation, frame, TITLE_ROI, scope):
        if item.center_point is None or not normalize_match_text(item.text):
            continue
        if not math.isfinite(float(item.confidence)) or float(item.confidence) < .75:
            uncertain = True
            continue
        selected.append(item)
    selected.sort(key=lambda item: (round(item.center_point[1]/12), item.center_point[0]))
    selected_title = "".join(str(item.text).strip() for item in selected)
    selected_key = normalize_match_text(selected_title)
    if uncertain or not entries or len(selected_key) < 2:
        return (), None, None
    return tuple(entries), selected_key, selected_title

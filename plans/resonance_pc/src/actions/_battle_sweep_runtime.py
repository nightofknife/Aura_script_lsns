"""One native sweep, with positive availability and reward evidence.

Only an explicitly recognized unavailable layout permits combat fallback. A
submitted sweep is never retried: an uncertain result is a technical failure.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import time

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.resonance_shared.battle_control import stop_requested
from ._battle_ui import BattleUi, compact, find_text
from ._battle_combat_runtime import _capture_number


PLAN = Path(__file__).resolve().parents[2]


class BattleSweepError(RuntimeError):
    def __init__(self, code, **detail):
        self.code, self.detail = code, detail
        super().__init__(code)

    def __str__(self):
        # run() adds the final submission/reward evidence before the framework
        # serializes this error. Keep that evidence in the propagated message.
        return f"{self.code}: {json.dumps(self.detail, ensure_ascii=False, default=str)}"


class BattleSweepRuntime:
    def __init__(self, ui, *, capture_count=None, formation_index=None, control_cid=None):
        if capture_count is not None and (isinstance(capture_count, bool)
                or not isinstance(capture_count, int) or capture_count < 1):
            raise ValueError("capture_count must be a positive integer")
        if formation_index is not None and (type(formation_index) is not int or not 1 <= formation_index <= 4):
            raise ValueError("formation_index must be an integer in [1,4]")
        self.ui, self.capture_count = ui, capture_count
        self.formation_index, self.control_cid = formation_index, control_cid
        self.catalog = json.loads((PLAN / "data/meta/battle_sweep_pc.json").read_text(encoding="utf-8"))
        structural = json.loads((PLAN / "data/meta/structural_exploration_pc.json").read_text(encoding="utf-8"))
        self.energy_pages = structural["native_auto"]["energy_pages"]
        self.templates = {}
        self.budget = ui.operation().child(75.)
        self.original, self.original_stage = None, None
        self.submitted = False
        self.reward_evidence = []
        self.capture = {"requested": capture_count, "selected": 0, "available": False}
        self.formation = {"requested": formation_index, "selected": None}
        self.last_page = "unknown"

    def _stop_requested(self):
        return bool(self.control_cid and stop_requested(self.control_cid))

    async def _read(self):
        self.budget.checkpoint()
        with self.budget.activate():
            return await self.ui.read()

    async def _click(self, frame, point):
        self.budget.checkpoint()
        with self.budget.activate():
            await self.ui.click(frame, point)

    async def _pause(self, duration=.2):
        await self.budget.sleep_async(duration)

    @staticmethod
    def _text(items, record, *, exact=False):
        # An OCR line's center remains meaningful even when native UI Text uses
        # horizontal overflow outside its nominal RectTransform width.
        roi = record.get("roi")
        if roi is not None and (roi[2] <= 0 or roi[3] <= 0):
            roi = None
        return find_text(items, record["text"], exact=exact, region=roi)

    def _button_score(self, frame, record):
        relative = record["template"]
        if relative not in self.templates:
            raw = np.frombuffer((PLAN / relative).read_bytes(), dtype=np.uint8)
            native = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
            if native is None or native.ndim != 3 or native.shape[2] != 4:
                raise BattleSweepError("sweep_native_template_unavailable", template=relative)
            self.templates[relative] = native
        native = self.templates[relative]
        x, y, width, height = map(int, record["rect"])
        actual = frame.rgb_image()[y:y+height, x:x+width]
        if actual.shape[:2] != native.shape[:2]:
            raise BattleSweepError("sweep_native_template_size_mismatch", template=relative)
        mask = native[:, :, 3] >= 240
        if np.count_nonzero(mask) < 100:
            raise BattleSweepError("sweep_native_template_mask_unusable", template=relative)
        expected = cv2.cvtColor(native, cv2.COLOR_BGRA2RGB)
        error = np.abs(actual.astype(np.float32)-expected.astype(np.float32))
        return 1.-float(error[mask].mean())/255.

    def _energy(self, items):
        for key, page in self.energy_pages.items():
            if find_text(items, page["identity_text"], region=page["identity_roi"]):
                return key
            if (key == "buy_tips" and find_text(items, "澄明度不足")
                    and find_text(items, "补充澄明度")):
                return key
        return None

    def _skip_page(self, items):
        page = self.catalog["skip"]
        return (self._text(items, page["title"])
                and self._text(items, page["confirm_label"])
                and self._text(items, page["cancel_label"]))

    def _arrest_page(self, items):
        arrest = self.catalog["arrest"]
        return (find_text(items, arrest["minimum_text"], exact=True, region=arrest["minimum_region"])
                and find_text(items, arrest["plus_text"], exact=True, region=arrest["plus_region"])
                and find_text(items, ("确定", "确认"), exact=True, region=(600, 450, 680, 200))
                and find_text(items, "取消", exact=True))

    def _reward_page(self, items):
        reward = self.catalog["reward"]
        if not find_text(items, reward["identity_text"], exact=True):
            return None
        x, y, width, height = reward["viewport"]["rect"]
        amounts = []
        for row in items:
            cx, cy = row["center"]
            if not (x <= cx < x+width and y <= cy < y+height) or row.get("confidence", 0) < .7:
                continue
            number = compact(row["text"]).replace(",", "").replace("，", "")
            match = re.fullmatch(r"[x×*]?(\d+(?:\.\d+)?)([万亿]?)", number)
            if match and float(match.group(1)) > 0:
                amounts.append({"text": row["text"], "center": list(row["center"]),
                                "confidence": row.get("confidence", 0)})
        return amounts or None

    def _entry(self, frame, items):
        if self._skip_page(items) or self._arrest_page(items) or self._energy(items):
            return None
        if find_text(items, self.catalog["reward"]["identity_text"], exact=True):
            return None
        for profile in self.catalog["gp_profiles"]:
            if not all(self._text(items, anchor) for anchor in profile["anchors"]):
                continue
            sweep = self._text(items, profile["sweep_label"], exact=True)
            group_start = self._text(items, profile["group_start_label"], exact=True)
            if sweep and group_start and self._button_score(frame, profile["sweep"]) >= .94:
                return "available", profile
            # Root-wide start and the two-button sweep group are mutually
            # exclusive in SelectDifficulty. Both label position and native
            # wide-button appearance positively identify first-clear mode.
            manual = self._text(items, profile["manual_only_label"], exact=True)
            if (manual and not sweep and not group_start
                    and self._button_score(frame, profile["manual_only"]) >= .94):
                return "unsupported", profile
        # FastBattle's three original controllers expose squad entry only.
        # Do not reinterpret any newly observed sweep button as unavailable.
        if not find_text(items, ("扫荡", "开始扫荡")):
            for profile in self.catalog["unsupported_profiles"]:
                if (self._text(items, profile["manual_only_label"], exact=True)
                        and all(self._text(items, anchor) for anchor in profile["anchors"])):
                    return "unsupported", profile
        return None

    def _stage(self, items, profile):
        if "stage_name" not in profile:
            return profile["name"]
        x, y, width, height = profile["stage_name"]["roi"]
        names = sorted((row for row in items if x <= row["center"][0] < x+width
                        and y <= row["center"][1] < y+height), key=lambda row: row["center"][0])
        return "|".join(compact(row["text"]) for row in names) or None

    async def _stable_entry(self, timeout=10., *, original=False):
        deadline, previous, stable = time.monotonic()+timeout, None, 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            entry = self._entry(frame, items)
            state = None
            if entry:
                kind, profile = entry
                stage = self._stage(items, profile)
                if stage and (not original or (stage == self.original_stage
                        and profile["name"] == self.original["name"])):
                    state = (kind, profile["name"], stage)
            stable = stable+1 if state and state == previous else 1 if state else 0
            previous = state
            if stable >= 2:
                return frame, items, entry, state[2]
            await self._pause()
        raise BattleSweepError("sweep_detail_page_unconfirmed", original_stage=self.original_stage,
                               last_page=self.last_page, submitted=self.submitted)

    async def _wait_skip(self):
        deadline, stable = time.monotonic()+12., 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            if self._skip_page(items):
                stable += 1
                if stable >= 2:
                    return frame, items
            else:
                stable = 0
            await self._pause()
        raise BattleSweepError("sweep_team_confirmation_unconfirmed")

    async def _close_energy(self, key, frame):
        await self._click(frame, self.energy_pages[key]["close"]["point"])
        deadline, stable = time.monotonic()+12., 0
        closed, previous_energy, energy_stable = {key}, None, 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            energy = self._energy(items)
            if energy:
                stable = 0
                energy_stable = energy_stable+1 if energy == previous_energy else 1
                previous_energy = energy
                # Cancel each positively identified layer at most once. A
                # lingering old frame never authorizes another back click.
                if energy not in closed and energy_stable >= 2:
                    await self._click(frame, self.energy_pages[energy]["close"]["point"])
                    closed.add(energy)
            elif self._skip_page(items):
                stable += 1
                if stable >= 2:
                    await self._click(frame, self.catalog["skip"]["cancel"]["point"])
                    break
            elif self._entry(frame, items):
                break
            await self._pause()
        await self._stable_entry(original=True)

    async def _cancel_skip(self, frame, items):
        if not self._skip_page(items):
            raise BattleSweepError("sweep_stop_page_unconfirmed")
        await self._click(frame, self.catalog["skip"]["cancel"]["point"])
        await self._stable_entry(original=True)
        return self._result("cancelled", "cancelled")

    async def _prepare_formation(self, frame, items):
        if self.formation_index is None:
            return frame, items
        page = self.catalog["skip"]
        target = next(tab for tab in page["formation_tabs"] if tab["index"] == self.formation_index)
        deadline, clicked, stable = time.monotonic()+10., False, 0
        while time.monotonic() < deadline:
            if not self._skip_page(items):
                raise BattleSweepError("sweep_formation_page_changed")
            if self._stop_requested():
                return frame, items
            current = find_text(items, (str(self.formation_index), f"{self.formation_index:02d}"),
                                exact=True, region=page["formation_current"]["roi"])
            stable = stable+1 if current else 0
            if stable >= 2:
                self.formation["selected"] = self.formation_index
                return frame, items
            if not current and not clicked:
                ordinal = find_text(items, (str(self.formation_index), f"{self.formation_index:02d}"),
                                    exact=True, region=target["index_roi"])
                if ordinal:
                    await self._click(frame, target["point"])
                    clicked = True
            await self._pause()
            frame, items = await self._read()
        raise BattleSweepError("sweep_formation_unconfirmed", requested=self.formation_index,
                               selection_clicked=clicked)

    async def _capture(self, frame, items):
        arrest = self.catalog["arrest"]
        self.capture["available"] = True
        if self.capture_count is None:
            await self._click(frame, arrest["cancel"]["point"])
            self.capture["reason"] = "not_requested"
            return
        minimum = find_text(items, arrest["minimum_text"], exact=True, region=arrest["minimum_region"])
        await self._click(frame, minimum["center"])
        deadline, last_count, adjustments, unchanged = time.monotonic()+18., None, 0, 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            if not self._arrest_page(items):
                raise BattleSweepError("sweep_capture_dialog_changed")
            number = _capture_number(items, arrest["count_region"])
            if number is None or number < 1:
                await self._pause()
                continue
            if last_count is None and number != 1:
                await self._pause()
                continue
            if last_count is not None and number not in (last_count, last_count+1):
                raise BattleSweepError("sweep_capture_count_changed", before=last_count, after=number)
            unchanged = unchanged+1 if last_count == number and adjustments else 0
            if number >= self.capture_count or unchanged >= 2:
                confirm = find_text(items, ("确定", "确认"), exact=True, region=(600, 450, 680, 200))
                await self._click(frame, confirm["center"])
                self.capture["selected"] = number
                return
            if unchanged == 1:
                await self._pause()
                continue
            plus = find_text(items, arrest["plus_text"], exact=True, region=arrest["plus_region"])
            last_count, adjustments = number, adjustments+1
            if adjustments > self.capture_count:
                raise BattleSweepError("sweep_capture_count_limit")
            await self._click(frame, plus["center"])
            await self._pause()
        raise BattleSweepError("sweep_capture_count_unconfirmed")

    def _result(self, outcome, branch):
        return {"success": True, "outcome": outcome, "branch": branch,
                "battle_started": False, "swept": 1 if outcome == "swept" else 0,
                "page_after": self.original["page_after"], "ready_for_next": True,
                "reward_confirmed": bool(self.reward_evidence),
                "reward_evidence": deepcopy(self.reward_evidence), "capture": deepcopy(self.capture),
                "formation": deepcopy(self.formation)}

    async def _run(self):
        frame, items, (kind, profile), stage = await self._stable_entry()
        self.original, self.original_stage = profile, stage
        if self._stop_requested():
            return self._result("cancelled", "cancelled")
        if kind == "unsupported":
            return self._result("unsupported", "sweep_unavailable")
        await self._click(frame, profile["sweep"]["point"])
        frame, items = await self._wait_skip()
        if self._stop_requested():
            return await self._cancel_skip(frame, items)
        frame, items = await self._prepare_formation(frame, items)
        if self._stop_requested():
            return await self._cancel_skip(frame, items)
        # Native StartSkipWin performs the resource check here. Submit once and
        # never convert an uncertain response into normal combat fallback.
        self.submitted = True
        await self._click(frame, self.catalog["skip"]["confirm"]["point"])
        deadline, previous, stable = time.monotonic()+35., None, 0
        capture_handled, arrest_result_closed = False, False
        while time.monotonic() < deadline:
            frame, items = await self._read()
            energy = self._energy(items)
            if energy:
                self.last_page = "energy:"+energy
                await self._close_energy(energy, frame)
                return self._result("insufficient_stamina", "sweep_insufficient_stamina")
            if self._arrest_page(items):
                if capture_handled:
                    raise BattleSweepError("sweep_capture_returned_unexpectedly")
                self.last_page = "arrest"
                await self._capture(frame, items)
                capture_handled = True
                previous, stable = None, 0
                await self._pause(.35)
                continue
            arrest_success = next((row for row in items
                                   if re.search(r"成功逮捕\d+名囚犯", compact(row["text"]))), None)
            if arrest_success and self.capture["selected"] and not arrest_result_closed:
                self.last_page = "skip_arrest_settlement"
                await self._click(frame, self.catalog["arrest"]["result_close"]["point"])
                arrest_result_closed = True
                await self._pause(.35)
                continue
            rewards = self._reward_page(items)
            if rewards:
                self.last_page = "sweep_rewards"
                signature = tuple(sorted((compact(row["text"]), tuple(row["center"])) for row in rewards))
                stable = stable+1 if signature == previous else 1
                previous = signature
                if stable >= 2:
                    self.reward_evidence = rewards
                    await self._click(frame, self.catalog["reward"]["close"]["point"])
                    await self._stable_entry(original=True)
                    return self._result("swept", "sweep")
            else:
                previous, stable = None, 0
                if self._skip_page(items):
                    self.last_page = "skip_confirmation"
                    invalid = find_text(items, ("至少上阵1名队员", "出战人数不得低于", "未设置队长"))
                    if invalid:
                        await self._click(frame, self.catalog["skip"]["cancel"]["point"])
                        await self._stable_entry(original=True)
                        raise BattleSweepError("sweep_squad_invalid", message=invalid["text"])
            await self._pause()
        raise BattleSweepError("sweep_result_unconfirmed", submitted=True, last_page=self.last_page,
                               reward_confirmed=False)

    async def run(self):
        try:
            return await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            partial = {"submitted": self.submitted, "battle_started": False,
                       "reward_confirmed": bool(self.reward_evidence),
                       "reward_evidence": deepcopy(self.reward_evidence),
                       "capture": deepcopy(self.capture), "formation": deepcopy(self.formation),
                       "last_page": self.last_page,
                       "ready_for_next": False}
            exc.sweep_partial_result = partial
            if isinstance(getattr(exc, "detail", None), dict):
                exc.detail["sweep_partial"] = partial
            logger.error("[BattleSweepPartial] %s", json.dumps({"error": str(exc), "sweep": partial}, ensure_ascii=False))
            raise


async def try_battle_sweep(*, app, ocr, interaction, engine, context, capture_count=None,
                           formation_index=None, control_cid=None):
    if engine is None or context is None:
        raise ValueError("sweep requires the current execution context")
    ui = BattleUi(app, ocr, interaction=interaction)
    return await BattleSweepRuntime(ui, capture_count=capture_count, formation_index=formation_index,
                                    control_cid=control_cid).run()

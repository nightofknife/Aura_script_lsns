"""Offline safety boundaries for a native sweep; no app/runner is created."""
import asyncio
from contextlib import nullcontext

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import _battle_sweep_runtime as sweep


class Budget:
    def child(self, _seconds):
        return self

    def checkpoint(self):
        pass

    def activate(self):
        return nullcontext()

    async def sleep_async(self, _seconds):
        pass


class Frame:
    def __init__(self, image, frame_id):
        self.image, self.id = image, frame_id

    def rgb_image(self):
        return self.image


def row(text, center, confidence=1.):
    return {"text": text, "center": center, "confidence": confidence}


def label(record):
    x, y, w, h = record["roi"]
    return row(record["text"], (x+w//2, y+h//2))


class FakeUi:
    """Screens change only after the expected native button was clicked."""
    def __init__(self, response="reward", *, manual=False):
        self.phase, self.response, self.manual = "entry", response, manual
        self.clicks, self.read_count = [], 0
        self.runtime = None
        self.last_id = None
        self.selected_formation = 1
        self.formation_visible = True

    def operation(self):
        return Budget()

    def entry(self):
        profile = self.runtime.catalog["gp_profiles"][0]
        record = profile["manual_only" if self.manual else "sweep"]
        native = cv2.imdecode(np.frombuffer((sweep.PLAN / record["template"]).read_bytes(),
                                           dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        image = np.full((720, 1280, 3), 73, dtype=np.uint8)
        x, y, w, h = record["rect"]
        rgb = cv2.cvtColor(native, cv2.COLOR_BGRA2RGB)
        alpha = native[:, :, 3:4].astype(float)/255
        image[y:y+h, x:x+w] = (rgb*alpha + 73*(1-alpha)).astype(np.uint8)
        items = [label(anchor) for anchor in profile["anchors"]]
        items.append(row("测试关卡", (780, 80)))
        if self.manual:
            items.append(label(profile["manual_only_label"]))
        else:
            items += [label(profile["sweep_label"]), label(profile["group_start_label"])]
        return image, items

    async def read(self):
        self.read_count += 1
        if self.read_count > 35:
            raise sweep.BattleSweepError("fake_observation_limit")
        self.last_id = self.read_count
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        if self.phase == "entry":
            image, items = self.entry()
        elif self.phase == "skip":
            page = self.runtime.catalog["skip"]
            items = [label(page[key]) for key in ("title", "confirm_label", "cancel_label")]
            x, y, w, h = page["formation_current"]["roi"]
            items.append(row(str(self.selected_formation), (x+w//2, y+h//2)))
            if self.formation_visible:
                for tab in page["formation_tabs"]:
                    x, y, w, h = tab["index_roi"]
                    items.append(row(f"{tab['index']:02d}", (x+w//2, y+h//2)))
        elif self.phase == "reward":
            items = [row("获得物品", (640, 120)), row("×12", (380, 360)), row("20", (550, 360))]
            # OCR result order is not a property of the native reward screen.
            if self.read_count % 2:
                items.reverse()
        elif self.phase in ("energy", "energy_nested"):
            # The native question may be returned as two OCR lines.
            items = [row("您当前的澄明度不足", (600, 310)), row("是否补充澄明度？", (600, 350))]
        elif self.phase == "energy_restore":
            page = self.runtime.energy_pages["energy"]
            x, y, w, h = page["identity_roi"]
            items = [row(page["identity_text"], (x+w//2, y+h//2))]
        elif self.phase == "arrest":
            items = [row("最小", (380, 375)), row("+1", (820, 375)),
                     row("确定", (850, 540)), row("取消", (320, 540))]
        else:
            items = [row("等待服务器响应", (640, 360))]
        return Frame(image, self.last_id), items

    async def click(self, frame, point):
        assert frame.id == self.last_id, "A click must use its current observation"
        point = tuple(point)
        catalog = self.runtime.catalog
        expected = {
            "entry": catalog["gp_profiles"][0]["sweep"]["point"],
            "reward": catalog["reward"]["close"]["point"],
            "energy": self.runtime.energy_pages["buy_tips"]["close"]["point"],
            "energy_nested": self.runtime.energy_pages["buy_tips"]["close"]["point"],
            "energy_restore": self.runtime.energy_pages["energy"]["close"]["point"],
            "arrest": catalog["arrest"]["cancel"]["point"],
        }
        if self.phase == "skip":
            tab = next((tab for tab in catalog["skip"]["formation_tabs"]
                        if point == tuple(tab["point"])), None)
            if tab:
                self.selected_formation = tab["index"]
                self.clicks.append(point)
                return
            confirm = tuple(catalog["skip"]["confirm"]["point"])
            cancel = tuple(catalog["skip"]["cancel"]["point"])
            assert point in (confirm, cancel)
            self.phase = self.response if point == confirm else "entry"
        else:
            assert point == tuple(expected[self.phase])
            self.phase = {"entry": "skip", "reward": "entry", "energy": "skip",
                          "energy_nested": "energy_restore", "energy_restore": "skip",
                          "arrest": "reward"}[self.phase]
        self.clicks.append(point)


def runtime(response="reward", *, manual=False, **kwargs):
    ui = FakeUi(response, manual=manual)
    ui.runtime = sweep.BattleSweepRuntime(ui, **kwargs)
    return ui.runtime, ui


def test_sweep_requires_reward_then_original_detail_and_submits_once():
    operation, ui = runtime()
    result = asyncio.run(operation.run())
    assert result["outcome"] == "swept"
    assert result["swept"] == 1 and result["battle_started"] is False
    assert result["ready_for_next"] is True and result["reward_confirmed"] is True
    assert len(result["reward_evidence"]) == 2
    assert ui.clicks.count(tuple(operation.catalog["skip"]["confirm"]["point"])) == 1
    assert ui.phase == "entry"


def test_native_first_clear_layout_allows_combat_without_clicking():
    operation, ui = runtime(manual=True)
    result = asyncio.run(operation.run())
    assert result["outcome"] == "unsupported"
    assert result["reward_confirmed"] is False and result["swept"] == 0
    assert ui.clicks == []


def test_missing_sweep_ocr_does_not_turn_two_button_layout_into_fallback():
    operation, ui = runtime()
    image, items = ui.entry()
    items = [item for item in items if item["text"] != "扫荡"]
    assert operation._entry(Frame(image, 1), items) is None


def test_submitted_unknown_response_is_failure_without_retry_or_fallback():
    operation, ui = runtime("unknown")
    with pytest.raises(sweep.BattleSweepError) as failure:
        asyncio.run(operation.run())
    partial = failure.value.sweep_partial_result
    assert partial["submitted"] is True and partial["reward_confirmed"] is False
    assert partial["ready_for_next"] is False
    assert '"submitted": true' in str(failure.value)
    assert ui.clicks.count(tuple(operation.catalog["skip"]["confirm"]["point"])) == 1


def test_insufficient_energy_closes_question_and_cancels_sweep_no_refill():
    operation, ui = runtime("energy")
    result = asyncio.run(operation.run())
    assert result["outcome"] == "insufficient_stamina"
    assert result["swept"] == 0 and result["battle_started"] is False
    assert ui.clicks[-2:] == [tuple(operation.energy_pages["buy_tips"]["close"]["point"]),
                             tuple(operation.catalog["skip"]["cancel"]["point"])]
    assert ui.phase == "entry"


def test_optional_arrest_is_cancelled_when_not_requested_before_reward():
    operation, ui = runtime("arrest")
    result = asyncio.run(operation.run())
    assert result["outcome"] == "swept"
    assert result["capture"] == {"requested": None, "selected": 0,
                                 "available": True, "reason": "not_requested"}
    assert tuple(operation.catalog["arrest"]["cancel"]["point"]) in ui.clicks


def test_empty_or_unrelated_reward_numbers_do_not_confirm_sweep():
    operation, _ = runtime()
    assert operation._reward_page([row("获得物品", (640, 120)), row("0", (380, 360))]) is None
    assert operation._reward_page([row("获得物品", (640, 120)), row("12", (30, 30))]) is None
    assert operation._reward_page([row("关卡详情", (640, 120)), row("12", (380, 360))]) is None


def test_requested_formation_is_selected_and_independently_confirmed_before_submit():
    operation, ui = runtime(formation_index=3)
    result = asyncio.run(operation.run())
    tab = operation.catalog["skip"]["formation_tabs"][2]
    confirm = tuple(operation.catalog["skip"]["confirm"]["point"])
    assert result["formation"] == {"requested": 3, "selected": 3}
    assert ui.clicks.index(tuple(tab["point"])) < ui.clicks.index(confirm)


def test_unreadable_requested_formation_never_submits_or_uses_default():
    operation, ui = runtime(formation_index=3)
    ui.formation_visible = False
    with pytest.raises(sweep.BattleSweepError) as failure:
        asyncio.run(operation.run())
    assert failure.value.sweep_partial_result["submitted"] is False
    assert tuple(operation.catalog["skip"]["confirm"]["point"]) not in ui.clicks


def test_stop_before_opening_sweep_does_not_click(monkeypatch):
    operation, ui = runtime()
    monkeypatch.setattr(operation, "_stop_requested", lambda: True)
    result = asyncio.run(operation.run())
    assert result["outcome"] == "cancelled" and result["ready_for_next"] is True
    assert ui.clicks == []


def test_stop_in_team_dialog_cancels_before_consumption(monkeypatch):
    operation, ui = runtime()
    monkeypatch.setattr(operation, "_stop_requested", lambda: ui.phase == "skip")
    result = asyncio.run(operation.run())
    assert result["outcome"] == "cancelled" and operation.submitted is False
    assert ui.clicks[-1] == tuple(operation.catalog["skip"]["cancel"]["point"])
    assert ui.phase == "entry"


def test_stop_after_submit_drains_current_reward_instead_of_abandoning_it(monkeypatch):
    operation, ui = runtime()
    monkeypatch.setattr(operation, "_stop_requested", lambda: operation.submitted)
    result = asyncio.run(operation.run())
    assert result["outcome"] == "swept" and result["reward_confirmed"] is True
    assert ui.phase == "entry"


def test_nested_energy_layers_each_close_once_and_cancel_sweep():
    operation, ui = runtime("energy_nested")
    result = asyncio.run(operation.run())
    assert result["outcome"] == "insufficient_stamina"
    for page in operation.energy_pages.values():
        assert ui.clicks.count(tuple(page["close"]["point"])) == 1
    assert ui.phase == "entry"


def test_reward_with_changed_return_stage_preserves_evidence_and_fails():
    operation, ui = runtime()
    original = ui.entry

    def changed_entry():
        image, items = original()
        if operation.reward_evidence:
            items = [dict(item, text="其他关卡") if item["text"] == "测试关卡" else item for item in items]
        return image, items

    ui.entry = changed_entry
    with pytest.raises(sweep.BattleSweepError) as failure:
        asyncio.run(operation.run())
    assert failure.value.sweep_partial_result["reward_confirmed"] is True
    assert failure.value.sweep_partial_result["ready_for_next"] is False

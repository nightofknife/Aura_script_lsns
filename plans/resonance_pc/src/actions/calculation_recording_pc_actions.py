"""Scan and advance the solo Calculation Recording challenge list."""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any
import unicodedata

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.events import Event
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationTimeout
from ....aura_base.src.services.interaction_service import InteractionError, confirm_async
from ._battle_combat_runtime import BattleResolutionError, callback_page, settle_battle
from ._battle_ui import BattleUi, find_text
from ._calculation_recording_progress import PROGRESS_VALUES, RecordingProgress
from ._calculation_recording_vision import (
    BOSS_NUMBER_ROI, PROGRESS_ROI, Card, is_recording_page, scan_visible_cards, selected_card_center,
    settlement_next_center,
)


SCHEMA = "resonance_pc.calculation_recording.v1"
PROGRESS_EVENT = "task.resonance_pc_calculation_recording_progress"
PAGE = "calculation_recording"
ENTRY_POINT = (945, 630)
FORMATION_START_POINT = (1200, 361)
ROW_TOLERANCE = 15
TOP_ROW_Y = 162
MAX_SCROLLS = 96
START_TEXT = ("开始作战", "开始战斗", "START BATTLE", "开始", "作战")
NO_TAPES_TEXT = "空白录像带不足"
LOCKED_TEXT = ("请先完成前置关卡的刻录", "前置关卡未完成", "等级不足", "尚未解锁")
NOTICE_REGION = (448, 170, 450, 350)
DETAIL_TIMEOUT = 10.0


class RecordingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ChallengeUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Detail:
    number: int
    progress: int


@dataclass(frozen=True)
class ListView:
    frame: Any
    cards: tuple[Card, ...]
    sample: np.ndarray

    def same_rows(self, other: ListView) -> bool:
        return (len(self.cards) == len(other.cards)
                and all(a.locked == b.locked and abs(a.center[1] - b.center[1]) <= 4
                        for a, b in zip(self.cards, other.cards)))

    def same_position(self, other: ListView) -> bool:
        # Portrait content distinguishes a whole-row displacement. It is
        # deliberately excluded from the stability check because it animates.
        return self.same_rows(other) and float(np.abs(self.sample - other.sample).mean()) <= 8.0


@dataclass(frozen=True)
class Formation:
    point: tuple[int, int] | None = None
    unavailable: str | None = None


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def _single_line(rows) -> str:
    if len(rows) != 1 or rows[0]["confidence"] < 0.85:
        return ""
    return _normalize(rows[0]["text"])


def _unavailable(items) -> str | None:
    if find_text(items, NO_TAPES_TEXT, region=NOTICE_REGION):
        return "insufficient_tapes"
    if find_text(items, LOCKED_TEXT, region=NOTICE_REGION) or (
        find_text(items, "列车长达到", region=NOTICE_REGION) and find_text(items, "解锁", region=NOTICE_REGION)
    ):
        return "stage_locked"
    return None


def _battle_callback(frame, items) -> str | None:
    if find_text(items, NO_TAPES_TEXT, region=NOTICE_REGION):
        raise ChallengeUnavailable("insufficient_tapes")
    return PAGE if is_recording_page(frame.rgb_image()) else None


def _record_overlay_dismiss_point(items) -> tuple[int, int] | None:
    heading = find_text(items, "通关记录", exact=True, region=(380, 52, 315, 62))
    body = find_text(items, ("只记录", "总输出", "作战用时"), region=(330, 84, 630, 210))
    # The record overlay covers the center of the settlement. Its outer
    # backdrop dismisses it without selecting a record or advancing the battle.
    return (1009, 382) if heading and body else None


class RecordingRun:
    def __init__(self, ui, store, target, event_bus):
        self.ui = ui
        self.store = store
        self.target = target
        self.event_bus = event_bus
        self.sequence = 0
        self.attempts = 0
        self.stages: list[dict[str, Any]] = []
        self.processed: set[int] = set()
        self.last_detail: Detail | None = None

    async def _emit(self, detail: Detail | None = None) -> None:
        self.sequence += 1
        payload = {
            "schema": SCHEMA, "cid": self.ui.scope.cid, "sequence": self.sequence,
            "number": detail.number if detail else None, "progress": detail.progress if detail else None,
            "target_progress": self.target, "attempts": self.attempts,
        }
        try:
            await self.event_bus.publish(Event(name=PROGRESS_EVENT, payload=payload))
        except Exception:
            logger.exception("[CalculationRecording] Could not publish progress")

    def _result(self, reason: str) -> dict:
        return {
            "schema": SCHEMA, "status": "completed" if reason == "scanned_all" else "stopped",
            "stop_reason": reason, "target_progress": self.target, "attempts": self.attempts,
            "stages": self.stages,
        }

    async def _observe_list(self) -> ListView | None:
        frame = await self.ui.capture()
        rgb = frame.rgb_image()
        if not is_recording_page(rgb):
            return None
        cards = scan_visible_cards(rgb)
        if not cards:
            return None
        sample = cv2.resize(rgb[96:599, 345:586], (30, 63), interpolation=cv2.INTER_AREA).astype(np.float32)
        return ListView(frame, cards, sample)

    async def _stable_list(self) -> ListView:
        view = await confirm_async(
            self._observe_list, scope=self.ui.operation().child(8.0),
            before_probe=False, poll_seconds=0.35, equivalent=ListView.same_rows,
        )
        if view is None:
            raise RecordingError("calculation_recording_list_unconfirmed", "请打开单队虚演刻录的关卡选择页并确认列表可见。")
        return view

    async def _scroll(self, view: ListView, *, toward_top: bool) -> ListView:
        # Less than the fully visible area. A paused release prevents inertia
        # from skipping the clipped row between consecutive views.
        start_y, end_y = (188, 416) if toward_top else (416, 188)
        await self.ui.interaction.send_input_async(
            lambda: self.ui.app.drag(334, start_y, 334, end_y, duration=0.55, hold_before_release_sec=0.22),
            scope=self.ui.operation().child(5.0), point=(334, start_y),
            frame=view.frame, session=self.ui.session,
        )
        await self.ui.sleep(0.9)
        return await self._stable_list()

    async def _go_to_top(self, view: ListView) -> ListView:
        no_motion = 0
        for _ in range(MAX_SCROLLS):
            first = view.cards[0]
            if not first.locked and abs(first.center[1] - TOP_ROW_Y) <= 4:
                # At the top, a downward drag can elastically move content and
                # return it. Verify the first card rather than counting bounces.
                _, detail = await self._select(view, first)
                view = await self._stable_list()
                if detail.number == 1:
                    return view
            next_view = await self._scroll(view, toward_top=True)
            no_motion = no_motion + 1 if view.same_position(next_view) else 0
            if no_motion >= 2:
                return next_view
            view = next_view
        raise RecordingError("calculation_recording_top_unconfirmed", "滚动后仍无法确认关卡列表顶部。")

    async def _detail(self, *, number=None, different_from=None, row_y=None,
                      min_progress=0, timeout=DETAIL_TIMEOUT):
        async def observe():
            frame = await self.ui.capture()
            rgb = frame.rgb_image()
            if not is_recording_page(rgb):
                return None
            if row_y is not None:
                selected = selected_card_center(rgb)
                if selected is None or abs(selected[1] - row_y) > ROW_TOLERANCE:
                    return None

            number_text = _single_line(await self.ui.recognize(frame, BOSS_NUMBER_ROI, recognition_only=True))
            progress_text = _single_line(await self.ui.recognize(frame, PROGRESS_ROI, recognition_only=True))
            if not re.fullmatch(r"[0-9]{1,3}", number_text) or not re.fullmatch(r"[0-9]{1,3}%", progress_text):
                return None
            observed = Detail(int(number_text), int(progress_text[:-1]))
            if (observed.number <= 0 or observed.number == different_from
                    or (number is not None and number != observed.number)
                    or observed.progress not in PROGRESS_VALUES or observed.progress < min_progress):
                return None
            return frame, observed

        return await confirm_async(
            observe, scope=self.ui.operation().child(timeout), before_probe=False, poll_seconds=0.35,
            equivalent=lambda first, second: first[1] == second[1] and first[0].id != second[0].id,
        )

    async def _select(self, view: ListView, card: Card):
        prior_selection = selected_card_center(view.frame.rgb_image())
        different_from = None
        if self.last_detail is not None and prior_selection is not None and abs(prior_selection[1] - card.center[1]) > ROW_TOLERANCE:
            different_from = self.last_detail.number
        await self.ui.click(view.frame, card.center)
        await self.ui.sleep(0.35)
        confirmed = await self._detail(row_y=card.center[1], different_from=different_from)
        if confirmed is None:
            raise RecordingError("calculation_recording_selection_unconfirmed", "点开卡片后无法确认右侧关卡编号与百分比。")
        self.last_detail = confirmed[1]
        return confirmed

    async def _formation(self):
        async def observe():
            frame, items = await self.ui.read()
            unavailable = _unavailable(items)
            if unavailable:
                return frame, Formation(unavailable=unavailable)
            if is_recording_page(frame.rgb_image()) or callback_page(items) != "battle_preparation":
                return None
            start = find_text(items, START_TEXT, exact=True, region=(1140, 290, 138, 175))
            return (frame, Formation(point=FORMATION_START_POINT)) if start else None

        confirmed = await confirm_async(
            observe, scope=self.ui.operation().child(16.0), before_probe=False, poll_seconds=0.35,
            equivalent=lambda first, second: first[1].unavailable == second[1].unavailable,
        )
        if confirmed is None:
            raise RecordingError("calculation_recording_formation_unconfirmed", "未确认虚演刻录的编队页，请查看游戏当前画面。")
        return confirmed

    async def _battle(self, frame, detail: Detail) -> str | None:
        await self.ui.click(frame, ENTRY_POINT)
        await self.ui.sleep(1.25)
        frame, formation = await self._formation()
        if formation.unavailable:
            return formation.unavailable
        try:
            await self.ui.click(frame, formation.point)
        except (InteractionError, OperationTimeout) as exc:
            raise RecordingError(
                "calculation_recording_start_unconfirmed",
                f"关卡 {detail.number:02d} 的开战提交未确认，请查看游戏当前画面。",
            ) from exc
        self.attempts += 1
        await self._emit(detail)
        still_in_formation = 0
        start_deadline = time.monotonic() + 15.0

        def observe_callback(frame, items):
            nonlocal still_in_formation
            if time.monotonic() >= start_deadline and callback_page(items) == "battle_preparation":
                still_in_formation += 1
                if still_in_formation >= 2:
                    raise RecordingError(
                        "calculation_recording_start_unconfirmed",
                        f"关卡 {detail.number:02d} 提交后仍停在编队页，请查看开战状态。",
                    )
            else:
                still_in_formation = 0
            return _battle_callback(frame, items)

        try:
            result = await settle_battle(
                self.ui, callback_from_frame=observe_callback, allow_early_callback=True, stop_on_defeat=True,
                dismiss_overlay_at=_record_overlay_dismiss_point,
                next_button_from_frame=settlement_next_center, next_max_clicks=3,
            )
        except ChallengeUnavailable as exc:
            return str(exc)
        except BattleResolutionError as exc:
            if exc.code == "battle_game_timeout":
                return "game_timeout"
            raise RecordingError(
                "calculation_recording_settlement_unconfirmed",
                f"关卡 {detail.number:02d} 已提交开战，结算未确认。请在游戏内查看本场结果。",
            ) from exc
        return result["outcome"]

    async def _after_battle(self, before: Detail, outcome: str | None):
        # The page can reopen before the server's updated percentage appears.
        advanced = await self._detail(number=before.number, min_progress=before.progress + 1, timeout=8.0)
        if advanced is not None:
            return advanced
        returned = await self._detail(number=before.number)
        if returned is None or returned[1].progress < before.progress or (outcome is None and returned[1] == before):
            raise RecordingError(
                "calculation_recording_result_unconfirmed",
                f"未能确认关卡 {before.number:02d} 本场的进度变化，请查看游戏结果。",
            )
        return returned

    async def _run_stage(self, frame, initial: Detail) -> str:
        self.store.save(initial.number, initial.progress)
        logger.info("[CalculationRecording] stage=%02d progress=%s target=%s",
                    initial.number, initial.progress, self.target)
        await self._emit(initial)
        detail = initial
        outcome = "already_at_target"
        while detail.progress < self.target:
            outcome = await self._battle(frame, detail)
            if outcome in {"defeat", "insufficient_tapes", "stage_locked", "game_timeout"}:
                break
            frame, returned = await self._after_battle(detail, outcome)
            self.store.save(returned.number, returned.progress)
            logger.info("[CalculationRecording] stage=%02d progress=%s->%s attempts=%s",
                        returned.number, detail.progress, returned.progress, self.attempts)
            self.last_detail = returned
            await self._emit(returned)
            if returned.progress == detail.progress:
                outcome = "no_progress"
                break
            detail = returned
            outcome = "target_reached"

        self.processed.add(initial.number)
        self.stages.append({"number": initial.number, "progress_before": initial.progress,
                            "progress_after": detail.progress, "outcome": outcome})
        return outcome

    async def run(self) -> dict:
        await self._emit()
        view = await self._go_to_top(await self._stable_list())
        checked_rows: list[int] = []
        scrolled = no_motion = 0
        moved_down = False
        check_first_number = not view.cards[0].locked
        while True:
            card = next((entry for entry in view.cards if not entry.locked
                         and all(abs(entry.center[1] - y) > ROW_TOLERANCE for y in checked_rows)), None)
            if card is not None:
                checked_rows.append(card.center[1])
                frame, detail = await self._select(view, card)
                if check_first_number:
                    if detail.number != 1:
                        raise RecordingError(
                            "calculation_recording_top_unconfirmed",
                            f"首张卡片读到 {detail.number:02d}，未能确认列表已回到顶部。",
                        )
                    check_first_number = False
                if detail.number in self.processed:
                    self.store.save(detail.number, detail.progress)
                else:
                    attempts_before = self.attempts
                    outcome = await self._run_stage(frame, detail)
                    if outcome in {"defeat", "insufficient_tapes", "no_progress", "game_timeout"}:
                        return self._result(outcome)
                    if self.attempts != attempts_before:
                        # Native settlement can re-center the list. Recheck its
                        # current rows; only confirmed numbers survive re-centering.
                        checked_rows.clear()
                view = await self._stable_list()
                continue

            if scrolled >= MAX_SCROLLS:
                raise RecordingError("calculation_recording_bottom_unconfirmed", "未能确认关卡列表底部，已停止滚动。")
            next_view = await self._scroll(view, toward_top=False)
            scrolled += 1
            if view.same_position(next_view):
                no_motion += 1
                if no_motion >= 2:
                    if not moved_down:
                        raise RecordingError("calculation_recording_scroll_unconfirmed", "未观察到关卡列表向下滚动。")
                    return self._result("scanned_all")
            else:
                moved_down = True
                no_motion = 0
                checked_rows.clear()
            view = next_view


@action_info(
    name="resonance_pc.run_calculation_recording",
    public=True,
    read_only=False,
    description="Scan solo Calculation Recording bosses, persist observed progress, and advance to the target.",
)
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", interaction="plans/aura_base/interaction",
                   persistent_data="core/persistent_data", event_bus="core/event_bus")
async def run_calculation_recording(
    target_progress: int = 100, app=None, ocr=None, interaction=None, persistent_data=None, event_bus=None,
) -> dict:
    if type(target_progress) is not int or target_progress not in PROGRESS_VALUES or target_progress == 0:
        raise ValueError("虚演刻录目标进度必须是 20、40、60、80 或 100。")
    ui = BattleUi(app, ocr=ocr, interaction=interaction)
    # The selection screen has no reliable UID. The configured Aura data
    # profile owns this file; every decision still uses fresh game evidence.
    store = RecordingProgress.load(persistent_data, "local")
    with ui.scope.activate():
        return await RecordingRun(ui, store, target_progress, event_bus).run()

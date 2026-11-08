"""Reminder-gated traversal of all active passes with bounded, confirmed input."""
from __future__ import annotations

import asyncio
import time

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationTimeout
from ....aura_base.src.services.interaction_service import InputSession
from ....aura_base.src.services.observation_service import Observation
from ._daily_reward_flow import DailyRewardFlow
from ._battle_pass_reward_vision import BattlePassRewardError, BattlePassRewardReader


PASS_PAGES = {"tasks", "manual"}


class BattlePassRewardFlow(DailyRewardFlow):
    reader_type = BattlePassRewardReader
    error_type = BattlePassRewardError
    timeout_seconds = 600.
    error_prefix = "battle_pass_reward"
    label = "通行证奖励"
    log_label = "BattlePassRewards"

    def __init__(self, app, observation, interaction):
        self.processed = {}
        self.active_key = None
        self.scrolls = 0
        self.scroll_session = InputSession()
        super().__init__(app, observation, interaction)

    def error(self, code, message):
        error = super().error(code, message)
        error.detail.update(active_pass=self.active_key, processed_passes=list(self.processed.values()),
                            list_scrolls=self.scrolls)
        return error

    @staticmethod
    def equivalent(previous, current):
        if (previous.frame.id == current.frame.id or previous.page_state != current.page_state
                or previous.selected_key != current.selected_key or set(previous.hits) != set(current.hits)
                or len(previous.entries) != len(current.entries)):
            return False
        for key, hit in current.hits.items():
            old = previous.hits[key]
            if old.state != hit.state:
                return False
            if hit.state == "present" and any(abs(a-b) > 2 for a,b in zip(old.client_center, hit.client_center)):
                return False
        return all(a.key == b.key and abs(a.center[1]-b.center[1]) <= 2
                   for a,b in zip(previous.entries, current.entries))

    def _current_pass(self, page, kind=None):
        return page.selected_key == self.active_key and page.page_state in ({kind} if kind else PASS_PAGES)

    @staticmethod
    def _same_list(before, after):
        return (len(before.entries) == len(after.entries) and bool(before.entries)
                and all(a.key == b.key and abs(a.center[1]-b.center[1]) <= 2
                        for a,b in zip(before.entries, after.entries)))

    def _scroll(self, *, down):
        """Two acknowledged, unchanged gestures establish a clamped boundary.

        A single missing/partial OCR result cannot prove the end of the list.
        Native ScrollGrid_Time is vertical and clamped; use overlapping drags.
        """
        self.stage = "scroll_passes_down" if down else "scroll_passes_up"
        for _ in range(2):
            before = self.confirmed_page(PASS_PAGES, timeout=8.)
            if not before.entries or not before.selected_key:
                raise self.error("battle_pass_reward_list_unconfirmed", "无法确认通行证列表，已停止滚动。")
            start_y, end_y = (440, 140) if down else (140, 440)
            scope = self.scope.child(12.)
            source = Observation("present", frame_id=before.frame.id, client_center=(110, start_y),
                                 semantic_key="battle_pass_list")
            self.interaction.send_input(
                lambda: self.app.drag(start_x=110, start_y=start_y, end_x=110, end_y=end_y,
                                      duration=.5, hold_before_release_sec=.4, stop_inertia=True),
                scope=scope, frame=before.frame, observation=source, point=source.client_center,
                session=self.scroll_session)
            self.scrolls += 1
            scope.sleep(.7)
            after = self.confirmed_page(PASS_PAGES, timeout=8.)
            if after.selected_key != before.selected_key:
                raise self.error("battle_pass_reward_scroll_changed_selection", "滚动时通行证选择发生意外变化，已停止。")
            if not self._same_list(before, after):
                return True
        return False

    def _list_top(self):
        for _ in range(16):
            if not self._scroll(down=False):
                return
        raise self.error("battle_pass_reward_list_top_unconfirmed", "未能在上限内确认通行证列表顶部，已停止。")

    def _select(self, entry):
        self.active_key = entry.key
        self.transition("select_pass", lambda p: p.page_state in PASS_PAGES,
                        lambda p: self._current_pass(p), entry.control)

    def _tab(self, kind):
        other = "manual" if kind == "tasks" else "tasks"
        self.transition(f"open_{kind}", lambda p: self._current_pass(p, other),
                        lambda p: self._current_pass(p, kind), f"{kind}_off")

    def _dismiss(self, kind):
        self.reward_overlays += 1
        self.transition(f"dismiss_{kind}_reward", lambda p: p.page_state == "reward",
                        lambda p: self._current_pass(p, kind), "reward_exit", fixed_point=(500, 500))

    def _collect(self, kind):
        self._tab(kind)
        control = "tasks_claim_all" if kind == "tasks" else "manual_claim_all_red"
        for _ in range(16):
            page = self.confirmed_page({kind, "reward"}, timeout=8.)
            if page.page_state == "reward":
                self._dismiss(kind)
                continue
            if not self._current_pass(page, kind):
                raise self.error("battle_pass_reward_selection_changed", "领取期间通行证选择发生变化，已停止。")
            if page.absent(control):
                return
            # A disappearing button alone does not prove receipt of rewards.
            # Both existing overlay anchors must appear before we continue.
            self.transition(f"claim_{kind}", lambda p: self._current_pass(p, kind) and p.present(control),
                            lambda p: p.page_state == "reward", control)
        raise self.error("battle_pass_reward_cycle_limit", "通行证奖励页面反复变化超过上限，已停止。")

    def result(self, status, reason=None):
        return {"success": True, "component": "battle_pass", "status": status, "reason": reason,
                "claim_clicks": self.clicks["claim_tasks"] + self.clicks["claim_manual"],
                "reward_overlays": self.reward_overlays, "returned_to_main": True,
                "passes_processed": len(self.processed), "passes": list(self.processed.values()),
                "list_scrolls": self.scrolls, "stage_clicks": dict(self.clicks),
                "elapsed_ms": round((time.monotonic()-self.started)*1000)}

    def run(self):
        try:
            with self.scope.activate():
                page = self.confirmed_page({"main"})
                if page.absent("main_entry_with_red"):
                    logger.info("[BattlePassRewards] skipped: confirmed main screen without star reminder")
                    return self.result("skipped_no_reminder", "no_battle_pass_reminder")
                self.transition("enter_passes", lambda p: p.page_state == "main" and p.present("main_entry_with_red"),
                                lambda p: p.page_state in PASS_PAGES, "main_entry_with_red")
                self._list_top()
                for _ in range(64):
                    page = self.confirmed_page(PASS_PAGES, timeout=8.)
                    remaining = [entry for entry in page.entries if entry.key not in self.processed]
                    if remaining:
                        if len(self.processed) >= 32:
                            raise self.error("battle_pass_reward_pass_limit", "通行证数量超过执行上限，已停止。")
                        entry = remaining[0]
                        self._select(entry)
                        tasks_before, levels_before = self.clicks["claim_tasks"], self.clicks["claim_manual"]
                        self._collect("tasks")
                        self._collect("manual")
                        self.processed[entry.key] = {"title": entry.title,
                            "task_claim_clicks": self.clicks["claim_tasks"]-tasks_before,
                            "level_claim_clicks": self.clicks["claim_manual"]-levels_before}
                        logger.info("[BattlePassRewards] processed pass=%s", entry.title)
                        continue
                    if self._scroll(down=True):
                        continue
                    final = self.confirmed_page(PASS_PAGES, timeout=8.)
                    if any(entry.key not in self.processed for entry in final.entries):
                        continue
                    self.transition("return_main", lambda p: p.page_state in PASS_PAGES
                                    and all(entry.key in self.processed for entry in p.entries),
                                    lambda p: p.page_state == "main", "city_main")
                    collected = self.clicks["claim_tasks"] + self.clicks["claim_manual"]
                    return self.result("collected" if collected else "nothing_to_claim")
                raise self.error("battle_pass_reward_traversal_limit", "通行证列表遍历超过上限，已停止。")
        except asyncio.CancelledError:
            raise
        except OperationTimeout as exc:
            self.scope.check_cancelled()
            raise self.error("battle_pass_reward_timeout", "通行证奖励当前阶段或总等待超时，已停止。") from exc
        except BattlePassRewardError as exc:
            exc.detail = {**self.error(exc.code, exc.message).detail, **exc.detail}
            raise
        except Exception as exc:
            error = self.error("battle_pass_reward_operation_failed", "通行证奖励执行失败，已停止并保留当前界面。")
            error.detail["cause"] = {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None)}
            raise error from exc

"""Collect rewards from visible pass reminders with bounded, confirmed input."""
from __future__ import annotations

import asyncio
import time

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationTimeout
from ._daily_reward_flow import DailyRewardFlow
from ._battle_pass_reward_vision import BattlePassRewardError, BattlePassRewardReader
from ._battle_pass_entries import LIST_ROI, reminder_control


PASS_PAGES = {"tasks", "manual"}


class BattlePassRewardFlow(DailyRewardFlow):
    reader_type = BattlePassRewardReader
    error_type = BattlePassRewardError
    timeout_seconds = 600.
    error_prefix = "battle_pass_reward"
    label = "通行证奖励"
    log_label = "BattlePassRewards"

    def __init__(self, app, observation, interaction):
        self.processed = []
        self.active_row = None
        super().__init__(app, observation, interaction)

    def error(self, code, message):
        error = super().error(code, message)
        error.detail.update(active_row=self.active_row, processed_passes=list(self.processed))
        return error

    @staticmethod
    def equivalent(previous, current):
        if (previous.frame.id == current.frame.id or previous.page_state != current.page_state
                or previous.selected_row != current.selected_row or previous.reminders != current.reminders
                or set(previous.hits) != set(current.hits)):
            return False
        for key, hit in current.hits.items():
            old = previous.hits[key]
            if old.state != hit.state:
                return False
            if hit.state == "present" and any(abs(a-b) > 2 for a,b in zip(old.client_center, hit.client_center)):
                return False
        return True

    def _current_pass(self, page, kind=None):
        return (self.active_row is not None and page.selected_row is not None
                and abs(page.selected_row - self.active_row) <= 3
                and page.page_state in ({kind} if kind else PASS_PAGES))

    def _select(self, row):
        self.active_row = row
        self.transition("select_pass", lambda p: p.page_state in PASS_PAGES,
                        lambda p: self._current_pass(p), reminder_control(row))

    def _confirm_reminder_cleared(self):
        self.stage = "confirm_pass_reminder_cleared"
        scope = self.scope.child(5.)
        result = self.interaction.confirm(
            lambda: self.observe(scope), scope=scope, stable_scans=2, poll_seconds=.25,
            before_probe=False, equivalent=self.equivalent,
            accept=lambda p: self._current_pass(p) and all(abs(row - self.active_row) > 3 for row in p.reminders),
            is_unknown=lambda p: p.page_state == "unknown",
        )
        if result is None:
            raise self.error("battle_pass_reward_reminder_not_cleared", "未确认当前通行证红点消失，已停止。")

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
                "passes_processed": len(self.processed), "passes": list(self.processed),
                "list_scrolls": 0, "stage_clicks": dict(self.clicks),
                "elapsed_ms": round((time.monotonic()-self.started)*1000)}

    def run(self):
        try:
            with self.scope.activate():
                page = self.confirmed_page({"main"})
                if page.absent("main_entry_with_red"):
                    logger.info("[BattlePassRewards] skipped: confirmed main screen without star reminder")
                    return self.result("skipped_no_reminder", "no_battle_pass_reminder")
                self.transition("enter_passes", lambda p: p.page_state == "main" and p.present("main_entry_with_red"),
                                lambda p: p.page_state in PASS_PAGES and (p.selected_row is not None or bool(p.reminders)),
                                "main_entry_with_red")
                while True:
                    page = self.confirmed_page(PASS_PAGES, timeout=8.)
                    if not page.reminders:
                        self.transition("return_main", lambda p: p.page_state in PASS_PAGES and not p.reminders,
                                        lambda p: p.page_state == "main", "city_main")
                        collected = self.clicks["claim_tasks"] + self.clicks["claim_manual"]
                        return self.result("collected" if collected else "nothing_to_claim")

                    row = page.reminders[0]
                    if row >= LIST_ROI[1] + LIST_ROI[3] - 9:
                        raise self.error("battle_pass_reward_reminder_unreachable", "红点对应的卡片未完整显示，已停止。")
                    if len(self.processed) >= 32:
                        raise self.error("battle_pass_reward_pass_limit", "通行证数量超过执行上限，已停止。")
                    self._select(row)
                    tasks_before, levels_before = self.clicks["claim_tasks"], self.clicks["claim_manual"]
                    self._collect("tasks")
                    self._collect("manual")
                    self._confirm_reminder_cleared()
                    self.processed.append({"row_y": row,
                        "task_claim_clicks": self.clicks["claim_tasks"] - tasks_before,
                        "level_claim_clicks": self.clicks["claim_manual"] - levels_before})
                    logger.info("[BattlePassRewards] cleared reminder row_y=%s", row)
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

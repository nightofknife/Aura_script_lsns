"""Bounded daily-reward collection with confirmed sources and postconditions."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace
import time

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from ....aura_base.src.services.interaction_service import TransitionPolicy, TransitionProbe, TransitionState
from ._daily_reward_vision import DailyRewardError, DailyRewardReader


def _cancel_check():
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Daily reward collection cancelled")


class DailyRewardFlow:
    reader_type = DailyRewardReader
    error_type = DailyRewardError
    timeout_seconds = 180.
    error_prefix = "daily_reward"
    label = "每日奖励"
    log_label = "DailyRewards"

    def __init__(self, app, observation, interaction):
        self.app, self.interaction = app, interaction
        self.started = time.monotonic()
        self.scope = OperationScope(self.timeout_seconds, parent=current_operation_scope(),
            target=getattr(app, "target_runtime", None), cancel_check=_cancel_check)
        self.reader = self.reader_type(observation)
        self.stage = "start"
        self.last = None
        self.clicks = defaultdict(int)
        self.reward_overlays = 0

    def error(self, code, message):
        code = code.replace("daily_reward", self.error_prefix, 1)
        message = message.replace("每日奖励", self.label)
        return self.error_type(code, message, {"stage": self.stage,
            "last_observation": self.last.summary() if self.last is not None else None,
            "stage_clicks": dict(self.clicks), "reward_overlays": self.reward_overlays,
            "elapsed_ms": round((time.monotonic() - self.started) * 1000)})

    def observe(self, scope):
        self.scope.checkpoint()
        self.last = self.reader.observe(scope)
        self.scope.checkpoint()
        return self.last

    @staticmethod
    def equivalent(a, b):
        if a.frame.id == b.frame.id or a.page_state != b.page_state:
            return False
        if set(a.hits) != set(b.hits):
            return False
        for key, current in b.hits.items():
            previous = a.hits[key]
            if current.state != previous.state:
                return False
            if key in {"main_screen", "main_entry_with_red", "daily_tab", "city_main", "reward_title", "reward_exit"} and current.state == "present" and any(abs(old - new) > 2
                    for old, new in zip(previous.client_center, current.client_center)):
                return False
        return True

    def confirmed_page(self, allowed, *, timeout=5.):
        scope = self.scope.child(timeout)
        result = self.interaction.confirm(lambda: self.observe(scope), scope=scope,
            stable_scans=2, poll_seconds=.25, before_probe=False,
            accept=lambda value: value.page_state in allowed,
            is_unknown=lambda value: value.page_state == "unknown", equivalent=self.equivalent)
        if result is None:
            raise self.error("daily_reward_page_unconfirmed", "未能稳定确认每日奖励所在页面，已停止。")
        return result

    def transition(self, stage, source, target, control, *, fixed_point=None):
        self.stage = stage
        stage_scope = self.scope.child(15.)
        # Statistics accumulate across distinct confirmed rewards; a new logical
        # transition gets its own first click plus two supplementary clicks.
        policy = TransitionPolicy(max_clicks=3, allow_retry=True, settle_seconds=1.2)
        state = TransitionState(policy)

        def raw_probe(allow_source):
            page = self.observe(probe_scope[0])
            if target(page):
                return TransitionProbe("target", key=(stage, "target", page.page_state),
                                       hit=page, frame=page.frame)
            if allow_source and source(page) and page.present(control):
                hit = page.hits[control]
                if fixed_point is not None:
                    # Only an observed reward overlay can authorize a blank-
                    # area click. Its hint supplies evidence, not the location.
                    hit = replace(hit, client_center=fixed_point)
                return TransitionProbe("source", key=(stage, "source", page.page_state),
                                       hit=hit, frame=page.frame)
            return TransitionProbe(frame=page.frame)

        def same_probe(previous, current):
            if current.kind == "target":
                return self.equivalent(previous.hit, current.hit)
            old, new = previous.hit.client_center, current.hit.client_center
            return all(abs(a - b) <= 2 for a, b in zip(old, new))

        # Each raw callback receives the current bounded probe budget, while
        # the enclosing transition retains its original total deadline.
        probe_scope = [None]
        def probe(allow_source):
            # A target-only wait never supplements input. After it expires,
            # run_transition requests fresh, stable source evidence for retry.
            probe_scope[0] = stage_scope.child(4. if allow_source else 2.5)
            return self.interaction.probe_transition(raw_probe, allow_source=allow_source,
                scope=probe_scope[0], stable_scans=2, poll_seconds=.25,
                equivalent=same_probe, allow_immediate_source=False, state=state)

        def click(observed, attempt):
            point = observed.hit.client_center
            self.clicks[stage] += 1
            logger.info("[%s] stage=%s click=%s point=%s", self.log_label, stage, self.clicks[stage], point)
            return self.app.click(x=round(point[0]), y=round(point[1]))

        result = self.interaction.run_transition(probe, click, scope=stage_scope, policy=policy, state=state,
            source_unconfirmed=lambda: self.error("daily_reward_source_unconfirmed", "未能确认可点击的原页面，已停止补点。"),
            exhausted=lambda: self.error("daily_reward_transition_unconfirmed", "每日奖励点击后的页面变化未确认，已停止。"))
        return result.observation.hit

    @staticmethod
    def daily_empty(page):
        return page.page_state == "daily" and page.absent("claimable") and page.absent("activity_gold")

    def result(self, status, reason=None):
        return {"success": True, "component": "daily_mission", "status": status, "reason": reason,
            "claim_clicks": self.clicks["claim_tasks"] + self.clicks["claim_activity"],
            "reward_overlays": self.reward_overlays, "returned_to_main": True,
            "stage_clicks": dict(self.clicks), "elapsed_ms": round((time.monotonic() - self.started) * 1000)}

    def run(self):
        try:
            with self.scope.activate():
                initial = self.confirmed_page({"main"})
                if initial.absent("main_entry_with_red"):
                    logger.info("[DailyRewards] skipped: confirmed main screen without reminder")
                    return self.result("skipped_no_reminder", "no_daily_reminder")
                self.transition("enter_daily", lambda p: p.page_state == "main" and p.present("main_entry_with_red"),
                    lambda p: p.page_state == "daily", "main_entry_with_red")
                for _ in range(24):
                    page = self.confirmed_page({"daily", "reward"})
                    if page.page_state == "reward":
                        self.reward_overlays += 1
                        self.transition("dismiss_reward", lambda p: p.page_state == "reward",
                            lambda p: p.page_state == "daily", "reward_exit",
                            fixed_point=(500, 500))
                    elif page.present("claimable"):
                        self.transition("claim_tasks", lambda p: p.page_state == "daily" and p.present("claimable"),
                            lambda p: p.page_state == "reward" or (p.page_state == "daily" and p.absent("claimable")),
                            "claimable")
                    elif page.present("activity_gold"):
                        self.transition("claim_activity", lambda p: p.page_state == "daily" and p.present("activity_gold"),
                            lambda p: p.page_state == "reward", "activity_gold")
                    elif self.daily_empty(page):
                        self.transition("return_main", self.daily_empty,
                            lambda p: p.page_state == "main", "city_main")
                        status = "collected" if self.clicks["claim_tasks"] + self.clicks["claim_activity"] else "nothing_to_claim"
                        return self.result(status)
                raise self.error("daily_reward_cycle_limit", "每日奖励页面反复变化超过上限，已停止。")
        except asyncio.CancelledError:
            raise
        except OperationTimeout as exc:
            self.scope.check_cancelled()
            raise self.error("daily_reward_timeout", "每日奖励当前阶段或总等待超时，已停止。") from exc
        except DailyRewardError as exc:
            exc.detail = {**self.error(exc.code, exc.message).detail, **exc.detail}
            raise
        except Exception as exc:
            error = self.error("daily_reward_operation_failed", "每日奖励执行失败，已停止并保留当前界面。")
            error.detail["cause"] = {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None)}
            raise error from exc

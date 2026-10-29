"""One ordinary settlement: identify its outcome before advancing its UI."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.observability.logging.core_logger import logger
from ._battle_ui import BattleUi, compact, find_text


PLAN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PLAN_ROOT / "data/meta/battle_combat_runtime_pc.json"


class BattleResolutionError(RuntimeError):
    def __init__(self, code, **detail):
        # Core's exception envelope retains str(error), so preserve the observed
        # outcome even if the later callback-page confirmation fails.
        super().__init__(f"{code}: {json.dumps(detail, ensure_ascii=False)}")
        self.code, self.detail = code, detail


def callback_page(items):
    """Require current, positive page evidence, never mere settlement absence."""
    if find_text(items, "作战终端", region=(1100, 380, 180, 140)):
        return "main"
    if find_text(items, "协同终端", region=(0, 0, 1280, 180)) and find_text(items, "全域整备", region=(0, 0, 1280, 180)):
        return "battle_terminal"
    if find_text(items, ("前往作战", "前往战斗"), exact=True):
        return "mission_preparation"
    if find_text(items, ("开始作战", "开始战斗", "START BATTLE"), exact=True, region=(800, 250, 480, 470)):
        if find_text(items, "自动战斗", region=(650, 500, 350, 180)):
            return "structural_detail"
        return "battle_preparation"
    if find_text(items, "编队", region=(0, 0, 1280, 180)) and find_text(items, ("开始", "作战"), exact=True, region=(1050, 250, 230, 470)):
        return "battle_preparation"
    if find_text(items, "开启自动讨伐") and find_text(items, "全域勘测"):
        return "world_scan"
    return None


def _outcome(items, titles):
    hit = {}
    for key in ("victory", "defeat", "timeout"):
        candidates = [row for row in items if row.get("confidence", 0) >= .75]
        hit[key] = find_text(candidates, titles[key], exact=True)
    if hit["timeout"]:
        raise BattleResolutionError("battle_game_timeout", title=hit["timeout"]["text"])
    if hit["victory"] and hit["defeat"]:
        raise BattleResolutionError("battle_outcome_ambiguous")
    return "victory" if hit["victory"] else "defeat" if hit["defeat"] else None


async def _ready(ui, *, timeout_sec, titles, callback_ready=None):
    budget = ui.operation().child(float(timeout_sec))
    previous, stable = None, 0
    try:
        while True:
            budget.checkpoint()
            with budget.activate():
                _frame, items = await ui.read()
            page = None if _outcome(items, titles) or find_text(items, titles["next"], exact=True) else (callback_ready or callback_page)(items)
            stable = stable + 1 if page and page == previous else 1 if page else 0
            previous = page
            budget.checkpoint()
            if stable >= 2:
                return page
            await budget.sleep_async(.35)
    except OperationTimeout as exc:
        ui.scope.checkpoint()
        raise BattleResolutionError("battle_callback_page_unconfirmed", last_page=previous) from exc


def _capture_number(items, region):
    x, y, w, h = region
    rows = sorted((row for row in items if x <= row["center"][0] < x+w and y <= row["center"][1] < y+h), key=lambda row: row["center"][0])
    for row in rows:
        match = re.fullmatch(r"(\d+)(?:[/／](\d+))?", compact(row["text"]))
        if match:
            return int(match.group(1))
    return None


async def _capture_prisoners(ui, frame, arrest_hit, requested, *, config, timeout_sec, interval):
    """Fresh +/- observations and a displayed count authorize each adjustment."""
    await ui.click(frame, arrest_hit["center"])
    budget = ui.operation().child(float(timeout_sec))
    arrest = config["arrest"]
    count, reset_done, adjustments, unchanged = None, False, 0, 0
    while True:
        budget.checkpoint()
        with budget.activate():
            frame, items = await ui.read()
        confirm = find_text(items, ("确认", "确定"), exact=True, region=(600, 450, 680, 200))
        minimum = find_text(items, arrest["min_text"], exact=True, region=arrest["min_region"])
        plus = find_text(items, arrest["plus_text"], exact=True, region=arrest["plus_region"])
        if confirm and minimum and plus:
            if not reset_done:
                with budget.activate():
                    await ui.click(frame, minimum["center"])
                reset_done = True
            else:
                observed = _capture_number(items, arrest["count_region"])
                if observed is None or observed < 1:
                    raise BattleResolutionError("capture_quantity_unreadable")
                if count is None and observed != 1:
                    # Min was submitted, but its effect has not been observed.
                    await budget.sleep_async(interval)
                    continue
                if count is not None and observed not in (count, count + 1):
                    raise BattleResolutionError("capture_quantity_changed_unexpectedly", before=count, after=observed)
                # The native control clamps to available prisoners/items. A
                # confirmed unchanged value ends adjustment instead of looping.
                unchanged = unchanged + 1 if count is not None and observed == count and adjustments > 0 else 0
                if unchanged == 1:
                    await budget.sleep_async(interval)
                    continue
                capped = unchanged >= 2
                if observed >= requested or capped:
                    with budget.activate():
                        await ui.click(frame, confirm["center"])
                    return {"requested": requested, "selected": observed, "available": True}
                count = observed
                adjustments += 1
                if adjustments > requested:
                    raise BattleResolutionError("capture_quantity_adjustment_exhausted")
                with budget.activate():
                    await ui.click(frame, plus["center"])
        elif not reset_done and find_text(items, config["titles"]["next"], exact=True):
            # A missing arrest dialog is not permission to wait forever for a
            # button which may be unavailable on this victory.
            if budget.remaining is not None and budget.remaining < max(1., float(timeout_sec) - 2.):
                return {"requested": requested, "selected": 0, "available": False}
        await budget.sleep_async(interval)


async def settle_battle(ui, *, capture_count=None, timeout_sec=600., poll_interval_sec=.5,
                        confirm_timeout_sec=30., exit_hint_timeout_sec=30., short_interval_sec=.5,
                        callback_ready=None):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    titles = config["titles"]
    if capture_count is not None and (isinstance(capture_count, bool) or int(capture_count) < 1):
        raise ValueError("capture_count must be a positive integer")
    poll = max(.1, min(float(poll_interval_sec), 1.))
    short = max(.1, float(short_interval_sec))
    budget = ui.operation().child(float(timeout_sec))
    previous, stable, outcome = None, 0, None
    events = []
    try:
        while True:
            budget.checkpoint()
            with budget.activate():
                frame, items = await ui.read()
            observed = _outcome(items, titles)
            next_hit = find_text(items, titles["next"], exact=True)
            arrest_hit = find_text(items, titles["arrest"], exact=True)
            key = observed if observed and (next_hit or arrest_hit) else None
            stable = stable + 1 if key and key == previous else 1 if key else 0
            previous = key
            budget.checkpoint()
            if stable >= 2:
                outcome = observed
                logger.info("[BattleSettlement] outcome_observed=%s frame=%s", outcome, frame.id)
                break
            await budget.sleep_async(poll)
    except OperationTimeout as exc:
        ui.scope.checkpoint()
        raise BattleResolutionError("battle_outcome_unconfirmed", last_outcome=previous) from exc

    capture = {"requested": capture_count, "selected": 0, "available": bool(arrest_hit)}
    branch = "next_step"
    if outcome == "victory" and capture_count is not None and arrest_hit:
        capture = await _capture_prisoners(ui, frame, arrest_hit, int(capture_count), config=config,
                                           timeout_sec=confirm_timeout_sec, interval=short)
        branch = "capture" if capture["available"] else "capture_unavailable"
        events.append({"event": branch, **capture})
    elif capture_count is not None:
        branch = "capture_unavailable" if outcome == "victory" else "defeat_next"

    transition = ui.operation().child(float(exit_hint_timeout_sec))
    next_clicked, reward_clicked = False, False
    previous, stable = None, 0
    try:
        while True:
            transition.checkpoint()
            with transition.activate():
                frame, items = await ui.read()
            current_outcome = _outcome(items, titles)
            next_hit = find_text(items, titles["next"], exact=True)
            exit_hit = find_text(items, ("触碰空白区域退出", "点击空白区域退出"))
            if exit_hit and not reward_clicked:
                with transition.activate():
                    await ui.click(frame, exit_hit["center"])
                reward_clicked = True
                events.append({"event": "dismiss_arrest_result"})
            elif next_hit and current_outcome == outcome and not next_clicked:
                with transition.activate():
                    await ui.click(frame, next_hit["center"])
                next_clicked = True
                events.append({"event": "next_step", "outcome": outcome})
            elif current_outcome is not None and current_outcome != outcome:
                raise BattleResolutionError("battle_outcome_changed", expected=outcome, observed=current_outcome)
            page = None if current_outcome or next_hit or exit_hit else (callback_ready or callback_page)(items)
            stable = stable + 1 if page and previous == page else 1 if page else 0
            previous = page
            transition.checkpoint()
            if stable >= 2:
                return {"success": True, "ok": True, "outcome": outcome, "battle_started": True,
                        "page_after": page, "ready_for_next": True, "branch": branch,
                        "capture": capture, "events": events}
            await transition.sleep_async(short)
    except OperationTimeout as exc:
        ui.scope.checkpoint()
        raise BattleResolutionError("battle_settlement_return_unconfirmed", outcome=outcome,
                                    next_clicked=next_clicked, reward_clicked=reward_clicked, events=events) from exc


async def detect_stamina(ui, vision, *, dialog_region, dialog_template, dialog_threshold,
                         cancel_region, cancel_template, cancel_threshold, timeout_sec,
                         interval_sec, stable_scans, dismiss_timeout_sec, dismiss_stable_scans,
                         after_click_sec, use_grayscale):
    """Reuse existing stamina templates; dismissal and callback are observations."""
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for path in (dialog_template, cancel_template):
        if not path.is_file():
            raise FileNotFoundError(path)
    interval = max(.01, float(interval_sec))
    deadline = time.monotonic() + max(0., float(timeout_sec))
    stable, absent, clicked = 0, 0, False
    scan = 0
    dismiss_deadline = None
    while True:
        ui.scope.checkpoint()
        frame = await ui.capture()
        async def match(region, template, threshold):
            x, y, w, h = region
            return await ui.operation().run_async(vision.find_template, source_image=frame.rgb_image()[y:y+h, x:x+w],
                template_image=str(template), threshold=float(threshold), use_grayscale=bool(use_grayscale))
        hit = await match(dialog_region, dialog_template, dialog_threshold)
        scan += 1
        if clicked:
            absent = absent + 1 if not hit.found else 0
            if absent >= max(1, int(dismiss_stable_scans)):
                page = await _ready(ui, timeout_sec=max(3., float(dismiss_timeout_sec)), titles=config["titles"])
                return {"success": True, "outcome": "insufficient_stamina", "insufficient_stamina": True,
                        "battle_started": False, "page_after": page, "ready_for_next": True,
                        "dialog_detected": True, "dialog_closed": True, "cancel_clicked": True,
                        "branch": "stamina_cancel", "scans": scan}
            if time.monotonic() >= dismiss_deadline:
                raise BattleResolutionError("stamina_dialog_dismiss_timeout")
        else:
            stable = stable + 1 if hit.found else 0
            if stable >= max(1, int(stable_scans)):
                cancel = await match(cancel_region, cancel_template, cancel_threshold)
                if not cancel.found or cancel.center_point is None:
                    raise BattleResolutionError("stamina_cancel_button_unconfirmed")
                x, y, _w, _h = cancel_region
                await ui.click(frame, (x+cancel.center_point[0], y+cancel.center_point[1]))
                clicked = True
                dismiss_deadline = time.monotonic() + max(float(dismiss_timeout_sec), .1)
                await ui.sleep(after_click_sec)
            elif time.monotonic() >= deadline:
                return {"outcome": "normal", "insufficient_stamina": False, "dialog_detected": False,
                        "cancel_clicked": False, "dialog_closed": True, "scans": scan}
        await ui.sleep(interval)

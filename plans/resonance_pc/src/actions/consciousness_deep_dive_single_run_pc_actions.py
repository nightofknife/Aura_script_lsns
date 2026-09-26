"""One-run Deep Dive board test: observe, make one guarded choice, and stop at settlement."""

from __future__ import annotations

import asyncio
import copy
import math
from datetime import datetime
from pathlib import Path
import random
import re
import secrets
import time
from typing import Any
from uuid import uuid4

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException

from ....aura_base.src.actions.input_actions import click as aura_click
from ._deep_dive_single_run_vision import observe
from . import _deep_dive_event_flow as event_flow
from . import _deep_dive_vortex_flow as vortex_flow
from . import _deep_dive_node_rewards as node_rewards
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


SCHEMA = "resonance_pc.deep_dive_single_run.v1"
POLL_SECONDS = .35
PHASE_TIMEOUTS = {
    "player_ready": 15,
    "move_options": 15,
    "move_followup": 30,
    "move_entry": 15,
    "event_wait": 18,
    "event_dispatch": 180,
    "vortex_dispatch": 180,
    "empty_wait": 30,
    "rotate_ready": 20,
    "rotate_options": 15,
    "rotate_preview": 15,
    "rotate_commit": 45,
    "rotate_next_wait": 180,
    "enemy_wait": 180,
    "budget_wait": 90,
    "battle_prepare": 45,
    "battle_wait": 600,
    "battle_loot": 30,
    "battle_selection_confirm": 20,
    "battle_obtain": 30,
    "battle_notice_wait": 30,
    "shop_wait": 30,
    "shop_return": 30,
}


def _cancelled() -> None:
    if is_current_task_cancel_requested():
        raise StopTaskException("识海深潜单局测试已取消。", success=False)


def _session_key() -> str:
    return f"deep_dive_single_run:{uuid4().hex}"


def _diagnostic_path(session_key: str, phase: str) -> Path:
    run_id = session_key.rsplit(":", 1)[-1]
    directory = resolve_base_path() / "logs" / "deep_dive_single_run" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return directory / f"{stamp}_{phase}.png"


def _save_frame(image_rgb: Any, session_key: str, phase: str) -> str:
    path = _diagnostic_path(session_key, phase)
    if not cv2.imwrite(str(path), cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)):
        raise RuntimeError(f"无法保存识海深潜测试截图：{path}")
    return str(path)


def _capture(app: Any, event_family: str = 'healing') -> tuple[Any, dict]:
    capture = app.capture()
    if not getattr(capture, "success", False) or getattr(capture, "image", None) is None:
        return None, {"valid": False, "scene": "capture_failed", "error": str(getattr(capture, "error_message", ""))}
    image = capture.image
    return image, observe(image, event_family=event_family)


def _read_progress(image_rgb: Any, ocr: Any) -> dict | str:
    """Best-effort secondary objective progress, never a movement success signal."""
    if image_rgb is None or ocr is None:
        return "unknown"
    crop = image_rgb[245:292, 1000:1280]
    enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    recognized = ocr.recognize_all(source_image=enlarged)
    texts = [str(getattr(item, "text", "") or "") for item in getattr(recognized, "results", ())]
    combined = " ".join(texts).replace("／", "/")
    match = re.search(r"\(?\s*(\d+)\s*/\s*(\d+)\s*\)?", combined)
    if match:
        return {"current": int(match.group(1)), "total": int(match.group(2)), "text": combined}
    return "unknown"


def _change_phase(state: dict, phase: str) -> None:
    state["phase"] = phase
    state["phase_started"] = time.time()
    state.pop("battle_observation_signature", None)
    state.pop("battle_stable_frames", None)
    state.pop("shop_stable_frames", None)
    state.pop('mode_button_pending', None)


def _retry_mode_button(state: dict, observation: dict, kind: str):
    pending = state.get('mode_button_pending')
    if not pending or pending['kind'] != kind:
        return 'wait', None
    if time.time()-pending['at'] < 2:
        return 'wait', None
    # A blue button already accepted the click, even while tile/arrow animation
    # prevents candidate recognition. Only retry the normal board state.
    cyan = observation.get('cyan_pixels')
    if (observation['scene'] != 'board' or not observation.get('player_turn')
            or not observation.get(kind+'_pending') or cyan is None
            or cyan[kind] >= 2000):
        return 'wait', None
    if pending['clicks'] >= 3:
        state['mode_button_error'] = ('移动' if kind=='move' else '旋转')+'按钮点击3次后仍未进入选择状态'
        return 'mode_button_blocked', None
    pending.update(at=time.time(), clicks=pending['clicks']+1)
    return 'retry_'+kind+'_button', [1100,406 if kind=='move' else 480]


def _choose(options: list[dict], state: dict) -> dict:
    choices = list(options)
    last = state.get("last_choice")
    if len(choices) > 1 and last:
        distinct = [row for row in choices if row["point"] != last]
        if distinct:
            choices = distinct
    rng = random.Random(int(state["random_seed"]) + int(state["choices_made"]))
    return rng.choice(choices)


async def _click(app: Any, point: list[int]) -> None:
    _cancelled()
    x, y = map(int, point)
    if not (0 <= x < 1280 and 0 <= y < 720):
        raise ValueError(f"识海深潜点击位置越界：{point}")
    await asyncio.to_thread(aura_click, app=app, x=x, y=y)
    _cancelled()


def _block(state: dict, image: Any, reason: str, observation: dict) -> None:
    state["status"] = "blocked"
    state["reason"] = reason
    state["last_scene"] = observation.get("scene", "unknown")
    state["last_scores"] = observation.get("scores", {})
    if image is not None:
        state["last_frame"] = _save_frame(image, state["session_key"], "blocked")
    logger.warning("[DeepDiveSingleRun] blocked phase=%s reason=%s frame=%s", state["phase"], reason, state.get("last_frame"))


def _finish_move(state: dict) -> None:
    move = state.pop("pending_move", None)
    if move is None:
        raise RuntimeError("移动完成时缺少待确认的目标")
    move["confirmed_at"] = time.time()
    state["move_history"].append(move)
    _change_phase(state, "rotate_ready")


def _empty_step(state: dict, observation: dict) -> tuple[str, list[int] | None]:
    """No event UI: verify entry disappearance and board UI, never pixel stillness."""
    now = time.time()
    scene = observation['scene']
    if scene == 'event_entry' and observation.get('event_type') == 'empty':
        state['empty_board_frames'] = 0
        state['empty_entry_frames'] = state.get('empty_entry_frames', 0) + 1
        if state['empty_entry_frames'] >= 2 and now-state['empty_last_click'] >= 2:
            if state['empty_clicks'] >= 3:
                state['event_error'] = '空白节点进入点击3次后预览卡仍未关闭'
                return 'event_blocked', None
            state['empty_clicks'] += 1
            state['empty_last_click'] = now
            state['empty_entry_frames'] = 0
            return 'retry_empty_enter', list(observation['click'])
        return 'wait', None
    state['empty_entry_frames'] = 0
    ready = (scene in {'board', 'choose_rotate'} and observation.get('event_board')
             and observation.get('player_turn'))
    state['empty_board_frames'] = state.get('empty_board_frames', 0) + 1 if ready else 0
    if state['empty_board_frames'] >= 3 and now-state['empty_last_click'] >= 5:
        state['events'].append({'type':'empty', 'status':'completed',
                                'entry_clicks':state['empty_clicks']})
        _finish_move(state)
        return 'event_completed', None
    return 'wait', None


def _battle_step(state: dict, observation: dict) -> tuple[str, list[int] | None]:
    """One verified transition in the existing task's battle branch."""
    scene = observation["scene"]
    cards = observation.get("cards", [])
    signature = repr((scene, observation.get("selected_count"),
                      [(row["point"], row["selected"]) for row in cards],
                      observation.get("move_done"), observation.get("player_turn")))
    if signature == state.get("battle_observation_signature"):
        state["battle_stable_frames"] = state.get("battle_stable_frames", 0) + 1
    else:
        state["battle_observation_signature"] = signature
        state["battle_stable_frames"] = 1
    if state["battle_stable_frames"] < 2:
        return "wait", None

    phase = state["phase"]
    if phase == "battle_prepare" and scene == "battle_formation":
        _change_phase(state, "battle_wait")
        return "click_battle_start", list(observation["click"])
    if phase == "battle_wait" and scene == "battle_victory":
        state["battle_victory_confirmed"] = True
        _change_phase(state, "battle_loot")
        return "click_battle_next", list(observation["click"])
    if phase in {"battle_loot", "battle_notice_wait"} and scene == "battle_reward_selection":
        if len(cards) not in {1, 2, 3}:
            return "wait", None
        middle = cards[0 if len(cards) < 3 else 1]
        if not any(card["selected"] for card in cards):
            state["battle_reward_point"] = list(middle["point"])
            _change_phase(state, "battle_selection_confirm")
            return "click_middle_reward", list(middle["point"])
        if middle["selected"] and sum(card["selected"] for card in cards) == 1:
            _change_phase(state, "battle_obtain")
            return "click_reward_confirm", list(observation["click"])
    if phase == "battle_selection_confirm" and scene == "battle_reward_selection":
        if len(cards) in {1, 2, 3}:
            middle = cards[0 if len(cards) < 3 else 1]
            if (sum(card["selected"] for card in cards) == 1 and middle["selected"]
                    and middle["point"] == state.get("battle_reward_point")):
                _change_phase(state, "battle_obtain")
                return "click_reward_confirm", list(observation["click"])
    if phase in {"battle_loot", "battle_obtain"} and scene == "reward_obtained":
        state["battle_reward_pages"] = state.get("battle_reward_pages", 0) + 1
        _change_phase(state, "battle_notice_wait")
        return "dismiss_battle_reward", list(observation["click"])
    if (phase == "battle_notice_wait" and scene in {"board", "choose_rotate"}
            and observation.get("player_turn") and observation.get("move_done")
            and time.time() - state["phase_started"] >= 1.0):
        state["events"].append({"type": state["pending_move"]["event"],
                                "status": "completed", "battle_result": "victory",
                                "reward_pages": state.get("battle_reward_pages", 0)})
        _finish_move(state)
        return "battle_completed", None
    return "wait", None


def _advance_state(state: dict, observation: dict) -> tuple[str, list[int] | None]:
    """Pure state transition; return one guarded click target when appropriate."""
    scene = observation["scene"]
    phase = state["phase"]
    if scene == "settlement":
        if phase in {"rotate_commit", "rotate_next_wait"} and state.get("pending_rotation") is not None:
            state["turns_completed"] += 1
            state["rotations"].append({"turn": state["turns_completed"],
                                       "point": state.pop("pending_rotation"), "confirmed_at": time.time()})
        state["status"] = "completed"
        state["outcome"] = observation.get("outcome", "unknown")
        return "settlement", None
    if not observation.get("valid"):
        if state.get('node_reward'):
            state['node_reward']['stable']=0
            state['node_reward']['signature']=None
        if phase == 'vortex_dispatch':
            vortex_flow.reset_observation(state)
        if phase == 'empty_wait':
            state['empty_board_frames'] = 0
            state['empty_entry_frames'] = 0
        if phase == 'event_dispatch':
            event_flow.reset_observation(state)
        if phase.startswith("battle_"):
            state.pop("battle_observation_signature", None)
            state["battle_stable_frames"] = 0
        if phase.startswith("shop_"):
            state["shop_stable_frames"] = 0
        return "wait", None
    extra_reward = node_rewards.step(state, observation, time.time())
    if extra_reward is not None:
        return extra_reward
    if phase == 'empty_wait':
        return _empty_step(state, observation)
    if phase == 'vortex_dispatch':
        action, point = vortex_flow.step(state, observation, time.time())
        if action == 'vortex_returned':
            state['events'].append({'type':'vortex','status':'completed',
                                    'trace':state['vortex_trace']})
            _finish_move(state)
            return 'event_completed', None
        return action, point
    if phase == 'event_dispatch':
        action, point = event_flow.step(state, observation, time.time())
        if action == 'event_returned':
            state['events'].append({'type':state['pending_move']['event'],
                                    'status':'completed','trace':state['event_trace']})
            _finish_move(state)
            return 'event_completed', None
        return action, point
    if phase.startswith("battle_"):
        return _battle_step(state, observation)
    if phase in {"shop_wait", "shop_return"}:
        ready = (scene == "shop" if phase == "shop_wait" else
                 scene in {"board", "choose_rotate"} and observation.get("player_turn")
                 and observation.get("move_done")
                 and (observation.get("rotate_pending") or scene == "choose_rotate"))
        state["shop_stable_frames"] = state.get("shop_stable_frames", 0) + 1 if ready else 0
        if state["shop_stable_frames"] < 2:
            return "wait", None
        if phase == "shop_wait":
            _change_phase(state, "shop_return")
            return "click_shop_back", list(observation["click"])
        if time.time() - state["phase_started"] >= 1.0:
            state["events"].append({"type": "shop", "status": "completed", "purchased": False})
            _finish_move(state)
            return "shop_completed", None
        return "wait", None

    if phase == "player_ready":
        if scene == "choose_move":
            _change_phase(state, "move_options")
            return "transition", None
        if scene == "board" and observation["player_turn"] and observation["move_pending"]:
            _change_phase(state, "move_options")
            state['mode_button_pending'] = {'kind':'move','at':time.time(),'clicks':1}
            return "click_move_button", [1100, 406]
    elif phase == "move_options":
        if scene == "choose_move":
            chosen = _choose(observation["options"], state)
            state["pending_move"] = {
                "turn": state["turns_completed"] + 1,
                "point": list(chosen["point"]),
                "options": [list(row["point"]) for row in observation["options"]],
                "selected_at": time.time(),
                "event": None,
            }
            state["last_choice"] = list(chosen["point"])
            state["choices_made"] += 1
            _change_phase(state, "move_followup")
            return "click_move_option", list(chosen["point"])
        return _retry_mode_button(state, observation, 'move')
    elif phase == "move_followup":
        if scene == "event_entry":
            if observation.get("event_type") not in {"item", "vortex", "battle", "elite_battle", "shop", "healing", "empty"}:
                state["pending_move"]["event"] = observation.get("entry_kind")
                return "unsupported_entry", None
            state["pending_move"]["event"] = observation["event_type"]
            _change_phase(state, "move_entry")
            return "transition", None
        if scene in {"board", "choose_rotate"} and observation["player_turn"] and observation["move_done"]:
            _finish_move(state)
            return "move_confirmed", None
        move = state.get('pending_move')
        if (scene == 'choose_move' and move and not move.get('retry_at')
                and time.time()-move['selected_at'] >= 2.):
            # The camera may settle after the first click. Re-locate the same
            # nearby tile; never randomly choose a different target for retry.
            candidates = sorted(observation.get('options', []),
                                key=lambda r: math.dist(r['point'], move['point']))
            if candidates and math.dist(candidates[0]['point'], move['point']) <= 100:
                move['retry_at'] = time.time()
                move['retry_point'] = list(candidates[0]['point'])
                return 'retry_move_option', list(move['retry_point'])
    elif phase == "move_entry":
        if scene == "event_entry" and observation["event_type"] == state["pending_move"]["event"]:
            if observation['event_type'] == 'empty':
                _change_phase(state, 'empty_wait')
                state.update(empty_last_click=time.time(), empty_clicks=1,
                             empty_board_frames=0, empty_entry_frames=0)
                return 'click_empty_enter', list(observation['click'])
            if observation["event_type"] == "shop":
                _change_phase(state, "shop_wait")
                return "click_shop_node_enter", list(observation["click"])
            if observation["event_type"] in {"battle", "elite_battle"}:
                state["battle_reward_pages"] = 0
                state["battle_victory_confirmed"] = False
                _change_phase(state, "battle_prepare")
                return "click_battle_node_enter", list(observation["click"])
            if observation['event_type'] in {'healing','item'}:
                _change_phase(state,'event_dispatch')
                event_flow.start(state,observation,time.time())
                return 'click_event_enter',list(observation['click'])
            if observation['event_type'] == 'vortex':
                _change_phase(state,'vortex_dispatch')
                vortex_flow.start(state,observation,time.time())
                return 'click_event_enter',list(observation['click'])
            _change_phase(state, "event_wait")
            state["event_board_seen"] = 0
            return "click_event_enter", list(observation["click"])
    elif phase == "event_wait":
        # Known entry cards share a button. Post-entry pages are deliberately not guessed.
        if scene == "event_vortex_choice":
            return "unsupported_event", None
        if (time.time() - state["phase_started"] >= 5.0 and observation["player_turn"]
                and observation["move_done"] and scene in {"board", "choose_rotate"}):
            state["event_board_seen"] = state.get("event_board_seen", 0) + 1
            if state["event_board_seen"] >= 3:
                state["events"].append({"type": state["pending_move"]["event"], "status": "returned_to_board"})
                _finish_move(state)
                return "event_completed", None
        else:
            state["event_board_seen"] = 0
    elif phase == "rotate_ready":
        if scene == "choose_rotate":
            _change_phase(state, "rotate_options")
            return "transition", None
        if scene == "board" and observation["player_turn"] and observation["rotate_pending"]:
            _change_phase(state, "rotate_options")
            state['mode_button_pending'] = {'kind':'rotate','at':time.time(),'clicks':1}
            return "click_rotate_button", [1100, 480]
    elif phase == "rotate_options":
        if scene == "choose_rotate":
            chosen = _choose(observation["options"], state)
            state["pending_rotation"] = list(chosen["point"])
            state["choices_made"] += 1
            _change_phase(state, "rotate_preview")
            return "click_rotate_option", list(chosen["point"])
        return _retry_mode_button(state, observation, 'rotate')
    elif phase == "rotate_preview":
        if scene == "rotate_preview":
            _change_phase(state, "rotate_commit")
            state['rotation_confirm_clicks'] = 1
            state['rotation_last_click'] = time.time()
            return "click_rotate_confirm", list(observation["click"])
    elif phase in {"rotate_commit", "rotate_next_wait"}:
        if phase == 'rotate_commit':
            if time.time()-state['rotation_last_click'] < 1.:
                return 'wait', None
            present = observation.get('scores', {}).get('rotate_confirm',
                                                       1. if scene=='rotate_preview' else 0.) >= .8
            if present:
                if state['rotation_confirm_clicks'] >= 3:
                    state['rotation_error'] = '旋转确认点击3次后，确认模板仍存在'
                    return 'rotation_blocked', None
                state['rotation_confirm_clicks'] += 1
                state['rotation_last_click'] = time.time()
                return 'retry_rotate_confirm', list(observation.get('rotate_confirm_point') or observation['click'])
            _change_phase(state, 'rotate_next_wait')
        if (scene == 'board' and observation.get('player_turn') and observation.get('move_pending')
                and observation.get('rotate_pending')):
            state["turns_completed"] += 1
            state["rotations"].append({"turn": state["turns_completed"], "point": state.pop("pending_rotation", None), "confirmed_at": time.time()})
            _change_phase(state, "budget_wait" if state["turns_completed"] >= state["round_budget"] else "enemy_wait")
            return "rotation_confirmed", None
    elif phase == "enemy_wait":
        if observation["player_turn"] and observation["move_pending"] and observation["rotate_pending"] and scene == "board":
            _change_phase(state, "player_ready")
            return "next_turn", None
    return "wait", None


@action_info(name="resonance_pc.deep_dive_single_run_initialize", public=True, read_only=False,
             description="Start a guarded Deep Dive board-only test session.")
@requires_services(app="plans/aura_base/app", state_store="core/state_store")
async def initialize_deep_dive_single_run(round_budget: int = 20, random_seed: int = 0,
                                          app: Any = None, state_store: Any = None) -> dict:
    if app is None or state_store is None:
        raise RuntimeError("app and state_store services are required")
    if isinstance(round_budget, bool) or int(round_budget) != round_budget or not 1 <= round_budget <= 100:
        raise ValueError("回合上限必须是 1 到 100 的整数")
    if isinstance(random_seed, bool) or int(random_seed) != random_seed:
        raise ValueError("随机种子必须是整数")
    round_budget = int(round_budget)
    random_seed = int(random_seed) or secrets.randbits(32)
    _cancelled()
    await asyncio.to_thread(resonance_pc_require_client_resolution, app=app)
    image, observed = await asyncio.to_thread(_capture, app)
    key = _session_key()
    state = {"schema": SCHEMA, "session_key": key, "status": "running", "phase": "player_ready",
             "phase_started": time.time(), "round_budget": round_budget, "random_seed": random_seed,
             "turns_completed": 0, "move_history": [], "events": [], "rotations": [],
             "choices_made": 0, "last_choice": None, "last_frame": "", "reason": "",
             "outcome": "unknown", "objective_progress": "unknown"}
    if not (observed.get("player_turn") and (
            observed.get("scene") == "choose_move" or
            observed.get("scene") == "board" and observed.get("move_pending"))):
        _block(state, image, "启动时未确认魔方玩家行动界面与未执行的移动步骤", observed)
    await state_store.set(key, copy.deepcopy(state))
    return {"success": True, "status": state["status"], "session_key": key, "phase": state["phase"],
            "last_frame": state["last_frame"]}


@action_info(name="resonance_pc.deep_dive_single_run_advance", public=True, read_only=False,
             description="Advance one guarded board, event, or rotation state.")
@requires_services(app="plans/aura_base/app", ocr="plans/aura_base/ocr", state_store="core/state_store")
async def advance_deep_dive_single_run(session_key: str, app: Any = None, ocr: Any = None,
                                       state_store: Any = None) -> dict:
    state = await state_store.get(session_key)
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or state.get("session_key") != session_key:
        raise RuntimeError("识海深潜单局测试会话不存在")
    if state["status"] != "running":
        return {"status": state["status"], "phase": state["phase"]}
    _cancelled()
    if state['phase'] == 'vortex_dispatch':
        image, observed = await asyncio.to_thread(_capture, app, 'vortex')
    else:
        image, observed = await asyncio.to_thread(_capture, app)
    action, point = _advance_state(state, observed)
    if action in {"move_confirmed", "event_completed", "battle_completed", "shop_completed"}:
        try:
            progress = await asyncio.to_thread(_read_progress, image, ocr)
            if progress != "unknown":
                state["objective_progress"] = progress
        except Exception as exc:
            logger.warning("[DeepDiveSingleRun] objective progress OCR unavailable: %s", exc)
    if action == "settlement":
        state["last_frame"] = await asyncio.to_thread(_save_frame, image, session_key, "settlement")
    elif action == "unsupported_event":
        await asyncio.to_thread(_block, state, image,
                                "unsupported_event: 地板塌陷事件选项尚未配置处理策略", observed)
    elif action == "unsupported_entry":
        await asyncio.to_thread(_block, state, image,
                                f"unsupported_entry: 已识别{observed.get('entry_kind')}节点，但该流程尚未接入",
                                observed)
    elif action == 'event_blocked':
        await asyncio.to_thread(_block, state, image, state['event_error'], observed)
    elif action == 'vortex_blocked':
        await asyncio.to_thread(_block, state, image, state['vortex_error'], observed)
    elif action == 'rotation_blocked':
        await asyncio.to_thread(_block, state, image, state['rotation_error'], observed)
    elif action == 'mode_button_blocked':
        await asyncio.to_thread(_block, state, image, state['mode_button_error'], observed)
    elif action == "wait":
        elapsed = time.time() - state["phase_started"]
        if elapsed >= PHASE_TIMEOUTS[state["phase"]]:
            reason = ("unsupported_event: 尚未识别进入后的事件页面" if state["phase"] == "event_wait"
                      else f"{state['phase']} 阶段在 {elapsed:.1f} 秒内未得到可确认的新画面")
            await asyncio.to_thread(_block, state, image, reason, observed)
        else:
            await asyncio.sleep(POLL_SECONDS)
    if point is not None:
        if state['phase'] in {'event_dispatch','vortex_dispatch'}:
            state['phase_started'] = time.time()
        # A fresh observation selected the target. A failed click leaves the task blocked,
        # rather than advancing the ledger or retrying a possibly accepted input blindly.
        try:
            await _click(app, point)
        except StopTaskException:
            raise
        except Exception as exc:
            await asyncio.to_thread(_block, state, image, f"{action} 点击失败：{exc}", observed)
    await state_store.set(session_key, copy.deepcopy(state))
    logger.info("[DeepDiveSingleRun] action=%s phase=%s scene=%s status=%s turns=%s",
                action, state["phase"], observed.get("scene"), state["status"], state["turns_completed"])
    return {"status": state["status"], "phase": state["phase"], "action": action,
            "scene": observed.get("scene"), "turns_completed": state["turns_completed"]}


@action_info(name="resonance_pc.deep_dive_single_run_finish", public=True, read_only=False,
             description="Return the verified settlement or blocked single-run result.")
@requires_services(app="plans/aura_base/app", state_store="core/state_store")
async def finish_deep_dive_single_run(session_key: str, app: Any = None,
                                      state_store: Any = None) -> dict:
    state = await state_store.get(session_key)
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise RuntimeError("识海深潜单局测试结果不存在")
    if state["status"] == "running":
        image, observed = await asyncio.to_thread(_capture, app)
        await asyncio.to_thread(_block, state, image, "任务循环达到安全迭代上限", observed)
    result = {key: copy.deepcopy(state[key]) for key in (
        "status", "outcome", "phase", "reason", "round_budget", "random_seed", "turns_completed",
        "move_history", "events", "rotations", "objective_progress", "last_frame")}
    result["pending_move"] = copy.deepcopy(state.get("pending_move"))
    result["last_scene"] = state.get("last_scene", "")
    result["last_scores"] = copy.deepcopy(state.get("last_scores", {}))
    result["success"] = state["status"] == "completed" and bool(state["last_frame"])
    result["terminal"] = "settlement" if result["success"] else None
    await state_store.delete(session_key)
    return result


__all__ = ["initialize_deep_dive_single_run", "advance_deep_dive_single_run", "finish_deep_dive_single_run"]

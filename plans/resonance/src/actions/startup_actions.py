from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.utils.exceptions import StopTaskException


DEFAULT_STARTUP_REGION = (0, 0, 1280, 720)

MAIN_MARKERS = (
    "访问城市",
    "访问地区",
    "启程",
    "STARTENGINE",
    "资产",
)
TITLE_MARKERS = (
    "SOLSTICESTUDIO",
    "FLASHPOINT",
    "健康游戏忠告",
    "健康游戏",
    "游戏忠告",
    "点击屏幕",
    "点击任意位置",
    "进入游戏",
    "开始游戏",
    "下载已经完成",
)
CITY_MARKERS = (
    "商会",
    "市政厅",
    "铁安局",
    "修格里城",
)
TRAIN_MARKERS = (
    "列车电力",
    "电力等级",
    "模块供电",
    "电力负荷",
    "车厢节数",
    "引擎核心",
)
OVERLAY_MARKERS = (
    "今日不再提示",
    "我知道了",
    "确定",
    "确认",
    "关闭",
    "同意",
)
INFO_PANEL_MARKERS = (
    "资讯",
    "公告",
    "触碰空白区域退出",
)
INFO_PANEL_STRONG_MARKERS = (
    "触碰空白区域退出",
)
EXTERNAL_WEB_MARKERS = (
    "MWEIBOCN",
    "微博认证",
    "官方微博",
    "精选",
    "微博",
    "超话",
    "相册",
    "客服",
)
UPDATE_MARKERS = (
    "更新",
    "下载",
    "补丁",
    "资源",
    "开始下载",
    "立即更新",
    "确认下载",
    "重新连接",
    "MB",
    "%",
)
LOGIN_MARKERS = (
    "验证码",
    "手机号",
    "账号登录",
    "实名认证",
    "密码",
)


def _coerce_region(region: Optional[List[int] | Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    if region is None:
        return DEFAULT_STARTUP_REGION
    if len(region) != 4:
        raise ValueError("region must contain four integers: x, y, width, height")
    return tuple(int(v) for v in region)  # type: ignore[return-value]


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s:：,，.。!！\-_/\\|·]+", "", str(text or "")).upper()


def _item_to_dict(item: Any, *, offset_x: int, offset_y: int) -> Dict[str, Any]:
    center = getattr(item, "center_point", None) or (0, 0)
    rect = getattr(item, "rect", None)
    text = str(getattr(item, "text", "") or "")
    return {
        "text": text,
        "norm": _normalize_text(text),
        "center": [int(center[0]) + offset_x, int(center[1]) + offset_y],
        "rect": [int(rect[0]) + offset_x, int(rect[1]) + offset_y, int(rect[2]), int(rect[3])] if rect else None,
        "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
    }


def _contains_marker(norm_text: str, markers: Tuple[str, ...]) -> Optional[str]:
    for marker in markers:
        if _normalize_text(marker) in norm_text:
            return marker
    return None


def _collect_hits(items: List[Dict[str, Any]], markers: Tuple[str, ...]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for item in items:
        marker = _contains_marker(str(item.get("norm") or ""), markers)
        if marker:
            hits.append(
                {
                    "marker": marker,
                    "text": item["text"],
                    "center": item["center"],
                    "confidence": item["confidence"],
                }
            )
    hits.sort(key=lambda row: float(row.get("confidence") or 0.0), reverse=True)
    return hits


def _detect_state(app: Any, ocr: Any, region: Optional[List[int] | Tuple[int, int, int, int]] = None) -> Dict[str, Any]:
    return resonance_detect_startup_state(region=list(_coerce_region(region)), app=app, ocr=ocr)


def _compact_state(result: Dict[str, Any], *, round_index: int, action: str = "detect") -> Dict[str, Any]:
    matched = result.get("matched") if isinstance(result.get("matched"), dict) else {}
    return {
        "round": int(round_index),
        "action": action,
        "state": str(result.get("state") or "unknown"),
        "main": bool(result.get("main")),
        "login_required": bool(result.get("login_required")),
        "item_count": int(result.get("item_count") or 0),
        "matched": {
            key: value[:2] if isinstance(value, list) else []
            for key, value in matched.items()
            if key in {"main", "title", "overlay", "login", "train", "city", "info_panel", "external_web", "update"}
        },
    }


def _first_overlay_hit(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    matched = result.get("matched") if isinstance(result.get("matched"), dict) else {}
    hits = matched.get("overlay")
    if isinstance(hits, list) and hits:
        first = hits[0]
        if isinstance(first, dict) and isinstance(first.get("center"), list) and len(first["center"]) >= 2:
            return first
    return None


@action_info(
    name="resonance.detect_startup_state",
    public=True,
    read_only=True,
    description="Detect Resonance startup/main/title/interior state with one OCR pass.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_detect_startup_state(
    region: Optional[List[int]] = None,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr service is required")

    region_tuple = _coerce_region(region)
    capture = app.capture(rect=region_tuple)
    if not capture.success:
        return {"ok": False, "state": "capture_failed", "main": False, "items": []}

    multi = ocr.recognize_all(source_image=capture.image)
    offset_x, offset_y = region_tuple[0], region_tuple[1]
    items = [_item_to_dict(item, offset_x=offset_x, offset_y=offset_y) for item in getattr(multi, "results", [])]

    main_hits = _collect_hits(items, MAIN_MARKERS)
    title_hits = _collect_hits(items, TITLE_MARKERS)
    city_hits = _collect_hits(items, CITY_MARKERS)
    train_hits = _collect_hits(items, TRAIN_MARKERS)
    overlay_hits = _collect_hits(items, OVERLAY_MARKERS)
    info_hits = _collect_hits(items, INFO_PANEL_MARKERS)
    info_strong_hits = _collect_hits(items, INFO_PANEL_STRONG_MARKERS)
    external_web_hits = _collect_hits(items, EXTERNAL_WEB_MARKERS)
    update_hits = _collect_hits(items, UPDATE_MARKERS)
    login_hits = _collect_hits(items, LOGIN_MARKERS)

    main = bool(main_hits)
    title = bool(title_hits) and not main
    train = bool(train_hits) and not main
    city = bool(city_hits) and not main and not train
    overlay = bool(overlay_hits) and not main
    info_panel = bool(info_strong_hits) or (
        _collect_hits(items, ("资讯",)) and _collect_hits(items, ("公告",))
    )
    info_panel = bool(info_panel) and not main
    external_web = bool(external_web_hits) and not main
    update = bool(update_hits) and not main and not external_web and not title
    login_required = bool(login_hits) and not main

    if main:
        state = "main"
    elif login_required:
        state = "login_required"
    elif external_web:
        state = "external_web"
    elif train:
        state = "train"
    elif city:
        state = "city"
    elif info_panel:
        state = "info_panel"
    elif update:
        state = "update"
    elif title:
        state = "title"
    elif overlay:
        state = "overlay"
    else:
        state = "unknown"

    logger.debug(
        "[DetectStartupState] state=%s main=%s title=%s city=%s train=%s overlay=%s info_panel=%s external_web=%s update=%s login=%s items=%s",
        state,
        main,
        title,
        city,
        train,
        overlay,
        info_panel,
        external_web,
        update,
        login_required,
        [{"text": item["text"], "center": item["center"], "confidence": round(item["confidence"], 4)} for item in items],
    )
    return {
        "ok": True,
        "state": state,
        "main": main,
        "title": title,
        "city": city,
        "train": train,
        "overlay": overlay,
        "info_panel": info_panel,
        "external_web": external_web,
        "update": update,
        "login_required": login_required,
        "matched": {
            "main": main_hits[:3],
            "title": title_hits[:3],
            "city": city_hits[:3],
            "train": train_hits[:3],
            "overlay": overlay_hits[:3],
            "info_panel": info_hits[:3],
            "external_web": external_web_hits[:3],
            "update": update_hits[:3],
            "login": login_hits[:3],
        },
        "item_count": len(items),
        "items": items[:80],
    }


@action_info(
    name="resonance.enter_main",
    public=True,
    read_only=False,
    timeout=900,
    description="Launch or resume Resonance and settle on the main screen with an internal polling loop.",
)
@requires_services(
    app="plans/aura_base/app",
    ocr="plans/aura_base/ocr",
)
def resonance_enter_main(
    launch_from_home: bool = True,
    max_settle_rounds: int = 300,
    round_interval_sec: float = 1.0,
    main_stable_sec: float = 3.0,
    fail_if_login_required: bool = True,
    android_package: str = "com.hermes.goda",
    click_x: int = 640,
    click_y: int = 560,
    info_close_x: int = 640,
    info_close_y: int = 675,
    region: Optional[List[int]] = None,
    app: Any = None,
    ocr: Any = None,
) -> Dict[str, Any]:
    if app is None or ocr is None:
        raise RuntimeError("app/ocr service is required")

    max_rounds = max(int(max_settle_rounds), 1)
    interval = max(float(round_interval_sec), 0.0)
    stable_sec = max(float(main_stable_sec), 0.0)
    fixed_x = int(click_x)
    fixed_y = int(click_y)
    info_x = int(info_close_x)
    info_y = int(info_close_y)
    region_tuple = _coerce_region(region)
    history: List[Dict[str, Any]] = []
    launch_result: Optional[Dict[str, Any]] = None
    main_stable_since: Optional[float] = None

    initial = _detect_state(app, ocr, region_tuple)
    history.append(_compact_state(initial, round_index=-1, action="initial_detect"))
    logger.info("[EnterMain] initial_state=%s main=%s login=%s", initial.get("state"), initial.get("main"), initial.get("login_required"))

    if initial.get("main"):
        main_stable_since = time.monotonic()

    if bool(initial.get("login_required")) and bool(fail_if_login_required):
        raise StopTaskException("Resonance startup stopped: manual login or verification is required.", success=False)

    if bool(launch_from_home) and not initial.get("main"):
        launch_result = dict(app.launch_app(str(android_package or "").strip(), timeout_sec=10.0) or {})
        logger.info("[EnterMain] launch_result=%s", launch_result)
        time.sleep(1.0)

    last_state = initial
    for round_index in range(max_rounds):
        state = _detect_state(app, ocr, region_tuple)
        last_state = state
        history.append(_compact_state(state, round_index=round_index))
        logger.info(
            "[EnterMain] round=%s/%s state=%s main=%s login=%s items=%s",
            round_index + 1,
            max_rounds,
            state.get("state"),
            state.get("main"),
            state.get("login_required"),
            state.get("item_count"),
        )

        if state.get("main"):
            now = time.monotonic()
            if main_stable_since is None:
                main_stable_since = now
            stable_elapsed = now - main_stable_since
            history[-1]["action"] = "observe_main"
            history[-1]["main_stable_elapsed_sec"] = round(stable_elapsed, 3)
            logger.info(
                "[EnterMain] main detected; stable_elapsed=%.2fs required=%.2fs",
                stable_elapsed,
                stable_sec,
            )
            if stable_elapsed >= stable_sec:
                return {
                    "ok": True,
                    "reached_main": True,
                    "initial_state": initial.get("state"),
                    "final_state": state.get("state"),
                    "rounds": round_index + 1,
                    "launched": bool(launch_result),
                    "launch_result": launch_result,
                    "main_stable_sec": round(stable_elapsed, 3),
                    "history": history[-30:],
                }
            if interval > 0:
                time.sleep(interval)
            continue

        if main_stable_since is not None:
            logger.info("[EnterMain] main stability reset because state=%s", state.get("state"))
            main_stable_since = None

        if bool(state.get("login_required")) and bool(fail_if_login_required):
            raise StopTaskException("Resonance startup stopped: manual login or verification is required.", success=False)

        state_name = str(state.get("state") or "unknown")
        overlay_hit = _first_overlay_hit(state)
        if overlay_hit is not None:
            x, y = overlay_hit["center"][:2]
            app.click(x=int(x), y=int(y))
            history[-1]["action"] = "click_overlay"
            history[-1]["click"] = {"x": int(x), "y": int(y), "text": str(overlay_hit.get("text") or "")}
            logger.info("[EnterMain] clicked overlay text=%s at=(%s,%s)", overlay_hit.get("text"), x, y)
        elif state_name == "info_panel":
            app.click(x=info_x, y=info_y)
            history[-1]["action"] = "close_info_panel"
            history[-1]["click"] = {"x": info_x, "y": info_y}
            logger.info("[EnterMain] clicked info panel blank area at=(%s,%s)", info_x, info_y)
        elif state_name == "external_web":
            app.press_key("back")
            history[-1]["action"] = "press_back_external_web"
            history[-1]["key"] = "back"
            logger.info("[EnterMain] pressed back from external web page")
        elif state_name == "update":
            history[-1]["action"] = "wait_update"
            logger.info("[EnterMain] update/download state detected; waiting without fixed click")
        else:
            app.click(x=fixed_x, y=fixed_y)
            history[-1]["action"] = "click_fixed"
            history[-1]["click"] = {"x": fixed_x, "y": fixed_y}
            logger.info("[EnterMain] clicked fixed point at=(%s,%s)", fixed_x, fixed_y)

        if interval > 0:
            time.sleep(interval)

    final_state = _detect_state(app, ocr, region_tuple)
    history.append(_compact_state(final_state, round_index=max_rounds, action="final_detect"))
    final_stable_elapsed = (time.monotonic() - main_stable_since) if (final_state.get("main") and main_stable_since is not None) else 0.0
    if final_state.get("main") and final_stable_elapsed >= stable_sec:
        return {
            "ok": True,
            "reached_main": True,
            "initial_state": initial.get("state"),
            "final_state": final_state.get("state"),
            "rounds": max_rounds,
            "launched": bool(launch_result),
            "launch_result": launch_result,
            "main_stable_sec": round(final_stable_elapsed, 3),
            "history": history[-30:],
        }

    raise StopTaskException(
        f"Resonance main screen was not reached within {max_rounds} startup settle rounds. "
        f"Last state: {final_state.get('state') or last_state.get('state') or 'unknown'}; "
        f"stable main time: {final_stable_elapsed:.2f}s/{stable_sec:.2f}s.",
        success=False,
    )


@action_info(
    name="resonance.close_game",
    public=True,
    read_only=False,
    description="Force-stop the Resonance Android package through the active MuMu ADB runtime.",
)
@requires_services(app="plans/aura_base/app")
def resonance_close_game(
    android_package: str = "com.hermes.goda",
    timeout_sec: float = 10.0,
    app: Any = None,
) -> Dict[str, Any]:
    if app is None:
        raise RuntimeError("app service is required")
    package = str(android_package or "").strip()
    if not package:
        raise StopTaskException("Resonance close game failed: android package is empty.", success=False)

    result = dict(app.force_stop_app(package, timeout_sec=float(timeout_sec)) or {})
    logger.info("[CloseGame] force-stopped package=%s result=%s", package, result)
    return {"ok": True, **result}

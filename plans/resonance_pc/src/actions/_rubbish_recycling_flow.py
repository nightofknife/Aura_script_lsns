"""Bounded, observed transitions for rubbish recycling; no blind click retries."""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable, Mapping

from packages.aura_core.observability.logging.core_logger import logger

from ._rubbish_recycling_vision import observe_recycling_page


_POLL_SEC = 0.3
_POST_CLICK_SEC = 0.5
_CONFIRM_SEC = 3.0
_STAGE_TIMEOUT_SEC = 12.0
_FLOW_TIMEOUT_SEC = 90.0
_MAX_CLICKS = 3
_STABLE_FRAMES = 2
_STABLE_CENTER_TOLERANCE = 2
_REWARD_DISMISS_POINT = (500, 500)

_FAILURE_CODES = {
    "open_station": "rubbish_station_not_entered",
    "enter_recycle": "rubbish_recycle_entry_click_not_effective",
    "recycle_all": "rubbish_recycle_all_click_not_effective",
    "dismiss_reward": "rubbish_reward_dismiss_timeout",
    "return_station": "rubbish_station_menu_not_restored",
    "return_city": "rubbish_city_panel_not_restored",
}


class RubbishRecyclingError(RuntimeError):
    """A failed transition with its last observation and click history."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.detail = str(code), str(message), detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class _RecyclingFlow:
    def __init__(self, *, city: Mapping, station: Mapping, app: Any, vision: Any,
                 check_cancelled: Callable[[], None]):
        self.city, self.station = city, station
        self.app, self.vision, self.check_cancelled = app, vision, check_cancelled
        self.started = time.monotonic()
        self.deadline = self.started + _FLOW_TIMEOUT_SEC
        self.stage_deadlines: dict[str, float] = {}
        self.clicks: dict[str, list[dict]] = {}
        self.last_clicks: dict[str, float] = {}
        self.transitions: list[dict] = []
        self.last: dict = {"state": "unknown"}
        self.reward_seen = False

    def fail(self, stage: str, *, code: str | None = None, cause: Any = None):
        detail = {"stage": stage, "last_observation": self.last,
                  "clicks": self.clicks, "transitions": self.transitions,
                  "elapsed_ms": round((time.monotonic() - self.started) * 1000)}
        if cause is not None:
            detail["cause"] = cause
        code = code or _FAILURE_CODES[stage]
        logger.error("[RubbishFlow] phase=failed code=%s detail=%s", code, detail)
        raise RubbishRecyclingError(code, f"Rubbish recycling stage '{stage}' was not confirmed", detail)

    def check_deadline(self, stage: str, deadline: float):
        self.check_cancelled()
        now = time.monotonic()
        if now >= self.deadline:
            self.fail(stage, code="rubbish_flow_timeout")
        if now >= deadline:
            self.fail(stage)

    def sleep(self, seconds: float, stage: str, deadline: float):
        end = min(time.monotonic() + seconds, deadline, self.deadline)
        while time.monotonic() < end:
            self.check_cancelled()
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        self.check_deadline(stage, deadline)

    def observe(self, stage: str, deadline: float) -> dict:
        self.check_deadline(stage, deadline)
        try:
            observation = observe_recycling_page(
                app=self.app, vision=self.vision, city_key=str(self.city["city_key"]),
                station_point=self.station, check_cancelled=self.check_cancelled,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.check_cancelled()
            self.fail(stage, code=str(getattr(exc, "code", "rubbish_observation_failed")),
                      cause={"message": str(exc), "detail": getattr(exc, "detail", {})})
        self.last = observation
        # A result produced after a deadline cannot authorize input or success.
        self.check_deadline(stage, deadline)
        logger.debug("[RubbishFlow] phase=observe stage=%s observation=%s", stage, observation)
        return observation

    @staticmethod
    def same_page(previous: dict | None, current: dict) -> bool:
        if (previous is None or current["state"] == "unknown"
                or previous["state"] != current["state"]):
            return False
        old_center = (previous.get("match") or {}).get("center")
        new_center = (current.get("match") or {}).get("center")
        return (old_center is not None and new_center is not None
                and all(abs(old - new) <= _STABLE_CENTER_TOLERANCE
                        for old, new in zip(old_center, new_center)))

    def transition(self, stage: str, *, sources: set[str], targets: set[str],
                   control: str | None = None, point: tuple[int, int] | None = None,
                   retry_sec: float = _CONFIRM_SEC) -> dict:
        # Re-entering a stage after a reward must not reset its click/time budget.
        deadline = self.stage_deadlines.setdefault(
            stage, min(self.deadline, time.monotonic() + _STAGE_TIMEOUT_SEC))
        clicks = self.clicks.setdefault(stage, [])
        previous, stable = None, 0
        while True:
            observation = self.observe(stage, deadline)
            state = observation["state"]
            stable = stable + 1 if self.same_page(previous, observation) else int(state != "unknown")
            previous = observation
            if stable >= _STABLE_FRAMES:
                if state in targets:
                    if state == "reward":
                        self.reward_seen = True
                    result = {"stage": stage, "state": state, "observation": observation,
                              "clicks": list(clicks), "confirmations": stable}
                    self.transitions.append({"stage": stage, "state": state,
                                             "click_attempts": len(clicks)})
                    logger.info("[RubbishFlow] phase=confirmed stage=%s state=%s clicks=%s evidence=%s",
                                stage, state, len(clicks), observation.get("evidence"))
                    return result
                elapsed = time.monotonic() - self.last_clicks.get(stage, -math.inf)
                if state in sources and elapsed >= retry_sec:
                    if len(clicks) >= _MAX_CLICKS:
                        self.fail(stage)
                    match = (observation.get("evidence", {}).get(control, {})
                             if control else observation.get("match") or {})
                    if point is not None:
                        center = point
                    elif match.get("found") is True:
                        center = match.get("center")
                    else:
                        center = None
                    if center is not None:
                        if (len(center) != 2 or any(isinstance(value, bool)
                                or not isinstance(value, (int, float))
                                or not math.isfinite(value) for value in center)
                                or not (0 <= center[0] < 1280 and 0 <= center[1] < 720)):
                            self.fail(stage, code="rubbish_click_point_invalid")
                        self.check_deadline(stage, deadline)
                        try:
                            self.app.click(x=int(center[0]), y=int(center[1]))
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self.check_cancelled()
                            self.fail(stage, code="rubbish_click_failed", cause=str(exc))
                        clicked_at = time.monotonic()
                        clicks.append({"clicked": True, "x": int(center[0]), "y": int(center[1]),
                                       "source_state": state, "match": match,
                                       "reason": "initial" if not clicks else "source_page_still_visible",
                                       "elapsed_ms": round((clicked_at - self.started) * 1000)})
                        self.last_clicks[stage] = clicked_at
                        self.check_deadline(stage, deadline)
                        logger.info("[RubbishFlow] phase=click stage=%s attempt=%s state=%s detail=%s",
                                    stage, len(clicks), state, clicks[-1])
                        # No old coordinates, observations, or stable-frame counts survive input.
                        previous, stable = None, 0
                        self.sleep(_POST_CLICK_SEC, stage, deadline)
                        continue
            self.sleep(_POLL_SEC, stage, deadline)

    def run(self) -> dict:
        station_result = self.transition(
            "open_station", sources={"city_panel"},
            targets={"station_menu", "has_rubbish", "empty"},
            point=(int(self.station["x"]), int(self.station["y"])),
        )
        entered = self.transition(
            "enter_recycle", sources={"station_menu"}, targets={"has_rubbish", "empty"},
            control="entry",
        )
        initial_state = entered["state"]
        final = entered
        reward = None
        if initial_state == "has_rubbish":
            while True:
                final = self.transition("recycle_all", sources={"has_rubbish"},
                                        targets={"reward", "empty"}, control="enabled")
                if final["state"] == "empty":
                    break
                reward = self.transition("dismiss_reward", sources={"reward"},
                                         targets={"empty", "has_rubbish"},
                                         point=_REWARD_DISMISS_POINT, retry_sec=1.0)
                final = reward
                if final["state"] == "empty":
                    break
                # A dismissed reward with an enabled button is not completion.
                # Retry recycling under the original stage deadline and click cap.
        returned_station = self.transition(
            "return_station", sources={"empty"}, targets={"station_menu", "city_panel"}, control="back")
        if returned_station["state"] == "city_panel":
            returned_city = returned_station
        else:
            returned_city = self.transition("return_city", sources={"station_menu"},
                                            targets={"city_panel"}, control="back")
        entry_clicks = self.clicks.get("enter_recycle", [])
        recycle_clicks = self.clicks.get("recycle_all", [])
        back_clicks = self.clicks.get("return_station", []) + self.clicks.get("return_city", [])
        return {
            "success": True, "status": "sold" if initial_state == "has_rubbish" else "empty",
            "reason": None if initial_state == "has_rubbish" else "no_rubbish",
            "city_id": self.city["city_id"], "city_name": self.city["city_name"],
            "initial_state": initial_state, "final_state": "empty",
            "reward_overlay_seen": self.reward_seen, "page_state": "city_panel",
            "station": dict(self.station), "station_entry": station_result,
            "entry": entry_clicks[0]["match"] if entry_clicks else None,
            "entry_click": entry_clicks[-1] if entry_clicks else None,
            "entry_clicks": entry_clicks, "entry_rounds": entry_clicks,
            "recycle_click": recycle_clicks[-1] if recycle_clicks else None,
            "recycle_clicks": recycle_clicks,
            "reward": ({"success": True, "attempts": self.clicks.get("dismiss_reward", []),
                        "empty_match": final["observation"]["evidence"]["empty"]}
                       if reward is not None else None),
            "back_click": back_clicks[-1] if back_clicks else None, "back_clicks": back_clicks,
            "returned_city_panel": {"success": True, "page_state": "city_panel",
                                    "confirmation": "observed", "result": returned_city},
            "empty_confirmation": final, "transitions": self.transitions,
            "click_history": self.clicks,
            "elapsed_ms": round((time.monotonic() - self.started) * 1000),
        }


def run_rubbish_recycling(*, city: Mapping, station: Mapping, app: Any, vision: Any,
                          check_cancelled: Callable[[], None]) -> dict:
    return _RecyclingFlow(city=city, station=station, app=app, vision=vision,
                          check_cancelled=check_cancelled).run()

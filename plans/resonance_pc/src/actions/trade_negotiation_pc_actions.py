"""Template-driven bargaining and raising helpers for Resonance PC trading."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from ._bounded_observation import ObservationBudget, wait_for_observation
from ._template_observation import observe_template
from ._cancellable_wait import sleep_until_deadline


_CAP_REGION = (980, 430, 90, 50)
_BUTTON_REGION = (1090, 425, 170, 70)
_CAP_THRESHOLD = 0.88
_BUTTON_THRESHOLD = 0.86
_EXHAUSTED_THRESHOLD = 0.90
_EXHAUSTED_STABLE_MATCHES = 2
_POLL_INTERVAL_SEC = 0.2
_CAP_CONFIRMATION_INTERVAL_SEC = 0.2
_TOTAL_TIMEOUT_SEC = 180.0
_BUTTON_WAIT_TIMEOUT_SEC = 3.0
_ANIMATION_START_TIMEOUT_SEC = 3.0
_ANIMATION_FINISH_TIMEOUT_SEC = 15.0
DEFAULT_NEGOTIATION_MAX_ATTEMPTS = 5
MAX_NEGOTIATION_MAX_ATTEMPTS = 6
NEGOTIATION_ATTEMPT_FATIGUE = 8

_NEGOTIATION_CONFIG: Mapping[str, Mapping[str, str]] = {
    "bargain": {
        "button_template": "templates/commerce/trade/negotiation/buy/bargain_button.png",
        "cap_template": "templates/commerce/trade/negotiation/buy/cap20_digits.png",
        "exhausted_template": "templates/commerce/trade/negotiation/buy/rebargain_button.png",
    },
    "raise": {
        "button_template": "templates/commerce/trade/negotiation/sell/raise_button.png",
        "cap_template": "templates/commerce/trade/negotiation/sell/cap20_digits.png",
    },
}


class NegotiationExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Trade negotiation cancelled")


def _cancelable_sleep(seconds: float) -> None:
    sleep_until_deadline(seconds, sleep=time.sleep, monotonic=time.monotonic,
                         check_cancelled=_check_cancelled)



def _template_path(relative_path: str) -> Path:
    return Path(__file__).resolve().parents[2] / relative_path


def _match_template(
    *,
    app: Any,
    vision: Any,
    template: str,
    region: Tuple[int, int, int, int],
    threshold: float,
    require_center: bool = False,
) -> Dict[str, Any]:
    result = observe_template(
        app=app, vision=vision, template=_template_path(template), region=region,
        threshold=threshold, check_cancelled=_check_cancelled,
        require_center=require_center,
    )
    result["template"] = template
    if not result["valid"]:
        capture_failed = result.get("reason") == "capture_failed"
        raise NegotiationExecutionError(
            "negotiation_page_lost" if capture_failed else "negotiation_template_match_failed",
            "Unable to capture the negotiation page." if capture_failed
            else "Unable to obtain valid negotiation template evidence.",
            result,
        )
    return result


def _wait_for_template_state(
    *,
    app: Any,
    vision: Any,
    template: str,
    region: Tuple[int, int, int, int],
    threshold: float,
    expected_found: bool,
    timeout_sec: float,
    poll_interval_sec: float,
    exhausted_template: str | None = None,
    budget: ObservationBudget | None = None,
) -> Dict[str, Any]:
    budget = budget if budget is not None else ObservationBudget(
        timeout_sec, monotonic=time.monotonic, check_cancelled=_check_cancelled,
        immediate_first_probe=True,
    )
    exhausted_stable = 0

    def observe():
        last = _match_template(
            app=app,
            vision=vision,
            template=template,
            region=region,
            threshold=threshold,
            require_center=expected_found,
        )
        if exhausted_template and budget.active():
            exhausted = _match_template(
                app=app,
                vision=vision,
                template=exhausted_template,
                region=region,
                threshold=_EXHAUSTED_THRESHOLD,
            )
            return {**last, "exhausted_match": exhausted}
        return last

    def accept(last):
        nonlocal exhausted_stable
        exhausted = last.get("exhausted_match")
        if exhausted is not None:
            exhausted_stable = exhausted_stable + 1 if exhausted.get("found") else 0
            if exhausted_stable >= _EXHAUSTED_STABLE_MATCHES:
                last.update(found=False, attempts_exhausted=True)
                return True
        # A possible 再交涉 hit takes precedence over the common button
        # background until its second frame confirms the exhausted state.
        return (exhausted_stable == 0 and last.get("valid") is True
                and last.get("found") is expected_found)

    observed = wait_for_observation(
        observe=observe, accept=accept, budget=budget,
        poll_seconds=max(float(poll_interval_sec), 0.05), sleep=_cancelable_sleep,
    )
    if observed.matched:
        return {**observed.value, "state_confirmed": True}
    return {
        "valid": False, "found": None, "confidence": None, "reason": "observation_timeout",
        "template": template, "region": list(region), "state_confirmed": False,
        "last_match": observed.value,
    }


def _confirm_cap(
    *,
    app: Any,
    vision: Any,
    cap_template: str,
    confirmation_interval_sec: float,
    budget: ObservationBudget | None = None,
) -> Dict[str, Any]:
    if budget is not None and not budget.active():
        return {"confirmed": False, "confidence": 0.0, "matches": []}
    first = _match_template(
        app=app,
        vision=vision,
        template=cap_template,
        region=_CAP_REGION,
        threshold=_CAP_THRESHOLD,
    )
    if budget is not None and not budget.active():
        return {"confirmed": False, "confidence": 0.0, "matches": [first]}
    if not first.get("found"):
        return {"confirmed": False, "confidence": float(first.get("confidence") or 0.0), "matches": [first]}
    interval = max(float(confirmation_interval_sec), 0.05)
    _cancelable_sleep(budget.pause_seconds(interval) if budget is not None else interval)
    if budget is not None and not budget.active():
        return {"confirmed": False, "confidence": 0.0, "matches": [first]}
    second = _match_template(
        app=app,
        vision=vision,
        template=cap_template,
        region=_CAP_REGION,
        threshold=_CAP_THRESHOLD,
    )
    return {
        "confirmed": bool(second.get("found")) and (budget is None or budget.active()),
        "confidence": min(
            float(first.get("confidence") or 0.0),
            float(second.get("confidence") or 0.0),
        ),
        "matches": [first, second],
    }


def _confirm_attempts_exhausted(
    *,
    app: Any,
    vision: Any,
    template: str,
    interval_sec: float,
    budget: ObservationBudget | None = None,
) -> Dict[str, Any]:
    if budget is not None and not budget.active():
        return {"valid": False, "found": None, "reason": "observation_timeout"}
    first = _match_template(
        app=app, vision=vision, template=template,
        region=_BUTTON_REGION, threshold=_EXHAUSTED_THRESHOLD,
    )
    if budget is not None and not budget.active():
        return {**first, "valid": False, "found": None, "reason": "observation_timeout"}
    if not first.get("found"):
        return first
    interval = max(float(interval_sec), 0.05)
    _cancelable_sleep(budget.pause_seconds(interval) if budget is not None else interval)
    if budget is not None and not budget.active():
        return {**first, "valid": False, "found": None, "reason": "observation_timeout"}
    second = _match_template(
        app=app, vision=vision, template=template,
        region=_BUTTON_REGION, threshold=_EXHAUSTED_THRESHOLD,
    )
    if budget is not None and not budget.active():
        return {**second, "valid": False, "found": None, "reason": "observation_timeout",
                "matches": [first, second]}
    return {
        **second,
        "confidence": min(float(first.get("confidence") or 0.0), float(second.get("confidence") or 0.0)),
        "matches": [first, second],
    }


def _normalize_max_attempts(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("negotiation_max_attempts must be an integer")
    normalized = int(value)
    if not 1 <= normalized <= MAX_NEGOTIATION_MAX_ATTEMPTS:
        raise ValueError(
            f"negotiation_max_attempts must be between 1 and {MAX_NEGOTIATION_MAX_ATTEMPTS}"
        )
    return normalized


def _result(
    *,
    requested: bool,
    completed: bool,
    confidence: float,
    started_at: float,
    attempts_used: int,
    max_attempts: int,
    stop_reason: str,
    exhausted_match: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    degraded = bool(requested) and not bool(completed) and stop_reason in {
        "attempt_limit_reached", "attempts_exhausted",
    }
    result = {
        "requested_to_cap": bool(requested),
        "completed_to_cap": bool(completed),
        "attempts_used": int(attempts_used),
        "max_attempts": int(max_attempts),
        "stop_reason": str(stop_reason),
        "degraded": degraded,
        "attempt_fatigue": NEGOTIATION_ATTEMPT_FATIGUE,
        "actual_fatigue_used": int(attempts_used) * NEGOTIATION_ATTEMPT_FATIGUE,
        "detection_method": "template",
        "cap_confidence": float(confidence),
        "elapsed_ms": max(int((time.monotonic() - started_at) * 1000), 0),
        "failure_reason": None,
    }
    if exhausted_match is not None:
        result["exhausted_match"] = dict(exhausted_match)
    return result


def _execute_negotiation_to_cap(
    *,
    kind: str,
    requested_to_cap: bool,
    app: Any,
    vision: Any,
    total_timeout_sec: float = _TOTAL_TIMEOUT_SEC,
    button_wait_timeout_sec: float = _BUTTON_WAIT_TIMEOUT_SEC,
    animation_start_timeout_sec: float = _ANIMATION_START_TIMEOUT_SEC,
    animation_finish_timeout_sec: float = _ANIMATION_FINISH_TIMEOUT_SEC,
    poll_interval_sec: float = _POLL_INTERVAL_SEC,
    cap_confirmation_interval_sec: float = _CAP_CONFIRMATION_INTERVAL_SEC,
    max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
) -> Dict[str, Any]:
    started_at = time.monotonic()
    normalized_max_attempts = _normalize_max_attempts(max_attempts)
    if not requested_to_cap:
        return _result(
            requested=False,
            completed=False,
            confidence=0.0,
            started_at=started_at,
            attempts_used=0,
            max_attempts=normalized_max_attempts,
            stop_reason="not_requested",
        )
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    config = _NEGOTIATION_CONFIG.get(str(kind))
    if config is None:
        raise ValueError(f"unsupported negotiation kind: {kind}")

    deadline = started_at + max(float(total_timeout_sec), 0.0)
    total_budget = ObservationBudget(
        max(deadline - time.monotonic(), 0.0),
        monotonic=time.monotonic, check_cancelled=_check_cancelled,
    )
    # Keep the original entry deadline, including validation/initialization work.
    total_budget.deadline = deadline
    cap_template = str(config["cap_template"])
    button_template = str(config["button_template"])
    exhausted_template = config.get("exhausted_template")
    last_cap: Dict[str, Any] = {"confirmed": False, "confidence": 0.0}
    last_button: Dict[str, Any] = {"found": False, "confidence": 0.0}
    no_animation_since: float | None = None
    attempts_used = 0

    def input_evidence() -> Dict[str, Any]:
        return {
            "kind": kind, "attempts_used": attempts_used,
            "actual_fatigue_used": attempts_used * NEGOTIATION_ATTEMPT_FATIGUE,
            "max_attempts": normalized_max_attempts,
        }

    def observe_phase(callback, **kwargs):
        try:
            return callback(**kwargs)
        except NegotiationExecutionError as exc:
            # A failed observation never erases inputs already sent this run.
            # Count only clicks which returned; never guess about failed input.
            exc.detail.update(input_evidence())
            raise

    def phase_budget(seconds: float) -> ObservationBudget:
        result = ObservationBudget(
            min(max(float(seconds), 0.0), max(deadline - time.monotonic(), 0.0)),
            monotonic=time.monotonic, check_cancelled=_check_cancelled,
            immediate_first_probe=float(seconds) <= 0 and total_budget.active(),
            deadline_limit=deadline,
        )
        return result

    def exhausted_result(observation: Dict[str, Any]) -> Dict[str, Any]:
        marker = dict(observation.get("exhausted_match") or {})
        cancelled_attempt = attempts_used + 1
        cancellation = (
            "本次砍价取消" if attempts_used == 0
            else f"第{cancelled_attempt}次及后续砍价取消"
        )
        logger.info(
            "resonance_pc negotiation attempts_exhausted "
            "因为识别到没有砍价次数（再交涉图标），所以%s，直接按当前价格买入。"
            "kind=%s attempts_used=%d cancelled_attempt=%d confidence=%.4f match=%s",
            cancellation, kind, attempts_used, cancelled_attempt,
            float(marker.get("confidence") or 0.0), marker,
        )
        return _result(
            requested=True,
            completed=False,
            confidence=float(last_cap.get("confidence") or 0.0),
            started_at=started_at,
            attempts_used=attempts_used,
            max_attempts=normalized_max_attempts,
            stop_reason="attempts_exhausted",
            exhausted_match=marker,
        )

    while total_budget.active():
        # 再交涉 is a terminal state regardless of configured attempts or cap.
        if exhausted_template:
            exhausted = observe_phase(_confirm_attempts_exhausted,
                app=app, vision=vision, template=exhausted_template,
                interval_sec=poll_interval_sec, budget=total_budget,
            )
            if not total_budget.active():
                break
            if exhausted.get("found"):
                return exhausted_result({"exhausted_match": exhausted})
        last_cap = observe_phase(_confirm_cap,
            app=app,
            vision=vision,
            cap_template=cap_template,
            confirmation_interval_sec=cap_confirmation_interval_sec,
            budget=total_budget,
        )
        logger.info(
            "resonance_pc negotiation cap_check kind=%s confirmed=%s confidence=%.4f",
            kind,
            bool(last_cap.get("confirmed")),
            float(last_cap.get("confidence") or 0.0),
        )
        if not total_budget.active():
            break
        if last_cap.get("confirmed"):
            return _result(
                requested=True,
                completed=True,
                confidence=float(last_cap.get("confidence") or 0.0),
                started_at=started_at,
                attempts_used=attempts_used,
                max_attempts=normalized_max_attempts,
                stop_reason="cap_reached",
            )
        if attempts_used >= normalized_max_attempts:
            return _result(
                requested=True,
                completed=False,
                confidence=float(last_cap.get("confidence") or 0.0),
                started_at=started_at,
                attempts_used=attempts_used,
                max_attempts=normalized_max_attempts,
                stop_reason="attempt_limit_reached",
            )

        remaining = max(deadline - time.monotonic(), 0.0)
        button_budget = phase_budget(float(button_wait_timeout_sec))
        last_button = observe_phase(_wait_for_template_state,
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=True,
            timeout_sec=min(float(button_wait_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
            exhausted_template=exhausted_template,
            budget=button_budget,
        )
        if not total_budget.active():
            break
        if last_button.get("attempts_exhausted"):
            return exhausted_result(last_button)
        if not last_button.get("found"):
            if time.monotonic() >= deadline:
                break
            raise NegotiationExecutionError(
                "negotiation_button_not_found",
                "Unable to find the negotiation button on the trade page.",
                {**input_evidence(), "last_cap": last_cap, "last_button": last_button},
            )
        center = last_button.get("center")
        if not isinstance(center, list) or len(center) != 2:
            raise NegotiationExecutionError(
                "negotiation_button_not_found",
                "Negotiation button match did not provide a click point.",
                {**input_evidence(), "last_button": last_button},
            )
        if not total_budget.active():
            break
        if not button_budget.active():
            if not total_budget.active():
                break
            raise NegotiationExecutionError(
                "negotiation_button_not_found",
                "Negotiation button evidence expired before input.",
                {**input_evidence(), "last_button": last_button},
            )
        app.click(x=int(center[0]), y=int(center[1]))
        attempts_used += 1
        logger.info(
            "resonance_pc negotiation button_clicked kind=%s attempt=%d/%d confidence=%.4f",
            kind,
            attempts_used,
            normalized_max_attempts,
            float(last_button.get("confidence") or 0.0),
        )
        remaining = max(deadline - time.monotonic(), 0.0)
        if not total_budget.active():
            break
        animation_start = observe_phase(_wait_for_template_state,
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=False,
            timeout_sec=min(float(animation_start_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
            exhausted_template=exhausted_template,
            budget=phase_budget(float(animation_start_timeout_sec)),
        )
        if not total_budget.active():
            break
        if animation_start.get("attempts_exhausted"):
            return exhausted_result(animation_start)
        if not animation_start.get("state_confirmed"):
            last_cap = observe_phase(_confirm_cap,
                app=app,
                vision=vision,
                cap_template=cap_template,
                confirmation_interval_sec=cap_confirmation_interval_sec,
                budget=total_budget,
            )
            if not total_budget.active():
                break
            if last_cap.get("confirmed"):
                return _result(
                    requested=True,
                    completed=True,
                    confidence=float(last_cap.get("confidence") or 0.0),
                    started_at=started_at,
                    attempts_used=attempts_used,
                    max_attempts=normalized_max_attempts,
                    stop_reason="cap_reached",
                )
            now = time.monotonic()
            if no_animation_since is None:
                no_animation_since = now
            elif now - no_animation_since >= max(float(animation_start_timeout_sec) * 2.0, 0.1):
                raise NegotiationExecutionError(
                    "negotiation_animation_start_timeout",
                    "Negotiation button clicks did not start the animation.",
                    {
                        **input_evidence(),
                        "last_cap": last_cap,
                        "last_button": animation_start,
                    },
                )
            if attempts_used >= normalized_max_attempts:
                raise NegotiationExecutionError(
                    "negotiation_animation_start_timeout",
                    "Negotiation button clicks did not start the animation.",
                    {
                        **input_evidence(),
                        "last_cap": last_cap,
                        "last_button": animation_start,
                    },
                )
            continue

        no_animation_since = None
        remaining = max(deadline - time.monotonic(), 0.0)
        animation_finish = observe_phase(_wait_for_template_state,
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=True,
            timeout_sec=min(float(animation_finish_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
            exhausted_template=exhausted_template,
            budget=phase_budget(float(animation_finish_timeout_sec)),
        )
        if not total_budget.active():
            break
        if animation_finish.get("attempts_exhausted"):
            return exhausted_result(animation_finish)
        if not animation_finish.get("found"):
            last_cap = observe_phase(_confirm_cap,
                app=app,
                vision=vision,
                cap_template=cap_template,
                confirmation_interval_sec=cap_confirmation_interval_sec,
                budget=total_budget,
            )
            if not total_budget.active():
                break
            if last_cap.get("confirmed"):
                return _result(
                    requested=True,
                    completed=True,
                    confidence=float(last_cap.get("confidence") or 0.0),
                    started_at=started_at,
                    attempts_used=attempts_used,
                    max_attempts=normalized_max_attempts,
                    stop_reason="cap_reached",
                )
            raise NegotiationExecutionError(
                "negotiation_animation_finish_timeout",
                "Negotiation animation did not return to the trade page.",
                {**input_evidence(), "last_cap": last_cap, "last_button": animation_finish},
            )
        logger.info(
            "resonance_pc negotiation animation_finished kind=%s button_confidence=%.4f",
            kind,
            float(animation_finish.get("confidence") or 0.0),
        )
        last_cap = observe_phase(_confirm_cap,
            app=app,
            vision=vision,
            cap_template=cap_template,
            confirmation_interval_sec=cap_confirmation_interval_sec,
            budget=total_budget,
        )
        if not total_budget.active():
            break
        if last_cap.get("confirmed"):
            return _result(
                requested=True,
                completed=True,
                confidence=float(last_cap.get("confidence") or 0.0),
                started_at=started_at,
                attempts_used=attempts_used,
                max_attempts=normalized_max_attempts,
                stop_reason="cap_reached",
            )
        if attempts_used >= normalized_max_attempts:
            logger.warning(
                "resonance_pc negotiation attempt_limit_reached kind=%s attempts=%d",
                kind,
                attempts_used,
            )
            return _result(
                requested=True,
                completed=False,
                confidence=float(last_cap.get("confidence") or 0.0),
                started_at=started_at,
                attempts_used=attempts_used,
                max_attempts=normalized_max_attempts,
                stop_reason="attempt_limit_reached",
            )

    raise NegotiationExecutionError(
        "negotiation_cap_detection_timeout",
        "Negotiation did not reach the 20.0% cap before the total timeout.",
        {
            **input_evidence(),
            "last_cap": last_cap,
            "last_button": last_button,
        },
    )


def execute_bargain_to_cap(
    *,
    requested_to_cap: bool,
    app: Any,
    vision: Any,
    max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    **kwargs: Any,
) -> Dict[str, Any]:
    return _execute_negotiation_to_cap(
        kind="bargain",
        requested_to_cap=requested_to_cap,
        app=app,
        vision=vision,
        max_attempts=max_attempts,
        **kwargs,
    )


def execute_raise_to_cap(
    *,
    requested_to_cap: bool,
    app: Any,
    vision: Any,
    max_attempts: int = DEFAULT_NEGOTIATION_MAX_ATTEMPTS,
    **kwargs: Any,
) -> Dict[str, Any]:
    return _execute_negotiation_to_cap(
        kind="raise",
        requested_to_cap=requested_to_cap,
        app=app,
        vision=vision,
        max_attempts=max_attempts,
        **kwargs,
    )


__all__ = [
    "DEFAULT_NEGOTIATION_MAX_ATTEMPTS",
    "MAX_NEGOTIATION_MAX_ATTEMPTS",
    "NEGOTIATION_ATTEMPT_FATIGUE",
    "NegotiationExecutionError",
    "execute_bargain_to_cap",
    "execute_raise_to_cap",
]

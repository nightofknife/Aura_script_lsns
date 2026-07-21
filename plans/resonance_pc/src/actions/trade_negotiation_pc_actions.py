"""Template-driven bargaining and raising helpers for Resonance PC trading."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from packages.aura_core.observability.logging.core_logger import logger


_CAP_REGION = (980, 430, 90, 50)
_BUTTON_REGION = (1090, 425, 170, 70)
_CAP_THRESHOLD = 0.88
_BUTTON_THRESHOLD = 0.86
_POLL_INTERVAL_SEC = 0.2
_CAP_CONFIRMATION_INTERVAL_SEC = 0.2
_TOTAL_TIMEOUT_SEC = 180.0
_BUTTON_WAIT_TIMEOUT_SEC = 3.0
_ANIMATION_START_TIMEOUT_SEC = 3.0
_ANIMATION_FINISH_TIMEOUT_SEC = 15.0

_NEGOTIATION_CONFIG: Mapping[str, Mapping[str, str]] = {
    "bargain": {
        "button_template": "templates/trade_buy_bargain_button.png",
        "cap_template": "templates/trade_buy_cap20_digits.png",
    },
    "raise": {
        "button_template": "templates/trade_sell_raise_button.png",
        "cap_template": "templates/trade_sell_cap20_digits.png",
    },
}


class NegotiationExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = dict(detail or {})


def _template_path(relative_path: str) -> Path:
    return Path(__file__).resolve().parents[2] / relative_path


def _match_template(
    *,
    app: Any,
    vision: Any,
    template: str,
    region: Tuple[int, int, int, int],
    threshold: float,
) -> Dict[str, Any]:
    capture = app.capture(rect=region)
    if not capture.success:
        raise NegotiationExecutionError(
            "negotiation_page_lost",
            "Unable to capture the negotiation page.",
            {"template": template, "region": list(region)},
        )
    match = vision.find_template(
        source_image=capture.image,
        template_image=str(_template_path(template)),
        threshold=float(threshold),
        use_grayscale=True,
    )
    center = getattr(match, "center_point", None)
    result: Dict[str, Any] = {
        "found": bool(getattr(match, "found", False)),
        "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
        "template": template,
        "region": list(region),
    }
    if center and len(center) == 2:
        result["center"] = [int(region[0] + center[0]), int(region[1] + center[1])]
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
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    last: Dict[str, Any] = {
        "found": False,
        "confidence": 0.0,
        "template": template,
        "region": list(region),
    }
    while True:
        last = _match_template(
            app=app,
            vision=vision,
            template=template,
            region=region,
            threshold=threshold,
        )
        if bool(last.get("found")) is bool(expected_found):
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(max(float(poll_interval_sec), 0.05))


def _confirm_cap(
    *,
    app: Any,
    vision: Any,
    cap_template: str,
    confirmation_interval_sec: float,
) -> Dict[str, Any]:
    first = _match_template(
        app=app,
        vision=vision,
        template=cap_template,
        region=_CAP_REGION,
        threshold=_CAP_THRESHOLD,
    )
    if not first.get("found"):
        return {"confirmed": False, "confidence": float(first.get("confidence") or 0.0), "matches": [first]}
    time.sleep(max(float(confirmation_interval_sec), 0.05))
    second = _match_template(
        app=app,
        vision=vision,
        template=cap_template,
        region=_CAP_REGION,
        threshold=_CAP_THRESHOLD,
    )
    return {
        "confirmed": bool(second.get("found")),
        "confidence": min(
            float(first.get("confidence") or 0.0),
            float(second.get("confidence") or 0.0),
        ),
        "matches": [first, second],
    }


def _result(*, requested: bool, completed: bool, confidence: float, started_at: float) -> Dict[str, Any]:
    return {
        "requested_to_cap": bool(requested),
        "completed_to_cap": bool(completed),
        "detection_method": "template",
        "cap_confidence": float(confidence),
        "elapsed_ms": max(int((time.monotonic() - started_at) * 1000), 0),
        "failure_reason": None,
    }


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
) -> Dict[str, Any]:
    started_at = time.monotonic()
    if not requested_to_cap:
        return _result(requested=False, completed=False, confidence=0.0, started_at=started_at)
    if app is None or vision is None:
        raise RuntimeError("app/vision services are required")
    config = _NEGOTIATION_CONFIG.get(str(kind))
    if config is None:
        raise ValueError(f"unsupported negotiation kind: {kind}")

    deadline = started_at + max(float(total_timeout_sec), 0.0)
    cap_template = str(config["cap_template"])
    button_template = str(config["button_template"])
    last_cap: Dict[str, Any] = {"confirmed": False, "confidence": 0.0}
    last_button: Dict[str, Any] = {"found": False, "confidence": 0.0}
    no_animation_since: float | None = None

    while time.monotonic() < deadline:
        last_cap = _confirm_cap(
            app=app,
            vision=vision,
            cap_template=cap_template,
            confirmation_interval_sec=cap_confirmation_interval_sec,
        )
        logger.info(
            "resonance_pc negotiation cap_check kind=%s confirmed=%s confidence=%.4f",
            kind,
            bool(last_cap.get("confirmed")),
            float(last_cap.get("confidence") or 0.0),
        )
        if last_cap.get("confirmed"):
            return _result(
                requested=True,
                completed=True,
                confidence=float(last_cap.get("confidence") or 0.0),
                started_at=started_at,
            )
        if time.monotonic() >= deadline:
            break

        remaining = max(deadline - time.monotonic(), 0.0)
        last_button = _wait_for_template_state(
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=True,
            timeout_sec=min(float(button_wait_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
        )
        if not last_button.get("found"):
            if time.monotonic() >= deadline:
                break
            raise NegotiationExecutionError(
                "negotiation_button_not_found",
                "Unable to find the negotiation button on the trade page.",
                {"kind": kind, "last_cap": last_cap, "last_button": last_button},
            )
        center = last_button.get("center")
        if not isinstance(center, list) or len(center) != 2:
            raise NegotiationExecutionError(
                "negotiation_button_not_found",
                "Negotiation button match did not provide a click point.",
                {"kind": kind, "last_button": last_button},
            )
        if time.monotonic() >= deadline:
            break

        app.click(x=int(center[0]), y=int(center[1]))
        logger.info(
            "resonance_pc negotiation button_clicked kind=%s confidence=%.4f",
            kind,
            float(last_button.get("confidence") or 0.0),
        )
        remaining = max(deadline - time.monotonic(), 0.0)
        animation_start = _wait_for_template_state(
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=False,
            timeout_sec=min(float(animation_start_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
        )
        if animation_start.get("found"):
            last_cap = _confirm_cap(
                app=app,
                vision=vision,
                cap_template=cap_template,
                confirmation_interval_sec=cap_confirmation_interval_sec,
            )
            if last_cap.get("confirmed"):
                return _result(
                    requested=True,
                    completed=True,
                    confidence=float(last_cap.get("confidence") or 0.0),
                    started_at=started_at,
                )
            now = time.monotonic()
            if no_animation_since is None:
                no_animation_since = now
            elif now - no_animation_since >= max(float(animation_start_timeout_sec) * 2.0, 0.1):
                raise NegotiationExecutionError(
                    "negotiation_animation_start_timeout",
                    "Negotiation button clicks did not start the animation.",
                    {"kind": kind, "last_cap": last_cap, "last_button": animation_start},
                )
            continue

        no_animation_since = None
        remaining = max(deadline - time.monotonic(), 0.0)
        animation_finish = _wait_for_template_state(
            app=app,
            vision=vision,
            template=button_template,
            region=_BUTTON_REGION,
            threshold=_BUTTON_THRESHOLD,
            expected_found=True,
            timeout_sec=min(float(animation_finish_timeout_sec), remaining),
            poll_interval_sec=poll_interval_sec,
        )
        if not animation_finish.get("found"):
            last_cap = _confirm_cap(
                app=app,
                vision=vision,
                cap_template=cap_template,
                confirmation_interval_sec=cap_confirmation_interval_sec,
            )
            if last_cap.get("confirmed"):
                return _result(
                    requested=True,
                    completed=True,
                    confidence=float(last_cap.get("confidence") or 0.0),
                    started_at=started_at,
                )
            raise NegotiationExecutionError(
                "negotiation_animation_finish_timeout",
                "Negotiation animation did not return to the trade page.",
                {"kind": kind, "last_cap": last_cap, "last_button": animation_finish},
            )
        logger.info(
            "resonance_pc negotiation animation_finished kind=%s button_confidence=%.4f",
            kind,
            float(animation_finish.get("confidence") or 0.0),
        )

    raise NegotiationExecutionError(
        "negotiation_cap_detection_timeout",
        "Negotiation did not reach the 20.0% cap before the total timeout.",
        {"kind": kind, "last_cap": last_cap, "last_button": last_button},
    )


def execute_bargain_to_cap(*, requested_to_cap: bool, app: Any, vision: Any, **kwargs: Any) -> Dict[str, Any]:
    return _execute_negotiation_to_cap(
        kind="bargain",
        requested_to_cap=requested_to_cap,
        app=app,
        vision=vision,
        **kwargs,
    )


def execute_raise_to_cap(*, requested_to_cap: bool, app: Any, vision: Any, **kwargs: Any) -> Dict[str, Any]:
    return _execute_negotiation_to_cap(
        kind="raise",
        requested_to_cap=requested_to_cap,
        app=app,
        vision=vision,
        **kwargs,
    )


__all__ = [
    "NegotiationExecutionError",
    "execute_bargain_to_cap",
    "execute_raise_to_cap",
]

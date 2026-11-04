"""Departure-specific errors and stable waits over shared main-UI evidence."""
import time

from packages.aura_core.observability.logging.core_logger import logger
from ._mainui_control_vision import probe_mainui_control


POLL_INTERVAL = 0.15


class DepartButtonError(RuntimeError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail or {}


def probe_depart_button(*, app, vision, check_cancelled):
    result = probe_mainui_control(app=app, control="depart", check_cancelled=check_cancelled)
    if result["valid"] is not True:
        code = {"capture_failed": "depart_capture_failed",
                "template_load_failed": "depart_template_invalid"}.get(
                    result["reason"], "depart_template_match_failed")
        raise DepartButtonError(code, "Unable to observe the departure button", result)
    logger.info("[DepartButton] phase=probe found=%s confidence=%.4f capture_ms=%s match_ms=%s",
                result["found"], result["confidence"], result["capture_ms"], result["match_ms"])
    return result


def wait_depart_button(*, app, vision, check_cancelled, timeout_sec=5.0, stable_matches=2):
    started = time.monotonic()
    deadline = started + max(float(timeout_sec), 0.)
    confirmations = 0
    required = max(int(stable_matches), 2)
    while True:
        match = probe_depart_button(app=app, vision=vision, check_cancelled=check_cancelled)
        confirmations = confirmations + 1 if match["found"] else 0
        confirmed = confirmations >= required
        if confirmed or time.monotonic() >= deadline:
            result = {"confirmed": confirmed, "match": match, "confirmations": confirmations,
                      "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
            logger.info("[DepartButton] phase=main_ready confirmed=%s confirmations=%s elapsed_ms=%s",
                        confirmed, confirmations, result["elapsed_ms"])
            return result
        check_cancelled()
        time.sleep(min(POLL_INTERVAL, max(deadline - time.monotonic(), 0.)))
        check_cancelled()

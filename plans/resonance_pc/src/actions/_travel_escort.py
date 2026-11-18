"""One intercity encounter, handled synchronously by the arrival loop."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import time
from uuid import uuid4

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from packages.aura_core.scheduler.utils import resolve_base_path
from ....aura_base.src.services.interaction_service import (
    InteractionError, TransitionPolicy, TransitionProbe, TransitionState, probe_transition, run_transition,
)
from ....aura_base.src.services.observation_service import CaptureError, Frame, Observation, ObservationService

from ._intercity_map_geometry import REFERENCE_CLIENT, require_reference_client
from ._operation_progress import observe_operation
from ._travel_gear_recovery import _GEAR_REGION, drive_point_from_brake, match_selected_gear


SCAN_INTERVAL = 5.0
_FULL_CLIENT = (0, 0, *REFERENCE_CLIENT)
_POLL_INTERVAL = 0.35
_BATTLE_TIMEOUT = 600.0
_PLAN_ROOT = Path(__file__).resolve().parents[2]
_TRAVEL = "templates/navigation/travel"


@dataclass(frozen=True)
class ScreenTemplate:
    path: str
    roi: tuple[int, int, int, int]
    threshold: float = 0.86
    grayscale: bool = True
    method: int = cv2.TM_CCOEFF_NORMED


# Coordinates are client-relative. The battle timer is the 33px Time sprite
# from ui/battle/count.asset, placed by /UI/Group_Count/Img_Time in ui/battle/ui.asset.
_SCREENS = {
    "engage": ScreenTemplate(f"{_TRAVEL}/escort/engage.png", (877, 155, 259, 148)),
    "start": ScreenTemplate(f"{_TRAVEL}/escort/start.png", (1100, 280, 180, 210)),
    "victory": ScreenTemplate(f"{_TRAVEL}/escort/victory_next.png", (1085, 543, 185, 165)),
    "defeat": ScreenTemplate(f"{_TRAVEL}/escort/defeat_next.png", (1030, 581, 250, 128)),
    "report": ScreenTemplate(f"{_TRAVEL}/escort/report.png", (844, 502, 105, 83), grayscale=False),
    "battle": ScreenTemplate(f"{_TRAVEL}/escort/battle_clock.png", (500, 0, 90, 64), 0.82),
}
_SETTLEMENTS = ("victory", "defeat")


@lru_cache(maxsize=1)
def _battle_clock():
    image = cv2.imdecode(
        np.frombuffer((_PLAN_ROOT / _SCREENS["battle"].path).read_bytes(), dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    if image is None or image.shape != (33, 33, 4):
        raise ValueError("Invalid battle clock template")
    # The sprite is white. Keep its full alpha silhouette for correlation,
    # including its gaps, because bright HUD backgrounds can match the ink alone.
    return np.ascontiguousarray(image[:, :, 3])


class TravelEscortError(RuntimeError):
    def __init__(self, stage: str, cause: Exception, screenshot: str | None):
        self.code = f"travel_escort_{stage}_unconfirmed"
        self.detail = {"stage": stage, "reason": getattr(cause, "code", str(cause)), "screenshot": screenshot}
        super().__init__(f"{self.code}: {self.detail['reason']}" + (f" ({screenshot})" if screenshot else ""))


class TravelEscort:
    def __init__(self, app, vision, *, started: float, check_cancelled, return_screens: dict[str, ScreenTemplate]):
        require_reference_client(app)
        self.app = app
        self.vision = vision
        self.observation = ObservationService(app=app, vision=vision)
        self.scope = OperationScope(parent=current_operation_scope(), target=getattr(app, "target_runtime", None),
                                    cancel_check=check_cancelled, monotonic=time.monotonic)
        self.screens = {**_SCREENS, **return_screens}
        self.return_screens = tuple(return_screens)
        self.next_check_at = started
        self.count = 0
        self._stage = "engage"
        self._last_frame: Frame | None = None

    def _capture(self, scope: OperationScope, roi=_FULL_CLIENT) -> Frame | None:
        try:
            frame = self.observation.capture(scope=scope, roi=roi)
        except CaptureError:
            logger.debug("[TravelEscort] capture failed stage=%s", self._stage, exc_info=True)
            return None
        self._last_frame = frame
        return frame

    def _match(self, frame: Frame, name: str, scope: OperationScope) -> Observation:
        if name in {"brake", "drive"}:
            match = match_selected_gear(self.vision, frame.crop(_GEAR_REGION).rgb_image(),
                                        "b" if name == "brake" else "d")
            point = drive_point_from_brake(match) if name == "brake" else None
            state = "unknown" if match is None else "present" if match.found else "absent"
            return Observation(state, frame.id, value=match, client_center=point)
        spec = self.screens[name]
        template = _PLAN_ROOT / spec.path
        if name == "battle":
            template = _battle_clock()
        return self.observation.match_template(frame, template, scope=scope, roi=spec.roi,
                                               threshold=spec.threshold, use_grayscale=spec.grayscale,
                                               match_method=spec.method)

    def _observe(self, scope: OperationScope, targets: tuple[str, ...], source: str | None = None):
        frame = self._capture(scope)
        if frame is None:
            return TransitionProbe()
        for name in targets:
            if self._match(frame, name, scope).state == "present":
                return TransitionProbe("target", key=name, frame=frame)
        if source is not None:
            hit = self._match(frame, source, scope)
            if hit.state == "present" and hit.client_center is not None:
                return TransitionProbe("source", key=source, hit=hit, frame=frame)
        return TransitionProbe(frame=frame)

    def _advance(self, source, targets, scope, *, first=None, settle=2.0, timeout=18.0):
        self._stage = source
        step = scope.child(timeout)
        policy = TransitionPolicy(max_clicks=2, allow_retry=True, settle_seconds=0.5, latch_source_loss=True)
        state = TransitionState(policy)

        def probe(allow_source):
            nonlocal first
            if first is not None:
                initial, first = first, None
                return initial
            budget = step.child(3.0 if allow_source else settle)
            return probe_transition(
                lambda read_source: self._observe(budget, targets, source if read_source else None),
                allow_source=allow_source, scope=budget, stable_scans=2,
                poll_seconds=_POLL_INTERVAL, state=state,
            )

        def click(observed, attempt):
            x, y = observed.hit.client_center
            logger.info("[TravelEscort] stage=%s click=%s point=(%s,%s)", source, attempt, x, y)
            return self.app.click(x=x, y=y)

        result = run_transition(probe, click, scope=step, policy=policy, state=state)
        logger.info("[TravelEscort] stage=%s confirmed=%s clicks=%s", source, result.observation.key,
                    len(result.receipts))
        return result.observation.key

    def _settlement(self, scope: OperationScope) -> str:
        self._stage = "settlement"
        budget = scope.child(_BATTLE_TIMEOUT)
        result = probe_transition(lambda _: self._observe(budget, _SETTLEMENTS), allow_source=False,
                                  scope=budget, stable_scans=2, poll_seconds=0.5)
        if result is None:
            raise OperationTimeout("Battle settlement was not confirmed")
        return result.key

    def _save_failure(self) -> str | None:
        if self._last_frame is None:
            return None
        path = resolve_base_path() / "logs" / "travel_escort" / f"{uuid4().hex}_{self._stage}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            image = cv2.cvtColor(self._last_frame.rgb_image(), cv2.COLOR_RGB2BGR)
            ok, png = cv2.imencode(".png", image)
            if ok:
                path.write_bytes(png.tobytes())
                return str(path)
        except (OSError, cv2.error):
            logger.warning("[TravelEscort] could not save failure screenshot", exc_info=True)
        return None

    def tick(self, deadline: float) -> bool:
        now = time.monotonic()
        if now < self.next_check_at or now >= deadline:
            return False
        self.next_check_at = now + SCAN_INTERVAL
        self._stage = "engage"
        scope = self.scope.child(deadline - now)
        frame = self._capture(scope, self.screens["engage"].roi)
        if frame is None:
            return False
        hit = self._match(frame, "engage", scope)
        if hit.state != "present" or hit.client_center is None:
            return False

        observe_operation("travel.escort", "处理护卫队迎击", "started")
        try:
            first = TransitionProbe("source", key="engage", hit=hit, frame=frame)
            self._advance("engage", ("start",), scope, first=first)
            result = self._advance("start", (*_SETTLEMENTS, "battle"), scope)
            self.count += 1
            if result == "battle":
                result = self._settlement(scope)
            if result == "victory":
                self._advance("victory", self.return_screens, scope)
            else:
                self._advance("defeat", ("report",), scope)
                gear = self._advance("report", ("drive", "brake"), scope, settle=6.0, timeout=32.0)
                if gear == "brake":
                    self._advance("brake", ("drive",), scope)
        except (InteractionError, OperationTimeout) as exc:
            error = TravelEscortError(self._stage, exc, self._save_failure())
            observe_operation("travel.escort", "护卫队迎击暂停", "blocked", **error.detail)
            raise error from exc

        observe_operation("travel.escort", "护卫队迎击已处理", "completed", outcome=result, count=self.count)
        self.next_check_at = time.monotonic() + SCAN_INTERVAL
        self._last_frame = None
        return True

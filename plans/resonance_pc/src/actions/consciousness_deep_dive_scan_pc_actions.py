"""Bounded, observation-only Deep Dive layout scan for manual comparison."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import time
from uuid import uuid4
from types import SimpleNamespace

import cv2
import numpy as np

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.utils import resolve_base_path
from packages.aura_core.utils.exceptions import StopTaskException

from ._deep_dive_single_run_vision import observe as observe_scene
from ._deep_dive_layout_vision import LayoutScanner, BASES
from ._deep_dive_layout_report import write_layout_report
from ._deep_dive_scan_policy import FaceScanPolicy, CellScanPolicy
from ._deep_dive_scan_stream import ScanVisionStream

MIN_DRAG_GAP_SEC = .2


async def _await_serial(operation):
    """Drain a threaded input/capture before allowing release or the next call."""
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


async def _capture_after_input(app, fallback_wait=.04):
    """Wait for two WGC generations after input; return (capture, metadata)."""
    backend = None
    generation = None
    try:
        backend = app.target_runtime._get_or_create_session().capture_backend
        generation = backend.self_check().get('generation')
    except (AttributeError, KeyError, TypeError):
        pass
    freshness = {'method': 'delay_fallback', 'wait_sec': fallback_wait}
    if isinstance(generation, (int, float)):
        deadline = time.monotonic()+.5
        current = generation
        while current < generation+2:
            _cancel_check()
            if time.monotonic() >= deadline:
                raise RuntimeError('fresh_frame_generation_timeout')
            await asyncio.sleep(.005)
            current = backend.self_check().get('generation', generation)
        freshness = {'method': 'wgc_generation', 'before': generation, 'after': current}
    else:
        await asyncio.sleep(fallback_wait)
    capture, cancelled = await _await_serial(app.capture_async())
    if cancelled:
        raise asyncio.CancelledError()
    return capture, freshness


def _gesture_path(scanner, direction, distance, *, inset_fallback=True):
    vector = np.asarray(direction, float)
    norm = float(np.linalg.norm(vector))
    if vector.shape != (2,) or not math.isfinite(norm) or norm < 1e-6:
        return None
    vector /= norm
    candidates = []
    for item in scanner.last_projected:
        point = np.asarray(item['centre'], float)
        if item.get('cosine', 0) < .4 or item.get('area', 0) < 800:
            continue
        if not (350 <= point[0] <= 910 and 150 <= point[1] <= 460):
            continue
        if any(x-12 <= point[0] <= x+w+12 and y-12 <= point[1] <= y+h+12
               for x,y,w,h in (target['box'] for target in scanner.last_targets)):
            continue
        available = float(distance)
        for axis, bounds in enumerate(((300, 970), (100, 610))):
            if abs(vector[axis]) > 1e-6:
                boundary = bounds[1] if vector[axis] > 0 else bounds[0]
                available = min(available, (boundary-point[axis])/vector[axis])
        # Stop before the fixed Reset View hit region, including its margins.
        for length in range(0, max(0, int(available))+1, 2):
            pos = point + vector*length
            if 588 <= pos[0] <= 700 and 478 <= pos[1] <= 592:
                available = max(0, length-4)
                break
        if available >= 20:
            start = tuple(map(int, np.rint(point)))
            end = tuple(map(int, np.rint(point + vector*available)))
            candidates.append((available, item['cosine'], start, end))
    if not candidates:
        if inset_fallback:
            # A sprite can cover a tile centre while leaving its inner surface
            # usable. Try inset points with the same HUD/target exclusions.
            projected = []
            for item in scanner.last_projected:
                centre = np.asarray(item['centre'], float)
                for vertex in item.get('quad', []):
                    projected.append(dict(item, centre=centre+.30*(np.asarray(vertex)-centre)))
            view = SimpleNamespace(last_projected=projected, last_targets=scanner.last_targets)
            return _gesture_path(view, direction, distance, inset_fallback=False)
        return None
    _, _, start, end = max(candidates)
    return start, end


async def _segmented_drag(app, scanner, start, end, duration, frame_wait,
                          output_dir, frames, action, started, deadline):
    """Hold once, move <=70px, then capture; never overlap capture and movement."""
    pressed = False
    cancelled = False
    actual = np.asarray(start, int)
    total = float(np.linalg.norm(np.asarray(end)-start))
    count = max(1, math.ceil(total/70.))
    action['segments'] = []
    failure = None
    try:
        _, cancelled = await _await_serial(app.move_to_async(*start, duration=0.))
        if cancelled:
            raise asyncio.CancelledError()
        _cancel_check()
        scanner.note_drag(end[0]-start[0], end[1]-start[1])
        pressed = True
        _, cancelled = await _await_serial(app.controller.mouse_down_async('left'))
        action['pressed_at_sec'] = time.monotonic()-started
        if cancelled:
            raise asyncio.CancelledError()
        # Unity samples OnMouseDown on its frame loop. Let it register the
        # initial cursor position before the first displacement is delivered.
        await _settle(.04)
        for index in range(1, count+1):
            _cancel_check()
            if time.monotonic() + duration/count + frame_wait >= deadline:
                failure = 'time_budget_exhausted'
                break
            point = np.rint(np.asarray(start)+(np.asarray(end)-start)*index/count).astype(int)
            _, cancelled = await _await_serial(app.move_to_async(int(point[0]), int(point[1]), duration=duration/count))
            actual = point
            segment = dict(end=point.tolist(), elapsed_sec=round(time.monotonic()-started,3))
            action['segments'].append(segment)
            if cancelled:
                raise asyncio.CancelledError()
            _cancel_check()
            try:
                capture, freshness = await _capture_after_input(app, frame_wait)
            except RuntimeError as exc:
                failure = str(exc)
                segment['capture_error'] = failure
                break
            if not capture.success or capture.image is None:
                failure = 'segment_capture_failed'
                break
            image = capture.image
            frame_id = len(frames)
            path = output_dir/'frames'/f'{frame_id:04d}.png'
            if not cv2.imwrite(str(path), cv2.cvtColor(image,cv2.COLOR_RGB2BGR)):
                raise OSError('segment_frame_save_failed')
            scene = observe_scene(image)
            row = dict(frame_id=frame_id,path=path.relative_to(output_dir).as_posix(),
                       elapsed_sec=round(time.monotonic()-started,3),scene=scene.get('scene'),
                       during_drag=True, segment_index=index, freshness=freshness)
            frames.append(row)
            segment['frame_id'] = frame_id
            if scene.get('scene') != 'board' or not scene.get('valid'):
                failure = 'unexpected_scene:'+str(scene.get('scene'))
                break
            observation = scanner.observe(image, frame_id=frame_id, semantic='auto')
            row['observation'] = observation
            overlay = path.with_name(f'{frame_id:04d}_overlay.png')
            if not cv2.imwrite(str(overlay),cv2.cvtColor(scanner.annotate(image),cv2.COLOR_RGB2BGR)):
                raise OSError('segment_overlay_save_failed')
            row['overlay_path'] = overlay.relative_to(output_dir).as_posix()
            if not observation.get('tracking_ok'):
                failure = 'segment_tracking_lost'
                break
    finally:
        action['actual_end'] = actual.tolist()
        action['executed_distance_px'] = float(np.linalg.norm(actual-np.asarray(start)))
        action['segment_stop_reason'] = failure
        if pressed:
            try:
                _, release_cancelled = await _await_serial(app.controller.mouse_up_async('left'))
                action['released_at_sec'] = time.monotonic()-started
                cancelled = cancelled or release_cancelled
            finally:
                scanner.finish_drag(int(actual[0]-start[0]), int(actual[1]-start[1]))
        if cancelled:
            raise asyncio.CancelledError()
    return failure


async def _continuous_scan(app, scanner, policy, output_dir, frames, actions,
                           started, deadline, step_limit):
    """Own input here; the independent worker exclusively owns scanner state."""
    adapter = app.target_runtime._get_or_create_session()
    stream = ScanVisionStream(scanner, adapter, output_dir, frames, started)
    policy = CellScanPolicy(base_step_px=policy.base_step_px, max_step_px=policy.max_step_px)
    pressed = False
    position = None
    action = None
    total_input = np.zeros(2)
    ledger = deque([(time.monotonic(), total_input.copy())], maxlen=512)
    sequence = -1
    choice = None
    snapshot = None
    interval = .12
    published = None
    stale_since = None
    status, reason = 'partial', 'time_budget_exhausted'
    cancelled = False
    issued_since_snapshot = 0.
    last_release_at = (started+actions[-1]['released_at_sec']
                       if actions and 'released_at_sec' in actions[-1] else None)

    async def release():
        nonlocal pressed, cancelled, action, last_release_at
        if pressed:
            _, was_cancelled = await _await_serial(app.controller.mouse_up_async('left'))
            last_release_at = time.monotonic()
            cancelled |= was_cancelled
            pressed = False
            if action is not None:
                action['actual_end'] = np.rint(position).astype(int).tolist()
                action['released_at_sec'] = last_release_at-started
            action = None

    stream_started = time.monotonic()
    try:
        stream.start()
        previous_tick = time.monotonic()
        while time.monotonic() < deadline:
            _cancel_check()
            if cancelled:
                raise asyncio.CancelledError()
            now = time.monotonic()
            elapsed = min(.035, max(.001, now-previous_tick))
            previous_tick = now
            snapshot = stream.snapshot()
            if not snapshot:
                await asyncio.sleep(.01)
                continue
            if snapshot.get('error'):
                status, reason = 'blocked', snapshot['error']
                break
            if snapshot['seq'] == 0:
                if now-stream_started > 1.:
                    status, reason = 'blocked', 'stream_start_timeout'
                    break
                await asyncio.sleep(.01)
                continue
            if snapshot.get('layout_complete'):
                status, reason = 'completed', 'layout_complete'
                break
            age = now - snapshot['frame_time']
            if age > .35 or not snapshot.get('tracking_ok'):
                # Do not keep rotating on an outdated pose. Resume only when a
                # fresh observation arrives; no stale command queue is replayed.
                stale_since = stale_since or now
                if now-stale_since > 1.:
                    status, reason = 'blocked', 'stream_feedback_timeout'
                    break
                await asyncio.sleep(.01)
                continue
            stale_since = None
            if snapshot['seq'] != sequence:
                if published is not None:
                    interval = .6*interval + .4*max(.02, snapshot['published_at']-published)
                published = snapshot['published_at']
                sequence = snapshot['seq']
                issued_since_snapshot = 0.
                # Predict only displacement sent after the captured frame.
                # Arrival time is not a renderer timestamp; speed/age guards
                # bound the residual error instead of assuming perfect timing.
                frame_input = ledger[0][1]
                for stamp, cumulative in ledger:
                    if stamp > snapshot['frame_time']:
                        break
                    frame_input = cumulative
                pending = total_input-frame_input
                axes = snapshot['response_axes']
                rotation_delta = sum((np.asarray(axes[k])*pending[k] for k in (0, 1)), np.zeros(3))
                predicted = cv2.Rodrigues(rotation_delta)[0] @ snapshot['rotation']
                choice = policy.choose(predicted, snapshot['cells'], axes,
                    quality=snapshot['quality'], observed_rotation=snapshot['rotation'],
                    tvec=snapshot['tvec'], elapsed=now-started,
                    semantic_revision=snapshot.get('semantic_revision'))
                if choice.get('direction') is None:
                    if choice.get('phase') == 'observe':
                        await asyncio.sleep(.01)
                        continue
                    status, reason = 'partial', choice.get('reason', 'no_useful_view_direction')
                    break
                if action is not None:
                    action['feedback'].append(dict(frame_id=snapshot['frame_id'],
                        frame_age_sec=round(age, 4), policy=choice))
            if choice is None or choice.get('direction') is None:
                await asyncio.sleep(.01)
                continue
            direction = np.asarray(choice['direction'], float)
            allowance = min(50., choice['distance_px'])-issued_since_snapshot
            if allowance < 1.:
                await asyncio.sleep(.01)
                continue
            if not pressed:
                if last_release_at is not None and now-last_release_at < MIN_DRAG_GAP_SEC:
                    # Unity needs a released interval before a new drag. Keep
                    # vision running and reconsider the newest pose next tick.
                    await asyncio.sleep(min(.01, MIN_DRAG_GAP_SEC-(now-last_release_at)))
                    continue
                if choice['distance_px'] < 20.:
                    # Tiny corrections are valid only within an existing drag.
                    # A newly pressed/released 2px stroke could become a click.
                    policy.request_replan('tiny_correction_requires_new_grip')
                    choice = None
                    await asyncio.sleep(.01)
                    continue
                if deadline-now < .3:
                    break
                if len(actions) >= step_limit:
                    status, reason = 'partial', 'step_budget_exhausted'
                    break
                view = SimpleNamespace(last_projected=snapshot['projected'],
                                       last_targets=snapshot['targets'])
                path = _gesture_path(view, direction, 450.)
                if path is None:
                    status, reason = 'partial', 'no_safe_drag_surface'
                    break
                start, _ = path
                position = np.asarray(start, float)
                _, was_cancelled = await _await_serial(app.move_to_async(*start, duration=0.))
                if was_cancelled:
                    raise asyncio.CancelledError()
                action = dict(after_frame_id=snapshot['frame_id'], start=list(start),
                    mode='continuous', policy=choice, feedback=[], executed_distance_px=0.,
                    pressed_at_sec=round(time.monotonic()-started, 3), input_ticks=0)
                actions.append(action)
                pressed = True
                _, was_cancelled = await _await_serial(app.controller.mouse_down_async('left'))
                action['pressed_at_sec'] = time.monotonic()-started
                action['gap_since_previous_release_sec'] = (
                    time.monotonic()-last_release_at if last_release_at is not None else None)
                cancelled |= was_cancelled
                await _settle(.04)
                previous_tick = time.monotonic()
                continue
            # At most ~50px per observed frame. Actual speed follows measured
            # recognition throughput, while near-target motion slows further.
            # Keep enough angular overlap for independent semantic samples;
            # the 35s best case leaves room for a steadier 450px/s ceiling.
            speed_cap = min(450., 50./max(.07, interval, age))
            speed = min(speed_cap, max(80., choice['distance_px']*5.))
            speed *= max(.4, min(1., snapshot['quality']))
            target = position + direction*min(speed*elapsed, allowance)
            x, y = target
            if not (305 <= x <= 965 and 105 <= y <= 605) or (584 <= x <= 704 and 474 <= y <= 596):
                await release()
                continue
            old = np.rint(position).astype(int)
            new = np.rint(target).astype(int)
            position = target
            if not np.array_equal(old, new):
                _, was_cancelled = await _await_serial(app.move_to_async(int(new[0]), int(new[1]), duration=0.))
                cancelled |= was_cancelled
                displacement = new-old
                total_input += displacement
                ledger.append((time.monotonic(), total_input.copy()))
                action['executed_distance_px'] += float(np.linalg.norm(displacement))
                issued_since_snapshot += float(np.linalg.norm(displacement))
                action['input_ticks'] += 1
            await asyncio.sleep(.016)
    finally:
        try:
            await release()
        finally:
            # Never close/rebind the runtime while the WGC reader is alive.
            _, was_cancelled = await _await_serial(asyncio.to_thread(stream.stop))
            cancelled |= was_cancelled
        if cancelled:
            raise asyncio.CancelledError()
    stats = stream.stats
    stats['control'] = dict(policy.stats)
    if stats.get('error'):
        status, reason = 'blocked', stats['error']
    return status, reason, snapshot or {}, stats


def _cancel_check():
    if is_current_task_cancel_requested():
        raise StopTaskException("魔方布局扫描已取消。", success=False)


async def _settle(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _cancel_check()
        await asyncio.sleep(min(.05, max(0., deadline - time.monotonic())))


def _attach_frame_paths(value, frame_lookup):
    """Make cell evidence usable without coupling to its nested schema."""
    if isinstance(value, dict):
        frame = frame_lookup.get(value.get("frame_id"))
        if frame is not None:
            value["frame_path"] = frame["path"]
            if frame.get("overlay_path"):
                value["overlay_path"] = frame["overlay_path"]
        for child in list(value.values()):
            _attach_frame_paths(child, frame_lookup)
    elif isinstance(value, list):
        for child in value:
            _attach_frame_paths(child, frame_lookup)


def _write_cell_crops(result, output_dir):
    directory=output_dir / "cells"
    directory.mkdir(exist_ok=True)
    for cell in result.get("cells", []):
        evidence=cell.get("evidence") or []
        if not evidence:continue
        best=evidence[0]
        quad=np.asarray(best.get("quad"),np.float32)
        if quad.shape!=(4,2) or not np.isfinite(quad).all():continue
        source=cv2.imread(str(output_dir / best.get("frame_path", "")))
        if source is None:continue
        transform=cv2.getPerspectiveTransform(quad,np.float32(((0,0),(95,0),(95,95),(0,95))))
        crop=cv2.warpPerspective(source,transform,(96,96))
        path=directory / f"{cell['face']}_{cell['row']}_{cell['col']}.png"
        if not cv2.imwrite(str(path),crop):raise OSError("cell_crop_save_failed")
        best["crop_path"]=path.relative_to(output_dir).as_posix()


async def _reset_view(app, output_dir, deadline):
    """Require two ordinary-board observations and an actual reset UI match."""
    template_path = Path(__file__).resolve().parents[2] / "templates" / "deep_dive_layout" / "reset_view.png"
    template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    mask = cv2.imread(str(template_path.with_name("reset_view_mask.png")), cv2.IMREAD_GRAYSCALE)
    if template is None or mask is None or mask.shape != template.shape:
        raise RuntimeError("reset_view_template_missing")
    point = None
    for index in range(2):
        _cancel_check()
        capture = await asyncio.wait_for(app.capture_async(), max(.1, min(5., deadline - time.monotonic())))
        if not capture.success or capture.image is None:
            raise RuntimeError("reset_capture_failed")
        scene = observe_scene(capture.image)
        if scene.get("scene") != "board" or not scene.get("player_turn"):
            raise RuntimeError("reset_requires_normal_player_board:" + str(scene.get("scene")))
        gray = cv2.cvtColor(capture.image, cv2.COLOR_RGB2GRAY)
        response = cv2.matchTemplate(gray[480:590, 590:700], template, cv2.TM_CCORR_NORMED, mask=mask)
        _, score, _, location = cv2.minMaxLoc(response)
        if not math.isfinite(score) or score < .94:
            raise RuntimeError("reset_view_not_recognized")
        candidate = (590 + location[0] + 32, 480 + location[1] + 28)
        if point is not None and math.dist(point, candidate) > 3:
            raise RuntimeError("reset_view_unstable")
        point = candidate
        if not cv2.imwrite(str(output_dir / f"reset_before_{index}.png"), cv2.cvtColor(capture.image, cv2.COLOR_RGB2BGR)):
            raise OSError("reset_frame_save_failed")
        await _settle(.2)
    _cancel_check()
    # SendInput's generic click emits down/up in the same instant. Unity can
    # miss that transition, so hold this visually verified UI button briefly.
    _, cancelled = await _await_serial(app.move_to_async(*point, duration=0.))
    if cancelled:
        raise asyncio.CancelledError()
    try:
        _, cancelled = await _await_serial(app.controller.mouse_down_async('left'))
        if cancelled:
            raise asyncio.CancelledError()
        await _settle(.08)
    finally:
        _, release_cancelled = await _await_serial(app.controller.mouse_up_async('left'))
        if release_cancelled:
            raise asyncio.CancelledError()
    # The live board has a small vertical/scale idle animation. Compare the
    # coherent geometry, not exact edge pixels, so that animation cannot hold
    # reset forever. Record this gate's observations for manual diagnosis.
    stable, previous = 0, None
    stability_rows = []
    reset_deadline = min(deadline, time.monotonic() + 8.)
    while time.monotonic() < reset_deadline:
        await _settle(.25)
        capture = await asyncio.wait_for(app.capture_async(), max(.1, min(3., reset_deadline - time.monotonic())))
        if not capture.success or capture.image is None:
            stable = 0
            continue
        scene = observe_scene(capture.image)
        if scene.get("scene") != "board" or not scene.get("player_turn"):
            raise RuntimeError("scene_changed_after_reset:" + str(scene.get("scene")))
        gray = cv2.cvtColor(capture.image[150:490, 440:840], cv2.COLOR_RGB2GRAY)
        diagnostic = {"stable": False}
        if previous is not None:
            points = cv2.goodFeaturesToTrack(previous, 250, .025, 10)
            if points is not None and len(points) >= 60:
                moved, valid, _ = cv2.calcOpticalFlowPyrLK(previous, gray, points, None)
                if moved is not None:
                    before=points.reshape(-1,2)[valid.ravel()>0]
                    after=moved.reshape(-1,2)[valid.ravel()>0]
                    if len(before)>=60:
                        matrix,inliers=cv2.estimateAffinePartial2D(before,after,method=cv2.RANSAC,ransacReprojThreshold=1.5)
                        if matrix is not None and inliers is not None:
                            shift=float(np.median(np.linalg.norm(after-before,axis=1)))
                            scale=math.hypot(matrix[0,0],matrix[1,0])
                            angle=abs(math.degrees(math.atan2(matrix[1,0],matrix[0,0])))
                            ratio=float(inliers.mean())
                            settled=shift<8. and .98<scale<1.02 and angle<.75 and ratio>=.40
                            diagnostic=dict(stable=bool(settled),motion_px=shift,scale=scale,angle_deg=angle,inlier_ratio=ratio)
        stable = stable + 1 if diagnostic["stable"] else 0
        stability_rows.append(diagnostic)
        (output_dir / "reset_observations.json").write_text(json.dumps(stability_rows,indent=2),encoding="utf-8")
        cv2.imwrite(str(output_dir / "reset_after.png"),cv2.cvtColor(capture.image,cv2.COLOR_RGB2BGR))
        previous = gray
        if stable >= 3:
            return {"point": list(point), "stable_frames": 4, "stability": diagnostic}
    raise RuntimeError("reset_structure_did_not_stabilize")


@action_info(name="resonance_pc.scan_consciousness_deep_dive_layout", public=True,
             read_only=False, timeout=-1,
             description="Scan an already open normal Deep Dive board and save its reconstructed layout.")
@requires_services(app="plans/aura_base/app")
async def scan_consciousness_deep_dive_layout(
    max_steps: int = 96, time_budget_sec: float = 60,
    drag_step_px: int = 300, drag_duration_sec: float = .15,
    settle_sec: float = .2, app=None,
):
    """No node clicks, game-layer rotations, or automatic battle entry are issued.

    Two short gestures calibrate the response. Then input, latest-frame vision,
    and evidence encoding run independently with bounded feedback latency.
    """
    started = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    output_dir = resolve_base_path() / "logs" / "deep_dive_scan" / run_id
    scanner = None
    frames, actions = [], []
    status, reason = "blocked", "initialization_failed"
    last_observation = {}
    reset = {}
    streaming = {}
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / "frames").mkdir()
        raw_parameters = tuple(float(value) for value in
                               (max_steps, time_budget_sec, drag_step_px,
                                drag_duration_sec, settle_sec))
        if not all(math.isfinite(value) for value in raw_parameters):
            raise ValueError("Scan parameters must be finite")
        step_limit = max(1, min(300, int(raw_parameters[0])))
        budget = max(5., min(900., raw_parameters[1]))
        step_px = max(30., min(450., raw_parameters[2]))
        duration = max(.15, min(1., raw_parameters[3]))
        settle = max(MIN_DRAG_GAP_SEC, min(2., raw_parameters[4]))
        if app is None or tuple(app.get_window_size() or ()) != (1280, 720):
            raise ValueError("扫描要求 1280×720 游戏客户区")
        # Leave a small portion of the hard budget for draining evidence and
        # producing the report. Normal operation targets 50s, with reserve to 60s.
        deadline = started + max(4., budget-1.)
        reset = await _reset_view(app, output_dir, deadline)
        scanner = LayoutScanner()
        policy = FaceScanPolicy(base_step_px=step_px, max_step_px=450)
        pending_response = False
        failures = 0
        policy_failures = 0
        previous_image = None
        while len(actions) <= step_limit:
            _cancel_check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status, reason = "partial", "time_budget_exhausted"
                break
            capture, freshness = await _capture_after_input(app)
            image = getattr(capture, "image", None)
            if not getattr(capture, "success", False) or image is None:
                failures += 1
                if failures >= 3:
                    status, reason = "blocked", "capture_failed"
                    break
                await _settle(settle)
                continue
            frame_id = len(frames)
            frame_path = output_dir / "frames" / f"{frame_id:04d}.png"
            if not cv2.imwrite(str(frame_path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
                raise OSError(f"Cannot save frame: {frame_path}")
            scene = observe_scene(image)
            row = {"frame_id": frame_id, "path": frame_path.relative_to(output_dir).as_posix(),
                   "elapsed_sec": round(time.monotonic() - started, 3),
                   "scene": scene.get("scene"), "freshness": freshness,
                   "capture_backend": getattr(capture, "backend", None)}
            frames.append(row)
            if scene.get("scene") != "board" or not scene.get("valid"):
                status, reason = "blocked", "unexpected_scene:" + str(scene.get("scene"))
                break
            duplicate = previous_image is not None and np.array_equal(image, previous_image)
            previous_image = image.copy()
            if duplicate and pending_response:
                row["duplicate_after_drag"] = True
                failures += 1
                if failures >= 3:
                    status, reason = "blocked", "no_fresh_visual_response"
                    break
                await _settle(settle)
                continue
            observation = scanner.observe(image, frame_id=frame_id)
            last_observation = observation
            overlay_path = frame_path.with_name(f"{frame_id:04d}_overlay.png")
            if not cv2.imwrite(str(overlay_path), cv2.cvtColor(scanner.annotate(image), cv2.COLOR_RGB2BGR)):
                raise OSError(f"Cannot save overlay: {overlay_path}")
            row["overlay_path"] = overlay_path.relative_to(output_dir).as_posix()
            row["observation"] = {key: observation.get(key) for key in
                                  ("tracking_ok", "pose_delta_deg", "motion_px", "quality", "faces_observed", "known_cells")}
            if scanner.result().get("layout_complete", False):
                status, reason = "completed", "layout_complete"
                break
            if not observation.get("tracking_ok", False):
                failures += 1
                row["recovery"] = "stationary_relocalization"
                if failures >= 3:
                    status, reason = "blocked", "tracking_lost"
                    break
                await _settle(settle)
                continue
            if pending_response:
                response = scanner.last_drag_response or {}
                angle = float(response.get('angle_deg') or 0.)
                if not math.isfinite(angle) or angle < .3:
                    failures += 1
                    if failures >= 3:
                        status, reason = "blocked", "no_geometric_response"
                        break
                    await _settle(settle)
                    continue
                actions[-1]['response'] = response
                last_choice = actions[-1]['policy']
                if last_choice.get('target_face') and last_choice.get('goal_normal'):
                    normal = scanner.rotation @ np.asarray(BASES[last_choice['target_face']][0], float)
                    goal = np.asarray(last_choice['goal_normal'], float)
                    actions[-1]['remaining_angle_deg'] = math.degrees(math.acos(float(
                        np.clip(np.dot(normal, goal) / (np.linalg.norm(normal)*np.linalg.norm(goal)), -1., 1.))))
                pending_response = False
            failures = 0
            if len(scanner.response_axes) == 2:
                status, reason, last_observation, streaming = await _continuous_scan(
                    app, scanner, policy, output_dir, frames, actions, started, deadline, step_limit)
                break
            if len(actions) >= step_limit:
                status, reason = "partial", "step_budget_exhausted"
                break
            if time.monotonic() + duration + settle >= deadline:
                status, reason = "partial", "time_budget_exhausted"
                break
            choice = policy.choose(scanner.rotation, scanner.result()['cells'],
                                   scanner.response_axes, quality=observation.get('quality', 0.))
            if choice.get('direction') is None:
                policy_failures += 1
                if policy_failures >= 3 or choice.get('phase') != 'recover':
                    status, reason = 'partial', choice.get('reason', 'no_useful_view_direction')
                    break
                await _settle(settle)
                continue
            policy_failures = 0
            path = _gesture_path(scanner, choice['direction'], min(450., choice['distance_px']))
            if path is None:
                status, reason = "partial", "no_safe_drag_surface"
                break
            start, end = path
            action = {"after_frame_id": frame_id, "start": list(start),
                      "end": list(end), "duration_sec": duration,
                      "policy": choice, "step_px": round(math.dist(start,end), 2)}
            actions.append(action)
            _cancel_check()
            failure = await _segmented_drag(app, scanner, start, end, duration, .04,
                                           output_dir, frames, action, started, deadline)
            if failure and failure.startswith('unexpected_scene:'):
                status, reason = 'blocked', failure
                break
            pending_response = True
            await _settle(settle)
    except (asyncio.CancelledError, StopTaskException):
        status, reason = "cancelled", "cancel_requested"
    except Exception as exc:
        status, reason = "blocked", f"{type(exc).__name__}: {exc}"
        logger.warning("[DeepDiveScan] stopped: %s", reason)
    try:
        result = scanner.result() if scanner is not None else {"layout_complete": False, "faces": {}}
    except Exception as exc:
        # Even a reconstruction failure must leave capture evidence available.
        result = {"layout_complete": False, "faces": {}, "reconstruction_error": str(exc)}
        if status != "cancelled":
            status, reason = "blocked", "result_export_failed"
    _attach_frame_paths(result, {frame["frame_id"]: frame for frame in frames})
    last_frame = str(output_dir / frames[-1]["path"]) if frames else None
    result.update(status=status, stop_reason=reason, reason=reason, run_id=run_id,
                  elapsed_sec=round(time.monotonic() - started, 3),
                  frames=frames, actions=actions,
                  reset=reset, last_frame=last_frame, streaming=streaming,
                  capture_freshness="wgc_atomic_latest_frame_with_generation_and_arrival_time" if streaming else
                  "serial_calibration_capture_with_generation_gate")
    # A snapshot is still useful for manual review after an interrupted scan,
    # but it must never be advertised as a current actionable layout.
    result["map_valid"] = status in ("completed", "partial")
    if not result["map_valid"]:
        result["layout_complete"] = False
    result["success"] = status == "completed" and bool(result.get("layout_complete"))
    try:
        _write_cell_crops(result, output_dir)
    except Exception as exc:
        result["crop_error"] = str(exc)
    paths = {}
    try:
        paths = write_layout_report(result, output_dir)
    except Exception as exc:
        result["report_error"] = str(exc)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            fallback = output_dir / "layout.json"
            fallback.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            paths = {"json_path": str(fallback)}
        except Exception as save_exc:
            result["report_error"] += "; " + str(save_exc)
    summary = {key: result.get(key) for key in ("success", "status", "stop_reason", "reason", "layout_complete", "run_id", "last_frame")}
    summary.update(output_dir=str(output_dir), steps=len(actions),
                   known_cells=result.get("known_cells", last_observation.get("known_cells", 0)),
                   faces_observed=result.get("faces_observed", last_observation.get("faces_observed", 0)),
                   **paths)
    summary.update(total_elapsed_sec=round(time.monotonic()-started, 3), target_elapsed_sec=50.,
                   reserve_used=time.monotonic()-started > 50.)
    if "report_error" in result:
        summary["report_error"] = result["report_error"]
    logger.info("[DeepDiveScan] status=%s reason=%s output=%s", status, reason, output_dir)
    return summary


"""Geometry-only, owned live experiment; never declares targets/action ready."""
from pathlib import Path
import asyncio
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from packages.aura_game import EmbeddedGameRunner
from plans.resonance_pc.src.actions import consciousness_deep_dive_scan_pc_actions as scan
from plans.resonance_pc.src.actions._deep_dive_layout_vision import LayoutScanner, BASES, DISTANCE, _angle
from plans.resonance_pc.src.actions._deep_dive_layout_semantics import detect_targets
from plans.resonance_pc.src.actions._deep_dive_front_view_control import choose_front_view
from plans.resonance_pc.src.actions._deep_dive_planned_run_vision import observe as observe_scene
from research.deep_dive_face_plane_tracker import FacePlaneScanner
from research.deep_dive_response_fit import fit_response_axes
from research.deep_dive_multiplane_tracker import MultiPlaneTracker
from research.deep_dive_face_view_quality import evaluate_face_view_quality
from research.deep_dive_exposure_prefix import choose_exposure_prefix
from research.deep_dive_local_grip import select_local_grip


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
        default=lambda v: v.tolist() if isinstance(v, np.ndarray) else str(v)), encoding='utf8')


async def experiment(app, *, output, tilt_deg=8., face='U', use_banks=False,
                     quality_stop=False, persistent_anchors=False,
                     local_navigation=False, exposure_hint_face=None,
                     exposure_hint_cosine=.60, **unused):
    output.mkdir(parents=True, exist_ok=False)
    requested_faces = face.split(',')
    if any(f not in BASES for f in requested_faces) or len(requested_faces)>1 and not use_banks:
        raise ValueError('multi-face routes require actual per-face banks')
    if exposure_hint_face is not None and (exposure_hint_face not in BASES or not use_banks):
        raise ValueError('exposure hint requires a valid normal and actual face bank')
    face = requested_faces[0]
    rows, moves = [], []
    response_records = []
    rejected_response_fits = 0
    completed_faces = []
    exposure_report = None
    tilt_azimuth = 90.
    azimuth_attempts = [45., 135., 0., 180.]
    scanner = FacePlaneScanner(target_detector=detect_targets, target_negative_evidence=False,
                               preferred_face=face)
    banks = MultiPlaneTracker(preferred_face=face,
        retain_bindings=persistent_anchors,
        direct_check_interval=5 if persistent_anchors else 0) if use_banks else None
    control = dict(latest_frame_metadata={}, observe_fn=observe_scene)
    token = scan._SCAN_CONTROL.set(control)
    last_source = None
    started = time.monotonic()
    status, reason = 'blocked', 'not_started'

    async def capture(initial=False):
        nonlocal last_source
        scan._cancel_check()
        adapter = app.target_runtime._session
        deadline = time.monotonic()+.5
        packet = None
        while packet is None and time.monotonic()<deadline:
            packet, cancelled = await scan._await_serial(asyncio.to_thread(
                adapter.capture_stream_frame,
                -1 if last_source is None else last_source['generation'],
                expected_session_id=None if last_source is None else last_source['session_id']))
            if cancelled:
                raise asyncio.CancelledError()
            if packet is None:
                await asyncio.sleep(.005)
        if packet is None:
            raise RuntimeError('atomic_capture_busy_or_not_advanced')
        image = packet['capture']
        source = dict(generation_source='atomic_wgc', capture_backend=image.backend,
            generation=packet['generation'], session_id=packet['session_id'],
            frame_time=packet['arrived_at_monotonic'], map_revision=0, frame_id=len(rows))
        if not image.success or image.image is None:
            raise RuntimeError('capture_failed')
        if source.get('generation_source') != 'atomic_wgc' or source.get('capture_backend') != 'wgc':
            raise RuntimeError('atomic_wgc_required')
        if time.monotonic()-source['frame_time'] > .5:
            raise RuntimeError('stale_capture')
        if last_source is not None and (source['session_id'] != last_source['session_id']
                or source['generation'] <= last_source['generation']
                or source['frame_time'] <= last_source['frame_time']):
            raise RuntimeError('capture_source_not_advanced')
        last_source = source
        scene = observe_scene(image.image)
        if not scan._player_board(scene):
            cv2.imwrite(str(output/'rejected_scene.png'), cv2.cvtColor(image.image, cv2.COLOR_RGB2BGR))
            save_json(output/'rejected_scene.json', dict(source=source, scene=scene,
                rejected_before_tracking=True, input_release_required=True))
            raise RuntimeError('ordinary_player_board_required')
        fid = len(rows)
        masks = detect_targets(image.image)
        # Native boxes are experimental exclusions only; no entity votes/negatives.
        masks = [dict(t, box=[t['box'][0]-16, t['box'][1]-16,
                              t['box'][2]+32, t['box'][3]+32]) for t in masks]
        before = scanner.rotation.copy()
        bank_snapshot = None
        if initial:
            observed = scanner.observe(image.image, fid, semantic=False)
            if banks is not None and observed.get('tracking_ok'):
                bank_snapshot = banks.bootstrap(image.image, source, scanner.objects, scanner.points,
                    scanner.rotation, scanner.tvec, mask_boxes=[t['box'] for t in masks])
        elif banks is not None:
            bank_snapshot = banks.update(image.image, source, mask_boxes=[t['box'] for t in masks])
            local = bank_snapshot['faces'].get(face, {})
            conflict = bank_snapshot['status'] == 'orientation_conflict'
            if not local.get('current') or conflict and not local_navigation:
                observed = dict(tracking_ok=False, reason='multiplane_'+bank_snapshot['status'])
            else:
                scanner.rotation = np.asarray(local['rotation'])
                scanner.rvec = cv2.Rodrigues(scanner.rotation)[0]
                scanner.tvec = np.asarray(local['translation']).reshape(3,1)
                scanner.quality = local['fit']['fraction']
                scanner.objects = np.asarray([r['object'] for r in local['indexed_features']])
                scanner.points = np.asarray([r['pixel'] for r in local['indexed_features']],np.float32)
                scanner.previous = cv2.cvtColor(image.image,cv2.COLOR_RGB2GRAY)
                scanner.motion_px = float('nan')  # actual image motion measured during settle
                scanner._plane_projection_face = face
                # A face near the silhouette can be steered using a different
                # currently observed face. Each grip retains its own measured
                # translation/source; the control target remains `face`.
                scanner.last_projected = ([item for name in bank_snapshot['faces']
                    for item in banks.local_visible(name)] if local_navigation else
                    [item for name in bank_snapshot['faces'] for item in banks.visible(name)])
                scanner.last_targets = masks
                observed = dict(tracking_ok=True, reason=('actual_local_face_geometry_shared_orientation_invalid'
                    if conflict else 'actual_current_multiplane_geometry_only'))
                scanner.face_plane_diagnostic = dict(selected_face=face, geometry_only=True,
                    shared_orientation_valid=not conflict,
                    faces=bank_snapshot['fit_reports'], full_cube_coordinates_valid=False)
        else:
            observed = scanner.track_frame(image.image, fid, mask_targets=masks)
        filename = f'frame_{fid:04d}.png'
        cv2.imwrite(str(output/filename), cv2.cvtColor(image.image, cv2.COLOR_RGB2BGR))
        row = dict(frame_id=fid, source=source, path=filename, scene=scene,
            tracking_ok=observed.get('tracking_ok'), reason=observed.get('reason'),
            quality=scanner.quality, rotation=scanner.rotation.copy(), translation=scanner.tvec.copy(),
            rotation_change_deg=_angle(scanner.rotation, before),
            motion_px=None if banks is not None and not initial else scanner.motion_px,
            feature_count=len(scanner.points), masks=masks,
            plane_fit=getattr(scanner, 'face_plane_diagnostic', None))
        if bank_snapshot is not None:
            row['multiplane'] = dict(status=bank_snapshot['status'],
                comparisons=bank_snapshot['orientation_comparisons'],
                direct_checks=bank_snapshot.get('direct_checks', {}),
                binding_policy=bank_snapshot.get('binding_policy'),
                current_measurement_counts=bank_snapshot.get('current_measurement_counts', {}),
                faces={f:{k:v for k,v in value.items() if k!='indexed_features'}
                       for f,value in bank_snapshot['faces'].items()})
        rows.append(row)
        if not observed.get('tracking_ok'):
            raise RuntimeError('tracking_failed:'+str(observed.get('reason')))
        return image.image, row

    async def settle():
        previous = scanner.rotation.copy()
        consecutive = 0
        quiet = []
        for _ in range(16):
            await asyncio.sleep(.08)
            old_gray = scanner.previous.copy()
            old_points = scanner.points.copy()
            old_objects = scanner.objects.copy()
            rgb, row = await capture()
            delta = _angle(scanner.rotation, previous)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            after, valid, _ = cv2.calcOpticalFlowPyrLK(old_gray, gray, old_points, None,
                winSize=(25,25), maxLevel=4)
            quiet_frame = False
            if after is not None:
                backward, reverse, _ = cv2.calcOpticalFlowPyrLK(gray, old_gray, after, None,
                    winSize=(25,25), maxLevel=4)
                good = ((valid.ravel()>0)&(reverse.ravel()>0)
                        &(np.linalg.norm(backward-old_points,axis=1)<1.8))
                before = old_points[good].reshape(-1,2)
                after = after[good].reshape(-1,2)
                measured_motion = float(np.median(np.linalg.norm(after-before,axis=1)))
                row['settle_measured_motion_px'] = measured_motion
                names = list(BASES)
                normals = np.asarray([BASES[f][0] for f in names])
                faces = np.argmin(np.abs(old_objects[good]@normals.T-DISTANCE),axis=1)
                audits = {}
                for face in range(6):
                    selected = faces==face
                    a, z = before[selected], after[selected]
                    if len(a)<25 or min(np.ptp(a,axis=0))<60:
                        continue
                    matrix, inliers = cv2.estimateAffinePartial2D(a, z,
                        method=cv2.RANSAC, ransacReprojThreshold=1.2)
                    if matrix is None or inliers is None:
                        continue
                    scale = float(np.hypot(matrix[0,0], matrix[1,0]))
                    roll = float(np.degrees(np.arctan2(matrix[1,0], matrix[0,0])))
                    residual = np.linalg.norm(z-(a@matrix[:,:2].T+matrix[:,2]), axis=1)
                    flow = dict(scale=scale, roll_deg=roll, inlier_ratio=float(inliers.mean()),
                                median_residual_px=float(np.median(residual)), points=len(a))
                    flow['quiet'] = (abs(roll)<.3
                        and .97<scale<1.03 and flow['inlier_ratio']>.7
                        and flow['median_residual_px']<.65)
                    audits[names[face]] = flow
                row['settle_flow_by_face'] = audits
                quiet_frame = bool(audits and all(v['quiet'] for v in audits.values())
                                   and delta<1. and measured_motion<8.)
            consecutive = consecutive+1 if quiet_frame else 0
            quiet = (quiet+[scanner.rotation.copy()])[-4:] if quiet_frame else []
            previous = scanner.rotation.copy()
            span = max((_angle(a,b) for a in quiet for b in quiet), default=999.)
            row['settle_rotation_span_deg'] = span
            if consecutive >= 4 and span < 1.1:
                row['settled'] = True
                return rgb, row
        raise RuntimeError('motion_did_not_settle')

    async def pulse(displacement, label):
        nonlocal rejected_response_fits
        scan._cancel_check()
        vector = np.asarray(displacement, float)
        distance = float(np.linalg.norm(vector))
        if distance < 2.:
            raise RuntimeError('input_below_quantization')
        path = scan._gesture_path(scanner, vector/distance, max(24., distance))
        grip_proof = None
        if path is None and local_navigation and banks is not None:
            if not 0 <= time.monotonic()-rows[-1]['source']['frame_time'] <= .5:
                raise RuntimeError('local_grip_requires_fresh_current_source')
            grip_report = select_local_grip(scanner.last_projected, vector/distance,
                min(24., distance), rows[-1]['source'],
                target_boxes=[dict(box=item['box'], source=rows[-1]['source'])
                    for item in scanner.last_targets])
            save_json(output/'latest_local_grip.json', grip_report)
            if grip_report['status'] == 'local_grip_proposal':
                grip_proof = grip_report['grip']
                path = (grip_proof['start'], grip_proof['end'])
        if path is None:
            raise RuntimeError('safe_drag_path_unavailable')
        start = np.asarray(path[0], int)
        approved = float(np.linalg.norm(np.asarray(path[1])-start))
        if grip_proof is not None:
            # Use the exact integer segment already checked against the quad,
            # HUD and sprite exclusions; do not re-quantize a different path.
            end = np.asarray(path[1], int)
            actual = end-start
        else:
            vector *= min(1., approved/max(distance, 1e-9))
            end = start+np.rint(vector).astype(int)
            actual = end-start
            if np.linalg.norm(actual) > approved+.01:
                actual = np.trunc(vector).astype(int)
                end = start+actual
        if np.linalg.norm(actual) < 2.:
            raise RuntimeError('input_below_quantization')
        before = scanner.rotation.copy()
        grip_candidates = [dict(face=item['face'], row=item['row'], col=item['col'],
                                source=item.get('source'))
                           for item in scanner.last_projected
                           if cv2.pointPolygonTest(np.float32(item['quad']),
                                                  tuple(map(float, start)), False) >= 0]
        scanner.note_drag(*map(int, actual))
        _, cancelled = await scan._await_serial(app.move_to_async(*map(int, start), duration=0.))
        if cancelled:
            raise asyncio.CancelledError()
        try:
            _, cancelled = await scan._await_serial(app.controller.mouse_down_async('left'))
            if cancelled:
                raise asyncio.CancelledError()
            await asyncio.sleep(.04)
            for fraction in np.linspace(.25, 1., 4):
                point = start+np.rint(actual*fraction).astype(int)
                scan._cancel_check()
                _, cancelled = await scan._await_serial(app.move_to_async(*map(int, point), duration=.03))
                if cancelled:
                    raise asyncio.CancelledError()
                await capture()
        finally:
            await scan._await_serial(app.controller.mouse_up_async('left'))
            scanner.finish_drag(*map(int, actual))
        rgb, row = await settle()
        rotation_delta = cv2.Rodrigues(scanner.rotation@before.T)[0].ravel()
        # Learn only genuinely axis-aligned, settled measured pulses.
        if actual[1] == 0 or actual[0] == 0:
            scanner._learn_response()
        else:
            scanner.pending_drag = None
        response_records.append(dict(dx=int(actual[0]), dy=int(actual[1]),
            rotation_delta=rotation_delta.tolist(), rotation_unit='radians',
            before_rotation=before.tolist(), after_rotation=scanner.rotation.tolist(),
            settled_source=row['source']))
        response_fit = fit_response_axes(response_records, previous_axes=scanner.response_axes)
        if response_fit['accepted']:
            scanner.response_axes = {k:np.asarray(v,float) for k,v in response_fit['axes'].items()}
            rejected_response_fits = 0
        else:
            rejected_response_fits += 1
        motion = dict(label=label, start=start, end=end, displacement=actual, approved_path_px=approved,
            grip_candidates=grip_candidates,
            rotation_delta=rotation_delta, rotation_deg=float(np.degrees(np.linalg.norm(rotation_delta))),
            response_axes=scanner.response_axes.copy(), settled_frame_id=row['frame_id'])
        motion['response_fit'] = response_fit
        motion['local_grip_proof'] = grip_proof
        moves.append(motion)
        print('PULSE', json.dumps(motion, default=lambda v:v.tolist()), flush=True)
        return rgb, row

    def view_quality():
        row = rows[-1]
        items = sorted(scanner.visible(), key=lambda item:item['index'])
        supported = bool(row['tracking_ok'])
        if banks is not None:
            snapshot = banks.snapshot()
            supported = (supported and (local_navigation or snapshot['status'] != 'orientation_conflict')
                         and snapshot['faces'].get(face, {}).get('current') is True)
        age = time.monotonic()-row['source']['frame_time']
        result = evaluate_face_view_quality([item['quad'] for item in items], face=face,
            source=row['source'], source_valid=0 <= age <= .35,
            current_support=dict(valid=supported, face=face, source=row['source']),
            settle_evidence=dict(settled=row.get('settled') is True, source=row['source']))
        # This is a research capture preference, not a recognition guarantee.
        result['minimum_facing_cosine'] = min((item['cosine'] for item in items), default=0.)
        result['near_front_preference_met'] = result['minimum_facing_cosine'] >= np.cos(np.radians(45.))
        result['source_age_at_quality_sec'] = age
        result['shared_orientation_valid'] = (banks is not None and
            banks.snapshot()['shared_rotation'] is not None)
        result['local_navigation_only'] = local_navigation
        row['view_quality'] = result
        return result

    async def acquire_exposure():
        # This route only captures pixels for independent discovery. It never
        # projects the unknown face or gives its normal hint an identity vote.
        observations = []
        total_rotation = 0.
        unfulfilled = 0
        for exposure_step in range(48):
            if rejected_response_fits >= 3:
                await pulse([24, 0], f'exposure_recalibration_x_{exposure_step}')
                await pulse([0, 24], f'exposure_recalibration_y_{exposure_step}')
            rotation = scanner.rotation.copy()
            position = scanner.tvec.reshape(3)+rotation@(
                np.asarray(BASES[face][0])*DISTANCE)
            ray = -position/np.linalg.norm(position)
            proposal = choose_exposure_prefix(rotation, ray, BASES[face][0],
                BASES[exposure_hint_face][0], scanner.response_axes,
                exposure_hint_cosine=exposure_hint_cosine)
            entry = dict(step=exposure_step, measured_reference_face=face,
                relative_target_normal_hint=exposure_hint_face, source=rows[-1]['source'],
                frame_path=rows[-1]['path'], proposal=proposal,
                independent_grid_discovery_required=True, unknown_face_identity_valid=False,
                unknown_face_projection_valid=False, targets_ready=False)
            observations.append(entry)
            save_json(output/'exposure_observations.json', observations)
            print('EXPOSURE', json.dumps(entry), flush=True)
            if proposal['status'] == 'exposure_hint_ready':
                return dict(status='relative_hint_ready_requires_pixel_audit',
                    target_normal_hint=exposure_hint_face, reference_face=face,
                    source=rows[-1]['source'], frame_path=rows[-1]['path'],
                    actual_grid_ready=False, identity_ready=False, targets_ready=False,
                    observed_rotation_path_deg=total_rotation, observations=len(observations))
            if proposal['status'] != 'move':
                raise RuntimeError('exposure_prefix_rejected:'+proposal['reason'])
            _, current = await pulse(proposal['displacement_px'],
                f'exposure_hint_{exposure_hint_face}_using_{face}_{exposure_step}')
            position_after = scanner.tvec.reshape(3)+scanner.rotation@(
                np.asarray(BASES[face][0])*DISTANCE)
            ray_after = -position_after/np.linalg.norm(position_after)
            actual_cosine = float((scanner.rotation@np.asarray(BASES[exposure_hint_face][0]))@ray_after)
            entry['actual_after_hint_cosine'] = actual_cosine
            entry['actual_after_source'] = current['source']
            entry['actual_after_frame_path'] = current['path']
            total_rotation += moves[-1]['rotation_deg']
            entry['observed_rotation_path_deg'] = total_rotation
            unfulfilled = unfulfilled+1 if actual_cosine < proposal['target_hint_cosine']-.015 else 0
            save_json(output/'exposure_observations.json', observations)
            if unfulfilled >= 3:
                raise RuntimeError('exposure_predicted_improvement_not_observed')
            if total_rotation > 150.:
                raise RuntimeError('exposure_observed_rotation_budget_exhausted')
        raise RuntimeError('bounded_exposure_prefix_without_hint_readiness')

    try:
        reset = await scan._reset_view(app, output, time.monotonic()+20.)
        await capture(initial=True)
        await settle()
        await pulse([24, 0], 'calibration_x')
        await pulse([0, 24], 'calibration_y')
        for step in range(64*len(requested_faces)):
            if rejected_response_fits >= 3:
                # A recent nearly parallel gesture window cannot identify both
                # axes. Acquire actual independent probes rather than invent J.
                await pulse([24,0], f'recalibration_x_{step}')
                await pulse([0,24], f'recalibration_y_{step}')
            choice = choose_front_view(scanner.rotation, scanner.tvec, face, scanner.response_axes,
                face_center=np.asarray(BASES[face][0])*DISTANCE,
                max_step_px=24, max_step_deg=5, tilt_deg=tilt_deg,
                tilt_azimuth_deg=tilt_azimuth, tolerance_deg=2.5)
            quality = view_quality()
            quality_ready = quality_stop and quality['geometry_shot_ready'] and quality['near_front_preference_met']
            save_json(output/'latest_choice.json', choice)
            print('CONTROL', step, json.dumps(choice), flush=True)
            if choice['status'] == 'aligned' or quality_ready:
                rgb, row = await settle()
                final_choice = choose_front_view(scanner.rotation, scanner.tvec, face, scanner.response_axes,
                    face_center=np.asarray(BASES[face][0])*DISTANCE,
                    max_step_px=24, max_step_deg=5, tilt_deg=tilt_deg,
                    tilt_azimuth_deg=tilt_azimuth, tolerance_deg=2.5)
                quality = view_quality()
                quality_ready = quality_stop and quality['geometry_shot_ready'] and quality['near_front_preference_met']
                if quality_stop and not quality_ready and final_choice['status'] == 'aligned':
                    if not azimuth_attempts:
                        raise RuntimeError('bounded_face_quality_search_exhausted')
                    tilt_azimuth = azimuth_attempts.pop(0)
                    continue
                if not quality_ready and final_choice['status'] != 'aligned':
                    continue
                save_json(output/'final_choice.json', final_choice)
                visible = [item for item in scanner.visible() if scanner.cells[item['index']]['face']==face]
                overlay = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                for item in visible:
                    cv2.polylines(overlay, [np.int32(item['quad'])], True, (0,255,0), 2)
                    cell = scanner.cells[item['index']]
                    cv2.putText(overlay, f"{face}{cell['row']}{cell['col']}", tuple(np.int32(item['centre'])),
                                cv2.FONT_HERSHEY_SIMPLEX, .45, (255,255,255), 1)
                cv2.imwrite(str(output/'aligned_overlay.png'), overlay)
                cv2.imwrite(str(output/f'{face}_aligned_overlay.png'), overlay)
                save_json(output/f'{face}_aligned_source.json', dict(frame=row,
                    choice=final_choice, view_quality=quality,
                    quality_stop=quality_stop, geometry_only=True, targets_ready=False))
                completed_faces.append(face)
                if len(completed_faces)==len(requested_faces):
                    if exposure_hint_face is not None:
                        exposure_report = await acquire_exposure()
                        status, reason = 'exposure_hint_only', 'requires_independent_pixel_grid_and_identity_audit'
                        break
                    status, reason = 'geometry_only', ('face_quality_ready_requires_entity_audit' if quality_stop
                        else 'front_normal_aligned_requires_manual_grid_audit')
                    break
                next_face = requested_faces[len(completed_faces)]
                # Retain the currently measured face during reveal. Its pose
                # proposes only a target normal; never a target-face grid/T.
                for reveal_step in range(24):
                    if banks.snapshot()['faces'].get(next_face, {}).get('current') is True:
                        break
                    proposal = choose_front_view(scanner.rotation, scanner.tvec,
                        next_face, scanner.response_axes,
                        face_center=np.asarray(BASES[next_face][0])*DISTANCE,
                        max_step_px=24, max_step_deg=5, tilt_deg=tilt_deg,
                        tolerance_deg=2.5)
                    save_json(output/'latest_reveal_proposal.json', dict(
                        measured_face=face, target_face=next_face, proposal=proposal,
                        reference_rotation=scanner.rotation,
                        reference_translation=scanner.tvec,
                        bank_status=banks.snapshot()['status'],
                        target_face_projection_valid=False,
                        camera_ray_uses_reference_face_translation=True,
                        source=rows[-1]['source']))
                    if proposal['status'] != 'move':
                        raise RuntimeError('unmeasured_face_reveal_rejected:'+proposal['reason'])
                    await pulse(proposal['displacement_px'],
                        f'reveal_{next_face}_using_{face}_measurement_{reveal_step}')
                    if banks.snapshot()['faces'].get(next_face, {}).get('current') is True:
                        break
                else:
                    raise RuntimeError('bounded_face_reveal_without_actual_support')
                face = next_face
                tilt_azimuth = 90.
                azimuth_attempts = [45., 135., 0., 180.]
                scanner.preferred_face = face
                banks.preferred_face = face
                await capture()
                response_records.clear()
                scanner.response_axes = {}
                rejected_response_fits = 0
                await pulse([24,0], f'transfer_calibration_{face}_x')
                await pulse([0,24], f'transfer_calibration_{face}_y')
                continue
            if choice['status'] != 'move':
                raise RuntimeError('control_rejected:'+choice['reason'])
            vector = np.asarray(choice['displacement_px'], float)
            # The mixed-response fit uses both actual input components; never
            # divide a diagonal gesture's rotation by its dominant mouse axis.
            await pulse(vector, f'align_{face}_{step}')
        else:
            reason = 'front_view_no_convergence'
    except Exception as exc:
        reason = f'{type(exc).__name__}:{exc}'
    finally:
        await scan._await_serial(app.controller.mouse_up_async('left'))
        scan._SCAN_CONTROL.reset(token)
        save_json(output/'frames.json', rows)
        save_json(output/'moves.json', moves)
        if banks is not None:
            save_json(output/'multiplane_final.json', banks.snapshot())
        summary = dict(status=status, reason=reason, elapsed_sec=time.monotonic()-started,
            frames=len(rows), moves=len(moves), targets_ready=False, formal_acceptance=False,
            mask_policy='native_current_padded_experimental_incomplete_recall',
            geometry_mode='multiplane_bank' if banks is not None else 'independent_face_plane',
            persistent_anchors=persistent_anchors,
            local_navigation_only=local_navigation,
            requested_tilt_deg=tilt_deg, preferred_face=face,
            quality_stop=quality_stop,
            exposure_hint_face=exposure_hint_face, exposure_hint_cosine=exposure_hint_cosine,
            exposure_report=exposure_report,
            requested_faces=requested_faces, completed_geometry_faces=completed_faces,
            full_cube_coordinates_valid=False,
            rotation=scanner.rotation, translation=scanner.tvec, response_axes=scanner.response_axes)
        save_json(output/'summary.json', summary)
        print('EXPERIMENT_RESULT', json.dumps(summary,default=lambda v:v.tolist()), flush=True)
        adapter = app.target_runtime._session
        backend = adapter.capture_backend
        native = getattr(backend, '_session', None)
        before_close = native.health() if native is not None else None
        save_json(output/'cleanup.json', dict(phase='closing_capture', before=before_close))
        await scan._await_serial(asyncio.to_thread(adapter.close))
        save_json(output/'cleanup.json', dict(phase='capture_closed', before=before_close,
            after=native.health() if native is not None else None,
            backend_session_released=getattr(backend, '_session', None) is None))
    return dict(summary=summary)


def main():
    output = (ROOT/Path(sys.argv[1])).resolve()
    if not output.is_relative_to(ROOT/'.pytest_tmp'):
        raise ValueError('experiment output must stay in repository .pytest_tmp')
    cv2.setNumThreads(1)
    original = scan.run_layout_scan
    tilt = float(sys.argv[2]) if len(sys.argv)>2 else 8.
    face = sys.argv[3] if len(sys.argv)>3 else 'U'
    use_banks = len(sys.argv)>4 and sys.argv[4]=='banks'
    quality_stop = len(sys.argv)>5 and sys.argv[5]=='quality'
    persistent_anchors = len(sys.argv)>6 and sys.argv[6]=='anchors'
    local_navigation = len(sys.argv)>7 and sys.argv[7]=='local'
    exposure_hint_face = sys.argv[8] if len(sys.argv)>8 else None
    exposure_hint_cosine = float(sys.argv[9]) if len(sys.argv)>9 else .60
    async def configured(app, **kwargs):
        return await experiment(app, output=output, tilt_deg=tilt, face=face,
                                use_banks=use_banks, quality_stop=quality_stop,
                                persistent_anchors=persistent_anchors,
                                local_navigation=local_navigation,
                                exposure_hint_face=exposure_hint_face,
                                exposure_hint_cosine=exposure_hint_cosine, **kwargs)
    scan.run_layout_scan = configured
    runner = EmbeddedGameRunner(profile='embedded_full')
    cid = None
    try:
        dispatch = runner.run_task(game_name='resonance_pc',
            task_ref='tasks:consciousness_deep_dive_scan_pc.yaml:consciousness_deep_dive_scan_pc',
            inputs={'time_budget_sec':90}, wait=False)
        cid = str(dispatch['cid'])
        for _ in range(1200):
            record = runner.get_run(cid)
            if str(record.get('status','')).lower() in {'success','failed','error','cancelled','stopped'} and not record.get('execution_pending'):
                save_json(output.parent/(output.name+'_dispatch.json'), record)
                break
            time.sleep(.5)
        else:
            runner.cancel_task(cid)
            runner.wait_for_run(cid, timeout_sec=20)
    finally:
        if cid:
            record = runner.get_run(cid)
            if record.get('execution_pending'):
                runner.cancel_task(cid)
                runner.wait_for_run(cid, timeout_sec=20)
        runner.close()
        scan.run_layout_scan = original
        print('OWNED_RUNTIME_CLOSED', flush=True)
        # Exit only this disposable helper, after explicit capture close, saved
        # cleanup evidence, input release, and owned scheduler close.
        import ctypes
        ctypes.windll.kernel32.TerminateProcess(ctypes.windll.kernel32.GetCurrentProcess(), 0)


if __name__ == '__main__':
    main()

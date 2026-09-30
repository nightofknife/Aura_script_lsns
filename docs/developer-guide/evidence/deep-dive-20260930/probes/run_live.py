"""Run the authorized live task once with an external cancellation flag."""
from pathlib import Path
import json
import sys
import time

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from packages.aura_game import EmbeddedGameRunner

folder = Path(__file__).resolve().parent
stop_flag = folder / 'stop_requested'
runner = EmbeddedGameRunner(profile='embedded_full')
cid = None
cancelled = False
started = time.monotonic()
last_progress = None
try:
    dispatch = runner.run_task(
        game_name='resonance_pc',
        task_ref='tasks:consciousness_deep_dive_planned_run_pc.yaml:consciousness_deep_dive_planned_run_pc',
        inputs={}, wait=False)
    cid = str(dispatch['cid'])
    (folder / 'dispatch.json').write_text(json.dumps(dispatch, ensure_ascii=False, indent=2), encoding='utf-8')
    print('LIVE_DISPATCH', cid, flush=True)
    while True:
        if not cancelled and (stop_flag.exists() or time.monotonic() - started > 3600):
            runner.cancel_task(cid)
            cancelled = True
            print('LIVE_CANCEL_REQUESTED', cid, flush=True)
        record = runner.get_run(cid)
        for path in sorted((ROOT / 'logs/deep_dive_planned_run').glob('*/session.json'),
                           key=lambda value: value.stat().st_mtime, reverse=True):
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if state.get('cid') != cid:
                continue
            compact = {key: state.get(key) for key in (
                'status', 'phase', 'plane_index', 'rounds_remaining', 'scan_epoch',
                'known_cells', 'turns_completed', 'reason', 'report_path')}
            progress = json.dumps(compact, ensure_ascii=False)
            if progress != last_progress:
                print('LIVE_PROGRESS', progress, flush=True)
                last_progress = progress
            break
        if str(record.get('status', '')).lower() in {
                'success', 'failure', 'failed', 'error', 'cancelled', 'canceled', 'stopped'} and not record.get('execution_pending'):
            (folder / ('run_' + cid + '.json')).write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            print('LIVE_TASK_ENDED', cid, record.get('status'), flush=True)
            break
        time.sleep(1)
finally:
    if cid is not None:
        record = runner.get_run(cid)
        if str(record.get('status', '')).lower() not in {
                'success', 'failure', 'failed', 'error', 'cancelled', 'canceled', 'stopped'}:
            runner.cancel_task(cid)
            try:
                runner.wait_for_run(cid, timeout_sec=30)
            except TimeoutError:
                pass
    runner.close()

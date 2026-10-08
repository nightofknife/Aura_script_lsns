"""Root-owned research controller: ordered visual arrival, never pixel endpoints."""
from pathlib import Path
import argparse
import json,sys,time,hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
from tools.deep_dive_vertical_command import issue
from research.deep_dive_four_view_template_alignment import FourViewTemplateAligner
from research.deep_dive_four_view_reading_check import check_readings,temporal_consensus,load_reference_inputs


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf8')


def main():
    from research.deep_dive_four_view_pose_feedback import orientation_feedback
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Existing broker run folder under repository .pytest_tmp')
    parser.add_argument('run_count', type=int, nargs='?', default=5, choices=range(1, 6))
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, required=True,
                        help='Root used to resolve image paths stored in annotations')
    args = parser.parse_args()
    cv2.setNumThreads(1)
    folder=(ROOT/args.folder).resolve()
    if not folder.is_relative_to(ROOT/'.pytest_tmp'):raise ValueError('repo_scope_required')
    run_count=args.run_count
    truth_path,reference_root,truth=load_reference_inputs(args.annotations,args.reference_root)
    aligner=FourViewTemplateAligner(truth_path,reference_root)
    if not (folder/'commands').is_dir():
        raise ValueError(f'Existing broker commands directory required: {folder / "commands"}')
    sources=[Path(__file__),ROOT/'research/deep_dive_four_view_pose_feedback.py',
        ROOT/'research/deep_dive_four_view_template_alignment.py',ROOT/'research/deep_dive_four_view_node_reader.py',
        ROOT/'research/deep_dive_four_view_reading_check.py',ROOT/'tools/deep_dive_vertical_capture_broker.py',
        ROOT/'plans/resonance_pc/src/actions/_deep_dive_layout_semantics.py',truth_path]
    sources.extend(sorted({(reference_root/g['image']).resolve() for g in truth['views']}))
    def source_key(path):
        return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    freeze={source_key(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    save(folder/'freeze.json',dict(files=freeze,sensitivity_adapter='placeholder: no calibrated cross-sensitivity model',
        arrival_role='visual orientation feedback; drag pixels are bounded actuator commands only'))
    number=0;ledger=[];trace=[]
    def command(value):
        nonlocal number
        number+=1
        return issue(folder,number,value)
    def inspect(frame,view):
        rgb=cv2.cvtColor(cv2.imread(str(folder/frame['path'])),cv2.COLOR_BGR2RGB)
        aligned=aligner.align(rgb,reference_view=view)
        feedback=orientation_feedback(aligned,expected_view=view)
        row=dict(view=view,frame=frame,feedback=feedback)
        trace.append(row);save(folder/'feedback_trace.json',trace)
        save(folder/f"alignment_view{view}_{frame['source']['frame_id']:04d}.json",aligned)
        print(json.dumps(dict(event='feedback',view=view,path=frame['path'],feedback=feedback),ensure_ascii=False),flush=True)
        return rgb,aligned,feedback
    try:
        for run in range(1,run_count+1):
            if any(hashlib.sha256(p.read_bytes()).hexdigest()!=freeze[source_key(p)] for p in sources):
                raise RuntimeError('frozen_implementation_changed_streak_invalid')
            begin=time.monotonic();reset=command(dict(op='reset',label=f'run{run}_reset'))
            entry=dict(run=run,reset=reset,views=[],passed=False)
            save(folder/'ledger.json',ledger+[entry])
            for view in range(1,5):
                frame=reset['frame'] if view==1 else entry['views'][-1]['frame']
                previous=None;previous_dy=None;seen_diagnostic=False;accepted=False
                for step in range(65):
                    rgb,aligned,feedback=inspect(frame,view)
                    if feedback.get('arrival_candidate'):
                        witness_readings=[check_readings(rgb,aligned,truth)]
                        witness_frames=[frame]
                        confirmations=[]
                        for repeat in range(2):
                            fresh=command(dict(op='capture',wait=.35,label=f'run{run}_view{view}_stable{repeat}'))['frame']
                            rgb,aligned,fb=inspect(fresh,view)
                            confirmations.append(dict(frame=fresh,feedback=fb))
                            if not fb.get('arrival_candidate'):break
                            witness_readings.append(check_readings(rgb,aligned,truth))
                            witness_frames.append(fresh)
                        if len(confirmations)==2 and all(c['feedback'].get('arrival_candidate') for c in confirmations):
                            source_ids=[(w['source']['session_id'],w['source']['generation']) for w in witness_frames]
                            if len(set(source_ids))!=3 or len({s[0] for s in source_ids})!=1:
                                raise RuntimeError('distinct_same_session_sources_required')
                            reading=temporal_consensus(witness_readings)
                            reading['actual_witness_frames']=witness_frames
                            save(folder/f'run{run}_view{view}_reading_witnesses.json',witness_readings)
                            save(folder/f'run{run}_view{view}_readings.json',reading)
                            if not reading['passed']:raise RuntimeError(f'run{run}_view{view}_node_reading_failed')
                            entry['views'].append(dict(view=view,frame=fresh,confirmations=confirmations,
                                reading_summary={k:v for k,v in reading.items() if k!='records'}))
                            accepted=True;break
                        frame=fresh;previous=None;previous_dy=None
                        continue
                    error=feedback.get('signed_error_deg')
                    valid=feedback.get('valid_diagnostic') and error is not None
                    dy=-80
                    if valid:
                        seen_diagnostic=True
                        direction=feedback['direction_up_error_sign']
                        # Coarse feedback sign is measured from actual calibration pairs.
                        dy=int(direction*(1 if error>0 else -1)*20)
                        if previous is not None and previous_dy:
                            slope=(error-previous)/previous_dy
                            if abs(slope)>.002:
                                dy=int(round(max(-80,min(80,-error/slope))))
                                if abs(dy)<4:dy=4 if dy>=0 else -4
                        previous=float(error);previous_dy=dy
                    elif seen_diagnostic:
                        raise RuntimeError(f'run{run}_view{view}_lost_local_feedback')
                    command_result=command(dict(op='drag',dy=dy,duration=.5,label=f'run{run}_view{view}_step{step}'))
                    frame=command_result['frame']
                if not accepted:raise RuntimeError(f'run{run}_view{view}_arrival_not_reached')
                save(folder/'ledger.json',ledger+[entry])
                print(json.dumps(dict(event='view_complete',run=run,view=view),ensure_ascii=False),flush=True)
            entry.update(passed=True,elapsed_sec=time.monotonic()-begin)
            ledger.append(entry);save(folder/'ledger.json',ledger)
            print(json.dumps(dict(event='run_complete',run=run,streak=len(ledger),elapsed_sec=entry['elapsed_sec']),ensure_ascii=False),flush=True)
    except Exception as exc:
        save(folder/'failure.json',dict(error=f'{type(exc).__name__}:{exc}',completed_streak=len(ledger)))
        raise
    finally:
        command(dict(op='close'))

if __name__=='__main__':main()

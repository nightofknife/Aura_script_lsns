"""Read actual registered panels, then compare against separate manual truth."""
from pathlib import Path
import argparse
import json, sys, time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from research.deep_dive_four_view_node_reader import read_four_view_node
from plans.resonance_pc.src.actions._deep_dive_layout_vision import _crop


def load_reference_inputs(annotations, reference_root):
    """Validate all caller-supplied reference pixels before inference or input."""
    annotations = Path(annotations).expanduser().resolve()
    reference_root = Path(reference_root).expanduser().resolve()
    if not annotations.is_file():
        raise ValueError(f'Annotation JSON does not exist: {annotations}')
    if not reference_root.is_dir():
        raise ValueError(f'Reference image root does not exist: {reference_root}')
    truth = json.loads(annotations.read_text(encoding='utf-8'))
    if {group['view'] for group in truth['views']} != {1, 2, 3, 4}:
        raise ValueError('Annotations must contain reference views 1, 2, 3 and 4')
    for group in truth['views']:
        image = (reference_root / group['image']).resolve()
        if not image.is_file():
            raise ValueError(f'Reference image does not exist: {image}')
        bgr = cv2.imread(str(image), cv2.IMREAD_COLOR)
        if bgr is None or bgr.shape != (720, 1280, 3):
            raise ValueError(f'Reference image must be readable 1280x720 RGB/BGR: {image}')
    return annotations, reference_root, truth


def check_readings(rgb, alignment, truth):
    records=[]
    for panel in alignment['results']:
        if not panel['proposal_accepted']:
            return dict(passed=False, reason='panel_geometry_rejected', records=[])
        for cell in panel['projected_reference_cells']:
            crop=_crop(rgb,np.float32(cell['quad']))
            reading=read_four_view_node(crop)
            records.append(dict(face=panel['reference_panel'],row=cell['row'],col=cell['col'],
                quad=cell['quad'],result=reading))
    # The recognition API above receives pixels only. Labels enter evaluation here.
    for record in records:
        teacher=next(g for g in truth['views'] if g['face']==record['face'])
        label=next(c for c in teacher['cells'] if (c['row'],c['col'])==(record['row'],record['col']))
        record.update(expected=label['expected_icon'],scored=not label['occluded'] and label['expected_icon'] is not None)
    scored=[r for r in records if r['scored']]
    correct=sum(r['result']['reading']['icon_id']==r['expected'] for r in scored)
    unknown=sum(r['result']['reading']['icon_id'] is None for r in scored)
    wrong=len(scored)-correct-unknown
    return dict(passed=correct==len(scored),correct=correct,unknown=unknown,wrong=wrong,
                scored=len(scored),records=records)


def temporal_consensus(reports):
    """Require two real agreeing readings; any confirmed class conflict is unknown.

    Caller must supply distinct stable sources on an unchanged board. Manual
    expected labels do not select the reading or discard conflicting classes.
    """
    if len(reports)!=3 or any(not r.get('records') for r in reports):
        return dict(passed=False,reason='three_registered_frames_required',records=[])
    fused=[]
    for base in reports[0]['records']:
        identity=(base['face'],base['row'],base['col'])
        witnesses=[next(r for r in p['records'] if (r['face'],r['row'],r['col'])==identity) for p in reports]
        classes=[r['result']['reading']['icon_id'] for r in witnesses]
        confirmed=[x for x in classes if x is not None]
        accepted=confirmed[0] if len(confirmed)>=2 and len(set(confirmed))==1 else None
        fused.append(dict(face=base['face'],row=base['row'],col=base['col'],
            reading=dict(icon_id=accepted),actual_frame_readings=classes,
            expected=base['expected'],scored=base['scored']))
    scored=[r for r in fused if r['scored']]
    correct=sum(r['reading']['icon_id']==r['expected'] for r in scored)
    unknown=sum(r['reading']['icon_id'] is None for r in scored)
    return dict(passed=correct==len(scored),correct=correct,unknown=unknown,
                wrong=len(scored)-correct-unknown,scored=len(scored),records=fused,
                policy='at_least_two_confirmed_same_class_zero_confirmed_conflicts')


def main():
    from research.deep_dive_four_view_template_alignment import FourViewTemplateAligner
    from research.read_saved_front_view import add_detector_arguments, detector_config
    from plans.resonance_pc.src.services.deep_dive_entity_detector_service import DeepDiveEntityDetectorService
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, required=True,
                        help='Root used to resolve image paths stored in annotations')
    parser.add_argument('--output', type=Path, default=ROOT/'.pytest_tmp/deep_dive_four_view_reading_check')
    add_detector_arguments(parser)
    args = parser.parse_args()
    labels, reference_root, truth = load_reference_inputs(args.annotations, args.reference_root)
    config = detector_config(args)
    cv2.setNumThreads(1)
    aligner=FourViewTemplateAligner(labels,reference_root)
    folder=args.output.resolve()
    if not folder.is_relative_to(ROOT/'.pytest_tmp'):
        raise ValueError('output must stay under repository .pytest_tmp')
    folder.mkdir(parents=True,exist_ok=True)
    detector=DeepDiveEntityDetectorService(config)
    reports=[]
    try:
        for view in range(1,5):
            source=next(g for g in truth['views'] if g['view']==view)
            rgb=cv2.cvtColor(cv2.imread(str(reference_root/source['image'])),cv2.COLOR_BGR2RGB)
            alignment=aligner.align(rgb,reference_view=view)
            report=check_readings(rgb,alignment,truth)
            packet=detector.detect_packet(rgb)
            report.update(view=view,source=source['source'],target_packet=packet)
            reports.append(report)
            overlay=cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR)
            for t in packet['targets']:
                x,y,w,h=map(lambda v:int(round(v)),t['box'])
                cv2.rectangle(overlay,(x,y),(x+w,y+h),(0,255,0) if t.get('confirmable') else (0,140,255),2)
                cv2.putText(overlay,t['kind'],(x,y-3),cv2.FONT_HERSHEY_SIMPLEX,.4,(0,255,0),1)
            cv2.imwrite(str(folder/f'view{view}_targets.png'),overlay)
            print(json.dumps(dict(view=view,correct=report['correct'],unknown=report['unknown'],wrong=report['wrong'],
                                 targets=packet['targets']),ensure_ascii=False),flush=True)
    finally:
        detector.close()
        (folder/'closed.json').write_text(json.dumps(detector.status(),indent=2),'utf8')
    (folder/'report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),'utf8')

if __name__=='__main__':main()

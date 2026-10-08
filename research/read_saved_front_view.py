"""Offline stationary-image reading probe; no input or live accuracy claim."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from research.deep_dive_face_plane_tracker import FacePlaneScanner
from plans.resonance_pc.src.actions._deep_dive_layout_vision import _crop, _ui_mask
from plans.resonance_pc.src.actions._deep_dive_layout_semantics import classify_icon
from plans.resonance_pc.src.services.deep_dive_entity_detector_service import DeepDiveEntityDetectorService


class ProbeConfig:
    def __init__(self, *, runtime_site=None, python_executable=None, device_id=None,
                 ort_version='1.24.4'):
        self.values = {'execution_provider': 'cpu', 'session.intra_op_num_threads': 4}
        supplied = (runtime_site is not None, python_executable is not None, device_id is not None)
        if any(supplied):
            if not all(supplied):
                raise ValueError('DirectML requires explicit runtime site, Python executable and device ID')
            runtime = Path(runtime_site).expanduser().resolve()
            python = Path(python_executable).expanduser().resolve()
            if not runtime.is_dir():
                raise ValueError(f'DirectML runtime site does not exist: {runtime}')
            if not python.is_file():
                raise ValueError(f'DirectML Python executable does not exist: {python}')
            if device_id < 0:
                raise ValueError('DirectML device ID must be nonnegative')
            self.values.update({'execution_provider': 'dml_worker',
                'worker.runtime_site': str(runtime), 'worker.python_executable': str(python),
                'worker.ort_version': ort_version, 'dml_device_id': device_id})

    def get(self, key, default=None):
        prefix = 'resonance_pc.deep_dive.entity_detector.'
        return self.values.get(key[len(prefix):], default) if key.startswith(prefix) else default


def add_detector_arguments(parser):
    parser.add_argument('--dml-runtime-site', type=Path)
    parser.add_argument('--dml-python', type=Path)
    parser.add_argument('--dml-device', type=int)
    parser.add_argument('--dml-ort-version', default='1.24.4')


def detector_config(args):
    return ProbeConfig(runtime_site=args.dml_runtime_site, python_executable=args.dml_python,
                       device_id=args.dml_device, ort_version=args.dml_ort_version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('face', nargs='?', choices=('U', 'R', 'F', 'D', 'L', 'B'))
    add_detector_arguments(parser)
    args = parser.parse_args()
    config = detector_config(args)
    folder = (ROOT/args.folder).resolve()
    if not folder.is_relative_to(ROOT/'.pytest_tmp'):
        raise ValueError('probe must read repository experiment artifacts')
    cv2.setNumThreads(1)
    summary = json.loads((folder/'summary.json').read_text(encoding='utf8'))
    if args.face:
        face = args.face
        saved = json.loads((folder/f'{face}_aligned_source.json').read_text(encoding='utf8'))
        frame = saved['frame']
    else:
        frame = json.loads((folder/'frames.json').read_text(encoding='utf8'))[-1]
        face = summary.get('preferred_face','U')
    rgb = cv2.cvtColor(cv2.imread(str(folder/frame['path'])),cv2.COLOR_BGR2RGB)
    scanner = FacePlaneScanner(preferred_face=face)
    scanner.ready = True
    scanner.rotation = np.asarray(frame['rotation'])
    scanner.rvec = cv2.Rodrigues(scanner.rotation)[0]
    scanner.tvec = np.asarray(frame['translation'])
    scanner._plane_projection_face = face
    cells = []
    for item in scanner.visible():
        crop = _crop(rgb, item['quad'])
        t = time.perf_counter()
        reading = classify_icon(crop)
        elapsed = time.perf_counter()-t
        mask = np.zeros(rgb.shape[:2],np.uint8)
        cv2.fillConvexPoly(mask,np.int32(item['quad']),1)
        usable = _ui_mask()>0
        ui_overlap = 1.-float(np.count_nonzero(mask&usable)/max(1,mask.sum()))
        cell = scanner.cells[item['index']]
        cv2.imwrite(str(folder/f"probe_{cell['face']}{cell['row']}{cell['col']}.png"),
                    cv2.cvtColor(crop,cv2.COLOR_RGB2BGR))
        cells.append(dict(cell, reading=reading, elapsed_sec=elapsed,
                          ui_overlap_fraction=ui_overlap, quad=item['quad'].tolist()))
    detector = DeepDiveEntityDetectorService(config)
    try:
        t = time.perf_counter()
        packet = detector.detect_packet(rgb)
        elapsed = time.perf_counter()-t
        result = dict(offline=True, targets_ready=False, formal_acceptance=False,
            source_frame=frame['path'], source=frame['source'], cells=cells,
            entity_packet=packet, first_inference_including_init_sec=elapsed,
            runtime=detector.status())
        print(json.dumps(dict(readings=[(r['face'],r['row'],r['col'],r['reading'],
                r['ui_overlap_fraction']) for r in cells], targets=packet['targets'],
                first_inference_sec=elapsed),ensure_ascii=False),flush=True)
    finally:
        detector.close()
    result['closed_runtime']=detector.status()
    name = f'stationary_read_probe_{face}.json' if args.face else 'stationary_read_probe.json'
    (folder/name).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')


if __name__ == '__main__':
    main()

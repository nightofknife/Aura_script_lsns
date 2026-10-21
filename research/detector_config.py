"""Explicit CPU or isolated DirectML configuration for offline research readers."""
from pathlib import Path


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

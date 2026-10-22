from pathlib import Path
import hashlib
import json
import sys
import numpy as np

root = Path.cwd()
sys.path.insert(0, str(root))
from plans.resonance_pc.development.native_projection.model import NativeModel

base = root / 'plans/resonance_pc/development/native_projection/evidence/20261022'
manifest = json.loads((base / 'manifest.json').read_text('utf-8'))
for item in manifest['files']:
    assert hashlib.sha256((base / item['path']).read_bytes()).hexdigest() == item['sha256'], item['path']
assets = base / 'assets'
model = NativeModel(assets, assets / 'camera_native_candidate.json', assets / 'shell_triangles.json')
cells = {cell['key']: cell for cell in model.cells}
assert len(cells) == 54
points = 0
for case in json.loads((base / 'projection/validation.json').read_text('utf-8')):
    rotation = np.array(case['rotation_matrix'])
    for point in case['points']:
        result = model.project(cells[tuple(point['cell'])]['rim'][point['vertex']], rotation)
        np.testing.assert_allclose(result, point['predicted'], atol=1e-7, rtol=0)
        points += 1
depth = model.raster_depth(np.eye(3), width=640, height=360)
assert depth.shape == (360, 640) and np.isfinite(depth).any()
assert (depth[np.isfinite(depth)] > 0).all()
try:
    NativeModel(base / 'missing', assets / 'camera_native_candidate.json', assets / 'shell_triangles.json')
except FileNotFoundError:
    pass
else:
    raise AssertionError('Missing assets must fail explicitly')
from scripts.release.assemble_release_plans import collect_plan_files
assert not any('development' in target.parts for _, target in collect_plan_files(root / 'plans'))
print(json.dumps(dict(archive_files_verified=len(manifest['files']), frozen_projection_points=points, slots=len(cells), depth_proxy='passed', missing_assets='explicit error', release_exclusion='passed')))

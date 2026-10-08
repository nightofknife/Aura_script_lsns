"""Exercise real pipes/lifecycle without requiring a GPU or changing ORT."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys

import numpy as np
import pytest

from plans.resonance_pc.src.services import _deep_dive_entity_worker_backend as module

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "resonance_pc.deep_dive.entity_detector."


class Config:
    def __init__(self, **values):
        self.values = {PREFIX+k: v for k, v in values.items()}

    def get(self, key, default=None):
        return self.values.get(key, default)


FAKE_WORKER = '''
import sys,hashlib,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import numpy as np
from plans.resonance_pc.src.services._deep_dive_entity_worker_backend import read_frame,write_frame
mode=sys.argv[2]
incoming,outgoing=sys.stdin.buffer,sys.stdout.buffer
h,p=read_frame(incoming)
meta=dict(op="initialized",request_id=0,ort_version=h["ort_version"],
 runtime_site=h["runtime_site"],ort_path=str(Path(h["runtime_site"])/"onnxruntime"/"__init__.py"),
 distribution="onnxruntime-directml",provider="DmlExecutionProvider",providers=["DmlExecutionProvider","CPUExecutionProvider"],
 device_id=h["device_id"],model_sha256=h["model_sha256"],classes=["player_head","singularity","inspiration"],input_shape=[1,3,640,640])
if mode=="provider":meta["provider"]="CPUExecutionProvider"
if mode=="version":meta["ort_version"]="1.27.0"
if mode=="origin":meta["ort_path"]=str(Path(sys.argv[1])/"outside"/"onnxruntime.py")
write_frame(outgoing,meta)
while True:
 h,p=read_frame(incoming)
 if h["op"]=="close":
  if mode=="close_timeout":time.sleep(20)
  write_frame(outgoing,dict(op="closed",request_id=h["request_id"],drained=True));break
 if mode=="timeout":time.sleep(20)
 if mode=="death":sys.exit(2)
 if mode=="logs":sys.stderr.write("x"*200000);sys.stderr.flush()
 raw=np.zeros((1,7,2),dtype=np.float32)
 raw[0,0,0]=p[0]
 if mode=="nan":raw[0,0,0]=np.nan
 data=raw.tobytes()
 response=dict(op="result",request_id=h["request_id"],rgb_sha256=h["rgb_sha256"],model_sha256=h["model_sha256"],
 provider="DmlExecutionProvider",raw_dtype="<f4",raw_shape=list(raw.shape),raw_sha256=hashlib.sha256(data).hexdigest(),
 letterbox=[640/750,0,59,750,610],inference_sec=.001)
 if mode=="id":response["request_id"]+=1
 if mode=="rgb":response["rgb_sha256"]="0"*64
 if mode=="model":response["model_sha256"]="0"*64
 if mode=="shape":response["raw_shape"]=[1,7,20001]
 if mode=="dtype":response["raw_dtype"]="<f8"
 if mode=="letterbox":response["letterbox"][2]+=1
 if mode=="output_digest":response["raw_sha256"]="0"*64
 write_frame(outgoing,response,data)
'''


def backend(tmp_path, mode="normal", **options):
    site = tmp_path/"runtime"
    (site/"onnxruntime").mkdir(parents=True)
    (site/"onnxruntime"/"__init__.py").write_text("", encoding="utf8")
    model = tmp_path/"model.onnx"
    model.write_bytes(b"protocol fixture model")
    script = tmp_path/"fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf8")
    config = Config(**{"worker.runtime_site": str(site), **options})
    result = module.EntityDmlWorkerBackend(model, config=config)
    result._worker_command = lambda: [sys.executable, "-u", str(script), str(ROOT), mode]
    return result


def test_parent_import_does_not_load_or_replace_ort():
    code = "import sys; from plans.resonance_pc.src.services import _deep_dive_entity_worker_backend; assert 'onnxruntime' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_real_worker_module_has_package_context_and_closed_missing_runtime(tmp_path):
    """Exercise the real entrypoint without model inference or GPU setup."""
    worker = backend(tmp_path)
    # Restore the production module command rather than the protocol fake.
    worker._worker_command = lambda: module.EntityDmlWorkerBackend._worker_command(worker)
    with pytest.raises(RuntimeError, match="pinned onnxruntime-directml"):
        worker.initialize()
    assert worker._process.poll() is not None
    assert worker.close()["drained"] is False


def test_exact_frame_round_trip_serialized_and_drained(tmp_path):
    worker = backend(tmp_path)
    try:
        metadata = worker.initialize()
        assert metadata["ort_version"] == "1.24.4"
        assert metadata["provider"] == "DmlExecutionProvider"
        metadata["classes"].clear()
        assert len(worker.status["classes"]) == 3
        frames = [np.full((720,1280,3), value, np.uint8) for value in (17,29)]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(worker.run, frames))
        assert [r[0][0,0,0] for r in results] == [17,29]
        assert [r[2]["request_id"] for r in results] == [1,2]
        for frame, (_, letterbox, diagnostic) in zip(frames, results):
            assert diagnostic["rgb_sha256"] == hashlib.sha256(frame.tobytes()).hexdigest()
            assert letterbox == (640/750,0,59,750,610)
        child = worker._process
        assert worker.close() == dict(drained=True, error=None)
        assert child.poll() == 0 and not worker.ready
        assert worker.close()["drained"]
    finally:
        worker.close()


@pytest.mark.parametrize("mode", ["provider", "version", "origin"])
def test_actual_initialization_metadata_must_match(tmp_path, mode):
    worker = backend(tmp_path, mode)
    with pytest.raises(ValueError, match="metadata"):
        worker.initialize()
    assert not worker.ready and worker._process.poll() is not None
    assert worker.close()["drained"] is False


@pytest.mark.parametrize("mode", ["id", "rgb", "model", "shape", "dtype", "letterbox", "output_digest", "nan"])
def test_invalid_model_or_current_frame_response_never_returns_predictions(tmp_path, mode):
    worker = backend(tmp_path, mode)
    with pytest.raises(ValueError):
        worker.run(np.zeros((720,1280,3), np.uint8))
    assert not worker.ready and worker._process.poll() is not None
    with pytest.raises(RuntimeError, match="not reusable"):
        worker.initialize()
    assert worker.close()["drained"] is False


@pytest.mark.parametrize("mode", ["timeout", "death"])
def test_timeout_and_child_death_are_closed_failures(tmp_path, mode):
    worker = backend(tmp_path, mode, **{"worker.request_timeout_sec": .1})
    with pytest.raises((TimeoutError, EOFError)):
        worker.run(np.zeros((720,1280,3), np.uint8))
    assert worker._process.poll() is not None
    assert worker.close()["drained"] is False


def test_close_timeout_reports_failure_and_terminates_only_owned_child(tmp_path):
    worker = backend(tmp_path, "close_timeout", **{"worker.close_timeout_sec": .1})
    worker.initialize()
    child = worker._process
    result = worker.close()
    assert not result["drained"] and "timed out" in result["error"]
    assert child.poll() is not None


def test_large_stderr_is_file_backed_and_cannot_block_protocol(tmp_path):
    worker = backend(tmp_path, "logs")
    try:
        worker.run(np.zeros((720,1280,3), np.uint8))
        assert Path(worker.status["stderr_path"]).is_relative_to(ROOT/".pytest_tmp")
        assert Path(worker.status["stderr_path"]).stat().st_size >= 200000
    finally:
        assert worker.close()["drained"]


@pytest.mark.parametrize("frame", [np.zeros((720,1280,3),np.float32),np.zeros((720,1280),np.uint8)])
def test_invalid_rgb_does_not_spawn_child(tmp_path, frame):
    worker = backend(tmp_path)
    with pytest.raises(ValueError, match="uint8 RGB"):
        worker.run(frame)
    assert worker._process is None


def test_explicit_isolation_location_required():
    with pytest.raises(ValueError, match="runtime_site"):
        module.EntityDmlWorkerBackend("missing.onnx")


@pytest.mark.parametrize("prefix", [b"bad!", module._MAGIC])
def test_oversized_binary_header_rejected_before_allocating(prefix):
    stream = io.BytesIO(struct.pack("!4sII", prefix, 20_000, 100_000_000))
    with pytest.raises(ValueError, match="oversized"):
        module.read_frame(stream)


def test_protocol_detects_truncated_payload():
    stream = io.BytesIO()
    module.write_frame(stream, dict(op="infer"), b"abc")
    stream = io.BytesIO(stream.getvalue()[:-1])
    with pytest.raises(EOFError):
        module.read_frame(stream)

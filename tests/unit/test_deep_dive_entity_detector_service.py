from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import json
import hashlib
import threading
import time

import numpy as np
import cv2
import pytest

from plans.resonance_pc.src.services import deep_dive_entity_detector_service as service_module
from plans.resonance_pc.src.services._deep_dive_entity_fusion import fuse_entities, local_player_heads
from plans.resonance_pc.src.services._deep_dive_entity_onnx import (
    EntityOnnxBackend, decode_raw_entities, prepare_rgb_roi, validate_model,
)


class Config:
    def __init__(self, **values):
        self.values = {f"resonance_pc.deep_dive.entity_detector.{key}": value for key, value in values.items()}

    def get(self, key, default=None):
        return self.values.get(key, default)


class Session:
    def __init__(self, raw=None, delay=0):
        self.raw = np.zeros((1, 7, 8400), np.float32) if raw is None else raw
        self.delay = delay
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()

    def get_inputs(self):
        return [SimpleNamespace(name="images", shape=[1, 3, 640, 640], type="tensor(float)")]

    def get_outputs(self):
        return [SimpleNamespace(shape=[1, 7, 8400])]

    def get_modelmeta(self):
        return SimpleNamespace(custom_metadata_map={"names": "{0: 'player_head', 1: 'singularity', 2: 'inspiration'}"})

    def run(self, _outputs, feed):
        assert feed["images"].dtype == np.float32
        assert feed["images"].shape == (1, 3, 640, 640)
        with self.lock:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        time.sleep(self.delay)
        with self.lock:
            self.concurrent -= 1
        return [self.raw]


def build_detector(tmp_path, monkeypatch, *, session=None, hybrid=False):
    model = tmp_path/"entity.onnx"
    model.write_bytes(b"fake session injection")
    session = session or Session()
    monkeypatch.setattr(EntityOnnxBackend, "create_session", lambda _self, _model: (session, "CPUExecutionProvider"))
    detector = service_module.DeepDiveEntityDetectorService(Config(model_path=str(model), hybrid=hybrid))
    return detector, session


def prediction(kind, box, confidence=.9, **extra):
    x, y, width, height = box
    return dict(kind=kind, box=box, point=[x+width/2, y+height/2], confidence=confidence,
        confirmable=confidence >= .6, anchor_type="head" if kind == "player" else None, **extra)


def test_default_model_is_packaged_with_matching_checksum():
    repository=Path(__file__).resolve().parents[2]
    detector=service_module.DeepDiveEntityDetectorService()
    expected=repository/'plans/resonance_pc/data/models/deep_dive_entities.onnx'
    assert detector.model_path==expected.resolve()
    metadata=json.loads(expected.with_suffix('.json').read_text(encoding='utf8'))
    assert expected.is_file() and expected.stat().st_size==metadata['size_bytes']
    assert hashlib.sha256(expected.read_bytes()).hexdigest()==metadata['sha256']
    assert metadata['classes']==['player_head','singularity','inspiration']


def test_preprocess_keeps_rgb_and_integer_odd_padding():
    rgb = np.full((720, 1280, 3), (255, 64, 0), np.uint8)
    tensor, letterbox = prepare_rgb_roi(rgb)
    assert tensor.shape == (1, 3, 640, 640)
    assert letterbox[1:3] == (0, 59)
    assert tensor[0, :, 60, 0] == pytest.approx([1, 64/255, 0])
    assert tensor[0, :, 0, 0] == pytest.approx([114/255]*3)


def test_decode_retains_weak_masks_and_applies_class_aware_nms():
    raw = np.zeros((1, 7, 5), np.float32)
    raw[0, :4] = np.asarray([[320, 320, 320, 500, 100], [320, 320, 320, 400, 100],
        [50, 50, 50, 30, 10], [50, 50, 50, 30, 10]])
    raw[0, 4:, :] = np.asarray([[.9, .8, .01, .01, .01], [.01, .01, .7, .01, .01], [.01, .01, .01, .10, .04]])
    targets = decode_raw_entities(raw, (.8533333333333334, 0, 59, 750, 610))
    assert len(targets) == 3
    assert [q["kind"] for q in targets] == ["player", "singularity", "inspiration"]
    assert targets[0]["anchor_type"] == "head"
    assert targets[2]["confidence"] == pytest.approx(.10)
    assert targets[2]["confirmable"] is False
    assert targets[0]["point"] == pytest.approx([625, 345.859375])


def test_model_contract_rejects_swapped_classes_and_malformed_output():
    session = Session()
    assert validate_model(session) == "images"
    session.get_modelmeta = lambda: SimpleNamespace(custom_metadata_map={"names": "{0:'inspiration',1:'singularity',2:'player_head'}"})
    with pytest.raises(ValueError, match="classes must"):
        validate_model(session)
    with pytest.raises(ValueError, match="nonfinite"):
        decode_raw_entities(np.full((1, 7, 4), np.nan), (1, 0, 0, 750, 610))


def test_cache_is_exact_one_full_rgb_and_results_are_detached(tmp_path, monkeypatch):
    detector, session = build_detector(tmp_path, monkeypatch)
    rgb = np.zeros((720, 1280, 3), np.uint8)
    first = detector.detect_packet(rgb)
    assert first["model_executed"] is True and first["coverage_valid"] is True
    first["targets"].append({"mutated": True})
    second = detector.detect_packet(rgb.copy())
    assert second["targets"] == []
    assert second["model_executed"] is False and second["cached"] is True
    rgb[0, 0, 0] = 1  # outside ROI still invalidates full-frame identity
    detector.detect(rgb)
    detector.detect(np.zeros_like(rgb))  # only one cached frame, no history lookup
    assert session.calls == 3
    detector.close()
    assert detector.status()["ready"] is False
    detector.detect(rgb)
    assert session.calls == 4


def test_concurrent_geometry_and_semantic_calls_share_serial_session(tmp_path, monkeypatch):
    detector, session = build_detector(tmp_path, monkeypatch, session=Session(delay=.025))
    images = [np.full((720, 1280, 3), i, np.uint8) for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(detector.detect_packet, images))
    assert all(q["model_executed"] for q in results)
    assert session.calls == 4 and session.max_concurrent == 1


def test_failure_never_becomes_empty_success_packet(tmp_path, monkeypatch):
    detector, session = build_detector(tmp_path, monkeypatch)
    session.run = lambda *_args: (_ for _ in ()).throw(RuntimeError("inference failure"))
    with pytest.raises(RuntimeError, match="inference failure"):
        detector.detect_packet(np.zeros((720, 1280, 3), np.uint8))
    assert detector.status()["model_executions"] == 0
    assert detector._cached_packet is None
    with pytest.raises(ValueError, match="uint8 RGB"):
        detector.detect(np.zeros((720, 1280, 3), np.float32))


def test_head_conflict_is_mask_only_and_adjacent_true_ring_survives():
    head = prediction("player", [388, 238, 25, 25], source="deep_dive_local_pink_head")
    false_head = prediction("inspiration", [386, 236, 28, 28], .946)
    adjacent = prediction("inspiration", [405, 242, 36, 38], .72)
    weak = prediction("singularity", [700, 300, 20, 20], .10)
    weak_true_head = prediction("player", [387, 237, 27, 27], .283)
    source = [false_head, adjacent, weak, weak_true_head]
    before = deepcopy(source)
    fused, _ = fuse_entities(source, [head])
    assert source == before
    rejected = next(q for q in fused if q.get("fusion_rejection"))
    assert rejected["confidence"] == .946 and rejected["confirmable"] is False
    assert any(q["kind"] == "inspiration" and q["confirmable"] for q in fused)
    assert next(q for q in fused if q["kind"] == "singularity")["confidence"] == .10
    assert any(q["kind"] == "player" and q["anchor_type"] == "head" for q in fused)


def test_hud_exclusion_retains_occlusion_and_matching_head_uses_model_box():
    head = prediction("player", [388, 238, 25, 25], source="deep_dive_local_pink_head")
    yolo_head = prediction("player", [386, 236, 32, 32], .30)
    hud = prediction("inspiration", [620, 525, 25, 25], .85)
    fused, _ = fuse_entities([yolo_head, hud], [head])
    matched = next(q for q in fused if q["kind"] == "player")
    assert matched["point"] == head["point"]
    assert matched["box"] == yolo_head["box"]
    assert matched["circle_box"] == head["box"]
    mask = next(q for q in fused if q["kind"] == "inspiration")
    assert mask["confirmable"] is False and mask["confidence"] == .85


def test_service_retains_unsupported_sphere_as_mask_and_other_weak_scores(tmp_path, monkeypatch):
    detector, session = build_detector(tmp_path, monkeypatch, hybrid=True)
    session.raw[0, :4, 0] = [100, 200, 20, 20]
    session.raw[0, 5, 0] = .1
    head = prediction("player", [388, 238, 25, 25], source="deep_dive_local_pink_head")
    monkeypatch.setattr(service_module, "local_player_heads", lambda _rgb: ([head], {"local_windows": 1}))
    targets = detector.detect(np.zeros((720, 1280, 3), np.uint8))
    assert next(q for q in targets if q["kind"] == "singularity")["confirmable"] is False
    assert next(q for q in targets if q["kind"] == "player")["confirmable"] is False


def test_confirmation_threshold_cannot_be_accidentally_relaxed():
    with pytest.raises(ValueError, match=">= .6"):
        service_module.DeepDiveEntityDetectorService(Config(confirm_confidence=.3))


def test_provider_auto_selects_dml_and_cpu_remains_explicit(monkeypatch):
    calls = []
    class Ort:
        ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")
        GraphOptimizationLevel = SimpleNamespace(ORT_DISABLE_ALL=0, ORT_ENABLE_BASIC=1,
            ORT_ENABLE_EXTENDED=2, ORT_ENABLE_ALL=3)
        SessionOptions = SimpleNamespace

        @staticmethod
        def get_available_providers():
            return ["DmlExecutionProvider", "CPUExecutionProvider"]

        @staticmethod
        def InferenceSession(path, sess_options, providers):
            calls.append((sess_options, providers))
            return SimpleNamespace(get_providers=lambda: [p[0] if isinstance(p, tuple) else p for p in providers])

    monkeypatch.setattr(EntityOnnxBackend, "load_onnxruntime_module", lambda _self: Ort)
    auto = EntityOnnxBackend(config=Config(dml_device_id=3), config_prefix="resonance_pc.deep_dive.entity_detector",
        runtime_name="test", install_hint="test")
    _, provider = auto.create_session(__file__)
    assert provider == "DmlExecutionProvider"
    options, providers = calls[-1]
    assert providers[0] == ("DmlExecutionProvider", {"device_id": 3})
    assert options.enable_mem_pattern is False and options.execution_mode == "sequential"
    cpu = EntityOnnxBackend(config=Config(execution_provider="cpu"), config_prefix="resonance_pc.deep_dive.entity_detector",
        runtime_name="test", install_hint="test")
    _, provider = cpu.create_session(__file__)
    assert provider == "CPUExecutionProvider" and calls[-1][1] == ["CPUExecutionProvider"]


def test_explicit_dml_unavailable_is_an_error_not_silent_cpu(monkeypatch):
    ort = SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(EntityOnnxBackend, "load_onnxruntime_module", lambda _self: ort)
    backend = EntityOnnxBackend(config=Config(execution_provider="dml"), config_prefix="resonance_pc.deep_dive.entity_detector",
        runtime_name="test", install_hint="test")
    with pytest.raises(RuntimeError, match="DirectML is unavailable"):
        backend.create_session(__file__)


def test_actual_pawn_body_circle_does_not_create_a_second_player():
    folder=Path(__file__).parents[1]/'fixtures/deep_dive_entity'
    meta=json.loads((folder/'player_head_body_0543.json').read_text())
    patch=cv2.cvtColor(cv2.imread(str(folder/'player_head_body_0543.png')),cv2.COLOR_BGR2RGB)
    rgb=np.zeros((720,1280,3),np.uint8);x,y=meta['crop_origin']
    rgb[y:y+patch.shape[0],x:x+patch.shape[1]]=patch
    heads,_=local_player_heads(rgb)
    assert any(np.linalg.norm(np.asarray(h['point'])-meta['false_body_circle'])<3 for h in heads), 'real fixture must exercise the original body false circle'
    fused,_=fuse_entities([meta['model_head']],heads,image_rgb=rgb)
    players=[q for q in fused if q['kind']=='player' and q['confirmable']]
    assert len(players)==1 and players[0]['point']==meta['true_head']
    false=next(q for q in fused if np.linalg.norm(np.asarray(q['point'])-meta['false_body_circle'])<3)
    assert false['confirmable'] is False
    assert false['evidence_role']=='mask_only'
    assert false['confidence']<=.24 and false['raw_circle_confidence']>.6
    assert false['fusion_rejection']=='local_circle_without_same_sphere_model_support'
    assert players[0]['source_frame_id']==543 and players[0]['source_frame_time']==56306.093
    # A low-confidence model body proposal must not let the same pink pawn
    # base upgrade itself into a second confirmed head.
    weak_body=prediction('player',[450,233,31,31],.09)
    fused,_=fuse_entities([meta['model_head'],weak_body],heads,image_rgb=rgb)
    assert sum(q['kind']=='player' and q['confirmable'] for q in fused)==1
    body_circle=next(q for q in fused if q.get('source')=='deep_dive_local_pink_head')
    assert body_circle['fusion_rejection']=='connected_body_circle_of_model_confirmed_pawn'


def test_close_distinct_model_players_are_not_deleted_by_top1_or_distance():
    first=prediction('player',[388,238,18,18],.94,source_frame_id=17)
    second=prediction('player',[412,238,18,18],.91,source_frame_id=17)
    head=prediction('player',[388,238,18,18],.80,source='deep_dive_local_pink_head')
    fused,_=fuse_entities([first,second],[head])
    players=[q for q in fused if q['kind']=='player' and q['confirmable']]
    assert len(players)==2
    assert {tuple(q['point']) for q in players}=={tuple(first['point']),tuple(second['point'])}
    assert all(q['source_frame_id']==17 for q in players)


def test_weak_model_head_and_true_sphere_can_recover_the_same_player():
    model=prediction('player',[387,237,27,27],.244,source_frame_id=353)
    head=prediction('player',[388,238,25,25],.81,source='deep_dive_local_pink_head')
    fused,_=fuse_entities([model],[head])
    assert len(fused)==1 and fused[0]['confirmable'] is True
    assert fused[0]['point']==head['point'] and fused[0]['source_frame_id']==353
    assert fused[0]['model_support_confidence']==.244


def test_proposal_limit_cannot_certify_negative_coverage(tmp_path, monkeypatch):
    detector,session=build_detector(tmp_path,monkeypatch)
    detector.max_det=1
    session.raw[0,:4,0]=[200,200,30,30]
    session.raw[0,4,0]=.9
    rgb=np.zeros((720,1280,3),np.uint8)
    packet=detector.detect_packet(rgb)
    assert packet['model_executed'] is True
    assert packet['coverage_valid'] is False and packet['proposal_limit_reached'] is True
    assert len(packet['targets'])==1
    cached=detector.detect_packet(rgb)
    assert cached['model_executed'] is False and cached['coverage_valid'] is False

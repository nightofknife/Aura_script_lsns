from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from packages.aura_core.services.onnx_runtime_backend import OnnxRuntimeBackend
from plans.aura_base.src.services.ocr_contract import OcrModelMetadata
from plans.aura_base.src.services.ocr_service import OcrService


class _SessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.entries = {}

    def add_session_config_entry(self, key, value):
        self.entries[key] = value


_ORT = SimpleNamespace(
    SessionOptions=_SessionOptions,
    GraphOptimizationLevel=SimpleNamespace(
        ORT_DISABLE_ALL=0, ORT_ENABLE_BASIC=1, ORT_ENABLE_EXTENDED=2, ORT_ENABLE_ALL=99,
    ),
)


def _ocr_options(config=None):
    service = OcrService(config=config)
    return service._onnx_backend.create_session_options(_ORT)


def _assert_spinning(options, enabled):
    assert options.entries == {
        "session.intra_op.allow_spinning": "1" if enabled else "0",
        "session.inter_op.allow_spinning": "1" if enabled else "0",
    }


def test_ocr_defaults_use_eight_threads_and_disable_both_spin_pools():
    options = _ocr_options()

    assert options.intra_op_num_threads == 8
    assert options.inter_op_num_threads == 0
    assert options.graph_optimization_level == _ORT.GraphOptimizationLevel.ORT_ENABLE_ALL
    _assert_spinning(options, False)


@pytest.mark.parametrize("threads", [0, 2])
def test_explicit_thread_override_including_automatic_zero_is_preserved(threads):
    options = _ocr_options({
        "ocr.session.intra_op_num_threads": threads,
        "ocr.session.allow_spinning": True,
    })

    assert options.intra_op_num_threads == threads
    _assert_spinning(options, True)


@pytest.mark.parametrize("raw,enabled", [
    (False, False), (True, True), (0, False), (1, True),
    ("false", False), (" TRUE ", True), ("0", False), (" 1 ", True),
    ("OFF", False), ("on", True), ("no", False), (" YeS ", True),
])
def test_spinning_configuration_parses_boolean_and_common_scalar_values(raw, enabled):
    options = _ocr_options({"ocr.session.allow_spinning": raw})

    assert options.intra_op_num_threads == 8
    _assert_spinning(options, enabled)


def test_thread_and_spinning_overrides_are_independent():
    threads_only = _ocr_options({"ocr.session.intra_op_num_threads": 2})
    spinning_only = _ocr_options({"ocr.session.allow_spinning": True})

    assert threads_only.intra_op_num_threads == 2
    _assert_spinning(threads_only, False)
    assert spinning_only.intra_op_num_threads == 8
    _assert_spinning(spinning_only, True)


def test_ocr_defaults_and_overrides_do_not_leak_into_other_services_or_instances():
    config = {"ocr.session.intra_op_num_threads": 2, "ocr.session.allow_spinning": True}
    overridden = _ocr_options(config)
    other_backend = OnnxRuntimeBackend(
        config=config, config_prefix="other_vision", runtime_name="Other vision", install_hint="ort",
    )
    other = other_backend.create_session_options(_ORT)
    fresh = _ocr_options()

    assert overridden.intra_op_num_threads == 2
    assert other.intra_op_num_threads == other.inter_op_num_threads == 0
    assert other.entries == {}
    assert fresh.intra_op_num_threads == 8
    _assert_spinning(fresh, False)


def test_backend_copies_caller_owned_session_defaults():
    defaults = {"intra_op_num_threads": 8, "allow_spinning": False}
    backend = OnnxRuntimeBackend(
        config={}, config_prefix="ocr", runtime_name="OCR", install_hint="ort",
        session_defaults=defaults,
    )
    defaults.update(intra_op_num_threads=1, allow_spinning=True)

    options = backend.create_session_options(_ORT)

    assert options.intra_op_num_threads == 8
    _assert_spinning(options, False)


@pytest.mark.parametrize("config,threads,spinning", [
    (None, 8, False),
    ({"ocr.session.intra_op_num_threads": 2, "ocr.session.allow_spinning": True}, 2, True),
])
def test_all_ocr_model_sessions_receive_the_selected_policy(monkeypatch, config, threads, spinning):
    service = OcrService(config=config)
    metadata = OcrModelMetadata.model_validate({
        "models": {"det": "det.onnx", "rec": "rec.onnx", "textline_orientation": "ori.onnx"},
        "rec_postprocess": {"character_dict": ["A"]},
    })
    calls = []
    monkeypatch.setattr(service, "_resolve_model_path", lambda root, relative, _label: root / relative)

    def create_session(path):
        options = service._onnx_backend.create_session_options(_ORT)
        session = object()
        calls.append((path.name, options, session))
        return session, "CPUExecutionProvider"

    monkeypatch.setattr(service._onnx_backend, "create_session", create_session)
    sessions, provider = service._create_pipeline_sessions(metadata, Path("models/ocr/example"))

    assert [name for name, _, _ in calls] == ["det.onnx", "rec.onnx", "ori.onnx"]
    assert [sessions.det, sessions.rec, sessions.textline_orientation] == [item[2] for item in calls]
    assert provider == "CPUExecutionProvider"
    for _, options, _ in calls:
        assert options.intra_op_num_threads == threads
        _assert_spinning(options, spinning)


def test_release_template_declares_the_same_ocr_policy():
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "packaging/templates/config.yaml").read_text(encoding="utf-8"))

    assert config["ocr"]["session"]["intra_op_num_threads"] == 8
    assert config["ocr"]["session"]["allow_spinning"] is False


def test_ocr_policy_is_accepted_by_real_onnxruntime_session_options():
    ort = pytest.importorskip("onnxruntime")
    options = OcrService()._onnx_backend.create_session_options(ort)

    assert options.intra_op_num_threads == 8
    assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert options.get_session_config_entry("session.inter_op.allow_spinning") == "0"

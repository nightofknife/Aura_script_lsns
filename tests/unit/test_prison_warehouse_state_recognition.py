"""Offline warehouse page/connection evidence; no game input or OCR engine.

The supplied captures exercise the production matcher. Corrupted captures and
conflicting matcher results cover refusal paths, not live-game acceptance.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.services import interaction_service
from plans.resonance_pc.src.actions._prison_warehouse_runtime import (
    WarehouseTransferError, WarehouseTransferRuntime,
)
from plans.resonance_pc.src.actions._prison_warehouse_ui import PrisonWarehouseUi


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
SOURCES = PLAN / "development/template_sources/warehouse_transfer/screenshots"


def client_frame(name: str) -> np.ndarray:
    path = (ROOT / "tests/fixtures/prison_warehouse_state/warehouse_before_failed_20261024.png"
            if name == "previous_live_failure" else SOURCES / f"{name}.png")
    frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None, f"Missing supplied warehouse capture: {path}"
    if frame.shape == (720, 1280, 3):
        return frame
    if name == "warehouse_stable":
        frame = frame[56:776, 26:1306]
    else:
        assert frame.shape == (752, 1282, 3)
        frame = frame[31:751, 1:1281]
    assert frame.shape == (720, 1280, 3)
    return frame.copy()


@pytest.fixture(scope="module")
def ui():
    # A missing service must never cause page/connection recognition to use OCR.
    class NoOcr:
        def recognize_all(self, **kwargs):
            pytest.fail("Page and connection recognition must not invoke OCR")

    return PrisonWarehouseUi(ocr=NoOcr())


def observe(ui, frame):
    return ui.probe(frame, read_quantities=False, read_list=False)


@pytest.mark.parametrize("name,page,connection,tp_connection", [
    ("overview", "overview", "unknown", "unknown"),
    ("warehouse_before", "warehouse", "not_train_product", "unselected"),
    ("warehouse_connected", "warehouse", "train_product", "selected"),
    ("warehouse_after", "warehouse", "train_product", "selected"),
    ("warehouse_stable", "warehouse", "not_train_product", "unselected"),
    ("previous_live_failure", "warehouse", "not_train_product", "unselected"),
])
def test_supplied_captures_recognize_page_and_target_connection_independently(
        ui, name, page, connection, tp_connection):
    result = observe(ui, client_frame(name))
    assert result["page"] == page
    assert result["connection"] == connection
    assert result["tp_connection"] == tp_connection


def clear_search_region(ui, frame, name):
    x, y, width, height = ui.controls_catalog["templates"][name]["search_region"]
    frame[y:y + height, x:x + width] = 0


@pytest.mark.parametrize("name", [
    "train_title_identity", "material_title_identity", "product_title_identity",
])
def test_missing_one_warehouse_title_cannot_authorize_connection(ui, name):
    frame = client_frame("warehouse_stable")
    clear_search_region(ui, frame, name)
    result = observe(ui, frame)
    assert result["page"] == "unknown"
    assert result["connection"] == result["tp_connection"] == "unknown"


def test_warehouse_page_survives_missing_target_button_but_connection_is_unknown(ui):
    frame = client_frame("warehouse_stable")
    clear_search_region(ui, frame, "tp_connection_anchor")
    result = observe(ui, frame)
    assert result["page"] == "warehouse"
    assert result["connection"] == result["tp_connection"] == "unknown"


def test_colored_button_corruption_does_not_authorize_a_connection(ui):
    frame = client_frame("warehouse_connected")
    x, y, width, height = ui.controls_catalog["templates"]["tp_connection_selected"]["search_region"]
    patch = frame[y:y + height, x:x + width]
    patch[:, :, 0] //= 4
    patch[:, :, 1] = np.maximum(patch[:, :, 1], 190)
    patch[:, :, 2] //= 4
    result = observe(ui, frame)
    assert result["page"] == "warehouse"
    assert result["connection"] == result["tp_connection"] == "unknown"


def test_title_brightness_does_not_encode_connection(ui):
    frame = client_frame("warehouse_stable")
    for name in ("train_title_identity", "material_title_identity", "product_title_identity"):
        x, y, width, height = ui.controls_catalog["templates"][name]["search_region"]
        patch = frame[y:y + height, x:x + width]
        frame[y:y + height, x:x + width] = cv2.convertScaleAbs(patch, alpha=.45, beta=5)
    result = observe(ui, frame)
    assert result["page"] == "warehouse"
    assert result["connection"] == "not_train_product"
    assert result["tp_connection"] == "unselected"


def test_conflicting_connection_evidence_preserves_page_without_authorizing_input(ui, monkeypatch):
    match = ui._control

    def conflicting(frame, name, **kwargs):
        result = match(frame, name, **kwargs)
        if name in {"tp_connection_selected", "tp_connection_unselected"}:
            return {**result, "valid": True, "found": True}
        return result

    monkeypatch.setattr(ui, "_control", conflicting)
    result = observe(ui, client_frame("warehouse_stable"))
    assert result["page"] == "warehouse"
    assert result["connection"] == result["tp_connection"] == "unknown"


def test_legacy_title_color_evidence_is_not_a_page_or_connection_requirement(ui, monkeypatch):
    match = ui._control

    def no_legacy_color(frame, name, **kwargs):
        result = match(frame, name, **kwargs)
        if name.endswith(("_active", "_inactive")):
            return {**result, "valid": True, "found": False}
        return result

    monkeypatch.setattr(ui, "_control", no_legacy_color)
    result = observe(ui, client_frame("warehouse_stable"))
    assert result["page"] == "warehouse"
    assert result["connection"] == "not_train_product"


def test_prepare_only_authorizes_a_known_unselected_target_on_a_known_warehouse_page():
    # Capture the production transition predicates. This is a gating contract,
    # not a replacement test of the shared transition/input implementation.
    runtime = object.__new__(WarehouseTransferRuntime)
    runtime.trace = []
    transitions = {}

    def transition(name, target, source, control, **options):
        transitions[name] = (target, source, control)

    runtime._transition = transition
    runtime._stable = lambda: {"capacity": {"current": 56, "limit": 1003}}
    runtime._prepare()
    target, source, control = transitions["connect_train_product"]
    assert control == "tp_connection_unselected"
    assert source({"page": "warehouse", "connection": "not_train_product"}) is True
    assert source({"page": "warehouse", "connection": "unknown"}) is False
    assert source({"page": "unknown", "connection": "not_train_product"}) is False
    assert source({"page": "warehouse", "connection": "material_product"}) is False
    selected = {"page": "warehouse", "connection": "train_product"}
    assert target(selected) is True
    assert source(selected) is False
    assert target({"page": "warehouse", "connection": "unknown"}) is False


@pytest.mark.parametrize("initial,expected_clicks", [
    ("train_product", 0),
    ("not_train_product", 1),
    ("unknown", 0),
])
def test_real_transition_waits_for_connection_without_blind_or_duplicate_clicks(initial, expected_clicks):
    """Use the actual transition/probe/input receipts with a fake app and clock.

    This verifies scheduling and gating only: captures are symbolic, the input
    callback appends a record, and no window or operating-system input is used.
    """
    now = [0.0]
    state = [initial]
    pending = []
    clicks = []
    runtime = object.__new__(WarehouseTransferRuntime)
    runtime.scope = OperationScope(timeout=5, monotonic=lambda: now[0])
    runtime.cancel_check = lambda: False
    runtime.options = {
        "transition_timeout_sec": 1,
        "transition_supplemental_clicks": 3,
        "transition_probe_sec": .2,
        "transition_settle_sec": .05,
        "poll_sec": .01,
    }
    runtime.interaction = SimpleNamespace(
        probe_transition=interaction_service.probe_transition,
        run_transition=interaction_service.run_transition,
    )
    runtime.trace = []
    runtime.frame = None

    def capture(**kwargs):
        if pending:
            state[0] = pending.pop(0)
        runtime.frame = None
        return {
            "page": "warehouse", "connection": state[0],
            "controls": {"tp_connection_unselected": {
                "valid": True, "found": True, "point": [540, 545],
            }},
            "capacity": {"current": 56, "limit": 1003},
        }

    def click(**point):
        clicks.append(point)
        # Observe a source-loss/unknown frame before target confirmation. This
        # must latch wait-only instead of authorizing a supplementary click.
        pending.extend(["unknown", "train_product", "train_product"])
        return True

    runtime.app = SimpleNamespace(click=click)
    runtime._capture = capture
    runtime._stable = capture
    runtime._sleep = lambda seconds: now.__setitem__(0, now[0] + max(seconds, .001))
    if initial == "unknown":
        with pytest.raises(WarehouseTransferError) as error:
            runtime._prepare()
        assert error.value.reason == "connect_train_product_source_unconfirmed"
    else:
        assert runtime._prepare()["connection"] == "train_product"
        connection_trace = next(row for row in runtime.trace if row["stage"] == "connect_train_product")
        assert connection_trace["confirmed"] is True
        assert len(connection_trace["receipts"]) == expected_clicks
        if expected_clicks:
            assert connection_trace["source_loss_latched"] is True
    assert len(clicks) == expected_clicks


def test_supplied_client_crop_is_explicit_and_never_auto_scaled(ui):
    path = SOURCES / "warehouse_before.png"
    original = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    result = observe(ui, original)
    assert result["valid"] is False
    assert result["page"] == result["connection"] == "unknown"
    assert result["unresolved"][0]["reason"] == "requires_1280x720_uint8_bgr_frame"

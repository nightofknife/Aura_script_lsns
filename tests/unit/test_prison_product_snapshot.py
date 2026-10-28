"""Visible inventory boundaries and real navigation transitions, without OS input."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.observation_service import Frame
from plans.resonance_pc.src.actions import _prison_product_snapshot as snapshot
from plans.resonance_pc.src.actions._prison_warehouse_runtime import WarehouseTransferError


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def view(*, cards=None, unresolved=None, state="populated"):
    return {"cards": cards or [], "unresolved": unresolved or [], "list_state": state}


def card(identity="113", name="涤纶", quantity=20):
    return {"id": identity, "name": name, "quantity": quantity}


def test_filtered_snapshot_never_synthesizes_zero_for_unobserved_goods():
    source = view(cards=[card(), card("1", "发动机", 3)])
    original = deepcopy(source)
    result = snapshot.visible_snapshot(source, ["113", "2"])
    assert result["goods"] == [{"goods_id": "113", "goods": "涤纶", "quantity": 20}]
    assert result["not_observed"] == ["2"]
    assert result["success"] is True and result["scope"] == "visible"
    assert result["inventory_complete"] is False
    assert source == original


def test_unfiltered_snapshot_reads_all_visible_goods():
    result = snapshot.visible_snapshot(view(cards=[card(), card("1", "发动机", 3)]))
    assert [row["goods_id"] for row in result["goods"]] == ["113", "1"]
    assert result["requested_goods"] is None and result["visible_complete"] is True


def test_unselected_goods_quantity_does_not_block_selected_goods():
    result = snapshot.visible_snapshot(view(cards=[card(), card("1", "发动机", None)]), ["113"])
    assert result["success"] is True and len(result["goods"]) == 1


@pytest.mark.parametrize("quantity", [None, True, "20", 0, -1])
def test_unknown_or_invalid_selected_quantity_cannot_become_zero_stock(quantity):
    result = snapshot.visible_snapshot(view(cards=[card(quantity=quantity)]), ["113"])
    assert result["success"] is False
    assert result["goods"] == []
    assert any(item["reason"] == "visible_quantity_unknown" for item in result["unresolved"])


def test_unknown_identity_blocks_claim_even_when_filter_is_present():
    result = snapshot.visible_snapshot(view(cards=[card()], unresolved=[{"reason": "occupied_card_identity_unknown"}]), ["113"])
    assert result["success"] is False and result["visible_complete"] is False
    assert result["goods"][0]["quantity"] == 20


def test_clipped_edge_is_reported_without_requiring_a_scroll():
    clipped = {"reason": "clipped_bottom", "coverage_only": True, "bbox": [586, 650, 84, 84]}
    result = snapshot.visible_snapshot(view(cards=[card()], unresolved=[clipped], state="partial"))
    assert result["success"] is True
    assert result["coverage_notes"] == [clipped]
    assert result["inventory_complete"] is False


def test_duplicate_id_does_not_overwrite_or_sum_conflicting_visible_rows():
    result = snapshot.visible_snapshot(view(cards=[card(), card(quantity=3)]))
    assert result["success"] is False
    assert result["goods"] == [{"goods_id": "113", "goods": "涤纶", "quantity": 20}]
    assert any(item["reason"] == "duplicate_visible_goods_id" for item in result["unresolved"])


def test_empty_is_a_visible_observation_only():
    result = snapshot.visible_snapshot(view(state="empty"), ["113"])
    assert result["success"] is True and result["goods"] == []
    assert result["not_observed"] == ["113"] and result["inventory_complete"] is False


def runtime_services(*, initial="profile", destination="profile", cancel_at=None, selected=True):
    now, phase, sequence = [0.0], [initial], [0]
    clicks, reads = [], []
    phases = ["profile", "overview_entry", "warehouse_unselected", "warehouse", "overview_return", "main", "unknown"]
    labels = {name: index + 1 for index, name in enumerate(phases)}
    reverse = {index: name for name, index in labels.items()}

    class Scope(OperationScope):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("monotonic", lambda: now[0])
            super().__init__(*args, **kwargs)

        def sleep(self, seconds):
            self.checkpoint()
            now[0] += max(float(seconds), .001)
            self.checkpoint()

    def capture(**kwargs):
        if phase[0] == cancel_at:
            raise asyncio.CancelledError("cancelled while observing")
        sequence[0] += 1
        return Frame.from_image(np.full((720, 1280, 3), labels[phase[0]], dtype=np.uint8),
                                frame_id=f"visible-{sequence[0]}", timestamp=now[0])

    def click(*, x, y):
        point = [x, y]
        expected = {
            "profile": ([611, 615], "overview_entry"),
            "overview_entry": ([75, 630], "warehouse" if selected else "warehouse_unselected"),
            "warehouse_unselected": ([540, 545], "warehouse"),
            "warehouse": ([82, 34], "overview_return"),
            "overview_return": ([82, 34], destination),
            "main": ([150, 655], "profile"),
        }
        assert phase[0] in expected, "Unknown pages must never receive an input"
        allowed, target = expected[phase[0]]
        # The existing input adapter rounds navigation centers to five pixels.
        assert all(abs(actual - center) <= 2.5 for actual, center in zip(point, allowed)), "Only navigation controls may be clicked"
        clicks.append((phase[0], point))
        phase[0] = target
        return True

    app = SimpleNamespace(get_window_size=lambda: (1280, 720), click=click,
                          drag=lambda **kwargs: pytest.fail("Visible snapshot tried to drag"))
    interaction = InteractionService(app=app, observation=SimpleNamespace(capture=capture))
    runtime = snapshot.VisiblePrisonProductRuntime(app, object(), object(), interaction, Scope(120), lambda: False)

    def probe(image, **kwargs):
        current = reverse[int(image[0, 0, 0])]
        connection = "train_product" if current == "warehouse" else "not_train_product" if current == "warehouse_unselected" else "unknown"
        page = "overview" if current.startswith("overview") else "warehouse" if current.startswith("warehouse") else current
        if kwargs.get("read_quantities"):
            reads.append(kwargs.get("goods_filter"))
        return {
            "page": page, "connection": connection, "signature": current,
            "cards": [{**card(), "bbox": [586, 431, 84, 84], "target": True}],
            "capacity": {"current": 78, "limit": 1003}, "list_state": "populated", "unresolved": [],
            "controls": {name: {"valid": True, "found": True, "point": point} for name, point in {
                "profile_reforming_entry": [611, 615], "overview_warehouse_shortcut": [75, 630],
                "tp_connection_unselected": [540, 545], "back": [82, 34], "profile_avatar": [150, 655],
            }.items()},
        }

    runtime.ui.probe = probe
    for name in ("_top", "_rescan", "_take_pass"):
        setattr(runtime, name, lambda *a, **k: pytest.fail("Visible-only refresh called a full-scan or transfer routine"))
    return runtime, clicks, reads, phase


@pytest.mark.parametrize("destination", ["profile", "main"])
@pytest.mark.parametrize("selected", [True, False])
def test_full_runtime_uses_real_transitions_only_navigation_no_scroll(destination, selected):
    runtime, clicks, reads, phase = runtime_services(destination=destination, selected=selected)
    result = runtime.run_visible(["涤纶"])
    assert result["success"] is True and result["returned_to_profile"] is True
    assert result["requested_goods"] == ["113"]
    assert reads and all(value == {"113"} for value in reads)
    assert phase[0] == "profile"
    assert len(clicks) == 4 + (not selected) + (destination == "main")
    assert all(row["confirmed"] is True for row in result["results"] if "confirmed" in row)


@pytest.mark.parametrize("initial", ["unknown", "main"])
def test_unknown_initial_page_never_clicks(initial):
    runtime, clicks, reads, phase = runtime_services(initial=initial)
    with pytest.raises(WarehouseTransferError, match="profile_required"):
        runtime.run_visible()
    assert clicks == []


def test_unknown_page_after_input_never_receives_cleanup_or_retry():
    runtime, clicks, reads, phase = runtime_services(destination="unknown")
    with pytest.raises(Exception):
        runtime.run_visible()
    assert phase[0] == "unknown"
    assert [name for name, _ in clicks] == ["profile", "overview_entry", "warehouse", "overview_return"]


def test_cancellation_during_warehouse_observation_has_no_cleanup_click():
    runtime, clicks, reads, phase = runtime_services(cancel_at="warehouse")
    with pytest.raises(asyncio.CancelledError):
        runtime.run_visible()
    assert [name for name, _ in clicks] == ["profile", "overview_entry"]
    assert phase[0] == "warehouse"


def _image(path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def test_actual_profile_capture_and_entry_template_match_through_production_capture():
    source = PLAN / "development/template_sources/prison_product_snapshot"
    metadata = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    image = _image(source / "profile_menu.png")
    x1, y1, x2, y2 = metadata["client_crop_xyxy"]
    rgb = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
    app = SimpleNamespace(get_window_size=lambda: (1280, 720), click=lambda **k: pytest.fail("Capture must not click"))
    interaction = InteractionService(app=app, observation=SimpleNamespace(capture=lambda **k: Frame.from_image(rgb)))
    runtime = snapshot.VisiblePrisonProductRuntime(app, object(), object(), interaction, OperationScope(20), lambda: False)
    result = runtime._capture()
    assert result["page"] == "profile"
    entry = result["controls"]["profile_reforming_entry"]
    assert entry["found"] is True and entry["score"] >= .92
    assert entry["point"] == [611, 615]


@pytest.mark.parametrize("selected", ["113", "1"])
def test_actual_warehouse_capture_reads_only_requested_product_quantities(monkeypatch, selected):
    image = _image(PLAN / "development/template_sources/warehouse_transfer/screenshots/warehouse_connected.png")
    image = image[31:751, 1:1281]
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    app = SimpleNamespace(get_window_size=lambda: (1280, 720), click=lambda **k: pytest.fail("Capture must not click"))
    interaction = InteractionService(app=app, observation=SimpleNamespace(capture=lambda **k: Frame.from_image(rgb)))
    runtime = snapshot.VisiblePrisonProductRuntime(app, object(), object(), interaction, OperationScope(20), lambda: False)
    runtime.request = {"goods_ids": [selected]}
    monkeypatch.setattr(runtime.ui, "_capacity", lambda *a: {"current": 56, "limit": 1003})
    reads = []

    def read_quantity(*args):
        assert selected == "113", "Nonselected goods must not invoke quantity OCR"
        reads.append(True)
        return {"status": "recognized", "value": 20}

    monkeypatch.setattr(runtime.ui.quantity_reader, "read_card", read_quantity)
    result = snapshot.visible_snapshot(runtime._capture(quantities=True), [selected])
    assert result["success"] is True
    if selected == "113":
        assert reads == [True]
        assert result["goods"] == [{"goods_id": "113", "goods": "涤纶", "quantity": 20}]
    else:
        assert reads == [] and result["goods"] == []
        assert result["not_observed"] == ["1"]
    assert result["scope"] == "visible" and result["inventory_complete"] is False

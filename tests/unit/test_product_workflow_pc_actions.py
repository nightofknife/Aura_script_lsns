"""Business gates for the city -> collection/withdrawal -> city workflow.

These are offline result/parameter tests. No OCR engine, window capture, input
service or live runner is started here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.services import interaction_service
from plans.aura_base.src.services.observation_service import Frame
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import product_workflow_pc_actions as workflow


COLLECT = "collect_products"
WITHDRAW = "withdraw_products"
SUCCESS = {"status": "SUCCESS"}
SKIPPED = {"status": "SKIPPED"}
CITY = {"success": True, "page_state": "city_panel", "city_key": "yunxiuqiao_base"}
ENTRY = {"success": True, "status": "entered", "page_state": "reforming_center_overview"}


def request(operations=None):
    return workflow.resonance_pc_product_workflow_prepare(
        operations=operations, goods=["涤纶"], quantity_threshold=1)


def collected(**changes):
    return {
        "success": True, "status": "completed", "reason": "",
        "visited_count": 2, "collected_count": 2, "skipped_count": 4,
        "results": [], "unlocks": {}, "returned_to_overview": True,
        "page_after": "reforming_center_overview", "stopped_room": None,
        **changes,
    }


def withdrawn(**changes):
    return {
        "success": True, "status": "completed", "reason": "no_visible_eligible_goods",
        "stop_reason": "no_visible_eligible_goods", "scan_scope": "visible", "inventory_complete": False,
        "goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": 20}],
        "total_taken": 20, "reconcile_complete": True, "unknown_goods": [],
        "remaining_targets": [], "scan_complete": True, "unresolved": [], "issues": [],
        "returned_to_overview": True, "page_after": "overview",
        "submission_state": "return_observed", "server_readback_confirmed": False,
        **changes,
    }


def evaluate(stage, req, previous=None, child=None, child_state=SUCCESS):
    return workflow.resonance_pc_product_workflow_evaluate(
        stage=stage, request=req, previous=previous, child=child, child_state=child_state)


def in_overview(req):
    city = evaluate("city", req, child=deepcopy(CITY))
    return evaluate("enter", req, city, deepcopy(ENTRY))


def after_collection(req, child=None, child_state=SUCCESS):
    entered = in_overview(req)
    return evaluate("collect", req, entered, collected() if child is None else child, child_state)


def finish(state, city_result=CITY, city_state=SUCCESS):
    context = SimpleNamespace(data={})
    engine = SimpleNamespace(root_context=context)
    try:
        result = workflow.resonance_pc_product_workflow_finish(
            state=state, city_result=deepcopy(city_result), city_state=city_state,
            engine=engine, context=context)
    except StopTaskException as exc:
        assert exc.success is False
        result = context.data["product_workflow_result"]
        assert result["success"] is False
        return result, exc
    assert context.data["product_workflow_result"] == result
    return result, None


def test_prepare_uses_fixed_order_and_canonical_product_identity():
    parsed = request([WITHDRAW, COLLECT, WITHDRAW])
    assert parsed["operations"] == [COLLECT, WITHDRAW]
    assert parsed["goods"] == ["113"]
    assert parsed["quantity_threshold"] == 1
    assert parsed["run_collect"] is parsed["run_withdraw"] is True
    assert request()["operations"] == [COLLECT, WITHDRAW]


def test_zero_threshold_is_explicit_and_not_replaced_by_default():
    parsed = workflow.resonance_pc_product_workflow_prepare(
        operations=[WITHDRAW], goods=["113", "涤纶"], quantity_threshold=0)
    assert parsed["goods"] == ["113"]
    assert parsed["quantity_threshold"] == 0


@pytest.mark.parametrize("operations", [COLLECT, True, ["unknown_operation"], [None]])
def test_invalid_operation_selection_is_rejected(operations):
    with pytest.raises(ValueError):
        request(operations)


@pytest.mark.parametrize("goods", ["涤纶", [""], ["不存在的仓库商品"], ["67"]])
def test_explicit_withdrawal_selection_must_be_valid_and_supported(goods):
    # 67 is a known product whose warehouse icon is unsupported in the catalog.
    with pytest.raises(ValueError):
        workflow.resonance_pc_product_workflow_prepare(
            operations=[WITHDRAW], goods=goods, quantity_threshold=1)


@pytest.mark.parametrize("fields", [{}, {"goods": None}, {"goods": []}])
def test_omitted_or_empty_goods_enable_an_unfiltered_withdrawal_attempt(fields):
    req = workflow.resonance_pc_product_workflow_prepare(
        operations=[WITHDRAW], quantity_threshold=1, **fields)
    assert req["goods"] is None and req["quantity_threshold"] == 1
    rows = [{"goods": "涤纶", "goods_id": "113", "quantity": 20},
            {"goods": "发动机", "goods_id": "1", "quantity": 3}]
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, withdrawn(goods_taken=rows, total_taken=23))
    result, error = finish(state)
    assert error is None and result["success"] is True
    assert result["goods_taken"] == rows and result["total_taken"] == 23


@pytest.mark.parametrize("extra_issues,unresolved,complete,accepted", [
    ([], [], True, True),
    (["unresolved_goods"], [{"reason": "identity_unknown"}], True, False),
    (["rescan_incomplete"], [], False, False),
])
def test_unfiltered_attempt_accepts_known_leftovers_without_relaxing_unknown_evidence(
        extra_issues, unresolved, complete, accepted):
    req = workflow.resonance_pc_product_workflow_prepare(
        operations=[WITHDRAW], quantity_threshold=1)
    child = withdrawn(status="partial", issues=["eligible_goods_remaining", *extra_issues],
                      remaining_targets=[{"goods": "涤纶", "goods_id": "113", "quantity": 2}],
                      unresolved=unresolved, scan_complete=complete)
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, child)
    result, error = finish(state)
    assert result["success"] is accepted
    assert (error is None) is accepted
    assert result["withdrawal_result"]["status"] == "partial"
    assert result["withdrawal_result"]["remaining_targets"] == child["remaining_targets"]
    assert result["goods_taken"] == child["goods_taken"]


def test_unfiltered_ledger_does_not_accept_unsupported_product_ids():
    req = workflow.resonance_pc_product_workflow_prepare(operations=[WITHDRAW], quantity_threshold=1)
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, withdrawn(
        goods_taken=[{"goods": "unsupported", "goods_id": "67", "quantity": 3}], total_taken=3))
    result, error = finish(state)
    assert error is not None and result["goods_taken"] is None
    assert result["withdrawal_quantity_known"] is False


@pytest.mark.parametrize("threshold", [None, True, -1, 1.5, "1"])
def test_enabled_withdrawal_requires_an_explicit_nonnegative_integer(threshold):
    with pytest.raises(ValueError):
        workflow.resonance_pc_product_workflow_prepare(
            operations=[WITHDRAW], goods=["涤纶"], quantity_threshold=threshold)


@pytest.mark.parametrize("operations", [[COLLECT], []])
def test_disabled_withdrawal_does_not_require_or_parse_stale_parameters(operations):
    parsed = workflow.resonance_pc_product_workflow_prepare(
        operations=operations, goods=["不存在的旧选择"], quantity_threshold="旧值")
    assert parsed["operations"] == operations
    assert parsed["run_withdraw"] is False


def test_no_operations_confirms_city_and_finishes_without_entering():
    req = request([])
    state = evaluate("city", req, child=deepcopy(CITY))
    assert state["can_enter"] is state["can_collect"] is state["can_withdraw"] is False
    result, error = finish(state, city_result=None, city_state=SKIPPED)
    assert error is None
    assert result["success"] is True and result["status"] == "skipped"
    assert result["returned_to_city"] is True and result["page_after"] == "city_panel"


def test_city_or_entry_framework_success_cannot_replace_business_page_confirmation():
    req = request()
    state = evaluate("city", req, child={"success": True, "page_state": "unknown"})
    assert state["can_enter"] is False
    city = evaluate("city", req, child=deepcopy(CITY))
    state = evaluate("enter", req, city, {"success": True, "page_state": "city_main"})
    assert state["can_collect"] is state["can_withdraw"] is state["can_return"] is False


def test_completed_collection_authorizes_withdrawal_only_after_overview_handoff():
    req = request()
    state = after_collection(req)
    assert state["can_withdraw"] is True
    assert state["collection_result"]["collected_count"] == 2
    missing_return = after_collection(req, collected(returned_to_overview=False, page_after="unknown"))
    assert missing_return["can_withdraw"] is missing_return["can_return"] is False


def test_only_capacity_blocked_collection_can_continue_to_requested_withdrawal():
    req = request()
    blocked = collected(success=False, status="blocked", reason="warehouse_capacity_blocked",
                        collected_count=0, visited_count=1, skipped_count=0)
    state = after_collection(req, blocked)
    assert state["can_withdraw"] is state["can_return"] is True
    state = evaluate("withdraw", req, state, withdrawn())
    result, error = finish(state)
    assert error is not None
    assert result["status"] == "partial"
    assert result["collection_result"]["status"] == "blocked"
    assert result["withdrawal_result"]["status"] == "completed"
    assert result["goods_taken"] == withdrawn()["goods_taken"]
    assert result["total_taken"] == 20 and result["returned_to_city"] is True


@pytest.mark.parametrize("changes", [
    {"reason": "some_other_block"},
    {"returned_to_overview": False},
    {"page_after": "unknown"},
])
def test_other_or_unconfirmed_collection_blocks_do_not_authorize_withdrawal(changes):
    req = request()
    blocked = collected(**{
        "success": False, "status": "blocked", "reason": "warehouse_capacity_blocked",
        "collected_count": 0, **changes})
    state = after_collection(req, blocked)
    assert state["can_withdraw"] is False


def test_collection_failure_can_return_only_from_an_explicit_overview():
    req = request()
    failed = collected(success=False, status="failed", reason="collection_room_failed")
    state = after_collection(req, failed)
    assert state["can_withdraw"] is False and state["can_return"] is True
    state = after_collection(req, {**failed, "returned_to_overview": False, "page_after": "unknown"})
    assert state["can_withdraw"] is state["can_return"] is False


def test_complete_workflow_preserves_actual_added_goods_and_both_child_results():
    req = request()
    collected_result = collected()
    prior = after_collection(req, collected_result)
    before = deepcopy(prior)
    state = evaluate("withdraw", req, prior, withdrawn())
    assert prior == before, "A checkpoint must not mutate its predecessor's recorded result"
    result, error = finish(state)
    assert error is None
    assert result["success"] is True and result["status"] == "completed"
    assert result["collection_result"]["collected_count"] == 2
    assert result["withdrawal_result"]["total_taken"] == result["total_taken"] == 20
    assert result["goods_taken"] == withdrawn()["goods_taken"]
    assert result["returned_to_city"] is True and result["page_after"] == "city_panel"


def test_collection_only_skips_withdrawal_and_does_not_invent_added_cargo():
    req = request([COLLECT])
    state = after_collection(req)
    assert state["can_withdraw"] is False
    state = evaluate("withdraw", req, state, child=None, child_state=SKIPPED)
    result, error = finish(state)
    assert error is None and result["success"] is True
    assert result["withdrawal_result"]["status"] == "skipped"
    assert result["goods_taken"] == [] and result["total_taken"] == 0


def test_withdrawal_only_has_no_collection_result_disguised_as_executed():
    req = request([WITHDRAW])
    state = in_overview(req)
    state = evaluate("collect", req, state, child=None, child_state=SKIPPED)
    assert state["can_withdraw"] is True
    state = evaluate("withdraw", req, state, withdrawn())
    result, error = finish(state)
    assert error is None and result["success"] is True
    assert result["collection_result"]["status"] == "skipped"


def test_withdrawal_partial_is_not_promoted_to_complete_after_return():
    req = request([WITHDRAW])
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, withdrawn(status="partial", issues=["eligible_goods_remaining"]))
    result, error = finish(state)
    assert error is not None and result["success"] is False and result["status"] == "partial"
    assert result["returned_to_city"] is True and result["total_taken"] == 20


@pytest.mark.parametrize("changes", [
    {"scan_complete": False},
    {"issues": ["eligible_goods_remaining"]},
    {"unresolved": [{"reason": "occupied_card_identity_unknown"}]},
])
def test_contradictory_completed_withdrawal_keeps_known_cargo_without_claiming_completion(changes):
    req = request([WITHDRAW])
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, withdrawn(**changes))
    result, error = finish(state)
    assert error is not None and result["success"] is False
    assert result["status"] != "completed"
    assert result["goods_taken"] == withdrawn()["goods_taken"]
    assert result["total_taken"] == 20 and result["withdrawal_quantity_known"] is True


def test_train_full_is_a_normal_withdrawal_stop_with_remaining_goods():
    req = request([WITHDRAW])
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    outcome = withdrawn(reason="train_full", stop_reason="train_full",
                        remaining_targets=[{"goods_id": "113", "goods": "涤纶", "quantity": 5}])
    state = evaluate("withdraw", req, state, outcome)
    result, error = finish(state)
    assert error is None and result["status"] == "completed"
    assert result["withdrawal_result"]["remaining_targets"] == outcome["remaining_targets"]


@pytest.mark.parametrize("changes", [
    {"goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": True}]},
    {"goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": 0}]},
    {"goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": -1}]},
    {"goods_taken": [{"goods": "涤纶", "goods_id": "113", "quantity": 20.0}]},
    {"goods_taken": [{"goods": "涤纶", "quantity": 20}]},
    {"total_taken": 21},
    {"reconcile_complete": False},
])
def test_invalid_or_unreconciled_cargo_is_not_reported_as_confirmed(changes):
    req = request([WITHDRAW])
    state = evaluate("collect", req, in_overview(req), child=None, child_state=SKIPPED)
    state = evaluate("withdraw", req, state, withdrawn(**changes))
    result, error = finish(state)
    assert error is not None and result["success"] is False
    assert result["goods_taken"] is None and result["total_taken"] is None
    assert result["withdrawal_quantity_known"] is False


def test_failed_child_keeps_prior_collection_and_does_not_invent_withdrawal_quantities():
    req = request()
    state = after_collection(req)
    state = evaluate("withdraw", req, state, child=None,
                     child_state={"status": "FAILED", "error": {"message": "child task failed"}})
    assert state["can_return"] is False
    result, error = finish(state, city_result=None, city_state=SKIPPED)
    assert error is not None and result["status"] == "partial"
    assert result["collection_result"]["status"] == "completed"
    assert result["goods_taken"] is None and result["total_taken"] is None
    assert result["withdrawal_quantity_known"] is False
    assert result["returned_to_city"] is False


def test_return_failure_does_not_erase_the_original_business_failure():
    req = request()
    state = after_collection(req, collected(success=False, status="failed", reason="collection_room_failed"))
    reason, stage = state["reason"], state["failure_stage"]
    result, error = finish(state, city_result=None,
                           city_state={"status": "FAILED", "error": {"message": "return failed"}})
    assert error is not None and result["success"] is False
    assert result["reason"] == reason and result["failure_stage"] == stage
    assert result["returned_to_city"] is False


def test_city_main_is_not_the_required_city_building_panel():
    req = request()
    state = evaluate("city", req, child={**CITY, "page_state": "city_main"})
    assert state["can_enter"] is False and state["returned_to_city"] is False
    completed = evaluate("withdraw", req, after_collection(req), withdrawn())
    result, error = finish(completed, city_result={**CITY, "page_state": "city_main"})
    assert error is not None and result["returned_to_city"] is False


def test_return_to_another_city_is_not_reported_as_return_to_the_start():
    req = request()
    state = evaluate("withdraw", req, after_collection(req), withdrawn())
    result, error = finish(state, city_result={**CITY, "city_key": "other_city"})
    assert error is not None and result["returned_to_city"] is False


@pytest.mark.parametrize("name", [
    "resonance_pc_product_workflow_require_city",
    "resonance_pc_product_workflow_enter_from_city",
    "resonance_pc_product_workflow_return_city",
])
def test_navigation_checks_cancellation_before_any_observation_or_input(monkeypatch, name):
    def cancelled():
        raise asyncio.CancelledError("test cancellation")

    monkeypatch.setattr(workflow, "_check_cancelled", cancelled)
    monkeypatch.setattr(workflow, "resonance_pc_require_client_resolution",
                        lambda **kwargs: pytest.fail("Cancelled navigation must not inspect the client"))
    with pytest.raises(asyncio.CancelledError):
        getattr(workflow, name)(app=object(), vision=object())


def test_city_preflight_rejects_invalid_resolution_before_page_matching(monkeypatch):
    def wrong_size(**kwargs):
        raise ValueError("client size mismatch")

    monkeypatch.setattr(workflow, "resonance_pc_require_client_resolution", wrong_size)
    monkeypatch.setattr(workflow, "wait_city",
                        lambda **kwargs: pytest.fail("Invalid size must stop before page matching"))
    result = workflow.resonance_pc_product_workflow_require_city(app=object(), vision=object())
    assert result["success"] is False and result["page_state"] == "unknown"


@pytest.mark.parametrize("city", [
    {"success": False, "page_state": "unknown"},
    {"success": True, "page_state": "city_main", "city_key": CITY["city_key"]},
    {"success": True, "page_state": "city_panel", "city_key": None},
])
def test_city_preflight_requires_confirmed_city_panel_identity(monkeypatch, city):
    monkeypatch.setattr(workflow, "resonance_pc_require_client_resolution", lambda **kwargs: None)
    monkeypatch.setattr(workflow, "wait_city", lambda **kwargs: city)
    result = workflow.resonance_pc_product_workflow_require_city(app=object(), vision=object())
    assert result["success"] is False and result["page_state"] == "unknown"


def test_actual_city_panel_capture_is_recognized_without_departure_button_or_input():
    path = Path(__file__).resolve().parents[2] / "tests/fixtures/prison_warehouse_state/city_panel_before_20261024.png"
    image_bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image_bgr is not None and image_bgr.shape == (720, 1280, 3)
    image = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    captures = []

    def capture(rect):
        captures.append(rect)
        x, y, width, height = rect
        return SimpleNamespace(success=True, image=image[y:y+height, x:x+width].copy())

    app = SimpleNamespace(
        get_window_size=lambda: (1280, 720), capture=capture,
        click=lambda **kwargs: pytest.fail("City preflight must not send input"),
    )
    result = workflow.resonance_pc_product_workflow_require_city(app=app, vision=VisionService())
    assert result["success"] is True and result["page_state"] == "city_panel"
    assert result["city_key"] == CITY["city_key"]
    assert len(captures) >= 2 and all(tuple(rect) == tuple(workflow.CITY_REGION) for rect in captures)


def enter_services(monkeypatch, *, initial_city=None, fresh_city=None, overview=True, lookup_error=False):
    clicks, lookups, sent = [], [], []
    monkeypatch.setattr(workflow, "resonance_pc_require_client_resolution", lambda **kwargs: None)
    monkeypatch.setattr(workflow, "wait_city", lambda **kwargs: deepcopy(CITY if initial_city is None else initial_city))
    fresh_city = {"found": True, "city_key": CITY["city_key"]} if fresh_city is None else fresh_city
    monkeypatch.setattr(workflow, "probe_city", lambda **kwargs: deepcopy(fresh_city))
    monkeypatch.setattr(workflow, "PrisonWarehouseUi", lambda: SimpleNamespace(
        probe=lambda *args, **kwargs: {"page": "overview" if overview else "unknown"}))

    def resolve(**kwargs):
        lookups.append(kwargs)
        if lookup_error:
            raise ValueError("整顿中心 is not present in this city")
        assert kwargs == {"city_name": CITY["city_key"], "shop_name": "整顿中心"}
        return {"x": 501, "y": 367}

    def click(**point):
        assert point == {"x": 501, "y": 367}, "Only the city building entry may be clicked"
        clicks.append(point)
        return True

    def send(callback, **kwargs):
        assert isinstance(kwargs.get("session"), interaction_service.InputSession), (
            "Frame-bound entry input requires an operation-local InputSession")
        sent.append(kwargs)
        # Preserve the real frame/session/point validation and receipt path;
        # only the final app callback is fake and records a coordinate.
        return interaction_service.send_input(callback, **kwargs)

    def confirm(observe, **kwargs):
        assert kwargs["stable_scans"] == 2
        value = observe()
        return value if kwargs["accept"](value) else None

    frame = Frame.from_image(np.zeros((720, 1280, 3), dtype=np.uint8))
    interaction = SimpleNamespace(
        observation=SimpleNamespace(capture=lambda **kwargs: frame), send_input=send, confirm=confirm)
    return SimpleNamespace(click=click), interaction, SimpleNamespace(resolve_shop_point=resolve), clicks, lookups, sent


def test_entry_uses_one_city_building_click_and_fresh_same_city_evidence(monkeypatch):
    app, interaction, data, clicks, lookups, sent = enter_services(monkeypatch)
    result = workflow.resonance_pc_product_workflow_enter_from_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction,
        resonance_pc_city_shop_data=data)
    assert result["success"] is True and result["page_state"] == "reforming_center_overview"
    assert result["city_key"] == CITY["city_key"]
    assert len(clicks) == len(lookups) == len(sent) == 1
    assert sent[0]["point"] == (501, 367) and sent[0]["frame"].image.shape == (720, 1280, 3)


@pytest.mark.parametrize("options", [
    {"initial_city": {**CITY, "page_state": "city_main"}},
    {"initial_city": {**CITY, "city_key": "other_city"}},
    {"fresh_city": {"found": False, "city_key": None}},
    {"fresh_city": {"found": True, "city_key": "other_city"}},
    {"lookup_error": True},
])
def test_unknown_wrong_or_unsupported_city_entry_never_falls_back_to_main_screen(monkeypatch, options):
    app, interaction, data, clicks, _, sent = enter_services(monkeypatch, **options)
    result = workflow.resonance_pc_product_workflow_enter_from_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction,
        resonance_pc_city_shop_data=data)
    assert result["success"] is False and result["entry_attempted"] is False
    assert clicks == sent == []


def exit_services(monkeypatch, initial, *, destination="city", cancel=False):
    """Real transition/probe/receipt code, with image labels and a fake clock."""
    clock, phase, pending, clicks = [0.0], [initial], [], []

    class FakeScope(OperationScope):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("monotonic", lambda: clock[0])
            super().__init__(*args, **kwargs)

        def child(self, timeout=None):
            return FakeScope(timeout, parent=self)

        def sleep(self, seconds):
            self.checkpoint()
            clock[0] += max(float(seconds), .001)
            self.checkpoint()

    monkeypatch.setattr(workflow, "OperationScope", FakeScope)
    monkeypatch.setattr(workflow, "resonance_pc_require_client_resolution", lambda **kwargs: None)
    labels = {"overview": 1, "warehouse": 2, "unknown": 3, "city": 4, "wrong_city": 5, "city_main": 6}
    sequence = [0]

    def capture(**kwargs):
        if cancel:
            raise asyncio.CancelledError("cancelled exit capture")
        if pending:
            phase[0] = pending.pop(0)
        sequence[0] += 1
        return Frame.from_image(np.full((720, 1280, 3), labels[phase[0]], dtype=np.uint8),
                                timestamp=clock[0], frame_id=f"exit-{sequence[0]}")

    def view(image, **kwargs):
        label = int(image[0, 0, 0])
        return {"page": "overview" if label == 1 else "warehouse" if label == 2 else "unknown",
                "controls": {"back": {"valid": True, "found": True, "point": [82, 37]}}}

    def city(**kwargs):
        label = int(kwargs["source_image"][0, 0, 0])
        return {"found": label in {4, 5}, "city_key": CITY["city_key"] if label == 4 else "other_city" if label == 5 else None}

    def click(**point):
        assert point == {"x": 82, "y": 37}, "Exit must use the back arrow, never the train/home icon"
        clicks.append(point)
        pending.extend(["unknown", destination, destination])
        return True

    monkeypatch.setattr(workflow, "PrisonWarehouseUi", lambda: SimpleNamespace(probe=view))
    monkeypatch.setattr(workflow, "probe_city", city)
    interaction = SimpleNamespace(
        observation=SimpleNamespace(capture=capture),
        probe_transition=interaction_service.probe_transition,
        run_transition=interaction_service.run_transition,
    )
    return SimpleNamespace(click=click), interaction, clicks


@pytest.mark.parametrize("page", ["warehouse", "unknown", "city_main", "wrong_city"])
def test_exit_refuses_unconfirmed_sources_and_the_train_main_screen(monkeypatch, page):
    app, interaction, clicks = exit_services(monkeypatch, page)
    result = workflow.resonance_pc_product_workflow_return_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction)
    assert result["success"] is False and result["page_state"] == "unknown"
    assert result["reason"] == "product_workflow_exit_source_unconfirmed"
    assert clicks == []


def test_exit_uses_back_once_and_waits_for_the_original_city_panel(monkeypatch):
    app, interaction, clicks = exit_services(monkeypatch, "overview")
    result = workflow.resonance_pc_product_workflow_return_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction)
    assert result["success"] is True and result["page_state"] == "city_panel"
    assert result["city_key"] == CITY["city_key"] and result["click_attempts"] == 1
    assert len(clicks) == 1


def test_exit_never_accepts_another_city_after_leaving_the_overview(monkeypatch):
    app, interaction, clicks = exit_services(monkeypatch, "overview", destination="wrong_city")
    result = workflow.resonance_pc_product_workflow_return_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction)
    assert result["success"] is False
    assert len(clicks) == 1, "Losing the source must prevent supplementary exit clicks"


def test_already_on_the_original_city_panel_performs_no_exit_input(monkeypatch):
    app, interaction, clicks = exit_services(monkeypatch, "city")
    result = workflow.resonance_pc_product_workflow_return_city(
        city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction)
    assert result["success"] is True and result["reason"] == "already_on_city_panel"
    assert result["click_attempts"] == 0 and clicks == []


def test_cancelled_exit_observation_does_not_start_cleanup_navigation(monkeypatch):
    app, interaction, clicks = exit_services(monkeypatch, "overview", cancel=True)
    with pytest.raises(asyncio.CancelledError):
        workflow.resonance_pc_product_workflow_return_city(
            city_key=CITY["city_key"], app=app, vision=object(), interaction=interaction)
    assert clicks == []



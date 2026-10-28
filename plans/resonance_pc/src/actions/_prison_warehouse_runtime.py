"""One product-to-train pass; every input consumes fresh, target-bound evidence."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.observability.logging.core_logger import logger
from ....aura_base.src.services.interaction_service import (
    InputSession, InteractionError, TransitionPolicy, TransitionProbe, TransitionState,
)
from ._prison_warehouse_ledger import normalize_request, reconcile_taken
from ._prison_warehouse_ui import PrisonWarehouseUi
from ._reforming_center_clicks import rounded_point


PLAN_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PLAN_ROOT / "data/meta/prison_warehouse_runtime_pc.json"


class WarehouseTransferError(RuntimeError):
    def __init__(self, reason: str, **evidence: Any):
        super().__init__(reason)
        self.reason, self.evidence = reason, evidence


class WarehouseTransferRuntime:
    """The caller owns the worker lease; this object owns one input/stock ledger."""

    def __init__(self, app, vision, ocr, interaction, scope, cancel_check):
        self.app, self.interaction = app, interaction
        self.scope, self.cancel_check = scope, cancel_check
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.options = self.config["execution"]
        self.scope = scope.child(self.options["total_timeout_sec"])
        self.ui = PrisonWarehouseUi(ocr=ocr, config=CONFIG_PATH)
        self.products = json.loads((PLAN_ROOT / "data/meta/products.json").read_text(encoding="utf-8"))
        self.session = InputSession()
        self.frame = None
        self.last = None
        self.phase = "prepare"
        self.trace: list[dict] = []
        self.before: dict[str, int] = {}
        self.after: dict[str, int] = {}
        self.effect_after: dict[str, int] = {}
        self.clicked: list[str] = []
        self.pending: dict | None = None
        self.quantity_evidence: dict[str, dict] = {}
        self.request: dict = {}
        self.rescan = {"complete": False, "scope": "visible", "inventory_complete": False,
                       "unresolved": [], "coverage_notes": [], "quantities": {}}
        self.returned = False
        self.return_attempted = False
        self.stop_reason = "not_started"
        self.reconciled: dict | None = None

    def _check(self):
        self.cancel_check()
        self.scope.checkpoint()

    def _sleep(self, seconds):
        self.frame = None
        self.scope.sleep(seconds)

    def _capture(self, *, quantities=False):
        self._check()
        self.frame = None
        size = self.app.get_window_size()
        if size is None or tuple(size) != (1280, 720):
            raise WarehouseTransferError("warehouse_client_size_mismatch", actual_size=size)
        frame = self.interaction.observation.capture(scope=self.scope, roi=(0, 0, 1280, 720))
        if (not isinstance(frame.image, np.ndarray) or frame.image.dtype != np.uint8
                or frame.image.shape != (720, 1280, 3)):
            raise WarehouseTransferError("warehouse_client_size_mismatch")
        goods_ids = self.request.get("goods_ids")
        observed = self.ui.probe(
            cv2.cvtColor(frame.image, cv2.COLOR_RGB2BGR),
            read_quantities=quantities,
            read_list=quantities,
            goods_filter=None if goods_ids is None else set(goods_ids) | set(self.clicked),
        )
        self._check()
        self.frame, self.last = frame, observed
        return observed

    @staticmethod
    def _key(view, *, quantities=True):
        return (
            view.get("page"), view.get("connection"), view.get("signature"),
            tuple((c.get("id"), tuple(c.get("bbox", [])), c.get("quantity") if quantities else None)
                  for c in view.get("cards", [])),
            (view.get("capacity", {}).get("current"), view.get("capacity", {}).get("limit"))
            if quantities else None,
        )

    def _stable(self, *, quantities=True):
        view = self.interaction.confirm(
            lambda: self._capture(quantities=quantities),
            scope=self.scope.child(self.options["observation_timeout_sec"]),
            stable_scans=2, poll_seconds=self.options["poll_sec"],
            accept=lambda v: v.get("page") == "warehouse" and v.get("connection") == "train_product",
            is_unknown=lambda v: v is None,
            equivalent=lambda a, b: self._key(a, quantities=quantities) == self._key(b, quantities=quantities),
            before_probe=False, sleep=self._sleep, check_cancelled=self.cancel_check,
        )
        if view is None:
            raise WarehouseTransferError("warehouse_observation_unstable")
        return view

    def _transition(self, name, target, source, control, *, retry=True):
        """Only a still-observed source may be clicked; source loss latches wait-only."""
        budget = self.scope.child(self.options["transition_timeout_sec"])
        policy = TransitionPolicy(
            max_clicks=1 + self.options["transition_supplemental_clicks"] if retry else 1,
            allow_retry=retry, settle_seconds=self.options["transition_settle_sec"],
            latch_source_loss=True,
        )
        state = TransitionState(policy)
        log = {"stage": name, "receipts": [], "confirmed": False}
        self.trace.append(log)

        def observe(allow_source):
            view = self._capture()
            if target(view):
                return TransitionProbe("target", key=name, frame=self.frame)
            hit = view.get("controls", {}).get(control, {})
            if source(view) and hit.get("valid") and hit.get("found") is True and hit.get("point"):
                return TransitionProbe("source", key=(name, control),
                                       hit={"center_point": hit["point"]}, frame=self.frame)
            return TransitionProbe("unknown", key=name, frame=self.frame)

        def probe(allow_source):
            # The one-shot warehouse entry may spend the remaining transition
            # budget loading; it must never gain another click from a short poll.
            probe_timeout = None if not retry and not allow_source else self.options["transition_probe_sec"]
            return self.interaction.probe_transition(
                observe, allow_source=allow_source, scope=budget.child(probe_timeout),
                stable_scans=2, poll_seconds=self.options["poll_sec"],
                allow_immediate_source=False, state=state,
                sleep=self._sleep, check_cancelled=self.cancel_check,
            )

        def click(observed, attempt):
            if name == "return_overview":
                self.return_attempted = True
            point = rounded_point(observed.hit["center_point"])
            return self.app.click(x=point[0], y=point[1])

        try:
            result = self.interaction.run_transition(
                probe, click, scope=budget, policy=policy, state=state,
                sleep=self._sleep, check_cancelled=self.cancel_check,
                source_unconfirmed=lambda: WarehouseTransferError(name + "_source_unconfirmed"),
                exhausted=lambda: WarehouseTransferError(name + "_unconfirmed"),
            )
            log["confirmed"] = result.confirmed
        finally:
            self.frame = None
            log["receipts"] = [asdict(receipt) for receipt in state.receipts]
            log["source_loss_latched"] = state.source_locked

    def _prepare(self):
        self._transition(
            "enter_warehouse", lambda v: v.get("page") == "warehouse",
            lambda v: v.get("page") == "overview", "overview_warehouse_shortcut", retry=False,
        )
        self._transition(
            "connect_train_product",
            lambda v: v.get("page") == "warehouse" and v.get("connection") == "train_product",
            lambda v: v.get("page") == "warehouse" and v.get("connection") == "not_train_product",
            "tp_connection_unselected",
        )
        view = self._stable()
        self.trace.append({"stage": "capacity_before", "capacity": view.get("capacity")})
        return view

    @staticmethod
    def _ids(view):
        return [str(c["id"]) for c in view.get("cards", []) if c.get("id") is not None]

    @staticmethod
    def _blocking(view):
        return [u for u in view.get("unresolved", []) if not u.get("coverage_only", False)]

    def _effect(self, before_view, goods_id, source_quantity):
        """Confirm one mutation and retain explicit local stock evidence."""
        before_ids = self._ids(before_view)
        before_capacity = before_view.get("capacity", {}).get("current")
        def evidence():
            view = self._capture(quantities=True)
            if view.get("page") != "warehouse" or view.get("connection") != "train_product":
                return None
            matching = [c for c in view.get("cards", []) if str(c.get("id")) == goods_id]
            method = None
            if len(matching) == 1 and type(matching[0].get("quantity")) is int:
                q = matching[0]["quantity"]
                if 0 < q < source_quantity:
                    method = "source_quantity_decreased"
                elif q > source_quantity:
                    raise WarehouseTransferError("warehouse_source_quantity_increased", goods_id=goods_id)
            new_capacity = view.get("capacity", {}).get("current")
            source_unchanged = (len(matching) == 1 and matching[0].get("quantity") == source_quantity)
            if (not source_unchanged and type(before_capacity) is int and type(new_capacity) is int
                    and new_capacity > before_capacity):
                method = method or "train_cargo_increased"
            after_ids = self._ids(view)
            removed = False
            if not matching and not self._blocking(view) and not self._blocking(before_view):
                if view.get("list_state") == "empty":
                    method = "product_list_became_empty"
                    removed = True
                else:
                    # At least one preserved neighbor must substantiate removal.
                    neighbors = [i for i in before_ids if i != goods_id]
                    if neighbors and after_ids[:len(neighbors)] == neighbors:
                        method = method or "ordered_neighbors_shifted"
                        removed = True
            if method:
                # No scrolling occurs between these observations. Preserved
                # neighbors shifting forward proves local removal; arbitrary
                # absence in the final viewport does not establish zero stock.
                remaining = 0 if removed else matching[0].get("quantity") if len(matching) == 1 else None
                return {"method": method, "view": view, "remaining": remaining}
            return None
        result = self.interaction.confirm(
            evidence, scope=self.scope.child(self.options["effect_timeout_sec"]),
            stable_scans=2, poll_seconds=self.options["poll_sec"],
            equivalent=lambda a, b: self._key(a["view"]) == self._key(b["view"]),
            before_probe=False, sleep=self._sleep, check_cancelled=self.cancel_check,
        )
        if result is None:
            raise WarehouseTransferError("warehouse_withdraw_effect_unknown", goods_id=goods_id)
        self.pending["effect_confirmed"] = True
        self.pending["effect"] = result["method"]
        if type(result["remaining"]) is int and 0 <= result["remaining"] < source_quantity:
            self.effect_after[goods_id] = result["remaining"]
            self.pending["observed_after_quantity"] = result["remaining"]
        return result["view"]

    def _take_one(self, view, card):
        goods_id, quantity = str(card["id"]), card["quantity"]
        self.before.setdefault(goods_id, quantity)
        self.quantity_evidence.setdefault(goods_id, card.get("quantity_result", {}))
        # Install pending before the callback: cancellation or an exception can
        # follow a sent input, in which case no recovery input is safe.
        record = {"stage": "withdraw", "goods_id": goods_id, "before_quantity": quantity,
                  "point": card["point"], "effect_confirmed": False}
        self.pending = record
        self.trace.append(record)
        frame, self.frame = self.frame, None
        if frame is None or view is not self.last:
            raise WarehouseTransferError("warehouse_withdraw_evidence_stale", goods_id=goods_id)
        point = rounded_point(card["point"])
        try:
            self.interaction.send_input(
                lambda: self.app.click(x=point[0], y=point[1]), scope=self.scope,
                point=point, frame=frame, session=self.session, bounds=(0, 0, 1280, 720),
                check_cancelled=self.cancel_check,
            )
        finally:
            receipts = [r for r in self.session.receipts if r.frame_id == frame.id]
            if receipts:
                record["receipt"] = asdict(receipts[-1])
        self._sleep(self.options["withdraw_settle_sec"])
        after = self._effect(view, goods_id, quantity)
        self.clicked.append(goods_id)
        self.pending = None
        return after

    def _take_pass(self, view):
        mutations = 0
        threshold = self.request["quantity_threshold"]
        while True:
            self._check()
            if view.get("capacity", {}).get("full") is True:
                return view, "train_full"
            candidates = [c for c in view.get("cards", [])
                          if c.get("target") and str(c.get("id")) not in self.clicked
                          and type(c.get("quantity")) is int and c["quantity"] > 0
                          and c["quantity"] >= threshold]
            if candidates:
                candidate_id = str(candidates[0]["id"])
                if sum(str(c.get("id")) == candidate_id for c in view.get("cards", [])) != 1:
                    raise WarehouseTransferError("warehouse_duplicate_visible_goods_id", goods_id=candidate_id)
                if mutations >= self.options["max_withdrawals"]:
                    return view, "withdrawal_limit"
                view = self._take_one(view, candidates[0])
                mutations += 1
                # A deletion can move new cards into view. Use the fresh effect
                # observation, never old indices or a scroll back to the top.
                continue
            return view, "no_visible_eligible_goods"

    def _rescan(self, view):
        # Read-only confirmation of this viewport; no claim about hidden rows.
        view = self._stable()
        quantities: dict[str, int] = {}
        unresolved = list(self._blocking(view))
        coverage_notes = [u for u in view.get("unresolved", []) if u.get("coverage_only", False)]
        conflicts: set[str] = set()
        visible_ids = self._ids(view)
        conflicts.update(goods_id for goods_id in visible_ids if visible_ids.count(goods_id) > 1)
        for card in view.get("cards", []):
            if not card.get("target"):
                continue
            goods_id, quantity = str(card["id"]), card.get("quantity")
            if type(quantity) is not int or quantity <= 0:
                unresolved.append({"reason": "quantity_unknown", "goods_id": goods_id,
                                   "quantity_result": card.get("quantity_result")})
            else:
                quantities[goods_id] = quantity
        for goods_id in conflicts:
            quantities.pop(goods_id, None)
            unresolved.append({"reason": "conflicting_quantity", "goods_id": goods_id})
        self.after = quantities
        complete = not unresolved
        self.rescan = {"complete": complete, "scope": "visible",
                       "inventory_complete": complete and not coverage_notes and view.get("list_state") == "empty",
                       "unresolved": unresolved, "coverage_notes": coverage_notes, "quantities": quantities}
        self.trace.append({"stage": "rescan", **self.rescan})
        return view

    def _reconcile(self, *, failure=False):
        after = dict(self.after)
        # Each identity is clicked once. Its confirmed local decrease/removal
        # remains usable even when that identity is no longer in this viewport.
        for goods_id, quantity in self.effect_after.items():
            after.setdefault(goods_id, quantity)
        self.reconciled = reconcile_taken(
            self.before, self.clicked, after,
            scan_complete=self.rescan["inventory_complete"], unresolved=bool(self.rescan["unresolved"]),
            products=self.products,
        )
        zero_effects = [item for item in self.reconciled["goods_taken"] if item["quantity"] <= 0]
        if zero_effects:
            self.reconciled["goods_taken"] = [item for item in self.reconciled["goods_taken"] if item["quantity"] > 0]
            self.reconciled["unknown_goods"].extend({**item, "quantity": None,
                "reason": "confirmed_effect_without_stock_decrease"} for item in zero_effects)
            self.reconciled["reconcile_complete"] = False
        if self.pending is not None:
            self.reconciled["unknown_goods"].append({
                "goods_id": self.pending["goods_id"],
                "goods": self.products[self.pending["goods_id"]],
                "reason": "withdraw_effect_unconfirmed", "quantity": None,
            })
            self.reconciled["reconcile_complete"] = False
        return self.reconciled

    def _result(self, *, success, reason):
        ledger = self.reconciled or self._reconcile(failure=not success)
        remaining = [{"goods_id": goods_id, "goods": self.products[goods_id], "quantity": quantity}
                     for goods_id, quantity in self.after.items()
                     if quantity > 0 and quantity >= self.request.get("quantity_threshold", 1)]
        issues = []
        if not self.rescan["complete"]:
            issues.append("rescan_incomplete")
        if self.rescan["unresolved"]:
            issues.append("unresolved_goods")
        if remaining and self.stop_reason != "train_full":
            issues.append("eligible_goods_remaining")
        if self.stop_reason not in {"train_full", "no_visible_eligible_goods"}:
            issues.append(self.stop_reason)
        return {
            "success": success, "status": ("partial" if issues else "completed") if success else "failed",
            "reason": reason, "stop_reason": self.stop_reason,
            **ledger, "remaining_targets": remaining, "issues": issues,
            "scan_complete": self.rescan["complete"], "unresolved": self.rescan["unresolved"],
            "scan_scope": "visible", "inventory_complete": self.rescan["inventory_complete"],
            "coverage_notes": self.rescan["coverage_notes"],
            "returned_to_overview": self.returned,
            "page_after": self.last.get("page", "unknown") if self.last else "unknown",
            "submission_state": "return_observed" if self.returned else (
                "return_attempted" if self.return_attempted else "staged_or_unchanged"),
            "server_readback_confirmed": False, "phase": self.phase,
            "request": self.request, "quantity_evidence": self.quantity_evidence,
            "input_receipts": [asdict(receipt) for receipt in self.session.receipts],
            "results": self.trace,
        }

    def run(self, goods=None, quantity_threshold=1):
        # Parse before observing or injecting any game input.
        self.request = normalize_request(goods, quantity_threshold, self.products)
        unsupported = [goods_id for goods_id in self.request["goods_ids"] or []
                       if not self.ui.goods_catalog["products"].get(goods_id, {}).get("supported")]
        if unsupported:
            raise ValueError("No supported warehouse icon templates for requested goods: " + ", ".join(unsupported))
        try:
            with self.scope.activate():
                initial = self._capture()
                if initial.get("page") != "overview":
                    raise WarehouseTransferError("warehouse_initial_overview_required")
                view = self._prepare()
                self.phase = "take_pass"
                view, self.stop_reason = self._take_pass(view)
                self.phase = "rescan"
                self._rescan(view)
                ledger = self._reconcile()
                if not ledger["reconcile_complete"]:
                    raise WarehouseTransferError("warehouse_taken_quantity_unknown")
                self.phase = "return_overview"
                self._transition(
                    "return_overview", lambda v: v.get("page") == "overview",
                    lambda v: v.get("page") == "warehouse" and v.get("connection") == "train_product",
                    "back",
                )
                self.returned = True
                self.phase = "summarize"
                return self._result(success=True, reason=self.stop_reason)
        except asyncio.CancelledError as exc:
            # Cancellation must never trigger navigation or submission input.
            self.trace.append({"stage": self.phase, "error": "cancelled",
                               "receipts": [asdict(r) for r in getattr(exc, "receipts", ())]})
            result = self._result(success=False, reason="cancelled")
            setattr(exc, "warehouse_transfer", result)
            logger.warning("[PrisonWarehouseTransfer] cancelled result=%s", result)
            raise
        except (WarehouseTransferError, InteractionError, OperationTimeout) as exc:
            reason = getattr(exc, "reason", getattr(exc, "code", "warehouse_operation_timeout"))
            self.trace.append({"stage": self.phase, "error": reason,
                               "evidence": getattr(exc, "evidence", {}),
                               "receipts": [asdict(r) for r in getattr(exc, "receipts", ())]})
            return self._result(success=False, reason=reason)
        except Exception as exc:
            # OCR/capture exceptions may follow a submitted mutation. Keep the
            # pending entry and return a failure without issuing any cleanup input.
            self.trace.append({"stage": self.phase, "error": type(exc).__name__, "message": str(exc)})
            return self._result(success=False, reason="warehouse_service_failed")

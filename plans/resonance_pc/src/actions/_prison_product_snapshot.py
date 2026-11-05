"""Visible-only product observations, from and back to the profile menu.

Reuse warehouse page/connection transitions, never the withdrawal or scan loops.
The caller owns persistence and the surrounding player-data refresh.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import cv2

from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope
from ._navigation_templates import PROFILE_MENU
from ._prison_warehouse_ledger import normalize_request
from ._prison_warehouse_runtime import PLAN_ROOT, WarehouseTransferRuntime, WarehouseTransferError
from .prison_warehouse_pc_actions import _check_cancelled


def normalize_goods(goods):
    if goods is not None and not isinstance(goods, list):
        raise ValueError("prison_product_goods must be a list of product names or IDs")
    products = json.loads((PLAN_ROOT / "data/meta/products.json").read_text(encoding="utf-8"))
    ids = normalize_request(goods, 1, products)["goods_ids"]
    catalog = json.loads((PLAN_ROOT / "data/meta/prison_warehouse_goods_templates_pc.json").read_text(encoding="utf-8"))
    if any(not catalog["products"].get(i, {}).get("supported") for i in ids or []):
        raise ValueError("Requested prison product has no supported icon template")
    return ids


def visible_snapshot(view, requested_goods=None):
    """No missing ID implies zero; completeness only covers fully visible cards."""
    unresolved = [dict(item) for item in view.get("unresolved", []) if not item.get("coverage_only")]
    goods, seen = [], set()
    for card in view.get("cards", []):
        identity = str(card["id"])
        if requested_goods is not None and identity not in requested_goods:
            continue
        if identity in seen:
            unresolved.append({"reason": "duplicate_visible_goods_id", "goods_id": identity})
            continue
        seen.add(identity)
        quantity = card.get("quantity")
        if type(quantity) is not int or quantity <= 0:
            unresolved.append({"reason": "visible_quantity_unknown", "goods_id": identity})
            continue
        goods.append({"goods_id": identity, "goods": card["name"], "quantity": quantity})
    if view.get("list_state") not in {"populated", "empty", "partial"}:
        unresolved.append({"reason": "visible_list_unconfirmed"})
    confirmed = not unresolved
    return {
        "success": confirmed, "status": "completed" if confirmed else "partial",
        "reason": "visible_products_read" if confirmed else "visible_products_unresolved",
        "scope": "visible", "visible_complete": confirmed, "inventory_complete": False,
        "requested_goods": requested_goods, "goods": goods, "unresolved": unresolved,
        "not_observed": [identity for identity in requested_goods or [] if identity not in seen],
        "coverage_notes": [dict(item) for item in view.get("unresolved", []) if item.get("coverage_only")],
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "returned_to_profile": False,
    }


class VisiblePrisonProductRuntime(WarehouseTransferRuntime):
    def _drag(self, *args, **kwargs):
        raise AssertionError("Visible-only snapshots must never scroll")

    def _take_one(self, *args, **kwargs):
        raise AssertionError("Snapshots must never transfer products")

    def _match(self, bgr, relative, roi, threshold):
        image = self.ui._asset(relative, gray=True)
        x, y, width, height = roi
        source = cv2.cvtColor(bgr[y:y+height, x:x+width], cv2.COLOR_BGR2GRAY)
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(source, image, cv2.TM_CCOEFF_NORMED))
        return {"valid": True, "found": score >= threshold, "score": score,
                "point": [x+point[0]+image.shape[1]//2, y+point[1]+image.shape[0]//2]}

    def _capture(self, *, quantities=False):
        view = super()._capture(quantities=quantities)
        if view["page"] != "unknown":
            return view
        bgr = cv2.cvtColor(self.frame.image, cv2.COLOR_RGB2BGR)
        profile = self._match(bgr, PROFILE_MENU.path, PROFILE_MENU.roi, PROFILE_MENU.threshold)
        if profile["found"]:
            view["page"] = "profile"
            view["controls"]["profile_reforming_entry"] = self._match(
                bgr, "templates/workshop/warehouse/controls/profile_reforming_entry.png",
                (550, 565, 125, 110), .92)
        else:
            main = self._match(bgr, "templates/navigation/global/train_maintenance.png", (920, 640, 80, 65), .86)
            if main["found"]:
                view["page"] = "main"
                view["controls"]["profile_avatar"] = {"valid": True, "found": True, "point": [150, 655]}
        return view

    def run_visible(self, goods=None):
        self.request = {"goods_ids": normalize_goods(goods), "quantity_threshold": 1}
        with self.scope.activate():
            initial = self.interaction.confirm(
                self._capture, scope=self.scope.child(8), stable_scans=2,
                poll_seconds=self.options["poll_sec"], accept=lambda v: v.get("page") == "profile",
                equivalent=lambda a, b: a.get("page") == b.get("page"),
                before_probe=False, sleep=self._sleep, check_cancelled=self.cancel_check,
            )
            if initial is None:
                raise WarehouseTransferError("prison_snapshot_profile_required")
            self._transition("enter_center", lambda v: v.get("page") == "overview",
                             lambda v: v.get("page") == "profile", "profile_reforming_entry", retry=False)
            view = self._prepare()
            result = visible_snapshot(view, self.request["goods_ids"])
            self._transition("return_overview", lambda v: v.get("page") == "overview",
                             lambda v: v.get("page") == "warehouse", "back")
            self._transition("return_profile_or_main", lambda v: v.get("page") in {"profile", "main"},
                             lambda v: v.get("page") == "overview", "back")
            self._transition("restore_profile", lambda v: v.get("page") == "profile",
                             lambda v: v.get("page") == "main", "profile_avatar")
            result["returned_to_profile"] = True
            result["results"] = self.trace
            return result


def read_visible_prison_products(app, ocr, vision, interaction, goods=None):
    normalize_goods(goods)  # Reject invalid selectors before even acquiring a frame.
    if any(service is None for service in (app, ocr, vision, interaction)):
        raise ValueError("Prison product refresh requires app/ocr/vision/interaction")
    scope = OperationScope(120, parent=current_operation_scope(), target=getattr(app, "target_runtime", None),
                           cancel_check=_check_cancelled)
    runtime = VisiblePrisonProductRuntime(app, vision, ocr, interaction, scope, _check_cancelled)
    return runtime.run_visible(goods)

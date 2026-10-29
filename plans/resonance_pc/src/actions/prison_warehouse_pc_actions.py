"""Public entry for one observed product-warehouse withdrawal operation."""

from __future__ import annotations

import asyncio
from typing import Any

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.cancellation import is_current_task_cancel_requested
from packages.aura_core.scheduler.operation_scope import OperationScope, current_operation_scope
from packages.aura_core.utils.exceptions import StopTaskException


def _check_cancelled() -> None:
    if is_current_task_cancel_requested():
        raise asyncio.CancelledError("Product-warehouse withdrawal cancelled")


@action_info(
    name="resonance_pc.read_visible_prison_products", public=False, read_only=False,
    timeout=120, description="Read only visible prison products from and back to the profile menu; never scroll or transfer.",
)
@requires_services(
    app="plans/aura_base/app", vision="plans/aura_base/vision", ocr="plans/aura_base/ocr",
    interaction="plans/aura_base/interaction",
)
def resonance_pc_read_visible_prison_products(goods: Any = None, *, app=None, vision=None, ocr=None, interaction=None):
    from ._prison_product_snapshot import read_visible_prison_products

    return read_visible_prison_products(app, ocr, vision, interaction, goods=goods)


@action_info(
    name="resonance_pc.withdraw_product_warehouse_to_train",
    public=True,
    read_only=False,
    description="Withdraw currently visible eligible products without scrolling and return to the reforming-center overview.",
)
@requires_services(
    app="plans/aura_base/app",
    vision="plans/aura_base/vision",
    ocr="plans/aura_base/ocr",
    interaction="plans/aura_base/interaction",
)
async def resonance_pc_withdraw_product_warehouse_to_train(
    goods: Any = None,
    quantity_threshold: int = 1,
    *,
    app: Any = None,
    vision: Any = None,
    ocr: Any = None,
    interaction: Any = None,
) -> dict:
    """Run a visible-only withdrawal pass, read-only viewport check and verified return.

    Args:
        goods: Optional product names or IDs; an unspecified selection attempts all visible products.
        quantity_threshold: Nonnegative minimum source-stack quantity; default 1.
    """
    if any(service is None for service in (app, vision, ocr, interaction)):
        raise ValueError("Warehouse withdrawal requires app, vision, ocr and interaction services")
    _check_cancelled()
    scope = OperationScope(
        parent=current_operation_scope(),
        target=getattr(app, "target_runtime", None),
        cancel_check=_check_cancelled,
    )

    def run() -> dict:
        # Construction and all synchronous work share the actual worker's target
        # lease. Cancellation never starts a second worker or a cleanup click.
        scope.checkpoint()
        from ._prison_warehouse_runtime import WarehouseTransferRuntime

        runtime = WarehouseTransferRuntime(
            app=app,
            vision=vision,
            ocr=ocr,
            interaction=interaction,
            scope=scope,
            cancel_check=_check_cancelled,
        )
        return runtime.run(goods=goods, quantity_threshold=quantity_threshold)

    result = await scope.run_async(run)
    scope.checkpoint()
    if not isinstance(result, dict):
        raise TypeError("Warehouse withdrawal runtime must return a business-result dictionary")
    if result.get("success") is True and result.get("returned_to_overview") is not True:
        result = {
            **result,
            "success": False,
            "status": "failed",
            "reason": "warehouse_final_overview_unconfirmed",
        }
    if result.get("success") is not True:
        # A returned {success: false} still becomes a successful framework node.
        # Preserve the whole business result and terminate with a failed node.
        logger.error("[PrisonWarehouseTransfer] stopped result=%s", result)
        reason = result.get("reason") or result.get("stop_reason") or "warehouse_transfer_failed"
        raise StopTaskException(
            f"Product-warehouse withdrawal failed: {reason}",
            success=False,
            details={"warehouse_transfer": result},
        )
    logger.info("[PrisonWarehouseTransfer] completed result=%s", result)
    return result

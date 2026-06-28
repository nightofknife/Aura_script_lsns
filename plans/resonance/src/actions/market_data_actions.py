"""Actions for Resonance market data service."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from packages.aura_core.api import action_info, requires_services

from ..services.resonance_market_data_service import ResonanceMarketDataService


def _require_service(service: Optional[ResonanceMarketDataService]) -> ResonanceMarketDataService:
    if service is None:
        raise RuntimeError("resonance_market_data service is not available.")
    return service


@action_info(name="resonance.market_refresh", public=True, read_only=False, description="Refresh Resonance market snapshot.")
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_refresh(
    force: bool = False,
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_market_data).refresh(force=force)


@action_info(
    name="resonance.market_sync_web_constants",
    public=True,
    read_only=False,
    description="Sync route constants (cities/fatigue) from webpage and optionally sync buy_lot.",
)
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_sync_web_constants(
    sync_buy_lot: bool = True,
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_market_data).sync_web_constants(sync_buy_lot=sync_buy_lot)


@action_info(name="resonance.market_get_latest", public=True, read_only=True, description="Get latest Resonance market snapshot.")
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_get_latest(
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_market_data).get_latest()


@action_info(name="resonance.market_get_snapshot", public=True, read_only=True, description="Get Resonance market snapshot by id.")
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_get_snapshot(
    snapshot_id: str,
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> Dict[str, Any]:
    return _require_service(resonance_market_data).get_snapshot(snapshot_id=snapshot_id)


@action_info(name="resonance.market_list_snapshots", public=True, read_only=True, description="List cached Resonance market snapshots.")
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_list_snapshots(
    limit: int = 50,
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> List[Dict[str, Any]]:
    return _require_service(resonance_market_data).list_snapshots(limit=limit)


@action_info(name="resonance.market_query_products", public=True, read_only=True, description="Query normalized products from latest Resonance snapshot.")
@requires_services(resonance_market_data="resonance_market_data")
def resonance_market_query_products(
    scope: Optional[str] = None,
    city_id: Optional[str] = None,
    side: Optional[str] = None,
    resonance_market_data: ResonanceMarketDataService | None = None,
) -> List[Dict[str, Any]]:
    return _require_service(resonance_market_data).query_products(
        scope=scope,
        city_id=city_id,
        side=side,
    )

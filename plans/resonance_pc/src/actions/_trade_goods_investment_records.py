"""Single-account confirmed levels in the investment page's fixed city order."""
from __future__ import annotations

from typing import Any

from packages.aura_core.context.persistence.persistent_data_errors import PersistentDataNotFoundError
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.aura_core.observability.logging.core_logger import logger

from ._player_data_persistence import USER_INFO_FILE


_LEVELS_FIELD = "trade_goods_investment_levels"
_SIGNATURES_FIELD = "trade_goods_investment_catalog_signatures"


class InvestmentLevelRecords:
    """Read or explicitly write actual levels without modifying player sections.

    Only ``record`` writes, using the service's atomic update. An absent service
    retains observations within this object only. Invalid whole JSON documents
    raise the persistence service's error rather than being replaced.
    """

    def __init__(
        self,
        persistent_data: PersistentDataService | None,
        city_id: str | int,
        product_count: int,
        catalog_signature: str | None = None,
    ) -> None:
        if type(city_id) is int:
            city_id = str(city_id)
        if not isinstance(city_id, str) or not city_id.isascii() or not city_id.isdecimal() or int(city_id) <= 0:
            raise ValueError("city_id must be a positive canonical numeric city ID")
        if type(product_count) is not int or product_count < 1:
            raise ValueError("product_count must be a positive integer")
        self._persistent_data = persistent_data
        self.city_id = str(int(city_id))
        self.product_count = product_count
        if catalog_signature is not None and (not isinstance(catalog_signature, str) or not catalog_signature):
            raise ValueError("catalog_signature must be a non-empty string")
        self.catalog_signature = catalog_signature
        self._local_levels: list[int | None] = [None] * product_count

    def _check_index(self, investment_index: int) -> None:
        if type(investment_index) is not int or not 1 <= investment_index <= self.product_count:
            raise ValueError("investment_index must be a valid one-based city product index")

    def _levels(self, document: dict[str, Any]) -> list[int | None]:
        if self.catalog_signature is not None:
            signatures = document.get(_SIGNATURES_FIELD)
            if not isinstance(signatures, dict) or signatures.get(self.city_id) != self.catalog_signature:
                return [None] * self.product_count
        city_levels = document.get(_LEVELS_FIELD)
        if city_levels is None:
            return [None] * self.product_count
        if not isinstance(city_levels, dict):
            logger.warning("[TradeGoodsInvestmentRecords] invalid_city_levels_mapping")
            return [None] * self.product_count
        raw = city_levels.get(self.city_id)
        if raw is None:
            return [None] * self.product_count
        if not isinstance(raw, list) or len(raw) != self.product_count:
            logger.warning("[TradeGoodsInvestmentRecords] invalid_city_levels_array city_id=%s", self.city_id)
            return [None] * self.product_count
        levels = [value if type(value) is int and 0 <= value <= 20 else None for value in raw]
        if any(value is not None and (type(value) is not int or not 0 <= value <= 20) for value in raw):
            logger.warning("[TradeGoodsInvestmentRecords] invalid_confirmed_level city_id=%s", self.city_id)
        return levels

    def confirmed_level(self, investment_index: int) -> int | None:
        self._check_index(investment_index)
        if self._persistent_data is None:
            return self._local_levels[investment_index - 1]
        try:
            document = self._persistent_data.read(file=USER_INFO_FILE)
        except PersistentDataNotFoundError:
            return None
        level = self._levels(document)[investment_index - 1]
        return level if level is not None else self._local_levels[investment_index - 1]

    def record(self, investment_index: int, actual_level: int) -> bool:
        """Persist a visually confirmed actual level; never pass a click target."""
        self._check_index(investment_index)
        if type(actual_level) is not int or not 0 <= actual_level <= 20:
            raise ValueError("Confirmed actual level must be an integer from 0 to 20")
        if self._persistent_data is None:
            self._local_levels[investment_index - 1] = actual_level
            return True

        def update(document: dict[str, Any]) -> dict[str, Any]:
            levels = self._levels(document)
            levels[investment_index - 1] = actual_level
            city_levels = document.get(_LEVELS_FIELD)
            city_levels = dict(city_levels) if isinstance(city_levels, dict) else {}
            city_levels[self.city_id] = levels
            document[_LEVELS_FIELD] = dict(sorted(city_levels.items(), key=lambda item: _city_sort_key(item[0])))
            if self.catalog_signature is not None:
                signatures = document.get(_SIGNATURES_FIELD)
                signatures = dict(signatures) if isinstance(signatures, dict) else {}
                signatures[self.city_id] = self.catalog_signature
                document[_SIGNATURES_FIELD] = signatures
            return document

        result = self._persistent_data.update(file=USER_INFO_FILE, updater=update)
        # Python compares False == 0 and True == 1. The persistence service may
        # therefore consider a repaired bool slot unchanged and leave it on disk.
        if not result.changed and self._levels(result.old_value)[investment_index - 1] != actual_level:
            self._local_levels[investment_index - 1] = actual_level
            logger.warning("[TradeGoodsInvestmentRecords] confirmed_level_not_persisted city_id=%s investment_index=%s", self.city_id, investment_index)
            return False
        return True


def _city_sort_key(city_id: str) -> tuple[int, int, str]:
    # Retain unrelated existing entries even if an older cache has a bad key.
    if city_id.isascii() and city_id.isdecimal():
        return (0, int(city_id), city_id)
    return (1, 0, city_id)


__all__ = ["InvestmentLevelRecords"]

from __future__ import annotations

import copy
from typing import Any

from packages.aura_core.context.persistence.persistent_data_errors import (
    PersistentDataError,
    PersistentDataNotFoundError,
)
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService


USER_INFO_FILE = "user-info.json"


class PlayerDataPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_pc_user_info(persistent_data: PersistentDataService) -> dict[str, Any]:
    """Read only user-data/user-info.json; never import or fall back to legacy files."""
    try:
        payload = persistent_data.read(file=USER_INFO_FILE)
    except PersistentDataNotFoundError as exc:
        raise PlayerDataPersistenceError(
            "player_data_incomplete",
            "No cached Resonance PC player data is available.",
        ) from exc
    except PersistentDataError as exc:
        raise PlayerDataPersistenceError(
            "player_data_invalid",
            f"Cached Resonance PC player data is invalid: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise PlayerDataPersistenceError(
            "player_data_invalid",
            "Cached Resonance PC player data must be a JSON object.",
        )
    return copy.deepcopy(payload)


__all__ = [
    "PlayerDataPersistenceError",
    "USER_INFO_FILE",
    "load_pc_user_info",
]

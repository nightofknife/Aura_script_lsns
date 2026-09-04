from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packages.aura_core.context.persistence.persistent_data_errors import (
    PersistentDataError,
    PersistentDataNotFoundError,
)
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from packages.aura_core.observability.logging.core_logger import logger


USER_INFO_FILE = "user-info.json"
_PLAN_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PLAYER_DATA_FILE = _PLAN_ROOT / "data" / "cache" / "player" / "latest.json"


class PlayerDataPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_pc_user_info_migrated(persistent_data: PersistentDataService) -> bool:
    """Import the legacy PC player cache once when the new file is absent."""
    try:
        if persistent_data.exists(file=USER_INFO_FILE):
            return False
    except PersistentDataError as exc:
        raise PlayerDataPersistenceError(
            "player_data_invalid",
            f"Persistent PC player data is invalid: {exc}",
        ) from exc

    if not LEGACY_PLAYER_DATA_FILE.is_file():
        return False

    try:
        payload = json.loads(LEGACY_PLAYER_DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Legacy PC player data cannot be imported: %s", exc)
        raise PlayerDataPersistenceError(
            "player_data_invalid",
            "Cached Resonance PC player data is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        logger.warning("Legacy PC player data root is not a JSON object: %s", LEGACY_PLAYER_DATA_FILE)
        raise PlayerDataPersistenceError(
            "player_data_invalid",
            "Cached Resonance PC player data must be a JSON object.",
        )

    migrated = copy.deepcopy(payload)
    migrated.setdefault("schema_version", 1)
    metadata = migrated.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = copy.deepcopy(metadata)
    metadata["migration"] = {
        "source": "plans/resonance_pc/data/cache/player/latest.json",
        "migrated_at": _utc_now_iso(),
    }
    migrated["metadata"] = metadata
    persistent_data.set(file=USER_INFO_FILE, path=[], value=migrated)
    logger.info("Imported legacy PC player data into %s", persistent_data.root / USER_INFO_FILE)
    return True


def load_pc_user_info(persistent_data: PersistentDataService) -> dict[str, Any]:
    ensure_pc_user_info_migrated(persistent_data)
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
    "LEGACY_PLAYER_DATA_FILE",
    "PlayerDataPersistenceError",
    "USER_INFO_FILE",
    "ensure_pc_user_info_migrated",
    "load_pc_user_info",
]

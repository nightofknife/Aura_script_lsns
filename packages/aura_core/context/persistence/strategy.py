# -*- coding: utf-8 -*-
"""Persistence strategy pattern for PlanContext state management.

This module defines the IPersistenceStrategy interface and concrete implementations
for different persistence backends (in-memory, file-based, database, etc.).
"""
import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, Dict
from pathlib import Path

from packages.aura_core.observability.logging.core_logger import logger


class IPersistenceStrategy(ABC):
    """Interface for plan state persistence strategies.

    All persistence implementations must provide async methods for
    loading and saving plan state data.
    """

    @abstractmethod
    async def load(self, plan_name: str) -> Dict[str, Any]:
        """Load persisted state for a plan.

        Args:
            plan_name: Name of the plan whose state to load.

        Returns:
            Dictionary of persisted state data, or empty dict if none exists.
        """
        pass

    @abstractmethod
    async def save(self, plan_name: str, state_data: Dict[str, Any]) -> None:
        """Persist state data for a plan.

        Args:
            plan_name: Name of the plan whose state to save.
            state_data: Dictionary of state data to persist.
        """
        pass

    @abstractmethod
    async def delete(self, plan_name: str, key: str) -> None:
        """Delete a specific key from persisted state.

        Args:
            plan_name: Name of the plan.
            key: State key to delete.
        """
        pass

    @abstractmethod
    async def clear(self, plan_name: str) -> None:
        """Clear all persisted state for a plan.

        Args:
            plan_name: Name of the plan whose state to clear.
        """
        pass


class NoPersistence(IPersistenceStrategy):
    """In-memory only persistence (no actual persistence).

    This is the default strategy. State exists only during runtime
    and is lost when the process exits.
    """

    async def load(self, plan_name: str) -> Dict[str, Any]:
        """Returns empty dict - no persistence."""
        logger.debug(f"NoPersistence.load('{plan_name}'): returning empty state")
        return {}

    async def save(self, plan_name: str, state_data: Dict[str, Any]) -> None:
        """No-op - state is not persisted."""
        logger.trace(f"NoPersistence.save('{plan_name}'): no-op")
        pass

    async def delete(self, plan_name: str, key: str) -> None:
        """No-op - state is not persisted."""
        logger.trace(f"NoPersistence.delete('{plan_name}', '{key}'): no-op")
        pass

    async def clear(self, plan_name: str) -> None:
        """No-op - state is not persisted."""
        logger.trace(f"NoPersistence.clear('{plan_name}'): no-op")
        pass


class StateStorePersistence(IPersistenceStrategy):
    """File-based persistence using JSON storage.

    This implementation uses the existing StateStoreService pattern:
    - Each plan gets its own namespace in a shared JSON file
    - Async file I/O with locking for thread safety
    - Automatic directory creation
    """

    def __init__(self, storage_path: str):
        """Initialize file-based persistence.

        Args:
            storage_path: Path to the JSON storage file.
        """
        self._storage_path = Path(storage_path).resolve()
        self._lock = asyncio.Lock()
        self._initialized = False
        self._data: Dict[str, Dict[str, Any]] = {}  # {plan_name: {key: value}}

    async def _ensure_initialized(self):
        """Lazy initialization - load data on first access."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            await self._load_from_file()
            self._initialized = True
            logger.info(f"StateStorePersistence initialized: {self._storage_path}")

    async def _load_from_file(self):
        """Load all plan states from the JSON file."""
        if not self._storage_path.exists():
            self._data = {}
            return

        loop = asyncio.get_running_loop()
        try:
            def _read():
                with open(self._storage_path, 'r', encoding='utf-8') as f:
                    return json.load(f)

            loaded = await loop.run_in_executor(None, _read)

            # Ensure structure is {plan_name: {key: value}}
            if isinstance(loaded, dict):
                self._data = loaded
            else:
                logger.warning(f"Invalid state file format, resetting: {self._storage_path}")
                self._data = {}

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load state file '{self._storage_path}': {e}. Using empty state.")
            self._data = {}

    async def _save_to_file(self):
        """Save all plan states to the JSON file."""
        loop = asyncio.get_running_loop()
        try:
            data_to_save = self._data.copy()

            def _write():
                self._storage_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._storage_path, 'w', encoding='utf-8') as f:
                    json.dump(data_to_save, f, indent=4, ensure_ascii=False)

            await loop.run_in_executor(None, _write)

        except Exception as e:
            logger.error(f"Failed to save state file '{self._storage_path}': {e}", exc_info=True)

    async def load(self, plan_name: str) -> Dict[str, Any]:
        """Load persisted state for a plan."""
        await self._ensure_initialized()
        return self._data.get(plan_name, {}).copy()

    async def save(self, plan_name: str, state_data: Dict[str, Any]) -> None:
        """Persist state data for a plan."""
        await self._ensure_initialized()

        async with self._lock:
            self._data[plan_name] = state_data.copy()
            await self._save_to_file()

    async def delete(self, plan_name: str, key: str) -> None:
        """Delete a specific key from persisted state."""
        await self._ensure_initialized()

        async with self._lock:
            if plan_name in self._data and key in self._data[plan_name]:
                del self._data[plan_name][key]
                await self._save_to_file()

    async def clear(self, plan_name: str) -> None:
        """Clear all persisted state for a plan."""
        await self._ensure_initialized()

        async with self._lock:
            if plan_name in self._data:
                self._data[plan_name] = {}
                await self._save_to_file()


class DatabasePersistence(IPersistenceStrategy):
    """Placeholder for future database-backed persistence.

    Future implementations could support:
    - SQLite for local storage
    - PostgreSQL/MySQL for shared storage
    - Redis for distributed caching
    """

    def __init__(self, connection_string: str):
        raise NotImplementedError("DatabasePersistence is not yet implemented")

    async def load(self, plan_name: str) -> Dict[str, Any]:
        raise NotImplementedError()

    async def save(self, plan_name: str, state_data: Dict[str, Any]) -> None:
        raise NotImplementedError()

    async def delete(self, plan_name: str, key: str) -> None:
        raise NotImplementedError()

    async def clear(self, plan_name: str) -> None:
        raise NotImplementedError()

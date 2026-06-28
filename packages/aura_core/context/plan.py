# -*- coding: utf-8 -*-
"""Plan execution context and plan-level data management.

This module provides:
1. current_plan_name ContextVar for tracking active plan
2. PlanContext class for plan-level data management with three layers:
   - config: Read-only configuration from config.yaml
   - state: Persistent runtime data (optional)
   - cache: Ephemeral temporary data
"""
import asyncio
from contextvars import ContextVar
from typing import Any, Dict, Optional

from packages.aura_core.observability.logging.core_logger import logger
from .persistence.strategy import IPersistenceStrategy, NoPersistence


# The current plan name in the execution context
current_plan_name: ContextVar[Optional[str]] = ContextVar('current_plan_name', default=None)


class PlanContext:
    """Plan-level data management with three distinct layers.

    This class provides a unified interface for accessing plan-specific data
    across three scopes with different characteristics:

    1. **config**: Read-only configuration loaded from plan's config.yaml
       - Immutable during plan execution
       - Merged with global config (plan overrides global)
       - Accessed via ConfigService facade

    2. **state**: Persistent runtime data
       - Read-write during execution
       - Optionally persisted across restarts (via IPersistenceStrategy)
       - Use for data that should survive restarts (counters, checkpoints, etc.)

    3. **cache**: Ephemeral temporary data
       - Read-write during execution
       - Never persisted (cleared on restart)
       - Use for temporary calculations, optimization data, etc.

    Thread-safety:
    - All operations use asyncio locks for concurrent access safety
    - Multiple tasks from same plan can safely access shared context

    Example usage:
        # In Orchestrator
        plan_ctx = PlanContext(
            plan_name="my_plan",
            config_data={"app": {"target": "MyApp"}},
            persistence_strategy=StateStorePersistence("./state.json")
        )
        await plan_ctx.initialize()

        # In ExecutionContext
        exec_ctx = ExecutionContext(inputs={...})
        exec_ctx.plan_context = plan_ctx

        # In Action/Service
        config_value = plan_ctx.config.get("app.target")  # Read-only
        await plan_ctx.state.set("counter", 42)  # Persisted
        plan_ctx.cache.set("temp_result", {...})  # Ephemeral
    """

    def __init__(
        self,
        plan_name: str,
        config_data: Optional[Dict[str, Any]] = None,
        persistence_strategy: Optional[IPersistenceStrategy] = None
    ):
        """Initialize a PlanContext.

        Args:
            plan_name: Name of the plan this context belongs to.
            config_data: Configuration dictionary loaded from config.yaml.
            persistence_strategy: Strategy for persisting state data.
                Defaults to NoPersistence (in-memory only).
        """
        self.plan_name = plan_name
        self._config_data = config_data or {}
        self._persistence = persistence_strategy or NoPersistence()

        # Three data layers
        self._state_data: Dict[str, Any] = {}
        self._cache_data: Dict[str, Any] = {}

        # Thread safety
        self._state_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()

        # Initialization flag
        self._initialized = False

        # Create accessor objects
        self.config = ConfigLayer(self._config_data)
        self.state = StateLayer(self)
        self.cache = CacheLayer(self)

    async def initialize(self):
        """Async initialization - load persisted state.

        This method should be called once after PlanContext creation,
        before the plan starts executing tasks.
        """
        if self._initialized:
            return

        async with self._state_lock:
            if self._initialized:
                return

            # Load persisted state
            try:
                persisted = await self._persistence.load(self.plan_name)
                self._state_data.update(persisted)
                logger.info(
                    f"PlanContext[{self.plan_name}] initialized: "
                    f"{len(persisted)} persisted state keys loaded"
                )
            except Exception as e:
                logger.error(
                    f"Failed to load persisted state for plan '{self.plan_name}': {e}",
                    exc_info=True
                )

            self._initialized = True

    async def shutdown(self):
        """Cleanup and final state persistence.

        This method should be called when a plan is being unloaded or
        the framework is shutting down.
        """
        async with self._state_lock:
            try:
                await self._persistence.save(self.plan_name, self._state_data)
                logger.info(f"PlanContext[{self.plan_name}] shutdown: state persisted")
            except Exception as e:
                logger.error(
                    f"Failed to persist state for plan '{self.plan_name}' during shutdown: {e}",
                    exc_info=True
                )

        # Clear cache (state is already persisted)
        async with self._cache_lock:
            self._cache_data.clear()

    def __repr__(self) -> str:
        return (
            f"PlanContext(plan='{self.plan_name}', "
            f"config_keys={len(self._config_data)}, "
            f"state_keys={len(self._state_data)}, "
            f"cache_keys={len(self._cache_data)})"
        )


class ConfigLayer:
    """Read-only configuration layer.

    Provides dot-notation access to nested configuration data.
    """

    def __init__(self, config_data: Dict[str, Any]):
        self._data = config_data

    def get(self, key_path: str, default: Any = None) -> Any:
        """Get a configuration value using dot notation.

        Args:
            key_path: Dot-separated path (e.g., "app.target_window_title")
            default: Value to return if key not found

        Returns:
            Configuration value or default

        Example:
            value = config.get("app.target_window_title", "DefaultApp")
        """
        keys = key_path.split('.')
        current = self._data

        try:
            for key in keys:
                if not isinstance(current, dict):
                    return default
                current = current[key]
            return current
        except (KeyError, TypeError):
            return default

    def get_all(self) -> Dict[str, Any]:
        """Get the entire configuration dictionary (copy).

        Returns:
            A copy of all configuration data
        """
        return self._data.copy()


class StateLayer:
    """Persistent state layer.

    Provides async read-write access to state data with optional persistence.
    """

    def __init__(self, plan_context: 'PlanContext'):
        self._ctx = plan_context

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a state value.

        Args:
            key: State key
            default: Value to return if key not found

        Returns:
            State value or default
        """
        return self._ctx._state_data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        """Set a state value and persist it.

        Args:
            key: State key
            value: Value to store
        """
        async with self._ctx._state_lock:
            self._ctx._state_data[key] = value
            # Persist immediately
            try:
                await self._ctx._persistence.save(
                    self._ctx.plan_name,
                    self._ctx._state_data
                )
            except Exception as e:
                logger.error(
                    f"Failed to persist state for plan '{self._ctx.plan_name}': {e}",
                    exc_info=True
                )

    async def delete(self, key: str) -> None:
        """Delete a state value and update persistence.

        Args:
            key: State key to delete
        """
        async with self._ctx._state_lock:
            if key in self._ctx._state_data:
                del self._ctx._state_data[key]
                # Update persistence
                try:
                    await self._ctx._persistence.delete(self._ctx.plan_name, key)
                except Exception as e:
                    logger.error(
                        f"Failed to delete persisted state key '{key}' for plan "
                        f"'{self._ctx.plan_name}': {e}",
                        exc_info=True
                    )

    async def clear(self) -> None:
        """Clear all state data and persistence.

        This removes all state for the plan.
        """
        async with self._ctx._state_lock:
            self._ctx._state_data.clear()
            try:
                await self._ctx._persistence.clear(self._ctx.plan_name)
            except Exception as e:
                logger.error(
                    f"Failed to clear persisted state for plan '{self._ctx.plan_name}': {e}",
                    exc_info=True
                )

    async def get_all(self) -> Dict[str, Any]:
        """Get all state data (copy).

        Returns:
            A copy of all state data
        """
        return self._ctx._state_data.copy()


class CacheLayer:
    """Ephemeral cache layer.

    Provides synchronous read-write access to temporary data (never persisted).
    """

    def __init__(self, plan_context: 'PlanContext'):
        self._ctx = plan_context

    def get(self, key: str, default: Any = None) -> Any:
        """Get a cached value.

        Args:
            key: Cache key
            default: Value to return if key not found

        Returns:
            Cached value or default
        """
        return self._ctx._cache_data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a cached value.

        Args:
            key: Cache key
            value: Value to cache
        """
        self._ctx._cache_data[key] = value

    def delete(self, key: str) -> None:
        """Delete a cached value.

        Args:
            key: Cache key to delete
        """
        self._ctx._cache_data.pop(key, None)

    def clear(self) -> None:
        """Clear all cached data.

        This is safe to call anytime and does not affect persistence.
        """
        self._ctx._cache_data.clear()

    def get_all(self) -> Dict[str, Any]:
        """Get all cached data (copy).

        Returns:
            A copy of all cache data
        """
        return self._ctx._cache_data.copy()

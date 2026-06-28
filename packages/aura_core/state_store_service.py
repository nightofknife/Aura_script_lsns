# packages/aura_core/state_store_service.py
# [NEW] This entire file is new.

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from filelock import FileLock, Timeout

from packages.aura_core.logger import logger


class StateStoreService:
    """
    Manages the persistent, long-term context (StateStore).
    Ensures thread-safe and process-safe file operations using file locks.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.store_type = config.get('type', 'file')

        if self.store_type != 'file':
            raise NotImplementedError("Only 'file' type StateStore is currently supported.")

        file_path_str = config.get('path', './project_state.json')
        self.file_path = Path(file_path_str).resolve()
        self.lock_path = self.file_path.with_suffix('.lock')
        self._file_lock = FileLock(self.lock_path, timeout=10)  # 10-second timeout to acquire lock
        self._memory_cache: Dict[str, Any] = {}
        self._cache_lock = threading.Lock()
        self._initialized = False

        logger.info(f"StateStoreService initialized for file: {self.file_path}")

    def _initialize_store(self):
        """Ensures the state file exists and loads initial data into cache."""
        with self._cache_lock:
            if self._initialized:
                return
            try:
                with self._file_lock:
                    self.file_path.parent.mkdir(parents=True, exist_ok=True)
                    if not self.file_path.exists():
                        self.file_path.write_text(json.dumps({}, indent=2), encoding='utf-8')

                    content = self.file_path.read_text(encoding='utf-8')
                    self._memory_cache = json.loads(content)
            except Timeout:
                logger.error("Could not acquire file lock to initialize StateStore. State may be stale.")
            except Exception as e:
                logger.error(f"Failed to initialize or load StateStore from {self.file_path}: {e}")

            self._initialized = True

    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Gets a value from the StateStore using dot notation.
        e.g., 'api.token.access_token'
        """
        if not self._initialized:
            self._initialize_store()

        with self._cache_lock:
            keys = key_path.split('.')
            current_level = self._memory_cache
            try:
                for key in keys:
                    if not isinstance(current_level, dict):
                        return default
                    current_level = current_level[key]
                return current_level
            except KeyError:
                return default

    def set(self, key_path: str, value: Any):
        """
        Sets a value in the StateStore using dot notation.
        This operation is atomic and safe for concurrent access.
        """
        if not self._initialized:
            self._initialize_store()

        with self._cache_lock:
            try:
                with self._file_lock:
                    # Reload from disk to ensure we have the latest version before modifying
                    content = self.file_path.read_text(encoding='utf-8')
                    self._memory_cache = json.loads(content)

                    # Set the value in the memory cache
                    keys = key_path.split('.')
                    d = self._memory_cache
                    for key in keys[:-1]:
                        d = d.setdefault(key, {})
                    d[keys[-1]] = value

                    # Write the entire updated cache back to disk
                    self.file_path.write_text(json.dumps(self._memory_cache, indent=2), encoding='utf-8')

                    logger.debug(f"StateStore updated: '{key_path}' set.")

            except Timeout:
                logger.error(f"Could not acquire file lock to set '{key_path}'. The update was lost.")
                raise IOError("Failed to acquire lock for state update.")
            except Exception as e:
                logger.error(f"Failed to set '{key_path}' in StateStore: {e}")
                raise

    def get_all(self) -> Dict[str, Any]:
        """Returns a copy of the entire state."""
        if not self._initialized:
            self._initialize_store()

        with self._cache_lock:
            return self._memory_cache.copy()


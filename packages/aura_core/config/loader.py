from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .service import ConfigService


@lru_cache(maxsize=4)
def get_config_service(base_path: Optional[str] = None):
    """Create and cache a ConfigService instance for global config lookups."""
    root = Path(base_path) if base_path else Path(__file__).resolve().parents[2]
    service = ConfigService()
    service.load_environment_configs(root)
    return service


def get_config_value(key_path: str, default: Any = None, base_path: Optional[str] = None) -> Any:
    """Read a config value using dot-path lookup with env overrides."""
    return get_config_service(base_path).get(key_path, default)


def get_config_section(section: str, default: Optional[dict] = None, base_path: Optional[str] = None) -> dict:
    """Read a config section (dict) with env overrides applied."""
    value = get_config_value(section, default or {}, base_path)
    return value if isinstance(value, dict) else (default or {})

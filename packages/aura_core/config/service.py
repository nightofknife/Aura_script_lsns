from __future__ import annotations

import os
from collections import ChainMap
from pathlib import Path
from typing import Any, Dict

import yaml

from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.observability.logging.core_logger import logger

__all__ = ["ConfigService", "current_plan_name"]


class ConfigService:
    """Context-aware configuration service."""

    def __init__(self):
        self._env_config: Dict[str, Any] = {}
        self._global_config: Dict[str, Any] = {}
        self._plan_configs: Dict[str, Dict[str, Any]] = {}
        logger.info("ConfigService v4.0 (Context Isolation) 已初始化。")

    def load_environment_configs(self, base_path: Path):
        self._env_config = {}
        self._global_config = {}
        try:
            from dotenv import load_dotenv

            dotenv_path = base_path / ".env"
            if dotenv_path.is_file():
                load_dotenv(dotenv_path=dotenv_path, override=True)
                logger.info("已从 '%s' 加载环境变量。", dotenv_path)
        except ImportError:
            logger.warning("python-dotenv is not installed; .env loading is disabled.")
        except Exception as exc:
            logger.error("加载 .env 文件时出错: %s", exc)

        for key, value in os.environ.items():
            if key.upper().startswith("AURA_"):
                config_key = key.upper().replace("AURA_", "").lower().replace("_", ".")
                self._set_nested_key(self._env_config, config_key, value)

        global_config_path = base_path / "config.yaml"
        if global_config_path.is_file():
            try:
                with open(global_config_path, "r", encoding="utf-8") as handle:
                    self._global_config.update(yaml.safe_load(handle) or {})
                logger.info("已加载全局配置文件: '%s'", global_config_path)
            except Exception as exc:
                logger.error("加载全局配置文件 '%s' 失败: %s", global_config_path, exc)

    def register_plan_config(self, plan_name: str, config_data: dict):
        if isinstance(config_data, dict):
            self._plan_configs[plan_name] = config_data

    def get(self, key_path: str, default: Any = None) -> Any:
        plan_name = current_plan_name.get()
        if plan_name and plan_name in self._plan_configs:
            maps_to_chain = [self._env_config, self._plan_configs[plan_name], self._global_config]
        else:
            maps_to_chain = [self._env_config, self._global_config]

        current_level: Any = ChainMap(*maps_to_chain)
        try:
            for key in key_path.split("."):
                if not isinstance(current_level, (dict, ChainMap)):
                    return default
                current_level = current_level[key]
            return current_level
        except KeyError:
            return default

    def _set_nested_key(self, data: dict, key_path: str, value: Any):
        keys = key_path.split(".")
        for key in keys[:-1]:
            data = data.setdefault(key, {})
        data[keys[-1]] = value

    def get_state_store_config(self) -> Dict[str, Any]:
        return self.get("state_store", {"type": "file", "path": "./project_state.json"})

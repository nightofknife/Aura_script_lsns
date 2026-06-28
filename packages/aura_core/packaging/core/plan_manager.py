# -*- coding: utf-8 -*-
"""Top-level plan manager."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from packages.aura_core.observability.logging.core_logger import logger
from .package_manager import PackageManager
from packages.aura_core.context.state.planner import StateMap, StatePlanner


class PlanManager:
    """Loads plan packages and creates one orchestrator per plan."""

    def __init__(
        self,
        base_dir: str,
        pause_event: Optional[asyncio.Event],
        runtime_services_provider=None,
        service_resolver=None,
        orchestrator_factory=None,
    ):
        self.base_path = Path(base_dir)
        self.pause_event = pause_event
        self._runtime_services_provider = runtime_services_provider
        self._service_resolver = service_resolver
        self._orchestrator_factory = orchestrator_factory

        logger.info("Using PackageManager for plan management")
        self.package_manager = PackageManager(
            packages_dir=self.base_path / "packages",
            plans_dir=self.base_path / "plans",
        )
        self.plans: Dict[str, Orchestrator] = {}

    def initialize(self):
        logger.info("======= PlanManager: initialize =======")
        self._initialize_with_package_manager()
        logger.info("======= PlanManager: ready (%d plans) =======", len(self.plans))

    def _initialize_with_package_manager(self):
        logger.info("--- loading packages ---")
        self.package_manager.load_all_packages()

        logger.info("--- creating orchestrators for loaded plans ---")
        self.plans.clear()

        for _, manifest in self.package_manager.loaded_packages.items():
            if manifest.path.parent.name not in {"plans", "games"}:
                continue

            plan_name = manifest.path.name
            plan_path = manifest.path
            self._load_plan_config(plan_path)

            logger.info("  -> create Orchestrator for plan: '%s'", plan_name)
            runtime_services = (
                self._runtime_services_provider()
                if callable(self._runtime_services_provider)
                else {}
            )
            if not callable(self._orchestrator_factory):
                raise RuntimeError("PlanManager requires an orchestrator_factory.")

            orchestrator = self._orchestrator_factory(
                base_dir=str(self.base_path),
                plan_name=plan_name,
                pause_event=self.pause_event,
                state_planner=None,
                loaded_package=manifest,
                runtime_services=runtime_services,
                service_resolver=self._service_resolver,
            )
            self.plans[plan_name] = orchestrator

            state_map_path = plan_path / "states_map.yaml"
            if state_map_path.is_file():
                try:
                    logger.info("  -> loading state map for '%s'", plan_name)
                    with open(state_map_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if data and isinstance(data.get("states"), dict) and isinstance(data.get("transitions"), list):
                        state_map = StateMap(data)
                        state_planner_instance = StatePlanner(state_map, orchestrator)
                        orchestrator.state_planner = state_planner_instance
                    else:
                        logger.warning("  -> invalid or empty state map: %s", state_map_path)
                except Exception as e:
                    logger.error("  -> load state map failed for '%s': %s", plan_name, e, exc_info=True)

    def _load_plan_config(self, plan_path: Path) -> Dict:
        config_path = plan_path / "config.yaml"
        if not config_path.is_file():
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("Failed to load plan config '%s': %s", config_path, e)
            return {}

    def get_plan(self, plan_name: str) -> Optional[Orchestrator]:
        return self.plans.get(plan_name)

    def list_plans(self) -> List[str]:
        return list(self.plans.keys())

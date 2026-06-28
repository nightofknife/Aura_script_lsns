# -*- coding: utf-8 -*-
"""Action name/FQID resolution policy."""

from __future__ import annotations

from typing import Any

from packages.aura_core.api import ACTION_REGISTRY
from packages.aura_core.observability.logging.core_logger import logger


class ActionResolver:
    """Resolve action references with package dependency checks."""

    def __init__(self, current_package: Any = None):
        self.current_package = current_package

    def resolve(self, action_name: str) -> str:
        if '/' not in action_name:
            if not self.current_package:
                return action_name

            canonical_id = self.current_package.package.canonical_id.lstrip('@')
            parts = canonical_id.split('/')
            local_fqid = (
                f"{parts[0]}/{parts[1]}/{action_name}"
                if len(parts) == 2
                else f"{canonical_id}/{action_name}"
            )

            if ACTION_REGISTRY.get(local_fqid):
                logger.debug("Resolved local action: %s -> %s", action_name, local_fqid)
                return local_fqid

            logger.debug(
                "Local action '%s' is not exported by package '%s'; no cross-package fallback is allowed.",
                action_name,
                canonical_id,
            )
            return local_fqid

        parts = action_name.split('/')
        if len(parts) != 3:
            raise ValueError(
                f"Invalid external action FQID: '{action_name}'. "
                "Expected format: 'author/package/action' (e.g., 'Aura-Project/base/click')"
            )

        author, package, _action = parts
        external_package_id = f"{author}/{package}"
        if self.current_package and not self.is_dependency_declared(external_package_id):
            raise ValueError(
                f"Action '{action_name}' references undeclared external package '{external_package_id}'. "
                "Please add it to manifest dependencies."
            )

        logger.debug("Resolved external action: %s", action_name)
        return action_name

    def is_dependency_declared(self, package_id: str) -> bool:
        if not self.current_package:
            return True

        for dep_name in self.current_package.dependencies.keys():
            if dep_name.lstrip('@') == package_id:
                return True

        for extend in self.current_package.extends:
            if extend.get('package', '').lstrip('@') == package_id:
                return True

        return False

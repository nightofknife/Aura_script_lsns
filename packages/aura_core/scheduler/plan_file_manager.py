# -*- coding: utf-8 -*-
"""Compatibility shim for historical scheduler plan file manager import."""

from packages.aura_core.packaging.core.workspace_service import PlanWorkspaceService


class PlanFileManager(PlanWorkspaceService):
    """Backward-compatible alias around the extracted workspace service."""

    pass

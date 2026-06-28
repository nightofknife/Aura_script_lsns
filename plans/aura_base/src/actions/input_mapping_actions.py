# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional

from packages.aura_core.api import action_info, requires_services

from ..services.app_provider_service import AppProviderService
from ..services.input_mapping_service import InputMappingService


@action_info(name="input.available_profiles", public=True, read_only=True)
@requires_services(input_mapping="input_mapping")
def input_available_profiles(
    input_mapping: InputMappingService,
) -> list[str]:
    return input_mapping.available_profiles()


@action_info(name="input.list_actions", public=True, read_only=True)
@requires_services(input_mapping="input_mapping")
def input_list_actions(
    input_mapping: InputMappingService,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.list_actions(profile=profile)


@action_info(name="input.resolve_action", public=True, read_only=True)
@requires_services(input_mapping="input_mapping")
def input_resolve_action(
    input_mapping: InputMappingService,
    action_name: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.resolve_binding(action_name, profile=profile)


@action_info(name="input.press_action", public=True)
@requires_services(input_mapping="input_mapping", app="app")
def input_press_action(
    input_mapping: InputMappingService,
    app: AppProviderService,
    action_name: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.execute_action(action_name, phase="press", app=app, profile=profile)


@action_info(name="input.tap_action", public=True)
@requires_services(input_mapping="input_mapping", app="app")
def input_tap_action(
    input_mapping: InputMappingService,
    app: AppProviderService,
    action_name: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.execute_action(action_name, phase="tap", app=app, profile=profile)


@action_info(name="input.hold_action", public=True)
@requires_services(input_mapping="input_mapping", app="app")
def input_hold_action(
    input_mapping: InputMappingService,
    app: AppProviderService,
    action_name: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.execute_action(action_name, phase="hold", app=app, profile=profile)


@action_info(name="input.release_action", public=True)
@requires_services(input_mapping="input_mapping", app="app")
def input_release_action(
    input_mapping: InputMappingService,
    app: AppProviderService,
    action_name: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    return input_mapping.execute_action(action_name, phase="release", app=app, profile=profile)

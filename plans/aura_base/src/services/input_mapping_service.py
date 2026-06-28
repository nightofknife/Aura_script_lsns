# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

from packages.aura_core.api import service_info
from packages.aura_core.config.service import ConfigService
from packages.aura_core.context.plan import current_plan_name

from ..platform.contracts import TargetRuntimeError
from ..platform.look_math import resolve_look_direction_vector
from .app_provider_service import AppProviderService
from .gamepad_service import GamepadService


@service_info(alias="input_mapping", public=True, singleton=True, deps={"config": "core/config", "gamepad": "gamepad"})
class InputMappingService:
    def __init__(self, config: ConfigService, gamepad: GamepadService):
        self._config = config
        self._gamepad = gamepad
        self._repo_root = _discover_repo_root(Path(__file__).resolve())
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def get_active_profile(self, profile: Optional[str] = None) -> str:
        resolved = str(profile or self._config.get("input.profile", "default_pc") or "default_pc").strip()
        return resolved or "default_pc"

    def list_actions(self, profile: Optional[str] = None) -> dict[str, Any]:
        payload = self._load_bindings(profile=profile)
        return dict(payload.get("actions", {}))

    def available_profiles(self) -> list[str]:
        plan_name = current_plan_name.get()
        profiles: set[str] = set()
        for candidate_plan in self._iter_profile_plan_names(plan_name):
            profile_dir = self._profile_dir_for_plan(candidate_plan)
            if not profile_dir.is_dir():
                continue
            profiles.update(path.stem for path in profile_dir.glob("*.yaml"))
        return sorted(profiles)

    def resolve_binding(self, action_name: str, profile: Optional[str] = None) -> dict[str, Any]:
        normalized_action = str(action_name or "").strip()
        if not normalized_action:
            raise TargetRuntimeError("input_action_invalid", "input action name is empty.")

        actions = self.list_actions(profile=profile)
        if normalized_action not in actions:
            raise TargetRuntimeError(
                "input_action_not_found",
                f"Input action '{normalized_action}' is not defined in the active mapping profile.",
                {"action_name": normalized_action, "profile": self.get_active_profile(profile)},
            )
        binding = actions[normalized_action]
        if not isinstance(binding, Mapping):
            raise TargetRuntimeError(
                "input_binding_invalid",
                f"Input action '{normalized_action}' must resolve to an object binding.",
                {"action_name": normalized_action, "binding": binding},
            )
        normalized = dict(binding)
        normalized.setdefault("action_name", normalized_action)
        self._validate_binding(normalized)
        return normalized

    def execute_action(
        self,
        action_name: str,
        *,
        phase: str,
        app: AppProviderService,
        profile: Optional[str] = None,
    ) -> dict[str, Any]:
        binding = self.resolve_binding(action_name, profile=profile)
        return self.execute_binding(binding, phase=phase, app=app)

    def execute_binding(
        self,
        binding: Mapping[str, Any],
        *,
        phase: str,
        app: AppProviderService,
    ) -> dict[str, Any]:
        normalized_phase = str(phase or "").strip().lower()
        if normalized_phase not in {"press", "tap", "hold", "release"}:
            raise TargetRuntimeError(
                "input_phase_invalid",
                "Input execution phase must be one of: press, tap, hold, release.",
                {"phase": phase},
            )

        binding_type = str(binding.get("type") or "").strip().lower()
        action_name = str(binding.get("action_name") or "").strip() or None
        result = {
            "ok": True,
            "phase": normalized_phase,
            "type": binding_type,
            "action_name": action_name,
        }

        if binding_type == "key":
            self._execute_key(binding, phase=normalized_phase, app=app)
        elif binding_type == "mouse_button":
            self._execute_mouse_button(binding, phase=normalized_phase, app=app)
        elif binding_type == "chord":
            self._execute_chord(binding, phase=normalized_phase, app=app)
        elif binding_type == "text":
            self._execute_text(binding, phase=normalized_phase, app=app)
        elif binding_type == "look":
            self._execute_look(binding, phase=normalized_phase, app=app)
        elif binding_type == "gamepad_button":
            self._execute_gamepad_button(binding, phase=normalized_phase)
        elif binding_type == "gamepad_stick":
            self._execute_gamepad_stick(binding, phase=normalized_phase)
        elif binding_type == "trigger":
            self._execute_gamepad_trigger(binding, phase=normalized_phase)
        else:
            raise TargetRuntimeError(
                "input_binding_invalid",
                f"Unsupported input binding type '{binding_type}'.",
                {"binding": dict(binding)},
            )

        result["binding"] = dict(binding)
        return result

    def _load_bindings(self, profile: Optional[str] = None) -> dict[str, Any]:
        plan_name = current_plan_name.get() or "__global__"
        resolved_profile = self.get_active_profile(profile)
        cache_key = (plan_name, resolved_profile)
        if cache_key in self._cache:
            return self._cache[cache_key]

        config_actions = self._config.get("input.actions", {}) or {}
        if not isinstance(config_actions, Mapping):
            config_actions = {}
        actions = dict(config_actions)

        for candidate_plan in self._iter_profile_plan_names(plan_name):
            file_payload = self._load_profile_file(plan_name=candidate_plan, profile=resolved_profile)
            file_actions = file_payload.get("actions", {})
            if isinstance(file_actions, Mapping):
                actions.update(dict(file_actions))

        payload = {
            "profile": resolved_profile,
            "plan_name": plan_name,
            "actions": actions,
        }
        self._cache[cache_key] = payload
        return payload

    def _load_profile_file(self, *, plan_name: str, profile: str) -> dict[str, Any]:
        if not plan_name or plan_name == "__global__":
            return {}
        path = self._profile_dir_for_plan(plan_name) / f"{profile}.yaml"
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, Mapping):
            return dict(data)
        return {}

    def _iter_profile_plan_names(self, plan_name: Optional[str]) -> list[str]:
        names = ["aura_base"]
        normalized = str(plan_name or "").strip()
        if normalized and normalized not in {"__global__", "aura_base"}:
            names.append(normalized)
        return names

    def _profile_dir_for_plan(self, plan_name: str) -> Path:
        return self._repo_root / "plans" / str(plan_name) / "data" / "input_profiles"

    def _validate_binding(self, binding: Mapping[str, Any]) -> None:
        binding_type = str(binding.get("type") or "").strip().lower()
        if binding_type not in {
            "key",
            "mouse_button",
            "chord",
            "text",
            "look",
            "gamepad_button",
            "gamepad_stick",
            "trigger",
        }:
            raise TargetRuntimeError(
                "input_binding_invalid",
                "Input binding type is unsupported.",
                {"binding": dict(binding)},
            )

    def _execute_key(self, binding: Mapping[str, Any], *, phase: str, app: AppProviderService) -> None:
        key = str(binding.get("key") or "").strip()
        if not key:
            raise TargetRuntimeError("input_binding_invalid", "Key binding requires a key.", {"binding": dict(binding)})
        presses = max(int(binding.get("presses", 1)), 1)
        interval = float(binding.get("interval", 0.0))
        if phase in {"press", "tap"}:
            app.press_key(key, presses=presses, interval=interval)
        elif phase == "hold":
            app.key_down(key)
        elif phase == "release":
            app.key_up(key)

    def _execute_mouse_button(self, binding: Mapping[str, Any], *, phase: str, app: AppProviderService) -> None:
        button = str(binding.get("button") or "left").strip().lower() or "left"
        clicks = max(int(binding.get("clicks", 1)), 1)
        interval = float(binding.get("interval", 0.0))
        if phase in {"press", "tap"}:
            app.controller.click(button=button, clicks=clicks, interval=interval)
        elif phase == "hold":
            app.controller.mouse_down(button)
        elif phase == "release":
            app.controller.mouse_up(button)

    def _execute_chord(self, binding: Mapping[str, Any], *, phase: str, app: AppProviderService) -> None:
        keys = [str(item).strip() for item in binding.get("keys", []) if str(item).strip()]
        if not keys:
            raise TargetRuntimeError("input_binding_invalid", "Chord binding requires keys.", {"binding": dict(binding)})
        interval = float(binding.get("interval", 0.0))
        if phase in {"press", "tap"}:
            held_keys = keys[:-1]
            trigger_key = keys[-1]
            try:
                for key in held_keys:
                    app.key_down(key)
                app.press_key(trigger_key, presses=1, interval=interval)
            finally:
                for key in reversed(held_keys):
                    app.key_up(key)
        elif phase == "hold":
            for key in keys:
                app.key_down(key)
        elif phase == "release":
            for key in reversed(keys):
                app.key_up(key)

    def _execute_text(self, binding: Mapping[str, Any], *, phase: str, app: AppProviderService) -> None:
        if phase not in {"press", "tap"}:
            raise TargetRuntimeError(
                "input_phase_invalid",
                "Text bindings only support press/tap phases.",
                {"binding": dict(binding), "phase": phase},
            )
        text = str(binding.get("text") or "")
        interval = float(binding.get("interval", 0.0))
        app.type_text(text, interval=interval)

    def _execute_look(self, binding: Mapping[str, Any], *, phase: str, app: AppProviderService) -> None:
        if phase == "release":
            return
        if "dx" in binding or "dy" in binding:
            app.look_delta(int(binding.get("dx", 0)), int(binding.get("dy", 0)))
            return
        if "direction" in binding:
            vx, vy = resolve_look_direction_vector(
                str(binding.get("direction") or ""),
                float(binding.get("strength", 0.4)),
            )
            duration_ms = max(int(binding.get("duration_ms", 200)), 1)
            tick_ms = max(int(binding.get("tick_ms", 16)), 1)
            app.look_hold(vx, vy, duration_ms=duration_ms, tick_ms=tick_ms)
            return
        raise TargetRuntimeError(
            "input_binding_invalid",
            "Look binding must define dx/dy or direction.",
            {"binding": dict(binding)},
        )

    def _execute_gamepad_button(self, binding: Mapping[str, Any], *, phase: str) -> None:
        button = str(binding.get("button") or "").strip()
        if not button:
            raise TargetRuntimeError(
                "input_binding_invalid",
                "gamepad_button binding requires button.",
                {"binding": dict(binding)},
            )
        if phase in {"press", "tap"}:
            duration_ms = max(int(binding.get("duration_ms", 0)), 0)
            self._gamepad.tap_button(button, duration_ms=duration_ms)
        elif phase == "hold":
            self._gamepad.press_button(button)
        elif phase == "release":
            self._gamepad.release_button(button)

    def _execute_gamepad_stick(self, binding: Mapping[str, Any], *, phase: str) -> None:
        stick = str(binding.get("stick") or "").strip().lower()
        x = float(binding.get("x", 0.0))
        y = float(binding.get("y", 0.0))
        duration_ms = max(int(binding.get("duration_ms", 0)), 0)
        if phase in {"press", "tap", "hold"}:
            self._gamepad.tilt_stick(stick=stick, x=x, y=y, duration_ms=duration_ms, auto_center=phase in {"press", "tap"})
        elif phase == "release":
            self._gamepad.center_stick(stick)

    def _execute_gamepad_trigger(self, binding: Mapping[str, Any], *, phase: str) -> None:
        side = str(binding.get("side") or "").strip().lower()
        value = float(binding.get("value", 0.0))
        duration_ms = max(int(binding.get("duration_ms", 0)), 0)
        if phase in {"press", "tap", "hold"}:
            self._gamepad.set_trigger(side=side, value=value, duration_ms=duration_ms, auto_reset=phase in {"press", "tap"})
        elif phase == "release":
            self._gamepad.set_trigger(side=side, value=0.0, duration_ms=0, auto_reset=False)


def _discover_repo_root(start_path: Path) -> Path:
    for parent in start_path.parents:
        if (parent / "plans" / "aura_base").is_dir():
            return parent
    return start_path.parents[4]

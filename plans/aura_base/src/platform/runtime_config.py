# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .contracts import TargetRuntimeError

WINDOWS_PROVIDER = "windows"
MUMU_PROVIDER = "mumu"

FAMILY_BY_PROVIDER = {
    WINDOWS_PROVIDER: "windows_desktop",
    MUMU_PROVIDER: "android_emulator",
}
PROVIDER_BY_FAMILY = {value: key for key, value in FAMILY_BY_PROVIDER.items()}

WINDOWS_CAPTURE_BACKENDS = ("wgc", "dxgi", "gdi", "printwindow")
WINDOWS_INPUT_BACKENDS = ("sendinput", "window_message")
WINDOWS_TARGET_MODES = ("hwnd", "process", "title")

MUMU_CAPTURE_BACKENDS = ("scrcpy_stream",)
MUMU_INPUT_BACKENDS = ("android_touch",)
MUMU_TARGET_MODES = ("adb_serial", "auto")
DEFAULT_MUMU_INPUT_OPTIONS = {
    "remote_dir": "/data/local/tmp/aura",
    "helper_path": "android_touch",
    "path_fps": 10,
    "key_input_provider": "adb",
    "text_input_provider": "adb",
}


@dataclass(frozen=True)
class RuntimeTargetConfig:
    mode: str
    hwnd: int | None = None
    pid: int | None = None
    process_name: str | None = None
    exe_path_contains: str | None = None
    title: str | None = None
    title_exact: bool = False
    title_regex: str | None = None
    class_name: str | None = None
    class_exact: bool = False
    class_regex: str | None = None
    adb_serial: str = "auto"
    connect_on_start: bool = False
    require_visible: bool = True
    require_foreground: bool = False
    allow_borderless: bool = True
    allow_child_window: bool = False
    allow_empty_title: bool = False
    monitor_index: int | None = None
    client_size_exact: tuple[int, int] | None = None
    client_size_min: tuple[int, int] | None = None
    client_size_max: tuple[int, int] | None = None
    prefer_largest_client_area: bool = False
    prefer_newest_process: bool = False
    exclude_titles: tuple[str, ...] = ()
    exclude_process_names: tuple[str, ...] = ()
    launcher_process_names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeCaptureConfig:
    backend: str
    max_stale_ms: int = 100
    crop_to_client: bool = True
    capture_cursor: bool = False
    candidates: tuple[dict[str, Any], ...] = ()
    windows: dict[str, Any] = field(default_factory=dict)
    mumu: dict[str, Any] = field(default_factory=dict)

    def provider_options(self, provider: str) -> dict[str, Any]:
        options = {
            "max_stale_ms": int(self.max_stale_ms),
            "crop_to_client": bool(self.crop_to_client),
            "capture_cursor": bool(self.capture_cursor),
            "candidates": [dict(item) for item in self.candidates],
        }
        provider_options = self.windows if provider == WINDOWS_PROVIDER else self.mumu
        options.update(dict(provider_options))
        return options

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeInputConfig:
    backend: str
    focus_before_input: bool = True
    mouse_move_duration_ms: int = 120
    key_interval_ms: int = 40
    click_post_delay_ms: int = 30
    look: "RuntimeLookConfig" = field(default_factory=lambda: RuntimeLookConfig())
    activation: "RuntimeActivationConfig" = field(default_factory=lambda: RuntimeActivationConfig())
    windows: dict[str, Any] = field(default_factory=dict)
    mumu: dict[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_MUMU_INPUT_OPTIONS)
    )

    def provider_options(self, provider: str) -> dict[str, Any]:
        options = {
            "focus_before_input": bool(self.focus_before_input),
            "mouse_move_duration_ms": int(self.mouse_move_duration_ms),
            "key_interval_ms": int(self.key_interval_ms),
            "click_post_delay_ms": int(self.click_post_delay_ms),
            "look": self.look.to_dict(),
            "activation": self.activation.to_dict(),
        }
        provider_options = self.windows if provider == WINDOWS_PROVIDER else self.mumu
        options.update(dict(provider_options))
        return options

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeLookConfig:
    tick_ms: int = 16
    base_delta: int = 24
    max_delta_per_tick: int = 96
    scale_x: float = 1.0
    scale_y: float = 1.0
    invert_y: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeActivationConfig:
    mode: str = "focus_sleep"
    sleep_ms: int = 300
    click_point: tuple[int, int] | None = None
    click_button: str = "left"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeRebindConfig:
    enabled: bool = False
    max_attempts: int = 1
    retry_delay_ms: int = 300
    error_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeCoordinateConfig:
    mode: str = "client_pixels"
    reference_resolution: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeWindowSpecConfig:
    mode: str = "off"
    client_size: tuple[int, int] | None = None
    position: tuple[int, int] | None = None
    monitor_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeGamepadConfig:
    enabled: bool = False
    backend: str = "vgamepad"
    device_type: str = "xbox360"
    auto_connect: bool = True
    update_delay_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeDebugConfig:
    capture_on_error: bool = False
    dump_window_summary_on_error: bool = False
    save_ocr_artifacts: bool = False
    input_trace_size: int = 0
    artifact_dir: str = "logs/debug_artifacts"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedRuntimeConfig:
    family: str
    provider: str
    target: RuntimeTargetConfig
    capture: RuntimeCaptureConfig
    input: RuntimeInputConfig
    rebind: RuntimeRebindConfig = field(default_factory=RuntimeRebindConfig)
    coordinates: RuntimeCoordinateConfig = field(default_factory=RuntimeCoordinateConfig)
    window_spec: RuntimeWindowSpecConfig = field(default_factory=RuntimeWindowSpecConfig)
    gamepad: RuntimeGamepadConfig = field(default_factory=RuntimeGamepadConfig)
    debug: RuntimeDebugConfig = field(default_factory=RuntimeDebugConfig)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "provider": self.provider,
            "target": self.target.to_dict(),
            "capture": self.capture.to_dict(),
            "input": self.input.to_dict(),
            "rebind": self.rebind.to_dict(),
            "coordinates": self.coordinates.to_dict(),
            "window_spec": self.window_spec.to_dict(),
            "gamepad": self.gamepad.to_dict(),
            "debug": self.debug.to_dict(),
            "warnings": list(self.warnings),
        }


def resolve_runtime_config(config: Any) -> ResolvedRuntimeConfig:
    runtime = _coerce_dict(_config_get(config, "runtime", {}))
    legacy_target = _coerce_dict(_config_get(config, "target", {}))
    warnings: list[str] = []

    runtime_provider = _normalized(runtime.get("provider"))
    runtime_family = _normalized(runtime.get("family"))
    legacy_provider = _normalized(legacy_target.get("provider"))

    if runtime_provider:
        provider = runtime_provider
    elif runtime_family:
        provider = PROVIDER_BY_FAMILY.get(runtime_family, runtime_family)
    else:
        provider = legacy_provider
        if provider:
            warnings.append("Legacy target.provider is deprecated; migrate to runtime.provider.")

    if provider not in FAMILY_BY_PROVIDER:
        raise TargetRuntimeError(
            "provider_unsupported",
            "Supported runtime providers are 'windows' and 'mumu'.",
            {"provider": provider or None, "supported": sorted(FAMILY_BY_PROVIDER)},
        )

    family = runtime_family or FAMILY_BY_PROVIDER[provider]
    expected_family = FAMILY_BY_PROVIDER[provider]
    if family != expected_family:
        raise TargetRuntimeError(
            "runtime_family_provider_mismatch",
            f"Provider '{provider}' requires runtime.family='{expected_family}'.",
            {"provider": provider, "family": family, "expected_family": expected_family},
        )

    legacy_runtime = _map_legacy_target_config(provider, legacy_target, warnings)
    target_data = _deep_merge(_coerce_dict(legacy_runtime.get("target", {})), _coerce_dict(runtime.get("target", {})))
    capture_data = _deep_merge(
        _coerce_dict(legacy_runtime.get("capture", {})),
        _coerce_dict(runtime.get("capture", {})),
    )
    input_data = _deep_merge(_coerce_dict(legacy_runtime.get("input", {})), _coerce_dict(runtime.get("input", {})))
    rebind_data = _coerce_dict(runtime.get("rebind", {}))
    coordinates_data = _coerce_dict(runtime.get("coordinates", {}))
    window_spec_data = _coerce_dict(runtime.get("window_spec", {}))
    gamepad_data = _coerce_dict(runtime.get("gamepad", {}))
    debug_data = _coerce_dict(runtime.get("debug", {}))

    target = _resolve_target_config(provider, target_data)
    capture = _resolve_capture_config(provider, capture_data)
    input_config = _resolve_input_config(provider, input_data)
    rebind = _resolve_rebind_config(rebind_data)
    coordinates = _resolve_coordinate_config(coordinates_data)
    window_spec = _resolve_window_spec_config(window_spec_data)
    gamepad = _resolve_gamepad_config(gamepad_data)
    debug = _resolve_debug_config(debug_data)
    return ResolvedRuntimeConfig(
        family=family,
        provider=provider,
        target=target,
        capture=capture,
        input=input_config,
        rebind=rebind,
        coordinates=coordinates,
        window_spec=window_spec,
        gamepad=gamepad,
        debug=debug,
        warnings=tuple(warnings),
    )


def supported_capture_backends(provider: str) -> list[str]:
    normalized = _normalized(provider)
    if normalized == WINDOWS_PROVIDER:
        return list(WINDOWS_CAPTURE_BACKENDS)
    if normalized == MUMU_PROVIDER:
        return list(MUMU_CAPTURE_BACKENDS)
    return []


def supported_input_backends(provider: str) -> list[str]:
    normalized = _normalized(provider)
    if normalized == WINDOWS_PROVIDER:
        return list(WINDOWS_INPUT_BACKENDS)
    if normalized == MUMU_PROVIDER:
        return list(MUMU_INPUT_BACKENDS)
    return []


def _resolve_target_config(provider: str, target_data: Mapping[str, Any]) -> RuntimeTargetConfig:
    mode = _normalized(target_data.get("mode"))
    hwnd = _coerce_int(target_data.get("hwnd"))
    pid = _coerce_int(target_data.get("pid"))
    process_name = _coerce_text(target_data.get("process_name"))
    exe_path_contains = _coerce_text(target_data.get("exe_path_contains"))
    title = _coerce_text(target_data.get("title"))
    title_regex = _coerce_text(target_data.get("title_regex"))
    class_name = _coerce_text(target_data.get("class_name"))
    class_regex = _coerce_text(target_data.get("class_regex"))
    adb_serial = _coerce_text(target_data.get("adb_serial"), default="auto") or "auto"

    if not mode:
        if provider == WINDOWS_PROVIDER:
            if hwnd is not None:
                mode = "hwnd"
            elif process_name or pid is not None or exe_path_contains or class_name or class_regex:
                mode = "process"
            elif title or title_regex:
                mode = "title"
        else:
            mode = "adb_serial" if adb_serial != "auto" else "auto"

    valid_modes = WINDOWS_TARGET_MODES if provider == WINDOWS_PROVIDER else MUMU_TARGET_MODES
    if mode not in valid_modes:
        raise TargetRuntimeError(
            "target_mode_invalid_for_provider",
            f"Provider '{provider}' does not support target.mode='{mode or None}'.",
            {"provider": provider, "mode": mode or None, "supported": list(valid_modes)},
        )

    target = RuntimeTargetConfig(
        mode=mode,
        hwnd=hwnd,
        pid=pid,
        process_name=process_name,
        exe_path_contains=exe_path_contains,
        title=title,
        title_exact=bool(target_data.get("title_exact", False)),
        title_regex=title_regex,
        class_name=class_name,
        class_exact=bool(target_data.get("class_exact", False)),
        class_regex=class_regex,
        adb_serial=adb_serial,
        connect_on_start=bool(target_data.get("connect_on_start", False)),
        require_visible=bool(target_data.get("require_visible", True)),
        require_foreground=bool(target_data.get("require_foreground", False)),
        allow_borderless=bool(target_data.get("allow_borderless", True)),
        allow_child_window=bool(target_data.get("allow_child_window", False)),
        allow_empty_title=bool(target_data.get("allow_empty_title", False)),
        monitor_index=_coerce_int(target_data.get("monitor_index")),
        client_size_exact=_coerce_size_tuple(target_data.get("client_size_exact")),
        client_size_min=_coerce_size_tuple(target_data.get("client_size_min")),
        client_size_max=_coerce_size_tuple(target_data.get("client_size_max")),
        prefer_largest_client_area=bool(target_data.get("prefer_largest_client_area", False)),
        prefer_newest_process=bool(target_data.get("prefer_newest_process", False)),
        exclude_titles=_coerce_text_tuple(target_data.get("exclude_titles")),
        exclude_process_names=_coerce_text_tuple(target_data.get("exclude_process_names")),
        launcher_process_names=_coerce_text_tuple(target_data.get("launcher_process_names")),
    )
    _validate_target_config(provider, target)
    return target


def _resolve_capture_config(provider: str, capture_data: Mapping[str, Any]) -> RuntimeCaptureConfig:
    default_backend = "gdi" if provider == WINDOWS_PROVIDER else "scrcpy_stream"
    backend = _normalized(capture_data.get("backend"), default=default_backend)
    valid_backends = supported_capture_backends(provider)
    if backend not in valid_backends:
        raise TargetRuntimeError(
            "capture_backend_invalid_for_provider",
            f"Provider '{provider}' does not support capture.backend='{backend}'.",
            {"provider": provider, "backend": backend, "supported": valid_backends},
        )

    return RuntimeCaptureConfig(
        backend=backend,
        max_stale_ms=max(_coerce_int(capture_data.get("max_stale_ms"), 100), 0),
        crop_to_client=bool(capture_data.get("crop_to_client", True)),
        capture_cursor=bool(capture_data.get("capture_cursor", False)),
        candidates=_coerce_candidate_tuple(capture_data.get("candidates")),
        windows=_coerce_dict(capture_data.get("windows", {})),
        mumu=_coerce_dict(capture_data.get("mumu", {})),
    )


def _resolve_input_config(provider: str, input_data: Mapping[str, Any]) -> RuntimeInputConfig:
    default_backend = "sendinput" if provider == WINDOWS_PROVIDER else "android_touch"
    backend = _normalized(input_data.get("backend"), default=default_backend)
    valid_backends = supported_input_backends(provider)
    if backend not in valid_backends:
        raise TargetRuntimeError(
            "input_backend_invalid_for_provider",
            f"Provider '{provider}' does not support input.backend='{backend}'.",
            {"provider": provider, "backend": backend, "supported": valid_backends},
        )

    return RuntimeInputConfig(
        backend=backend,
        focus_before_input=bool(input_data.get("focus_before_input", True)),
        mouse_move_duration_ms=max(_coerce_int(input_data.get("mouse_move_duration_ms"), 120), 0),
        key_interval_ms=max(_coerce_int(input_data.get("key_interval_ms"), 40), 0),
        click_post_delay_ms=max(_coerce_int(input_data.get("click_post_delay_ms"), 30), 0),
        look=_resolve_look_config(_coerce_dict(input_data.get("look", {}))),
        activation=_resolve_activation_config(_coerce_dict(input_data.get("activation", {}))),
        windows=_coerce_dict(input_data.get("windows", {})),
        mumu=_deep_merge(
            DEFAULT_MUMU_INPUT_OPTIONS,
            _coerce_dict(input_data.get("mumu", {})),
        ),
    )


def _resolve_look_config(look_data: Mapping[str, Any]) -> RuntimeLookConfig:
    tick_ms = max(_coerce_int(look_data.get("tick_ms"), 16), 0)
    if tick_ms <= 0:
        raise TargetRuntimeError(
            "look_tick_invalid",
            "runtime.input.look.tick_ms must be greater than 0.",
            {"tick_ms": tick_ms},
        )

    base_delta = max(_coerce_int(look_data.get("base_delta"), 24), 0)
    if base_delta <= 0:
        raise TargetRuntimeError(
            "look_base_delta_invalid",
            "runtime.input.look.base_delta must be greater than 0.",
            {"base_delta": base_delta},
        )

    max_delta_per_tick = max(_coerce_int(look_data.get("max_delta_per_tick"), 96), 0)
    if max_delta_per_tick <= 0:
        raise TargetRuntimeError(
            "look_max_delta_invalid",
            "runtime.input.look.max_delta_per_tick must be greater than 0.",
            {"max_delta_per_tick": max_delta_per_tick},
        )

    scale_x = _coerce_float(look_data.get("scale_x"), 1.0)
    if scale_x is None or scale_x <= 0:
        raise TargetRuntimeError(
            "look_scale_invalid",
            "runtime.input.look.scale_x must be greater than 0.",
            {"scale_x": scale_x},
        )

    scale_y = _coerce_float(look_data.get("scale_y"), 1.0)
    if scale_y is None or scale_y <= 0:
        raise TargetRuntimeError(
            "look_scale_invalid",
            "runtime.input.look.scale_y must be greater than 0.",
            {"scale_y": scale_y},
        )

    return RuntimeLookConfig(
        tick_ms=tick_ms,
        base_delta=base_delta,
        max_delta_per_tick=max_delta_per_tick,
        scale_x=float(scale_x),
        scale_y=float(scale_y),
        invert_y=bool(look_data.get("invert_y", False)),
    )


def _resolve_activation_config(activation_data: Mapping[str, Any]) -> RuntimeActivationConfig:
    mode = _normalized(activation_data.get("mode"), default="focus_sleep")
    if mode not in {"focus_sleep", "focus_click_sleep"}:
        raise TargetRuntimeError(
            "activation_mode_invalid",
            "runtime.input.activation.mode must be one of: focus_sleep, focus_click_sleep.",
            {"mode": mode},
        )

    sleep_ms = max(_coerce_int(activation_data.get("sleep_ms"), 300), 0)
    click_button = _normalized(activation_data.get("click_button"), default="left")
    if click_button not in {"left", "right", "middle"}:
        raise TargetRuntimeError(
            "activation_button_invalid",
            "runtime.input.activation.click_button must be one of: left, right, middle.",
            {"click_button": click_button},
        )
    return RuntimeActivationConfig(
        mode=mode,
        sleep_ms=sleep_ms,
        click_point=_coerce_point_tuple(activation_data.get("click_point")),
        click_button=click_button,
    )


def _resolve_rebind_config(rebind_data: Mapping[str, Any]) -> RuntimeRebindConfig:
    return RuntimeRebindConfig(
        enabled=bool(rebind_data.get("enabled", False)),
        max_attempts=max(_coerce_int(rebind_data.get("max_attempts"), 1), 0),
        retry_delay_ms=max(_coerce_int(rebind_data.get("retry_delay_ms"), 300), 0),
        error_codes=_coerce_text_tuple(
            rebind_data.get(
                "error_codes",
                [
                    "window_target_lost",
                    "window_not_found",
                ],
            )
        ),
    )


def _resolve_coordinate_config(coordinate_data: Mapping[str, Any]) -> RuntimeCoordinateConfig:
    mode = _normalized(coordinate_data.get("mode"), default="client_pixels")
    if mode not in {"client_pixels", "reference_client"}:
        raise TargetRuntimeError(
            "coordinate_mode_invalid",
            "runtime.coordinates.mode must be one of: client_pixels, reference_client.",
            {"mode": mode},
        )
    reference_resolution = _coerce_size_tuple(coordinate_data.get("reference_resolution"))
    if mode == "reference_client" and reference_resolution is None:
        raise TargetRuntimeError(
            "coordinate_reference_required",
            "runtime.coordinates.reference_resolution is required when mode='reference_client'.",
            {"mode": mode},
        )
    return RuntimeCoordinateConfig(
        mode=mode,
        reference_resolution=reference_resolution,
    )


def _resolve_window_spec_config(window_spec_data: Mapping[str, Any]) -> RuntimeWindowSpecConfig:
    mode = _normalized(window_spec_data.get("mode"), default="off")
    if mode not in {"off", "require_exact", "try_resize_then_verify"}:
        raise TargetRuntimeError(
            "window_spec_mode_invalid",
            "runtime.window_spec.mode must be one of: off, require_exact, try_resize_then_verify.",
            {"mode": mode},
        )
    client_size = _coerce_size_tuple(window_spec_data.get("client_size"))
    position = _coerce_point_tuple(window_spec_data.get("position"))
    return RuntimeWindowSpecConfig(
        mode=mode,
        client_size=client_size,
        position=position,
        monitor_index=_coerce_int(window_spec_data.get("monitor_index")),
    )


def _resolve_gamepad_config(gamepad_data: Mapping[str, Any]) -> RuntimeGamepadConfig:
    backend = _normalized(gamepad_data.get("backend"), default="vgamepad")
    device_type = _normalized(gamepad_data.get("device_type"), default="xbox360")
    if device_type not in {"xbox360", "ds4"}:
        raise TargetRuntimeError(
            "gamepad_device_type_invalid",
            "runtime.gamepad.device_type must be one of: xbox360, ds4.",
            {"device_type": device_type},
        )
    return RuntimeGamepadConfig(
        enabled=bool(gamepad_data.get("enabled", False)),
        backend=backend,
        device_type=device_type,
        auto_connect=bool(gamepad_data.get("auto_connect", True)),
        update_delay_ms=max(_coerce_int(gamepad_data.get("update_delay_ms"), 0), 0),
    )


def _resolve_debug_config(debug_data: Mapping[str, Any]) -> RuntimeDebugConfig:
    return RuntimeDebugConfig(
        capture_on_error=bool(debug_data.get("capture_on_error", False)),
        dump_window_summary_on_error=bool(debug_data.get("dump_window_summary_on_error", False)),
        save_ocr_artifacts=bool(debug_data.get("save_ocr_artifacts", False)),
        input_trace_size=max(_coerce_int(debug_data.get("input_trace_size"), 0), 0),
        artifact_dir=_coerce_text(debug_data.get("artifact_dir"), "logs/debug_artifacts") or "logs/debug_artifacts",
    )


def _validate_target_config(provider: str, target: RuntimeTargetConfig) -> None:
    if provider == WINDOWS_PROVIDER:
        if target.mode == "hwnd" and target.hwnd is None:
            raise TargetRuntimeError(
                "target_config_invalid",
                "Windows target.mode='hwnd' requires runtime.target.hwnd.",
            )
        if target.mode == "process" and not any(
            (
                target.process_name,
                target.pid is not None,
                target.exe_path_contains,
                target.class_name,
                target.class_regex,
            )
        ):
            raise TargetRuntimeError(
                "target_config_invalid",
                "Windows target.mode='process' requires process_name, pid, exe_path_contains, class_name, or class_regex.",
            )
        if target.mode == "title" and not (target.title or target.title_regex):
            raise TargetRuntimeError(
                "target_config_invalid",
                "Windows target.mode='title' requires title or title_regex.",
            )
        return

    if target.mode == "adb_serial" and target.adb_serial == "auto":
        raise TargetRuntimeError(
            "target_config_invalid",
            "MuMu target.mode='adb_serial' requires runtime.target.adb_serial to be an explicit serial.",
        )


def _map_legacy_target_config(provider: str, legacy_target: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    if provider != MUMU_PROVIDER:
        legacy_windows = _coerce_dict(legacy_target.get("windows", {}))
        if legacy_windows:
            warnings.append("Legacy target.windows is deprecated; migrate to runtime.target/runtime.capture/runtime.input.")
        return {}

    legacy_mumu = _coerce_dict(legacy_target.get("mumu", {}))
    if not legacy_mumu:
        return {}

    warnings.append("Legacy target.mumu is deprecated; migrate to runtime.target/runtime.capture/runtime.input.")

    adb_cfg = _coerce_dict(legacy_mumu.get("adb", {}))
    serial = _coerce_text(adb_cfg.get("serial"), default="auto") or "auto"
    target_mode = "adb_serial" if serial != "auto" else "auto"

    capture_cfg = _coerce_dict(legacy_mumu.get("capture", {}))
    input_cfg = _coerce_dict(legacy_mumu.get("input", {}))
    key_cfg = _coerce_dict(legacy_mumu.get("key_input", {}))
    text_cfg = _coerce_dict(legacy_mumu.get("text_input", {}))

    mapped_input_mumu = dict(input_cfg)
    if "provider" in key_cfg:
        mapped_input_mumu["key_input_provider"] = key_cfg.get("provider")
    if "provider" in text_cfg:
        mapped_input_mumu["text_input_provider"] = text_cfg.get("provider")

    return {
        "target": {
            "mode": target_mode,
            "adb_serial": serial,
            "connect_on_start": bool(adb_cfg.get("connect_on_start", False)),
        },
        "capture": {
            "backend": "scrcpy_stream",
            "mumu": capture_cfg,
        },
        "input": {
            "backend": "android_touch",
            "mumu": mapped_input_mumu,
        },
    }


def _config_get(config: Any, key_path: str, default: Any = None) -> Any:
    if hasattr(config, "get") and not isinstance(config, dict):
        try:
            return config.get(key_path, default)
        except TypeError:
            pass

    current = config
    for part in str(key_path).split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(_coerce_dict(merged[key]), _coerce_dict(value))
        else:
            merged[key] = value
    return merged


def _normalized(value: Any, default: str = "") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _coerce_text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    return int(value)


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    return float(value)


def _coerce_size_tuple(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise TargetRuntimeError(
            "target_config_invalid",
            "Expected a [width, height] pair.",
            {"value": value},
        )
    width = int(value[0])
    height = int(value[1])
    if width <= 0 or height <= 0:
        raise TargetRuntimeError(
            "target_config_invalid",
            "Size values must be greater than 0.",
            {"value": value},
        )
    return width, height


def _coerce_point_tuple(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise TargetRuntimeError(
            "target_config_invalid",
            "Expected a [x, y] pair.",
            {"value": value},
        )
    return int(value[0]), int(value[1])


def _coerce_candidate_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TargetRuntimeError(
            "capture_candidate_invalid",
            "runtime.capture.candidates must be a list of candidate objects.",
            {"value": value},
        )
    candidates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TargetRuntimeError(
                "capture_candidate_invalid",
                "Each capture candidate must be an object.",
                {"candidate": item},
            )
        candidate = dict(item)
        if "backend" not in candidate:
            raise TargetRuntimeError(
                "capture_candidate_invalid",
                "Each capture candidate must include a backend field.",
                {"candidate": candidate},
            )
        candidates.append(candidate)
    return tuple(candidates)


def _coerce_text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TargetRuntimeError(
            "target_config_invalid",
            "Expected a list of strings.",
            {"value": value},
        )
    return tuple(str(item).strip() for item in value if str(item).strip())


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}

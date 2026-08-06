"""Application entrypoint for the Resonance GUI."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from packages.aura_game import SubprocessGameRunner

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


APP_ICON_RELATIVE_PATH = Path(
    "packaging/assets/aura_resonance_chibi_icon-optimized.ico"
)


def _application_icon_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parents[2]
    return root / APP_ICON_RELATIVE_PATH


def _load_application_icon() -> QIcon:
    icon_path = _application_icon_path()
    if not icon_path.is_file():
        raise RuntimeError(f"Aura Resonance application icon is missing: {icon_path}")
    icon = QIcon(str(icon_path))
    if icon.isNull():
        raise RuntimeError(f"Aura Resonance application icon is unreadable: {icon_path}")
    return icon


def _configure_application(app: QApplication) -> QIcon:
    app.setApplicationName("Aura Resonance GUI")
    app.setOrganizationName("Aura")
    app.setStyle("Fusion")
    icon = _load_application_icon()
    app.setWindowIcon(icon)
    return icon


def _import_required_wgc_module() -> object:
    return importlib.import_module("windows_capture")


def launch_resonance_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    icon = _configure_application(app)
    window = ResonanceMainWindow()
    window.setWindowIcon(icon)
    window.show()
    return int(app.exec())


def self_check_resonance_gui() -> int:
    try:
        _import_required_wgc_module()
    except Exception as exc:
        raise RuntimeError("Required WGC capture module 'windows_capture' is unavailable.") from exc

    app = QApplication.instance() or QApplication(["AuraResonanceRuntime", "--self-check"])
    icon = _configure_application(app)

    base_path = str(os.environ.get("AURA_BASE_PATH") or "").strip()
    runner = SubprocessGameRunner(
        env_overrides={"AURA_BASE_PATH": base_path} if base_path else None,
    )
    window = None
    try:
        discovered = {row.get("game_name") for row in runner.list_games(include_shared=True)}
        required = {"aura_base", "aura_benchmark", "resonance", "resonance_pc"}
        missing = sorted(required - discovered)
        if missing:
            raise RuntimeError(f"Required external plans were not discovered: {', '.join(missing)}")

        with tempfile.TemporaryDirectory(prefix="aura-gui-self-check-") as temp_dir:
            settings = QSettings(f"{temp_dir}/settings.ini", QSettings.Format.IniFormat)
            repository = ResonanceConfigRepository(settings)
            window = ResonanceMainWindow(settings=repository, initialize_on_startup=False)
            window.setWindowIcon(icon)
            if window.centralWidget() is None:
                raise RuntimeError("Resonance main window did not create a central widget.")
            if app.windowIcon().isNull() or window.windowIcon().isNull():
                raise RuntimeError("Resonance application icon was not applied to the GUI window.")
            window.close()
            app.processEvents()
        return 0
    finally:
        if window is not None and window.isVisible():
            window.close()
        runner.close()


__all__ = [
    "APP_ICON_RELATIVE_PATH",
    "ResonanceMainWindow",
    "launch_resonance_gui",
    "self_check_resonance_gui",
]

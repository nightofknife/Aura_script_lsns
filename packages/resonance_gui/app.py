"""Application entrypoint for the Resonance GUI."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEventLoop, QSettings
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from packages.aura_game import SubprocessGameRunner
from packages.aura_core.observability.logging.core_logger import logger

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.paths import resolve_application_root


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


def _light_application_palette() -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#f2ebdd",
        QPalette.ColorRole.WindowText: "#38342e",
        QPalette.ColorRole.Base: "#fffaf0",
        QPalette.ColorRole.AlternateBase: "#f3ecdf",
        QPalette.ColorRole.ToolTipBase: "#fffaf0",
        QPalette.ColorRole.ToolTipText: "#38342e",
        QPalette.ColorRole.Text: "#38342e",
        QPalette.ColorRole.Button: "#f8f3e8",
        QPalette.ColorRole.ButtonText: "#38342e",
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: "#77866b",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#52654b",
        QPalette.ColorRole.LinkVisited: "#6e5f78",
        QPalette.ColorRole.PlaceholderText: "#8d8578",
    }
    for role, value in colors.items():
        palette.setColor(QPalette.ColorGroup.Active, role, QColor(value))
        palette.setColor(QPalette.ColorGroup.Inactive, role, QColor(value))
    disabled = {
        QPalette.ColorRole.WindowText: "#9b958b",
        QPalette.ColorRole.Text: "#9b958b",
        QPalette.ColorRole.ButtonText: "#9b958b",
        QPalette.ColorRole.HighlightedText: "#f3efe7",
        QPalette.ColorRole.PlaceholderText: "#aaa398",
    }
    for role, value in disabled.items():
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(value))
    return palette


def _configure_application(app: QApplication) -> QIcon:
    app.setApplicationName("Aura Resonance GUI")
    app.setOrganizationName("Aura")
    app.setStyle("Fusion")
    app.setPalette(_light_application_palette())
    icon = _load_application_icon()
    app.setWindowIcon(icon)
    return icon


def _import_required_wgc_module() -> object:
    return importlib.import_module("windows_capture")


def launch_resonance_gui() -> int:
    # GUI shutdown outlives the worker's session log; keep a separate parent log.
    logger.setup(
        log_dir=str(resolve_application_root() / "logs"),
        task_name=f"aura_gui_{os.getpid()}",
    )
    app = QApplication.instance() or QApplication(sys.argv)
    icon = _configure_application(app)
    window = ResonanceMainWindow()
    window.setWindowIcon(icon)
    window.show()
    try:
        return int(app.exec())
    finally:
        logger.info("[GuiShutdown] phase=gui_event_loop_exited pid=%s", os.getpid())
        logging.shutdown()


def _close_window_and_wait(window: ResonanceMainWindow) -> None:
    """Keep dispatching Qt shutdown signals for callers without app.exec()."""
    close_loop = QEventLoop()
    window._bridge_thread.finished.connect(close_loop.quit)
    try:
        window.close()
        if window._bridge_thread.isRunning():
            close_loop.exec()
    finally:
        window._bridge_thread.finished.disconnect(close_loop.quit)


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
            _close_window_and_wait(window)
            app.processEvents()
        return 0
    finally:
        if window is not None and window._bridge_thread.isRunning():
            _close_window_and_wait(window)
        runner.close()


__all__ = [
    "APP_ICON_RELATIVE_PATH",
    "ResonanceMainWindow",
    "launch_resonance_gui",
    "self_check_resonance_gui",
]

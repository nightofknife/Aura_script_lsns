"""Application entrypoint for the Resonance GUI."""

from __future__ import annotations

import sys
import tempfile

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.aura_game import EmbeddedGameRunner

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.main_window import ResonanceMainWindow


def launch_resonance_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Aura Resonance GUI")
    app.setOrganizationName("Aura")
    app.setStyle("Fusion")
    window = ResonanceMainWindow()
    window.show()
    return int(app.exec())


def self_check_resonance_gui() -> int:
    app = QApplication.instance() or QApplication(["AuraResonanceRuntime", "--self-check"])
    app.setApplicationName("Aura Resonance GUI")
    app.setOrganizationName("Aura")
    app.setStyle("Fusion")

    runner = EmbeddedGameRunner()
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
            if window.centralWidget() is None:
                raise RuntimeError("Resonance main window did not create a central widget.")
            window.close()
            app.processEvents()
        return 0
    finally:
        if window is not None and window.isVisible():
            window.close()
        runner.close()


__all__ = ["ResonanceMainWindow", "launch_resonance_gui", "self_check_resonance_gui"]

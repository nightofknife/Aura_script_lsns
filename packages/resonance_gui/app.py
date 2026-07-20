"""Application entrypoint for the Resonance GUI."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import ResonanceMainWindow


def launch_resonance_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Aura Resonance GUI")
    app.setOrganizationName("Aura")
    app.setStyle("Fusion")
    window = ResonanceMainWindow()
    window.show()
    return int(app.exec())


__all__ = ["ResonanceMainWindow", "launch_resonance_gui"]

"""Two-state button with the checkbox API used by the operator console."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QPushButton, QToolTip, QWidget


class ToggleButton(QPushButton):
    """Expose boolean settings as buttons, retaining checkbox state signals."""

    stateChanged = Signal(int)
    TOOLTIP_DELAY_MS = 700

    def __init__(self, text: str | QWidget = "", parent: QWidget | None = None) -> None:
        if isinstance(text, QWidget):
            parent, text = text, ""
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setProperty("toggleButton", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.setInterval(self.TOOLTIP_DELAY_MS)
        self._tooltip_timer.timeout.connect(self._show_tooltip)
        self.toggled.connect(self._emit_state_changed)

    def _emit_state_changed(self, checked: bool) -> None:
        self.stateChanged.emit(
            Qt.CheckState.Checked.value if checked else Qt.CheckState.Unchecked.value
        )

    def checkState(self) -> Qt.CheckState:
        return Qt.CheckState.Checked if self.isChecked() else Qt.CheckState.Unchecked

    def setCheckState(self, state: Qt.CheckState | int) -> None:
        """Coerce any non-zero state to checked; this is a two-state control."""
        value = state.value if isinstance(state, Qt.CheckState) else int(state)
        self.setChecked(value != Qt.CheckState.Unchecked.value)

    def event(self, event: QEvent) -> bool:
        # QWidget may send events during construction, before the timer exists.
        if not hasattr(self, "_tooltip_timer"):
            return super().event(event)
        if event.type() == QEvent.Type.ToolTip:
            event.accept()
            return True
        if event.type() == QEvent.Type.Enter and self.toolTip():
            self._tooltip_timer.start()
        elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
            self._tooltip_timer.stop()
            QToolTip.hideText()
        return super().event(event)

    def _show_tooltip(self) -> None:
        if self.isVisible() and self.underMouse() and self.toolTip():
            QToolTip.showText(QCursor.pos(), self.toolTip(), self)

"""Click to record, click again to commit; losing focus never commits a key."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from ..hotkeys import _MODIFIER_KEYS


class HotkeyEdit(QWidget):
    recordingChanged = Signal(bool)
    shortcutChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shortcut = ""
        self._candidate = ""
        self._recording = False
        self.line_edit = QLineEdit(self)
        self.line_edit.setReadOnly(True)
        self.line_edit.setPlaceholderText("点击设置快捷键")
        self.line_edit.setToolTip("点击开始录制，按键后再次点击确认；Esc 或离开输入框取消")
        self.line_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.line_edit.installEventFilter(self)
        self.clear_button = QPushButton("清空", self)
        self.clear_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_button.setToolTip("清除快捷键，恢复未设置状态")
        self.clear_button.clicked.connect(self._clear)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.clear_button)
        self.setFocusProxy(self.line_edit)

    @property
    def recording(self) -> bool:
        return self._recording

    def shortcut(self) -> str:
        return self._shortcut

    def set_shortcut(self, value: str) -> None:
        if self._recording:
            self.cancel_recording()
        sequence = QKeySequence.fromString(value.strip(), QKeySequence.SequenceFormat.PortableText)
        if value.strip() and (sequence.count() != 1 or sequence[0].key() == Qt.Key.Key_unknown or int(sequence[0].key()) == 0):
            raise ValueError("快捷键必须是一次同时按键")
        self._shortcut = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        self.line_edit.setText(self._shortcut)

    def _begin_recording(self) -> None:
        self.line_edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self._candidate = ""
        self._recording = True
        self.line_edit.setText("请按下快捷键")
        self.line_edit.setProperty("recording", True)
        self.recordingChanged.emit(True)

    def _finish_recording(self, *, commit: bool) -> None:
        if not self._recording:
            return
        old = self._shortcut
        if commit and self._candidate:
            self._shortcut = self._candidate
        self._candidate = ""
        self._recording = False
        self.line_edit.setText(self._shortcut)
        self.line_edit.setProperty("recording", False)
        if self._shortcut != old:
            self.shortcutChanged.emit(self._shortcut)
        self.recordingChanged.emit(False)

    def cancel_recording(self) -> None:
        self._finish_recording(commit=False)

    def _clear(self) -> None:
        self.cancel_recording()
        if self._shortcut:
            self._shortcut = ""
            self.line_edit.clear()
            self.shortcutChanged.emit("")

    def hideEvent(self, event) -> None:  # noqa: N802, ANN001
        self.cancel_recording()
        super().hideEvent(event)

    def _toggle_recording(self) -> None:
        if self.isEnabled():
            if self._recording:
                self._finish_recording(commit=True)
            else:
                self._begin_recording()

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_recording()
            event.accept()
            return
        super().mousePressEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, ANN001
        if watched is self.line_edit:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._toggle_recording()
                return True
            if self._recording:
                if event.type() in {QEvent.Type.FocusOut, QEvent.Type.Hide}:
                    self.cancel_recording()
                elif event.type() == QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                elif event.type() == QEvent.Type.KeyPress:
                    if event.isAutoRepeat():
                        return True
                    if event.key() == Qt.Key.Key_Escape:
                        self.cancel_recording()
                    elif event.key() not in _MODIFIER_KEYS and event.key() != Qt.Key.Key_unknown:
                        sequence = QKeySequence(event.keyCombination())
                        self._candidate = sequence.toString(QKeySequence.SequenceFormat.PortableText)
                        self.line_edit.setText(self._candidate)
                    return True
                elif event.type() == QEvent.Type.KeyRelease:
                    return True
        return super().eventFilter(watched, event)

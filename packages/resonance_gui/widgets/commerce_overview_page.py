"""Minimal controls for running freight and passenger tasks as one sequence."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CommerceOverviewPage(QWidget):
    startRequested = Signal(bool, bool)
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False
        self._stopping = False
        self._external_busy = False
        self._build_ui()
        self._sync_controls()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 24)
        root.addStretch(1)

        panel = QFrame(self)
        panel.setObjectName("commerceOverviewPanel")
        panel.setMaximumWidth(520)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(38, 32, 38, 36)
        panel_layout.setSpacing(22)

        heading = QHBoxLayout()
        title = QLabel("跑商总控", panel)
        title.setObjectName("commerceOverviewTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        badge = QLabel("开发中", panel)
        badge.setObjectName("developmentBadge")
        heading.addWidget(badge)
        panel_layout.addLayout(heading)

        self.freight_checkbox = QCheckBox("货运", panel)
        self.freight_checkbox.setProperty("commerceSwitch", True)
        self.freight_checkbox.setChecked(True)
        self.passenger_checkbox = QCheckBox("客运", panel)
        self.passenger_checkbox.setProperty("commerceSwitch", True)
        self.passenger_checkbox.setChecked(True)
        panel_layout.addWidget(self.freight_checkbox)
        panel_layout.addWidget(self.passenger_checkbox)

        self.run_button = QPushButton("运行", panel)
        self.run_button.setProperty("commerceRun", True)
        self.run_button.clicked.connect(self._handle_run_clicked)
        panel_layout.addWidget(self.run_button)

        self.freight_checkbox.toggled.connect(self._sync_controls)
        self.passenger_checkbox.toggled.connect(self._sync_controls)

        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(panel)
        centered.addStretch(1)
        root.addLayout(centered)
        root.addStretch(2)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_stopping(self) -> bool:
        return self._stopping

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        self._stopping = False
        self._sync_controls()

    def set_stopping(self) -> None:
        if not self._running:
            return
        self._stopping = True
        self._sync_controls()

    def set_external_busy(self, busy: bool) -> None:
        self._external_busy = bool(busy)
        self._sync_controls()

    def _handle_run_clicked(self) -> None:
        if self._stopping:
            return
        if self._running:
            self.stopRequested.emit()
            return
        self.startRequested.emit(
            self.freight_checkbox.isChecked(),
            self.passenger_checkbox.isChecked(),
        )

    def _sync_controls(self, *_args: object) -> None:
        selection_enabled = not self._running and not self._external_busy
        self.freight_checkbox.setEnabled(selection_enabled)
        self.passenger_checkbox.setEnabled(selection_enabled)

        if self._stopping:
            self.run_button.setText("停止中…")
            self.run_button.setEnabled(False)
            state = "stopping"
        elif self._running:
            self.run_button.setText("停止")
            self.run_button.setEnabled(True)
            state = "stop"
        else:
            self.run_button.setText("运行")
            self.run_button.setEnabled(
                not self._external_busy
                and (self.freight_checkbox.isChecked() or self.passenger_checkbox.isChecked())
            )
            state = "run"
        self.run_button.setProperty("commerceRunState", state)
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)

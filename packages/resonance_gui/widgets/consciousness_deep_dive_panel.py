"""Small-task panel for the Consciousness Deep Dive entry flow."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ConsciousnessDeepDivePanel(QWidget):
    runRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._task_running = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        title = QLabel("识海深潜", self)
        title.setObjectName("workflowTitle")
        note = QLabel("进入关卡并停留在识海深潜棋盘。", self)
        note.setWordWrap(True)
        note.setProperty("caption", True)
        layout.addWidget(title)
        layout.addWidget(note)

        self.status_label = QLabel("尚未运行", self)
        self.status_label.setObjectName("deepDiveStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("resultState", "waiting")
        layout.addWidget(self.status_label)

        self.summary_label = QLabel("", self)
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("caption", True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)

        action_band = QFrame(self)
        action_band.setObjectName("smallTaskRunBand")
        action_layout = QHBoxLayout(action_band)
        action_layout.setContentsMargins(0, 9, 0, 0)
        action_layout.setSpacing(8)
        self.run_status = QLabel("待运行", action_band)
        self.run_status.setObjectName("smallTaskRunStatus")
        self.run_status.setProperty("status", "waiting")
        action_layout.addWidget(self.run_status)
        action_layout.addStretch(1)
        self.cancel_button = QPushButton("取消", action_band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        action_layout.addWidget(self.cancel_button)
        self.run_button = QPushButton("开始下潜", action_band)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self.runRequested.emit)
        action_layout.addWidget(self.run_button)
        layout.addWidget(action_band)

    def begin_run(self) -> None:
        self._task_running = True
        self.summary_label.clear()
        self._set_result_status("正在进入识海深潜关卡……", "running")
        self._set_run_status("正在进入关卡……", "running")
        self._sync_controls()

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "")
        page_state = str(payload.get("page_state") or "")
        if status != "completed" or page_state != "deep_dive_board":
            self.show_error(str(payload.get("reason") or "任务未进入识海深潜棋盘。"))
            return

        transitions = payload.get("transitions")
        transition_rows = list(transitions) if isinstance(transitions, list) else []
        click_attempts = sum(
            int(row.get("click_attempts") or 0)
            for row in transition_rows
            if isinstance(row, Mapping)
        )
        elapsed_ms = int(payload.get("elapsed_ms") or 0)
        self.summary_label.setText(
            f"完成 {len(transition_rows)} 个页面转换 · 点击 {click_attempts} 次 · "
            f"耗时 {elapsed_ms / 1000:.1f} 秒"
        )
        self._set_result_status("已进入识海深潜棋盘", "success")
        self._set_run_status("进入完成", "success")
        self._sync_controls()

    def show_error(self, message: str) -> None:
        self._task_running = False
        text = str(message or "未知错误")
        self.summary_label.clear()
        self._set_result_status(f"进入失败：{text}", "error")
        self._set_run_status("进入失败", "error")
        self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        self.run_button.setEnabled(not self._runner_busy and not self._task_running)
        self.cancel_button.setEnabled(self._runner_busy and self._task_running)

    def _set_result_status(self, text: str, state: str) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("resultState", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _set_run_status(self, text: str, state: str) -> None:
        self.run_status.setText(text)
        self.run_status.setProperty("status", state)
        self.run_status.style().unpolish(self.run_status)
        self.run_status.style().polish(self.run_status)


__all__ = ["ConsciousnessDeepDivePanel"]

"""Configuration, progress, and results for the Calculation Recording small task."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..config_repository import ResonanceConfigRepository
from ..logic import CALCULATION_RECORDING_TARGETS


_STOP_REASONS = {
    "scanned_all": "本轮扫描完成。",
    "defeat": "战败，已停止本轮。",
    "insufficient_tapes": "录像带不足，已停止本轮。",
    "no_progress": "关卡进度未提升，已停止本轮。",
    "game_timeout": "战斗超时，已停止本轮。",
}
_OUTCOMES = {
    "target_reached": "已达目标",
    "already_at_target": "已达标，跳过",
    "stage_locked": "关卡锁定",
    "defeat": "战败",
    "no_progress": "进度未提升",
    "insufficient_tapes": "录像带不足",
    "game_timeout": "战斗超时",
}


class CalculationRecordingPanel(QWidget):
    runRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._runner_busy = False
        self._task_running = False
        self._stopping = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("虚演刻录", self)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)
        note = QLabel("请先在游戏中打开「虚演刻录」关卡选择页。挑战使用当前编队，出牌沿用游戏内的自动战斗设置。", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.target_combo = QComboBox(self)
        for value in CALCULATION_RECORDING_TARGETS:
            self.target_combo.addItem(f"{value}%", value)
        self.target_combo.setCurrentIndex(self.target_combo.findData(settings.load_calculation_recording_target()))
        self.target_combo.currentIndexChanged.connect(self._save_target)
        form.addRow("目标进度", self.target_combo)
        layout.addLayout(form)

        self.status_label = QLabel("尚未运行", self)
        self.detail_label = QLabel(self)
        for label in (self.status_label, self.detail_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        self.results_view = QPlainTextEdit(self)
        self.results_view.setReadOnly(True)
        self.results_view.hide()
        layout.addWidget(self.results_view, 1)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.stop_button = QPushButton("停止", self)
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self._request_stop)
        self.run_button = QPushButton("开始", self)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._request_run)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)
        self._sync_controls()

    def _save_target(self, _index: int) -> None:
        self._settings.save_calculation_recording_target(int(self.target_combo.currentData()))

    def _request_run(self) -> None:
        if not self._runner_busy and not self._task_running:
            self.runRequested.emit({"target_progress": int(self.target_combo.currentData())})

    def _request_stop(self) -> None:
        if self._task_running and not self._stopping:
            self.mark_stopping()
            self.cancelRequested.emit()

    def begin_run(self, inputs: Mapping[str, Any]) -> None:
        self._task_running = True
        self._stopping = False
        self.status_label.setText("正在扫描关卡列表……")
        self.detail_label.setText(f"目标 {inputs['target_progress']}%")
        self.results_view.clear()
        self.results_view.hide()
        self._sync_controls()

    def apply_progress(self, event: Mapping[str, Any]) -> None:
        if not self._task_running:
            return
        payload = event.get("payload", event)
        if not isinstance(payload, Mapping):
            return
        number, progress = payload.get("number"), payload.get("progress")
        details = [f"目标 {payload['target_progress']}%"] if type(payload.get("target_progress")) is int else []
        if type(number) is int and type(progress) is int:
            details.insert(0, f"关卡 {number:02d} · 进度 {progress}%")
            if not self._stopping:
                self.status_label.setText("正在处理关卡……")
        if type(payload.get("attempts")) is int:
            details.append(f"挑战 {payload['attempts']} 场")
        self.detail_label.setText(" · ".join(details))

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        self._stopping = False
        default = "本轮结束。" if payload.get("status") == "completed" else "本轮已停止。"
        self.status_label.setText(_STOP_REASONS.get(str(payload.get("stop_reason")), default))
        details = [f"目标 {payload['target_progress']}%"] if type(payload.get("target_progress")) is int else []
        if type(payload.get("attempts")) is int:
            details.append(f"挑战 {payload['attempts']} 场")
        self.detail_label.setText(" · ".join(details))

        lines = []
        stages = payload.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, Mapping) or type(stage.get("number")) is not int:
                    continue
                before, after = stage.get("progress_before"), stage.get("progress_after")
                result = [f"关卡 {stage['number']:02d}"]
                if type(before) is int and type(after) is int:
                    result.append(f"{before}% → {after}%" if before != after else f"{after}%")
                if outcome := _OUTCOMES.get(str(stage.get("outcome"))):
                    result.append(outcome)
                lines.append(" · ".join(result))
        self.results_view.setPlainText("\n".join(lines))
        self.results_view.setVisible(bool(lines))
        self._sync_controls()

    def show_error(self, message: str, *, cancelled: bool = False) -> None:
        self._task_running = False
        self._stopping = False
        self.status_label.setText("本轮已停止。" if cancelled else message)
        self._sync_controls()

    def mark_stopping(self) -> None:
        if self._task_running:
            self._stopping = True
            self.status_label.setText("正在停止，等待执行端确认……")
            self._sync_controls()

    def cancel_failed(self, message: str) -> None:
        if self._task_running:
            self._stopping = False
            self.status_label.setText(message)
            self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        editable = not self._runner_busy and not self._task_running
        self.target_combo.setEnabled(editable)
        self.run_button.setEnabled(editable)
        self.stop_button.setEnabled(self._task_running and not self._stopping)

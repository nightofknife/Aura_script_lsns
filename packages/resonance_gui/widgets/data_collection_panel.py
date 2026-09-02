"""Small-task panel for development data collection."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class DataCollectionPanel(QWidget):
    captureRequested = Signal(object)
    sensitivityProbeRequested = Signal()
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

        title = QLabel("数据采集", self)
        title.setObjectName("workflowTitle")
        note = QLabel("识海深潜视觉识别素材。", self)
        note.setWordWrap(True)
        note.setProperty("caption", True)
        layout.addWidget(title)
        layout.addWidget(note)

        sensitivity_row = QHBoxLayout()
        sensitivity_row.setContentsMargins(0, 4, 0, 4)
        sensitivity_row.setSpacing(8)
        sensitivity_label = QLabel("四角度灵敏度", self)
        sensitivity_row.addWidget(sensitivity_label)
        self.capture_sensitivity_combo = QComboBox(self)
        self.capture_sensitivity_combo.addItem("慢灵敏度", "slow")
        self.capture_sensitivity_combo.addItem("快灵敏度", "fast")
        self.capture_sensitivity_combo.setCurrentIndex(0)
        sensitivity_row.addWidget(self.capture_sensitivity_combo)
        sensitivity_row.addStretch(1)
        layout.addLayout(sensitivity_row)

        self.status_label = QLabel("尚未运行", self)
        self.status_label.setObjectName("dataCollectionStatus")
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

        self.capture_button = QPushButton("采集四角度素材", action_band)
        self.capture_button.clicked.connect(self._request_capture)
        action_layout.addWidget(self.capture_button)

        self.sensitivity_probe_button = QPushButton("采集灵敏度素材", action_band)
        self.sensitivity_probe_button.clicked.connect(
            self.sensitivityProbeRequested.emit
        )
        action_layout.addWidget(self.sensitivity_probe_button)
        layout.addWidget(action_band)

    def _request_capture(self) -> None:
        if self._runner_busy or self._task_running:
            return
        self.captureRequested.emit(
            {"sensitivity": str(self.capture_sensitivity_combo.currentData() or "slow")}
        )

    def begin_capture_run(self, sensitivity: str = "slow") -> None:
        self._task_running = True
        self.summary_label.clear()
        label = "快灵敏度" if sensitivity == "fast" else "慢灵敏度"
        self._set_result_status(f"正在采集{label}的四个识别角度……", "running")
        self._set_run_status("正在采集四角度素材……", "running")
        self._sync_controls()

    def begin_sensitivity_probe_run(self) -> None:
        self._task_running = True
        self.summary_label.clear()
        self._set_result_status("正在采集灵敏度探测素材……", "running")
        self._set_run_status("正在采集灵敏度素材……", "running")
        self._sync_controls()

    def apply_capture_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "")
        captures = payload.get("captures")
        capture_rows = list(captures) if isinstance(captures, list) else []
        if status != "completed" or len(capture_rows) != 4:
            self.show_capture_error(
                str(payload.get("reason") or "任务未返回四张识别角度素材。")
            )
            return
        sensitivity = str(payload.get("sensitivity") or "slow")
        label = "快灵敏度" if sensitivity == "fast" else "慢灵敏度"
        raw_displacements = payload.get("capture_displacements")
        displacements = (
            [int(value) for value in raw_displacements]
            if isinstance(raw_displacements, list)
            else []
        )
        if not displacements:
            displacements = (
                [280, 1280, 1920, 2880]
                if sensitivity == "fast"
                else [560, 2480, 3680, 5600]
            )
        displacement_text = "、".join(str(value) for value in displacements)
        self.summary_label.setText(
            f"{label}采集完成：已按 {displacement_text} 位移保存 4 张素材"
        )
        self._set_result_status("四角度素材采集完成", "success")
        self._set_run_status("采集完成", "success")
        self._sync_controls()

    def apply_sensitivity_probe_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "")
        relative_path = str(payload.get("relative_path") or "")
        if status != "completed" or not relative_path:
            self.show_sensitivity_probe_error(
                str(payload.get("reason") or "任务未返回灵敏度探测素材。")
            )
            return
        self.summary_label.setText(f"已保存 320px 探测素材：{relative_path}")
        self._set_result_status("灵敏度素材采集完成", "success")
        self._set_run_status("采集完成", "success")
        self._sync_controls()

    def show_capture_error(self, message: str) -> None:
        self._show_error(message, "四角度素材采集失败")

    def show_sensitivity_probe_error(self, message: str) -> None:
        self._show_error(message, "灵敏度素材采集失败")

    def _show_error(self, message: str, title: str) -> None:
        self._task_running = False
        text = str(message or "未知错误")
        self.summary_label.clear()
        self._set_result_status(f"{title}：{text}", "error")
        self._set_run_status("采集失败", "error")
        self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        enabled = not self._runner_busy and not self._task_running
        self.capture_sensitivity_combo.setEnabled(enabled)
        self.capture_button.setEnabled(enabled)
        self.sensitivity_probe_button.setEnabled(enabled)
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


__all__ = ["DataCollectionPanel"]

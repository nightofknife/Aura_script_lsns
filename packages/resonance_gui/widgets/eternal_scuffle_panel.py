"""Configuration and progress for the Eternal Scuffle small task."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget

from ..config_repository import ResonanceConfigRepository


STAGES = {
    "preflight": "检查起始页面", "coin": "投币", "choose_role": "选择角色",
    "choose_equipment": "选择装备", "captain": "确认队长", "stage": "开始关卡",
    "battle": "等待战斗", "loot": "选择战利品", "assign": "分配装备",
    "abandon": "放弃本局", "settlement": "领取奖励", "return_home": "返回活动首页",
    "completed": "全部完成", "failed": "执行失败", "cancelled": "已取消",
}


class EternalScufflePanel(QWidget):
    runRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._runner_busy = False
        self._task_running = False
        self._state: dict[str, Any] = {}
        self._last_operation_stage = "preflight"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("无垠乱斗", self)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)
        note = QLabel("请停留在无垠乱斗活动首页。通关或失败放弃后领取奖励，返回首页算完成一局。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.coins_spin = QSpinBox(self)
        self.coins_spin.setRange(1, 5)
        self.run_count_spin = QSpinBox(self)
        self.run_count_spin.setRange(1, 9999)
        inputs = settings.load_eternal_scuffle_inputs()
        self.coins_spin.setValue(inputs["coins_per_run"])
        self.run_count_spin.setValue(inputs["run_count"])
        form.addRow("每局投币数量", self.coins_spin)
        form.addRow("执行局数", self.run_count_spin)
        layout.addLayout(form)
        self.coins_spin.valueChanged.connect(self._save_inputs)
        self.run_count_spin.valueChanged.connect(self._save_inputs)
        self.status_label = QLabel("尚未运行", self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label = QLabel("", self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        self.run_button = QPushButton("开始乱斗", self)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._request_run)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)
        self._sync_controls()

    def collect_inputs(self) -> dict[str, int]:
        return {"coins_per_run": self.coins_spin.value(), "run_count": self.run_count_spin.value()}

    def _save_inputs(self, *_args: Any) -> None:
        self._settings.save_eternal_scuffle_inputs(self.collect_inputs())

    def _request_run(self) -> None:
        if not self._runner_busy and not self._task_running:
            self._save_inputs()
            self.runRequested.emit(self.collect_inputs())

    def begin_run(self, inputs: Mapping[str, Any]) -> None:
        self._last_operation_stage = "preflight"
        self._state = {"run_count": inputs.get("run_count", 1), "run_index": 1,
                       "completed_runs": 0, "stage": "preflight", "status": "running"}
        self._task_running = True
        self._render()
        self._sync_controls()

    def apply_progress(self, event: Mapping[str, Any]) -> None:
        if not self._task_running:
            return
        payload = event.get("payload", event)
        if isinstance(payload, Mapping):
            self._merge(payload)
            self._render()

    def _merge(self, payload: Mapping[str, Any]) -> None:
        for key in ("run_count", "run_index", "completed_runs", "stage", "status", "error"):
            if key in payload:
                # Terminal events must preserve the last actual operation.
                if key == "stage" and payload[key] in {"failed", "cancelled"}:
                    continue
                if key == "stage" and payload[key] != "completed":
                    self._last_operation_stage = str(payload[key])
                self._state[key] = payload[key]

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        self._merge(payload)
        self._task_running = False
        if not (payload.get("success") is True and payload.get("status") == "completed"
                and self._state.get("completed_runs") == self._state.get("run_count")):
            self._state["status"] = "cancelled" if payload.get("status") == "cancelled" else "failed"
            self._state["stage"] = self._last_operation_stage
            self._state["error"] = payload.get("error") or payload.get("reason") or self._state.get("error") or "任务未完整完成。"
        self._render()
        self._sync_controls()

    def show_error(self, message: str, *, cancelled: bool = False) -> None:
        self._task_running = False
        self._state.update(status="cancelled" if cancelled else "failed", error=message)
        self._state["stage"] = self._last_operation_stage
        self._render()
        self._sync_controls()

    def _render(self) -> None:
        state = self._state
        status = state.get("status", "running")
        stage = STAGES.get(str(state.get("stage", "")), str(state.get("stage", "")))
        label = {"running": "运行中", "completed": "全部完成", "failed": "执行失败", "cancelled": "已取消"}.get(status, "执行失败")
        self.status_label.setText(f"{label} · {stage}" + (f"\n{state['error']}" if state.get("error") else ""))
        self.summary_label.setText(f"当前第 {state.get('run_index', 1)} / {state.get('run_count', 1)} 局 · 已完成 {state.get('completed_runs', 0)} 局")

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        editable = not self._runner_busy and not self._task_running
        for widget in (self.run_button, self.coins_spin, self.run_count_spin):
            widget.setEnabled(editable)
        self.cancel_button.setEnabled(self._runner_busy and self._task_running)

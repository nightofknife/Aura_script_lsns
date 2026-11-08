"""Independently collect daily-mission or battle-pass rewards from main."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class DailyRewardsPanel(QWidget):
    runRequested = Signal()
    runBattlePassRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._task_running = False
        self._stopping = False
        self._component = "daily_mission"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("日常奖励收集", self)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)
        note = QLabel(
            "请从站场主界面启动。每日任务奖励和通行证奖励可分别运行；对应入口有红点才进入，领取后返回主界面。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status_label = QLabel("尚未运行", self)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.summary_label = QLabel("", self)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self._request_cancel)
        buttons.addWidget(self.cancel_button)
        self.run_button = QPushButton("每日任务奖励", self)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._request_run)
        buttons.addWidget(self.run_button)
        self.battle_pass_button = QPushButton("通行证奖励", self)
        self.battle_pass_button.setObjectName("primaryButton")
        self.battle_pass_button.clicked.connect(self._request_battle_pass)
        buttons.addWidget(self.battle_pass_button)
        layout.addLayout(buttons)
        self._sync_controls()

    def _request_run(self) -> None:
        if not self._runner_busy and not self._task_running:
            self.runRequested.emit()

    def _request_cancel(self) -> None:
        if self._task_running and not self._stopping:
            self.mark_stopping()
            self.cancelRequested.emit()

    def _request_battle_pass(self) -> None:
        if not self._runner_busy and not self._task_running:
            self.runBattlePassRequested.emit()

    def begin_run(self, component: str = "daily_mission") -> None:
        self._component = component
        self._task_running = True
        self._stopping = False
        self.status_label.setText(f"正在收集{'通行证' if component == 'battle_pass' else '每日任务'}奖励……")
        self.summary_label.clear()
        self._sync_controls()

    def mark_stopping(self) -> None:
        if self._task_running:
            self._stopping = True
            self.status_label.setText("正在停止，等待执行端确认……")
            self._sync_controls()

    def cancel_failed(self, message: str) -> None:
        if self._task_running:
            self._stopping = False
            self.status_label.setText(f"取消请求失败，任务可能仍在运行：{message}")
            self._sync_controls()

    def apply_result(self, payload: Mapping[str, Any]) -> None:
        status = str(payload.get("status") or "")
        label = "通行证" if self._component == "battle_pass" else "每日任务"
        if (payload.get("success") is not True
                or payload.get("component") != self._component
                or payload.get("returned_to_main") is not True
                or status not in {"collected", "nothing_to_claim", "skipped_no_reminder"}):
            self.show_error(
                str(payload.get("message") or payload.get("reason") or f"未确认{label}奖励收集完成并返回主界面。"),
                cancelled=status == "cancelled",
            )
            return
        self._task_running = False
        self._stopping = False
        self.status_label.setText({
            "collected": f"{label}奖励收集完成，已返回主界面。",
            "nothing_to_claim": f"没有可领取的{label}奖励，已返回主界面。",
            "skipped_no_reminder": f"{label}没有红点，已跳过，保持在主界面。",
        }[status])
        clicks = payload.get("claim_clicks")
        summary = []
        passes = payload.get("passes_processed")
        if self._component == "battle_pass" and type(passes) is int and passes >= 0:
            summary.append(f"已处理通行证：{passes}")
        if type(clicks) is int and clicks >= 0:
            summary.append(f"领取操作次数：{clicks}（不代表奖励物品数量）")
        self.summary_label.setText("；".join(summary))
        self._sync_controls()

    def show_error(self, message: str, *, cancelled: bool = False) -> None:
        self._task_running = False
        self._stopping = False
        self.status_label.setText(f"{'已取消' if cancelled else '收集失败'}：{message}")
        self._sync_controls()

    def set_runner_busy(self, busy: bool) -> None:
        self._runner_busy = bool(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        self.run_button.setEnabled(not self._runner_busy and not self._task_running)
        self.battle_pass_button.setEnabled(not self._runner_busy and not self._task_running)
        self.cancel_button.setEnabled(self._task_running and not self._stopping)

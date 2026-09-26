"""Small-task panel for the Consciousness Deep Dive entry flow."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class ConsciousnessDeepDivePanel(QWidget):
    runRequested = Signal()
    runSingleRunRequested = Signal(object)
    runLoopRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._task_running = False
        self._mode = "entry"
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

        test_note = QLabel("单局测试从已打开的魔方界面开始，结算时停止并保留画面。", self)
        test_note.setWordWrap(True)
        test_note.setProperty("caption", True)
        layout.addWidget(test_note)
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("回合安全上限", self))
        self.round_budget_spin = QSpinBox(self)
        self.round_budget_spin.setRange(1, 100)
        self.round_budget_spin.setValue(20)
        budget_row.addWidget(self.round_budget_spin)
        budget_row.addStretch(1)
        layout.addLayout(budget_row)
        loop_row = QHBoxLayout()
        loop_row.addWidget(QLabel('循环次数（-1 持续循环）', self))
        self.loop_count_spin = QSpinBox(self)
        self.loop_count_spin.setRange(-1, 2147483647)
        self.loop_count_spin.setValue(1)
        loop_row.addWidget(self.loop_count_spin)
        loop_row.addStretch(1)
        layout.addLayout(loop_row)
        loop_note = QLabel('完整循环从活动首页开始，回到首页后才计为完成一局。',self)
        loop_note.setWordWrap(True)
        layout.addWidget(loop_note)
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
        self.test_button = QPushButton("测试移动循环", action_band)
        self.test_button.clicked.connect(
            lambda: self.runSingleRunRequested.emit({"round_budget": self.round_budget_spin.value()})
        )
        action_layout.addWidget(self.test_button)
        self.loop_button = QPushButton('开始完整循环',action_band)
        self.loop_button.clicked.connect(self._request_loop)
        action_layout.addWidget(self.loop_button)
        layout.addWidget(action_band)

    def begin_run(self) -> None:
        self._mode = "entry"
        self._task_running = True
        self.summary_label.clear()
        self._set_result_status("正在进入识海深潜关卡……", "running")
        self._set_run_status("正在进入关卡……", "running")
        self._sync_controls()

    def _request_loop(self):
        value=self.loop_count_spin.value()
        if value==0:
            self._set_result_status('循环次数必须为正整数，或 -1（持续循环）','error')
            return
        self.runLoopRequested.emit({'loop_count':value})

    def begin_loop(self):
        self._mode='loop';self._task_running=True;self._loop_completed=0
        self.summary_label.clear()
        self._set_result_status('正在检查活动首页……','running')
        self._set_run_status('循环运行中','running');self._sync_controls()

    def apply_loop_progress(self,event):
        if self._mode!='loop' or not self._task_running:return
        p=event.get('payload',event)
        self._loop_completed=int(p.get('completed_runs') or 0)
        stages={'ready':'检查首页','entry':'进入活动','single':'单局运行','cleanup':'结算收尾','activity_home':'已回首页'}
        count=p.get('loop_count');target='持续循环' if count==-1 else f'目标 {count} 局'
        self.summary_label.setText(f'已完成 {self._loop_completed} 局 · {target}')
        self._set_result_status(f"第 {p.get('run_index',0)} 局 · {stages.get(p.get('stage'),p.get('stage',''))}",'running')

    def apply_loop_result(self,payload):
        self._task_running=False
        count=int(payload.get('completed_runs',getattr(self,'_loop_completed',0)))
        self.summary_label.setText(f'已完成 {count} 局')
        ok=payload.get('success') is True
        message='循环完成，已停在活动首页' if ok else '循环停止：'+str(payload.get('reason') or '任务已停止')
        self._set_result_status(message,'success' if ok else 'error')
        self._set_run_status('完成' if ok else '已停止','success' if ok else 'error')
        self._sync_controls()

    def show_loop_error(self,message):
        self.apply_loop_result({'reason':message,'success':False})

    def begin_single_run(self) -> None:
        self._mode = "single_run"
        self._task_running = True
        self.summary_label.clear()
        self._set_result_status("正在运行魔方单局测试……", "running")
        self._set_run_status("测试运行中", "running")
        self._sync_controls()

    def apply_single_run_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "")
        turns = int(payload.get("turns_completed") or 0)
        frame = str(payload.get("last_frame") or "")
        self.summary_label.setText(
            f"已确认 {turns} 回合 · 截图：{frame or '未保存'}"
        )
        if status == "completed" and payload.get("terminal") == "settlement":
            outcome = {"failure": "失败结算", "victory": "胜利结算"}.get(
                str(payload.get("outcome") or ""), "结算"
            )
            self._set_result_status(f"已到达{outcome}，画面保持不动", "success")
            self._set_run_status("测试完成", "success")
        else:
            reason = str(payload.get("reason") or "未能确认下一步画面")
            self._set_result_status(f"单局测试停止：{reason}", "error")
            self._set_run_status("待补样本", "error")
        self._sync_controls()

    def show_single_run_error(self, message: str) -> None:
        self._mode = "single_run"
        self._task_running = False
        self.summary_label.clear()
        self._set_result_status(f"单局测试失败：{message}", "error")
        self._set_run_status("测试失败", "error")
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
        self.test_button.setEnabled(not self._runner_busy and not self._task_running)
        self.round_budget_spin.setEnabled(not self._runner_busy and not self._task_running)
        self.loop_button.setEnabled(not self._runner_busy and not self._task_running)
        self.loop_count_spin.setEnabled(not self._runner_busy and not self._task_running)
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

"""Small-task panel for the Consciousness Deep Dive entry flow."""

from __future__ import annotations

import json
from typing import Any, Mapping
from pathlib import Path

from PySide6.QtCore import Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class ConsciousnessDeepDivePanel(QWidget):
    runRequested = Signal()
    runSingleRunRequested = Signal(object)
    runLoopRequested = Signal(object)
    runScanRequested = Signal()
    runPlanRequested = Signal(object)
    runPlannedRunRequested = Signal(object)
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner_busy = False
        self._task_running = False
        self._mode = "entry"
        self._scan_report_path = ""
        self._plan_report_path = ""
        self._planned_run_snapshot: dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(9)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        layout = QVBoxLayout(content)
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
        planned_run_note = QLabel(
            "关卡内自动运行从玩家棋盘或位面间隙开始，实时扫描、规划并执行行动。"
            "队员不足五人时暂停；到达结算后保存结果并保留画面。", self
        )
        planned_run_note.setWordWrap(True)
        planned_run_note.setProperty("caption", True)
        layout.addWidget(planned_run_note)
        planned_run_options = QHBoxLayout()
        planned_run_options.addWidget(QLabel("运行策略", self))
        self.planned_run_strategy_combo = QComboBox(self)
        self.planned_run_strategy_combo.addItem("只追奇点", "chase")
        self.planned_run_strategy_combo.addItem("灵感收益优先", "inspiration")
        planned_run_options.addWidget(self.planned_run_strategy_combo)
        planned_run_options.addWidget(QLabel("自动运行回合安全上限", self))
        self.planned_run_safety_limit_spin = QSpinBox(self)
        self.planned_run_safety_limit_spin.setRange(1, 100)
        self.planned_run_safety_limit_spin.setValue(100)
        planned_run_options.addWidget(self.planned_run_safety_limit_spin)
        planned_run_options.addStretch(1)
        layout.addLayout(planned_run_options)
        self.planned_run_button = QPushButton("关卡内自动运行", self)
        self.planned_run_button.clicked.connect(self._request_planned_run)
        layout.addWidget(self.planned_run_button)
        scan_note = QLabel("布局扫描从魔方界面开始，拖动观察六面并导出目标位置和节点图标，供人工对照。", self)
        scan_note.setWordWrap(True)
        scan_note.setProperty("caption", True)
        layout.addWidget(scan_note)
        scan_row = QHBoxLayout()
        self.scan_button = QPushButton("扫描魔方布局", self)
        self.scan_button.clicked.connect(self.runScanRequested.emit)
        scan_row.addWidget(self.scan_button)
        self.scan_report_button = QPushButton("打开扫描报告", self)
        self.scan_report_button.setEnabled(False)
        self.scan_report_button.clicked.connect(self._open_scan_report)
        scan_row.addWidget(self.scan_report_button)
        scan_row.addStretch(1)
        layout.addLayout(scan_row)

        plan_note = QLabel(
            "移动规划默认使用最近一次完整扫描或目标已确认的扫描，也可选择已有布局 JSON。"
            "请确认游戏布局仍与扫描一致。生成方案仅离线计算，不会操作游戏。", self
        )
        plan_note.setWordWrap(True)
        plan_note.setProperty("caption", True)
        layout.addWidget(plan_note)
        plan_options = QHBoxLayout()
        plan_options.addWidget(QLabel("规划策略", self))
        self.plan_strategy_combo = QComboBox(self)
        self.plan_strategy_combo.addItem("只追奇点", "chase")
        self.plan_strategy_combo.addItem("灵感收益优先", "inspiration")
        plan_options.addWidget(self.plan_strategy_combo)
        plan_options.addWidget(QLabel("规划回合数", self))
        self.plan_turn_budget_spin = QSpinBox(self)
        self.plan_turn_budget_spin.setRange(1, 30)
        self.plan_turn_budget_spin.setValue(6)
        plan_options.addWidget(self.plan_turn_budget_spin)
        plan_options.addStretch(1)
        layout.addLayout(plan_options)
        layout_row = QHBoxLayout()
        self.plan_layout_edit = QLineEdit(self)
        self.plan_layout_edit.setReadOnly(True)
        self.plan_layout_edit.setPlaceholderText("自动使用最近可规划扫描")
        layout_row.addWidget(self.plan_layout_edit, 1)
        self.plan_layout_button = QPushButton("选择布局", self)
        self.plan_layout_button.clicked.connect(self._select_plan_layout)
        layout_row.addWidget(self.plan_layout_button)
        self.plan_latest_button = QPushButton("使用最近扫描", self)
        self.plan_latest_button.clicked.connect(self.plan_layout_edit.clear)
        layout_row.addWidget(self.plan_latest_button)
        layout.addLayout(layout_row)
        plan_actions = QHBoxLayout()
        self.plan_button = QPushButton("生成移动方案", self)
        self.plan_button.clicked.connect(self._request_plan)
        plan_actions.addWidget(self.plan_button)
        self.plan_report_button = QPushButton("打开方案", self)
        self.plan_report_button.setEnabled(False)
        self.plan_report_button.clicked.connect(self._open_plan_report)
        plan_actions.addWidget(self.plan_report_button)
        plan_actions.addStretch(1)
        layout.addLayout(plan_actions)
        self.plan_details = QPlainTextEdit(self)
        self.plan_details.setReadOnly(True)
        self.plan_details.setMaximumHeight(180)
        self.plan_details.hide()
        layout.addWidget(self.plan_details)
        layout.addStretch(1)

        action_band = QFrame(self)
        action_band.setObjectName("smallTaskRunBand")
        action_layout = QVBoxLayout(action_band)
        action_layout.setContentsMargins(0, 9, 0, 0)
        action_layout.setSpacing(8)
        self.run_status = QLabel("待运行", action_band)
        self.run_status.setObjectName("smallTaskRunStatus")
        self.run_status.setProperty("status", "waiting")
        action_layout.addWidget(self.run_status)
        entry_actions = QHBoxLayout()
        action_layout.addLayout(entry_actions)
        self.cancel_button = QPushButton("取消", action_band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        entry_actions.addWidget(self.cancel_button)
        self.run_button = QPushButton("开始下潜", action_band)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self.runRequested.emit)
        entry_actions.addWidget(self.run_button)
        run_actions = QHBoxLayout()
        action_layout.addLayout(run_actions)
        self.test_button = QPushButton("测试移动循环", action_band)
        self.test_button.clicked.connect(
            lambda: self.runSingleRunRequested.emit({"round_budget": self.round_budget_spin.value()})
        )
        run_actions.addWidget(self.test_button)
        self.loop_button = QPushButton('开始完整循环',action_band)
        self.loop_button.clicked.connect(self._request_loop)
        run_actions.addWidget(self.loop_button)
        root.addWidget(action_band)

    def begin_scan(self) -> None:
        self._mode = "scan"
        self._task_running = True
        self._scan_report_path = ""
        self.summary_label.clear()
        self._set_result_status("正在拖动魔方并识别布局……", "running")
        self._set_run_status("扫描中", "running")
        self._sync_controls()

    def _request_planned_run(self) -> None:
        if self._runner_busy or self._task_running:
            return
        self.runPlannedRunRequested.emit({
            "strategy": self.planned_run_strategy_combo.currentData(),
            "safety_round_limit": self.planned_run_safety_limit_spin.value(),
        })

    def begin_planned_run(self) -> None:
        self._mode = "planned_run"
        self._task_running = True
        self._planned_run_snapshot = {}
        self.summary_label.clear()
        self.plan_details.hide()
        self._set_result_status("正在检查关卡状态并准备自动规划……", "running")
        self._set_run_status("关卡内运行中", "running")
        self._sync_controls()

    def apply_planned_run_progress(self, event: Mapping[str, Any]) -> None:
        if self._mode != "planned_run" or not self._task_running:
            return
        payload = event.get("payload", event)
        if not isinstance(payload, Mapping):
            return
        self._planned_run_snapshot.update(payload)
        self._render_planned_run_snapshot()
        phase = self._planned_run_phase_label(payload.get('phase'))
        scan = payload.get('scan_progress')
        if payload.get('phase') == 'scan_turn' and isinstance(scan, Mapping):
            view = scan.get('view_index')
            if type(view) is int and 1 <= view <= 4:
                phase += f" · 视角 {view}/4"
        self._set_result_status(f"关卡内运行中 · {phase}", "running")

    @staticmethod
    def _planned_run_phase_label(phase: Any) -> str:
        value = str(phase or "准备")
        return {
            "initializing": "准备运行", "check": "检查关卡状态",
            "observe": "观察棋盘", "scan": "扫描魔方布局",
            "scan_turn": "扫描当前回合布局", "accept_scan": "核对扫描结果",
            "reset_wide": "重置到宽视图", "wide_reference": "确认三面与玩家位置",
            "read_registration_anchors": "补读定位图案", "supplement_cells": "补读行动所需格",
            "planning": "计算移动方案", "plan": "计算移动方案",
            "operation": "执行玩家行动", "operation_outcome": "确认行动结果",
            "verify_move": "核验玩家位置", "event_dispatch": "处理节点事件",
            "enemy_wait": "等待奇点行动", "plane_transition": "等待位面转换",
            "rest_area": "处理休整区", "next_plane_wait": "等待下一位面棋盘",
            "stopped": "已停止运行",
            "execute": "执行玩家行动", "move": "移动玩家", "rotate": "旋转魔方层",
            "event": "处理节点事件", "battle": "处理战斗",
            "wait_enemy": "等待奇点行动", "enemy": "等待奇点行动",
            "next_plane": "进入下一位面", "transition": "等待位面转换",
            "settlement": "结算", "terminal": "结算", "blocked": "等待人工处理",
        }.get(value, value)

    def _render_planned_run_snapshot(self) -> None:
        payload = self._planned_run_snapshot
        strategy = {"chase": "只追奇点", "inspiration": "灵感收益优先"}.get(
            str(payload.get("strategy") or ""), ""
        )
        summary = [f"运行策略：{strategy}"] if strategy else []
        for key, label in (("plane_index", "当前位面"), ("turns_completed", "已完成回合"),
                           ("rounds_remaining", "剩余回合")):
            if payload.get(key) is not None:
                summary.append(f"{label}：{payload[key]}")
        if payload.get("phase"):
            summary.append(f"阶段：{self._planned_run_phase_label(payload['phase'])}")
        scan = payload.get('scan_progress')
        if payload.get('phase') == 'scan_turn' and isinstance(scan, Mapping):
            view = scan.get('view_index')
            if type(view) is int and 1 <= view <= 4:
                summary.append(f"当前采集：视角 {view}/4")
        for key, label in (("report_path", "运行报告"), ("json_path", "运行 JSON"),
                           ("final_frame", "停止截图"), ("output_dir", "输出目录")):
            if payload.get(key):
                summary.append(f"{label}：{payload[key]}")
        self.summary_label.setText("\n".join(summary))

    def apply_planned_run_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        self._planned_run_snapshot.update(payload)
        self._render_planned_run_snapshot()
        status = str(payload.get("status") or "blocked")
        completed = (payload.get("success") is True and status == "completed"
                     and payload.get("terminal_reached") is True)
        if completed:
            outcome = {"victory": "胜利", "failure": "失败"}.get(str(payload.get("outcome") or ""))
            message = "已到达结算" + (f"（{outcome}）" if outcome else "")
        else:
            message = "关卡内自动运行已取消" if status == "cancelled" else "关卡内自动运行已停止"
            if payload.get("reason"):
                message += f"：{payload['reason']}"
        self._set_result_status(message, "success" if completed else "error")
        self._set_run_status("已到达结算" if completed else "运行已停止", "success" if completed else "error")
        self._sync_controls()

    def show_planned_run_error(self, message: str) -> None:
        self.apply_planned_run_result({"success": False, "status": "blocked", "reason": message})

    def _select_plan_layout(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择魔方扫描布局", self.plan_layout_edit.text(), "布局 JSON (*.json)"
        )
        if selected:
            self.plan_layout_edit.setText(selected)

    def _request_plan(self) -> None:
        if self._runner_busy or self._task_running:
            return
        self.runPlanRequested.emit({
            "strategy": self.plan_strategy_combo.currentData(),
            "turn_budget": self.plan_turn_budget_spin.value(),
            "layout_path": self.plan_layout_edit.text().strip(),
            "time_budget_sec": 30,
        })

    def begin_plan(self) -> None:
        self._mode = "plan"
        self._task_running = True
        self._plan_report_path = ""
        self.summary_label.clear()
        self.plan_details.clear()
        self.plan_details.hide()
        self._set_result_status("正在读取扫描布局并计算移动方案……", "running")
        self._set_run_status("规划中", "running")
        self._sync_controls()

    def apply_plan_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "blocked")
        solved = status == "solved" and payload.get("success") is True
        self._plan_report_path = str(payload.get("report_path") or "")
        metrics = payload.get("metrics")
        summary = []
        if isinstance(metrics, Mapping):
            for key, label, percent in (
                ("deadline_encounter_probability", "限时到达率", True),
                ("expected_new_inspirations", "平均获取灵感", False),
                ("expected_successful_new_inspirations", "按期到达灵感收益", False),
                ("expected_boss_consumed_inspirations", "平均被奇点摧毁灵感", False),
                ("expected_encounter_turns_given_deadline", "按期到达平均回合", False),
                ("optimal_expected_turns", "最短平均回合", False),
            ):
                value = metrics.get(key)
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    if percent:
                        display = ">99.9999%" if 1 - 5e-7 < value < 1 else f"{value * 100:.4f}%"
                    else:
                        display = f"{value:.3f}"
                    summary.append(f"{label}：{display}")
        for key, label in (("report_path", "方案"), ("json_path", "方案 JSON"),
                           ("output_dir", "输出目录")):
            if payload.get(key):
                summary.append(f"{label}：{payload[key]}")
        self.summary_label.setText("\n".join(summary))
        details = {
            "下一步": payload.get("next_action"),
            "玩家回合动作": payload.get("player_turn_actions"),
            "评估结果": metrics,
        }
        if any(value is not None for value in details.values()):
            self.plan_details.setPlainText(json.dumps(details, ensure_ascii=False, indent=2))
            self.plan_details.show()
        message = "移动方案已生成，请打开方案查看动作和随机分支" if solved else {
            "blocked": "未能生成移动方案",
            "cancelled": "移动规划已取消",
            "search_budget_exhausted": "计算预算已耗尽，当前结果尚未完成求解",
        }.get(status, "移动规划未完成")
        if payload.get("reason"):
            message += f"：{payload['reason']}"
        self._set_result_status(message, "success" if solved else "error")
        self._set_run_status("规划完成" if solved else "规划未完成", "success" if solved else "error")
        self._sync_controls()

    def show_plan_error(self, message: str) -> None:
        self.apply_plan_result({"status": "blocked", "reason": message})

    def _open_plan_report(self) -> None:
        if self._plan_report_path:
            report = Path(self._plan_report_path).resolve()
            if report.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(report)))
            else:
                self._set_result_status("移动方案文件不存在，请查看输出目录。", "error")

    def apply_scan_result(self, payload: Mapping[str, Any]) -> None:
        self._task_running = False
        status = str(payload.get("status") or "blocked")
        complete = (payload.get("success") is True
                    and status == "completed" and payload.get("layout_complete") is True)
        targets = (payload.get("success") is True and status == "targets_ready"
                   and payload.get("targets_ready") is True
                   and payload.get("recognition_goal") == "targets")
        self._scan_report_path = str(payload.get("report_path") or "")
        faces = payload.get("faces_observed", 0)
        if isinstance(faces, (list, tuple, dict)):
            faces = len(faces)
        summary = [f"已识别 {payload.get('known_cells', 0)} / 54 格 · 已观察 {faces} / 6 面"]
        if targets:
            summary.append("目标位置已确认；节点图案未作为完整布局验收。")
        for key, label in (("report_path", "报告"), ("json_path", "布局 JSON"),
                           ("output_dir", "输出目录"), ("last_frame", "最后截图")):
            if payload.get(key):
                summary.append(f"{label}：{payload[key]}")
        if payload.get("report_error"):
            summary.append(f"报告导出异常：{payload['report_error']}")
        self.summary_label.setText("\n".join(summary))
        label = ("布局扫描完成，请人工对照输出" if complete else
                 "目标识别完成，请人工对照输出" if targets else {
            "partial": "扫描结束，布局仍有未知或冲突",
            "cancelled": "扫描已取消",
            "blocked": "扫描未能完成",
        }.get(status, "扫描未能确认完整布局"))
        if payload.get("reason"):
            label += f"：{payload['reason']}"
        success = complete or targets
        self._set_result_status(label, "success" if success else "error")
        self._set_run_status("扫描完成" if complete else "目标识别完成" if targets else "扫描未完成",
                             "success" if success else "error")
        self._sync_controls()

    def show_scan_error(self, message: str) -> None:
        self.apply_scan_result({"status": "blocked", "reason": message})

    def _open_scan_report(self) -> None:
        if self._scan_report_path:
            report = Path(self._scan_report_path).resolve()
            if report.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(report)))
            else:
                self._set_result_status("扫描报告文件不存在，请查看输出目录。", "error")

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
        self.scan_button.setEnabled(not self._runner_busy and not self._task_running)
        idle = not self._runner_busy and not self._task_running
        self.plan_button.setEnabled(idle)
        self.planned_run_button.setEnabled(idle)
        self.planned_run_strategy_combo.setEnabled(idle)
        self.planned_run_safety_limit_spin.setEnabled(idle)
        self.plan_strategy_combo.setEnabled(idle)
        self.plan_turn_budget_spin.setEnabled(idle)
        self.plan_layout_button.setEnabled(idle)
        self.plan_latest_button.setEnabled(idle)
        self.plan_layout_edit.setEnabled(idle)
        self.plan_report_button.setEnabled(bool(self._plan_report_path) and not self._task_running)
        self.scan_report_button.setEnabled(bool(self._scan_report_path) and not self._task_running)
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

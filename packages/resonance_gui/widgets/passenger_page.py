"""Dedicated Resonance PC passenger round-trip workspace."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import ResonanceConfigRepository
from ..logic import (
    PassengerProgressState,
    extract_final_result,
    extract_run_id,
    extract_status,
    pretty_json,
    reduce_passenger_progress,
    render_result_text,
)


class PassengerPage(QWidget):
    startRequested = Signal(object, float)
    cancelRequested = Signal()
    refreshTargetRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._busy = False
        self._target_ready = False
        self._current_cid = ""
        self._elapsed_seconds = 0
        self._progress = PassengerProgressState()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._build_ui()
        self.set_inputs(self._settings.load_passenger_inputs())
        self.set_busy(False)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_status_band())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_parameter_panel())
        body.addWidget(self._build_progress_panel(), 1)
        root.addLayout(body, 1)
        root.addWidget(self._build_action_bar())

    def _build_status_band(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("statusBand")
        layout = QHBoxLayout(band)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(28)
        self.target_value = self._status_pair(layout, "目标窗口", "检查中")
        self.run_status_value = self._status_pair(layout, "任务状态", "待命")
        self.stage_value = self._status_pair(layout, "当前阶段", "待开始")
        self.cid_value = self._status_pair(layout, "CID", "--")
        self.elapsed_value = self._status_pair(layout, "运行时长", "00:00")
        layout.addStretch(1)
        refresh = QPushButton("刷新目标", band)
        refresh.clicked.connect(self.refreshTargetRequested.emit)
        layout.addWidget(refresh)
        return band

    @staticmethod
    def _status_pair(layout: QHBoxLayout, caption: str, value: str) -> QLabel:
        box = QVBoxLayout()
        box.setSpacing(1)
        caption_label = QLabel(caption)
        caption_label.setProperty("caption", True)
        value_label = QLabel(value)
        value_label.setProperty("value", True)
        value_label.setMinimumWidth(78)
        box.addWidget(caption_label)
        box.addWidget(value_label)
        layout.addLayout(box)
        return value_label

    def _build_parameter_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("parameterPanel")
        panel.setMinimumWidth(310)
        panel.setMaximumWidth(370)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel("海角城 ↔ 岚心城", panel)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("独立客运 · 传单揽客 · 三客车厢", panel)
        subtitle.setProperty("caption", True)
        layout.addWidget(subtitle)
        layout.addSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        self.round_trips = QSpinBox(panel)
        self.round_trips.setRange(1, 99)
        self.round_trips.valueChanged.connect(self._refresh_expected_fatigue)
        form.addRow("往返次数", self.round_trips)

        self.reposition_to_route = QCheckBox("不在线路端点时自动前往海角城", panel)
        form.addRow("自动归位", self.reposition_to_route)
        self.use_medicine = QCheckBox("允许使用疲劳药", panel)
        self.use_medicine.toggled.connect(self._sync_medicine_controls)
        form.addRow("疲劳恢复", self.use_medicine)
        self.allowed_medicines = QLineEdit(panel)
        self.allowed_medicines.setPlaceholderText("提神棒棒糖, 提神口香糖")
        form.addRow("药品白名单", self.allowed_medicines)
        self.medicine_max_uses = QSpinBox(panel)
        self.medicine_max_uses.setRange(0, 99)
        form.addRow("最大用药次数", self.medicine_max_uses)
        self.arrival_timeout = QSpinBox(panel)
        self.arrival_timeout.setRange(60, 3600)
        self.arrival_timeout.setSuffix(" 秒")
        form.addRow("单程到站超时", self.arrival_timeout)
        layout.addLayout(form)

        layout.addSpacing(18)
        self.expected_fatigue = QLabel(panel)
        self.expected_fatigue.setWordWrap(True)
        self.expected_fatigue.setObjectName("summaryCard")
        layout.addWidget(self.expected_fatigue)
        warning = QLabel(
            "启动时若检测到车上已有乘客会停止；揽客后若疲劳不足，需要人工完成该单程。",
            panel,
        )
        warning.setWordWrap(True)
        warning.setProperty("caption", True)
        layout.addWidget(warning)
        layout.addStretch(1)
        return panel

    def _build_progress_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("contentPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 16, 22, 16)
        title = QLabel("客运执行进度", panel)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.stage_detail = QLabel("等待开始", panel)
        self.stage_detail.setWordWrap(True)
        layout.addWidget(self.stage_detail)

        summary = QFrame(panel)
        summary.setObjectName("summaryCard")
        form = QFormLayout(summary)
        self.route_value = QLabel("--", summary)
        self.leg_value = QLabel("0 / 0", summary)
        self.passenger_value = QLabel("--", summary)
        self.fatigue_value = QLabel("0 / 0", summary)
        self.revenue_value = QLabel("0", summary)
        self.manual_value = QLabel("否", summary)
        form.addRow("当前方向", self.route_value)
        form.addRow("完成单程", self.leg_value)
        form.addRow("本程乘客", self.passenger_value)
        form.addRow("预计疲劳", self.fatigue_value)
        form.addRow("累计收益", self.revenue_value)
        form.addRow("需要人工处理", self.manual_value)
        layout.addWidget(summary)

        self.result_view = QTextBrowser(panel)
        self.result_view.setPlaceholderText("任务结果和错误详情将在这里显示。")
        layout.addWidget(self.result_view, 1)
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("actionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.addStretch(1)
        self.cancel_button = QPushButton("停止任务", bar)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        layout.addWidget(self.cancel_button)
        self.start_button = QPushButton("开始客运", bar)
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self._request_start)
        layout.addWidget(self.start_button)
        return bar

    def set_inputs(self, values: Mapping[str, Any]) -> None:
        self.round_trips.setValue(int(values.get("round_trips", 1)))
        self.reposition_to_route.setChecked(bool(values.get("reposition_to_route", True)))
        self.use_medicine.setChecked(bool(values.get("use_fatigue_medicine", False)))
        self.allowed_medicines.setText(", ".join(str(v) for v in values.get("allowed_fatigue_medicines", [])))
        self.medicine_max_uses.setValue(int(values.get("fatigue_medicine_max_uses", 4)))
        self.arrival_timeout.setValue(int(values.get("arrival_timeout_seconds", 1800)))
        self._sync_medicine_controls()
        self._refresh_expected_fatigue()

    def collect_inputs(self) -> dict[str, Any]:
        medicines = [
            value.strip()
            for value in self.allowed_medicines.text().replace("，", ",").split(",")
            if value.strip()
        ]
        inputs = {
            "round_trips": self.round_trips.value(),
            "reposition_to_route": self.reposition_to_route.isChecked(),
            "preferred_start_city_id": "11",
            "use_fatigue_medicine": self.use_medicine.isChecked(),
            "allowed_fatigue_medicines": medicines,
            "fatigue_medicine_max_uses": self.medicine_max_uses.value(),
            "arrival_timeout_seconds": self.arrival_timeout.value(),
        }
        self._settings.save_passenger_inputs(inputs)
        return inputs

    def _request_start(self) -> None:
        self.startRequested.emit(self.collect_inputs(), 0.0)

    def _sync_medicine_controls(self) -> None:
        enabled = self.use_medicine.isChecked() and not self._busy
        self.allowed_medicines.setEnabled(enabled)
        self.medicine_max_uses.setEnabled(enabled)

    def _refresh_expected_fatigue(self) -> None:
        value = self.round_trips.value() * 152
        self.expected_fatigue.setText(
            f"固定线路预计疲劳：{value}\n"
            "单程 76；若从其他城市启动，另加前往海角城的空驶疲劳。"
        )

    def set_target_status(self, payload: Mapping[str, Any]) -> None:
        target = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
        self._target_ready = bool(payload.get("ok")) and bool(target.get("visible", True))
        self.target_value.setText(str(target.get("title") or "已连接") if self._target_ready else "未连接")
        self._sync_actions()

    def begin_run(self, payload: Mapping[str, Any]) -> None:
        self._current_cid = extract_run_id(payload) or str(payload.get("cid") or "")
        self._progress = PassengerProgressState(cid=self._current_cid)
        self.cid_value.setText(self._current_cid or "--")
        self.run_status_value.setText("运行中")
        self.stage_value.setText("准备启动")
        self.stage_detail.setText("正在检查城市主界面和已有乘客")
        self.result_view.clear()
        self._elapsed_seconds = 0
        self.elapsed_value.setText("00:00")
        self._elapsed_timer.start()
        self.set_busy(True)

    def apply_progress(self, event: Mapping[str, Any]) -> None:
        self._progress = reduce_passenger_progress(
            self._progress,
            event,
            expected_cid=self._current_cid,
        )
        state = self._progress
        self.stage_value.setText(state.stage_label)
        state_label = {"started": "开始", "completed": "完成", "blocked": "已阻塞"}.get(state.state, state.state)
        self.stage_detail.setText(f"{state.stage_label} · {state_label}")
        self.route_value.setText(
            f"{state.source_city} → {state.destination_city}"
            if state.source_city or state.destination_city
            else "--"
        )
        self.leg_value.setText(f"{state.leg_index or 0} / {state.leg_count}")
        self.passenger_value.setText(
            f"{state.recruited_count} / {state.seat_capacity}"
            if state.recruited_count is not None and state.seat_capacity is not None
            else "--"
        )
        self.fatigue_value.setText(f"{state.expected_fatigue_used} / {state.expected_fatigue_total}")
        self.revenue_value.setText(f"{state.total_revenue:,}")
        self.manual_value.setText("是" if state.requires_manual_completion else "否")

    def update_run(self, payload: Mapping[str, Any]) -> None:
        status = extract_status(payload)
        if status:
            self.run_status_value.setText({"running": "运行中", "queued": "排队中"}.get(status, status))

    def finish_run(self, payload: Mapping[str, Any]) -> None:
        result = extract_final_result(payload)
        success = bool(result.get("success"))
        self.run_status_value.setText("已完成" if success else "已阻塞")
        self.stage_value.setText("任务结束")
        self.stage_detail.setText(str(result.get("reason") or "客运往返已完成"))
        self.leg_value.setText(f"{len(result.get('completed_legs') or [])} / {int(result.get('requested_round_trips') or 0) * 2}")
        self.fatigue_value.setText(str(result.get("expected_fatigue_used") or 0))
        self.revenue_value.setText(f"{int(result.get('total_revenue') or 0):,}")
        self.manual_value.setText("是" if result.get("requires_manual_completion") else "否")
        self.result_view.setPlainText(render_result_text(payload))
        self._finish_common()

    def show_failure(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("异常")
        self.stage_detail.setText(str(payload.get("error") or "客运任务执行异常"))
        self.result_view.setPlainText(pretty_json(payload))
        if not bool(payload.get("recoverable")):
            self._finish_common()

    def show_history_result(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("历史记录")
        self.stage_detail.setText("只读历史客运结果")
        self.cid_value.setText(extract_run_id(payload) or "--")
        self.result_view.setPlainText(render_result_text(payload))

    def cancel_requested(self, payload: Mapping[str, Any]) -> None:
        del payload
        self.run_status_value.setText("正在停止")
        self.stage_detail.setText("取消请求已发送；若已经揽客，需要人工完成当前单程")

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (
            self.round_trips,
            self.reposition_to_route,
            self.use_medicine,
            self.arrival_timeout,
        ):
            widget.setEnabled(not self._busy)
        self._sync_medicine_controls()
        self._sync_actions()

    def is_busy(self) -> bool:
        return self._busy

    def _sync_actions(self) -> None:
        self.start_button.setEnabled(not self._busy and self._target_ready)
        self.cancel_button.setEnabled(self._busy)

    def _finish_common(self) -> None:
        self._elapsed_timer.stop()
        self.set_busy(False)

    def _tick_elapsed(self) -> None:
        self._elapsed_seconds += 1
        minutes, seconds = divmod(self._elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        self.elapsed_value.setText(
            f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes:02d}:{seconds:02d}"
        )

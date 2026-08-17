"""Dedicated Resonance PC passenger trip workspace."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import ResonanceConfigRepository
from ..passenger_catalog import PassengerRouteEstimate, load_passenger_route_catalog
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
        self._route_catalog = load_passenger_route_catalog()
        self._route_estimate: PassengerRouteEstimate | None = None
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
        self.parameter_panel = self._build_parameter_panel()
        body.addWidget(self.parameter_panel)
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
        self.elapsed_value = self._status_pair(layout, "运行时长", "00:00")
        self.cid_value = QLabel("--", band)
        self.cid_value.hide()
        layout.addStretch(1)
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
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 18)
        title = QLabel("客运任务", panel)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.route_title = QLabel(panel)
        self.route_title.setObjectName("passengerRouteTitle")
        layout.addWidget(self.route_title)
        route_note = QLabel("自选线路 · 传单揽客 · 可选倒货", panel)
        route_note.setObjectName("passengerRouteNote")
        route_note.setProperty("caption", True)
        layout.addWidget(route_note)
        layout.addSpacing(22)

        input_title = QLabel("运行设置", panel)
        input_title.setObjectName("sectionTitle")
        layout.addWidget(input_title)
        input_note = QLabel("设置单程次数，并按需启用倒货或自动归位。", panel)
        input_note.setProperty("caption", True)
        layout.addWidget(input_note)
        layout.addSpacing(10)

        form = QFormLayout()
        form.setSpacing(12)
        self.city_a = QComboBox(panel)
        self.city_b = QComboBox(panel)
        for city in self._route_catalog.cities:
            self.city_a.addItem(city.name, city.city_id)
            self.city_b.addItem(city.name, city.city_id)
        form.addRow("线路城市 A", self.city_a)
        form.addRow("线路城市 B", self.city_b)

        self.trip_count = QSpinBox(panel)
        self.trip_count.setRange(1, 198)
        self.trip_count.setSuffix(" 次")
        self.trip_count.valueChanged.connect(self._refresh_expected_fatigue)
        form.addRow("客运次数", self.trip_count)

        self.trade_during_trip = QCheckBox("启用", panel)
        self.trade_during_trip.setChecked(False)
        self.trade_during_trip.setToolTip("每程揽客前强制刷新行情，先卖后买；末站只清仓")
        form.addRow("中途买卖货", self.trade_during_trip)

        self.auto_reposition = QCheckBox("启用", panel)
        self.auto_reposition.setChecked(True)
        self.auto_reposition.setToolTip("当前不在线路端点时，前往疲劳消耗较低的端点")
        form.addRow("自动前往线路", self.auto_reposition)
        layout.addLayout(form)
        self.city_a.currentIndexChanged.connect(
            lambda _index: self._route_changed(self.city_a, self.city_b)
        )
        self.city_b.currentIndexChanged.connect(
            lambda _index: self._route_changed(self.city_b, self.city_a)
        )

        layout.addSpacing(22)
        self.expected_fatigue = QLabel(panel)
        self.expected_fatigue.setWordWrap(True)
        self.expected_fatigue.setObjectName("passengerEstimate")
        layout.addWidget(self.expected_fatigue)
        self.policy_label = QLabel(panel)
        self.policy_label.setWordWrap(True)
        self.policy_label.setObjectName("passengerPolicy")
        layout.addWidget(self.policy_label)
        layout.addStretch(1)
        return panel

    def _build_progress_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("contentPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(12)
        title = QLabel("本次客运", panel)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("按单程追踪揽客、行驶和结算状态。", panel)
        subtitle.setProperty("caption", True)
        layout.addWidget(subtitle)

        route_band = QFrame(panel)
        route_band.setObjectName("passengerRouteBand")
        route_layout = QVBoxLayout(route_band)
        route_layout.setContentsMargins(20, 16, 20, 16)
        route_layout.setSpacing(8)
        self.timeline_value = QLabel(route_band)
        self.timeline_value.setObjectName("passengerTimeline")
        self.timeline_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        route_layout.addWidget(self.timeline_value)
        self.stage_detail = QLabel("等待开始", route_band)
        self.stage_detail.setWordWrap(True)
        self.stage_detail.setObjectName("passengerStageDetail")
        self.stage_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        route_layout.addWidget(self.stage_detail)
        layout.addWidget(route_band)

        summary = QFrame(panel)
        summary.setObjectName("passengerMetrics")
        form = QFormLayout(summary)
        form.setContentsMargins(18, 14, 18, 14)
        form.setHorizontalSpacing(28)
        form.setVerticalSpacing(10)
        self.route_value = QLabel("--", summary)
        self.leg_value = QLabel("0 / 0", summary)
        self.passenger_value = QLabel("--", summary)
        self.fatigue_value = QLabel("0 / 0", summary)
        self.manual_value = QLabel("否", summary)
        for value in (
            self.route_value,
            self.leg_value,
            self.passenger_value,
            self.fatigue_value,
            self.manual_value,
        ):
            value.setProperty("metricValue", True)
        form.addRow("当前方向", self.route_value)
        form.addRow("完成单程", self.leg_value)
        form.addRow("本程乘客", self.passenger_value)
        form.addRow("预计疲劳", self.fatigue_value)
        form.addRow("需要人工处理", self.manual_value)
        layout.addWidget(summary)

        detail_title = QLabel("运行详情", panel)
        detail_title.setObjectName("sectionTitle")
        layout.addWidget(detail_title)
        self.result_view = QTextBrowser(panel)
        self.result_view.setObjectName("passengerDetails")
        self.result_view.setPlaceholderText("完成记录和异常详情会显示在这里。")
        layout.addWidget(self.result_view, 1)
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("actionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.addStretch(1)
        self.cancel_button = QPushButton("停止任务", bar)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        layout.addWidget(self.cancel_button)
        self.start_button = QPushButton("开始客运", bar)
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._request_start)
        layout.addWidget(self.start_button)
        return bar

    def set_inputs(self, values: Mapping[str, Any]) -> None:
        self._set_combo_data(self.city_a, str(values.get("passenger_city_a_id") or "11"))
        self._set_combo_data(self.city_b, str(values.get("passenger_city_b_id") or "15"))
        self.trip_count.setValue(int(values.get("trip_count", 1)))
        self.trade_during_trip.setChecked(bool(values.get("trade_during_trip", False)))
        self.auto_reposition.setChecked(bool(values.get("reposition_to_route", True)))
        self._refresh_expected_fatigue()

    def collect_inputs(self) -> dict[str, Any]:
        estimate = self._current_route_estimate()
        inputs = {
            "passenger_city_a_id": estimate.city_a.city_id,
            "passenger_city_b_id": estimate.city_b.city_id,
            "trip_count": self.trip_count.value(),
            "trade_during_trip": self.trade_during_trip.isChecked(),
            "reposition_to_route": self.auto_reposition.isChecked(),
        }
        self._settings.save_passenger_inputs(inputs)
        return inputs

    def _request_start(self) -> None:
        self.startRequested.emit(self.collect_inputs(), 0.0)

    def _refresh_expected_fatigue(self) -> None:
        try:
            estimate = self._current_route_estimate()
        except ValueError as exc:
            self._route_estimate = None
            self.expected_fatigue.setText(str(exc))
            return
        self._route_estimate = estimate
        trips = self.trip_count.value()
        total = estimate.trip_fatigue * trips
        self.route_title.setText(f"{estimate.city_a.name}  ↔  {estimate.city_b.name}")
        self.timeline_value.setText(self._route_timeline())
        self.policy_label.setText(
            "倒货只购买强制刷新行情中税后预计盈利的商品，不使用砍价、抬价或进货书。"
            f"关闭自动前往起点时，若当前不在{estimate.city_a.name}或{estimate.city_b.name}，任务会直接停止。"
        )
        self.expected_fatigue.setText(
            f"预计疲劳  {total}\n"
            f"{trips} 次 × 单次疲劳 {estimate.trip_fatigue}"
        )

    def _route_changed(self, changed: QComboBox, other: QComboBox) -> None:
        if changed.currentData() == other.currentData():
            for index in range(other.count()):
                if other.itemData(index) != changed.currentData():
                    other.setCurrentIndex(index)
                    break
        self._refresh_expected_fatigue()

    def _current_route_estimate(self) -> PassengerRouteEstimate:
        return self._route_catalog.estimate(
            str(self.city_a.currentData() or ""),
            str(self.city_b.currentData() or ""),
        )

    def _route_timeline(self, *, completed: bool = False) -> str:
        estimate = self._route_estimate or self._current_route_estimate()
        trips = self.trip_count.value()
        suffix = "已完成" if completed else f"共 {trips} 次"
        return f"{estimate.city_a.name}   ●━━━━━━━━●   {estimate.city_b.name} · {suffix}"

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(str(value))
        combo.setCurrentIndex(max(index, 0))

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
        self.stage_detail.setText("正在识别当前城市")
        self.timeline_value.setText(self._route_timeline())
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
        if state.source_city or state.destination_city:
            self.timeline_value.setText(
                f"{state.source_city or '起点'}   ●━━━━━━━━▶   {state.destination_city or '目的地'}"
            )
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
        self.stage_detail.setText(str(result.get("reason") or "客运任务已完成"))
        if success:
            route = result.get("passenger_route") if isinstance(result.get("passenger_route"), Mapping) else {}
            city_a = route.get("city_a") if isinstance(route.get("city_a"), Mapping) else {}
            city_b = route.get("city_b") if isinstance(route.get("city_b"), Mapping) else {}
            city_a_id = str(city_a.get("city_id") or "")
            city_b_id = str(city_b.get("city_id") or "")
            if city_a_id and city_b_id:
                self._set_combo_data(self.city_a, city_a_id)
                self._set_combo_data(self.city_b, city_b_id)
                self._refresh_expected_fatigue()
            self.timeline_value.setText(self._route_timeline(completed=True))
        self.leg_value.setText(
            f"{len(result.get('completed_legs') or [])} / {int(result.get('requested_trips') or 0)}"
        )
        self.fatigue_value.setText(str(result.get("expected_fatigue_used") or 0))
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
        self.city_a.setEnabled(not self._busy)
        self.city_b.setEnabled(not self._busy)
        self.trip_count.setEnabled(not self._busy)
        self.trade_during_trip.setEnabled(not self._busy)
        self.auto_reposition.setEnabled(not self._busy)
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

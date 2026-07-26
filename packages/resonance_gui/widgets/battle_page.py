"""Dedicated Resonance PC battle task workspace."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..battle_catalog import (
    DIFFICULTY_LABELS,
    MAIN_CATEGORY_LABELS,
    SUBCATEGORY_LABELS,
    BattleRoute,
    battle_job_summary,
    load_battle_routes,
)
from ..config_repository import ResonanceConfigRepository
from ..logic import extract_run_id, extract_status, pretty_json, render_result_text


class BattlePage(QWidget):
    startRequested = Signal(object, float)
    validateRequested = Signal(object, float)
    cancelRequested = Signal()
    refreshTargetRequested = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._routes = load_battle_routes()
        self._jobs: list[dict[str, Any]] = []
        self._job_statuses: list[str] = []
        self._busy = False
        self._target_ready = False
        self._current_cid = ""
        self._active_mode = ""
        self._elapsed_seconds = 0
        self._loading = False
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._build_ui()
        self.set_inputs(self._settings.load_battle_inputs())
        self.set_busy(False)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_status_band())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_builder_panel())
        body.addWidget(self._build_job_panel(), 1)
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
        self.job_count_value = self._status_pair(layout, "任务数量", "0")
        self.cid_value = self._status_pair(layout, "CID", "--")
        self.elapsed_value = self._status_pair(layout, "运行时长", "00:00")
        layout.addStretch(1)
        refresh = QPushButton("刷新目标", band)
        refresh.setToolTip("重新检查雷索纳斯 PC 窗口")
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

    def _build_builder_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("parameterPanel")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(360)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        title = QLabel("新增作战任务", panel)
        title.setObjectName("pageTitle")
        outer.addWidget(title)

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 8, 4, 8)
        stack.setSpacing(10)

        category_label = QLabel("作战大类", content)
        category_label.setProperty("caption", True)
        stack.addWidget(category_label)
        category_row = QHBoxLayout()
        category_row.setSpacing(0)
        self.category_group = QButtonGroup(self)
        self.category_group.setExclusive(True)
        self.category_buttons: dict[str, QPushButton] = {}
        for index, category in enumerate(("ct", "gp")):
            button = QPushButton(MAIN_CATEGORY_LABELS[category], content)
            button.setCheckable(True)
            button.setProperty("segment", True)
            self.category_group.addButton(button, index)
            self.category_buttons[category] = button
            category_row.addWidget(button)
        self.category_group.idClicked.connect(self._sync_subcategories)
        stack.addLayout(category_row)

        self.job_form = QFormLayout()
        self.job_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.subcategory_combo = QComboBox(content)
        self.route_combo = QComboBox(content)
        self.subcategory_combo.currentIndexChanged.connect(self._sync_routes)
        self.route_combo.currentIndexChanged.connect(self._sync_dynamic_fields)
        self.job_form.addRow("作战分类", self.subcategory_combo)
        self.job_form.addRow("城市 / 关卡", self.route_combo)

        self.stage_spin = QSpinBox(content)
        self.stage_spin.setRange(1, 3)
        self.difficulty_combo = QComboBox(content)
        for value, label in DIFFICULTY_LABELS.items():
            self.difficulty_combo.addItem(label, value)
        self.threat_spin = QSpinBox(content)
        self.threat_spin.setRange(1, 999)
        self.threat_spin.setValue(1)
        self.formation_combo = QComboBox(content)
        self.formation_combo.addItem("保持当前队伍", None)
        for index in range(1, 5):
            self.formation_combo.addItem(f"队伍 {index}", index)
        self.capture_spin = QSpinBox(content)
        self.capture_spin.setRange(0, 99)
        self.capture_spin.setSpecialValueText("默认")
        self.job_form.addRow("关卡", self.stage_spin)
        self.job_form.addRow("难度", self.difficulty_combo)
        self.job_form.addRow("威胁等级", self.threat_spin)
        self.job_form.addRow("战斗队伍", self.formation_combo)
        self.job_form.addRow("抓捕次数", self.capture_spin)
        stack.addLayout(self.job_form)

        availability = QLabel("每日开放情况与剩余次数以游戏内实际状态为准。", content)
        availability.setWordWrap(True)
        availability.setProperty("caption", True)
        stack.addWidget(availability)
        stack.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        self.add_button = QPushButton("加入任务单", panel)
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self._add_job)
        outer.addWidget(self.add_button)

        self.category_buttons["ct"].setChecked(True)
        self._sync_subcategories()
        return panel

    def _build_job_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("作战任务单", panel)
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.stop_on_failure = QCheckBox("普通失败时停止后续任务", panel)
        self.stop_on_failure.toggled.connect(self._save_inputs)
        title_row.addWidget(self.stop_on_failure)
        layout.addLayout(title_row)

        self.job_table = QTableWidget(0, 5, panel)
        self.job_table.setHorizontalHeaderLabels(["#", "类别 / 任务", "参数", "队伍", "状态"])
        self.job_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.job_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.job_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.job_table.setAlternatingRowColors(True)
        self.job_table.verticalHeader().setVisible(False)
        header = self.job_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.itemSelectionChanged.connect(self._sync_actions)
        layout.addWidget(self.job_table, 3)

        row_actions = QHBoxLayout()
        self.move_up_button = QPushButton("上移", panel)
        self.move_down_button = QPushButton("下移", panel)
        self.duplicate_button = QPushButton("复制", panel)
        self.delete_button = QPushButton("删除", panel)
        self.delete_button.setObjectName("dangerButton")
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.duplicate_button.clicked.connect(self._duplicate_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        for button in (
            self.move_up_button,
            self.move_down_button,
            self.duplicate_button,
            self.delete_button,
        ):
            row_actions.addWidget(button)
        row_actions.addStretch(1)
        layout.addLayout(row_actions)

        result_header = QHBoxLayout()
        result_title = QLabel("执行状态", panel)
        result_title.setObjectName("sectionTitle")
        result_header.addWidget(result_title)
        result_header.addStretch(1)
        self.stage_detail = QLabel("添加任务后即可校验或开始", panel)
        self.stage_detail.setProperty("caption", True)
        result_header.addWidget(self.stage_detail)
        layout.addLayout(result_header)

        self.result_view = QTextBrowser(panel)
        self.result_view.setMinimumHeight(120)
        self.result_view.setPlaceholderText("校验结果、执行结果和失败原因会显示在这里。")
        layout.addWidget(self.result_view, 2)
        return panel

    def _build_action_bar(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("resultBand")
        layout = QHBoxLayout(band)
        layout.setContentsMargins(18, 9, 18, 9)
        self.ready_hint = QLabel("正在检查目标窗口", band)
        self.ready_hint.setProperty("caption", True)
        layout.addWidget(self.ready_hint)
        layout.addStretch(1)
        self.cancel_button = QPushButton("停止任务", band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        self.validate_button = QPushButton("校验任务单", band)
        self.validate_button.setToolTip("只校验任务参数，不操作游戏")
        self.validate_button.clicked.connect(self._request_validation)
        self.start_button = QPushButton("开始战斗", band)
        self.start_button.setObjectName("primaryButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_button.clicked.connect(self._request_start)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.validate_button)
        layout.addWidget(self.start_button)
        return band

    def _selected_main_category(self) -> str:
        return "gp" if self.category_buttons["gp"].isChecked() else "ct"

    def _sync_subcategories(self, *_args: object) -> None:
        category = self._selected_main_category()
        values = (
            ("tie_an", "regional_ops_center")
            if category == "ct"
            else ("action_summary", "structural_exploration")
        )
        previous = self.subcategory_combo.currentData()
        self.subcategory_combo.blockSignals(True)
        self.subcategory_combo.clear()
        for value in values:
            self.subcategory_combo.addItem(SUBCATEGORY_LABELS[value], value)
        selected = self.subcategory_combo.findData(previous)
        self.subcategory_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.subcategory_combo.blockSignals(False)
        self._sync_routes()

    def _sync_routes(self, *_args: object) -> None:
        category = self._selected_main_category()
        subcategory = str(self.subcategory_combo.currentData() or "")
        selected_route = self.route_combo.currentData()
        self.route_combo.blockSignals(True)
        self.route_combo.clear()
        for route in self._routes:
            if route.main_category == category and route.subcategory == subcategory:
                self.route_combo.addItem(route.title, route.route_id)
        index = self.route_combo.findData(selected_route)
        self.route_combo.setCurrentIndex(index if index >= 0 else 0)
        self.route_combo.blockSignals(False)
        self._sync_dynamic_fields()

    def _selected_route(self) -> BattleRoute | None:
        route_id = str(self.route_combo.currentData() or "")
        return next((route for route in self._routes if route.route_id == route_id), None)

    def _sync_dynamic_fields(self, *_args: object) -> None:
        route = self._selected_route()
        self._set_form_field_visible(self.stage_spin, bool(route and route.uses_stage))
        self._set_form_field_visible(self.difficulty_combo, bool(route and route.uses_difficulty))
        self._set_form_field_visible(self.threat_spin, bool(route and route.uses_threat_level))
        uses_combat = bool(route and route.uses_combat_options)
        self._set_form_field_visible(self.formation_combo, uses_combat)
        self._set_form_field_visible(self.capture_spin, uses_combat)
        self.add_button.setEnabled(not self._busy and route is not None and len(self._jobs) < 50)

    def _set_form_field_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self.job_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _job_from_controls(self) -> dict[str, Any]:
        route = self._selected_route()
        if route is None:
            raise ValueError("请选择一个有效的作战关卡。")
        job: dict[str, Any] = {"route_id": route.route_id}
        if route.uses_stage:
            job["stage"] = int(self.stage_spin.value())
        if route.uses_difficulty:
            job["difficulty"] = int(self.difficulty_combo.currentData())
        if route.uses_threat_level:
            job["threat_level"] = int(self.threat_spin.value())
        if route.uses_combat_options:
            formation = self.formation_combo.currentData()
            if formation is not None:
                job["formation_index"] = int(formation)
            if self.capture_spin.value() > 0:
                job["capture_count"] = int(self.capture_spin.value())
        return job

    def _add_job(self) -> None:
        if len(self._jobs) >= 50:
            QMessageBox.warning(self, "任务数量超限", "单次最多可以添加 50 个作战任务。")
            return
        try:
            job = self._job_from_controls()
        except ValueError as exc:
            QMessageBox.warning(self, "任务参数错误", str(exc))
            return
        self._jobs.append(job)
        self._job_statuses.append("等待")
        self._render_jobs(select_row=len(self._jobs) - 1)
        self._save_inputs()

    def _selected_row(self) -> int:
        rows = self.job_table.selectionModel().selectedRows()
        return int(rows[0].row()) if rows else -1

    def _move_selected(self, offset: int) -> None:
        row = self._selected_row()
        target = row + int(offset)
        if row < 0 or target < 0 or target >= len(self._jobs):
            return
        self._jobs[row], self._jobs[target] = self._jobs[target], self._jobs[row]
        self._job_statuses[row], self._job_statuses[target] = (
            self._job_statuses[target],
            self._job_statuses[row],
        )
        self._render_jobs(select_row=target)
        self._save_inputs()

    def _duplicate_selected(self) -> None:
        row = self._selected_row()
        if row < 0 or len(self._jobs) >= 50:
            return
        self._jobs.insert(row + 1, dict(self._jobs[row]))
        self._job_statuses.insert(row + 1, "等待")
        self._render_jobs(select_row=row + 1)
        self._save_inputs()

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        del self._jobs[row]
        del self._job_statuses[row]
        self._render_jobs(select_row=min(row, len(self._jobs) - 1))
        self._save_inputs()

    def _render_jobs(self, *, select_row: int = -1) -> None:
        self.job_table.setRowCount(len(self._jobs))
        for row, job in enumerate(self._jobs):
            title, params, formation = battle_job_summary(job, self._routes)
            values = (str(row + 1), title, params or "无额外参数", formation, self._job_statuses[row])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.job_table.setItem(row, column, item)
            self.job_table.item(row, 1).setToolTip(str(job.get("route_id") or ""))
        if 0 <= select_row < len(self._jobs):
            self.job_table.selectRow(select_row)
        self.job_count_value.setText(str(len(self._jobs)))
        self._sync_actions()

    def set_inputs(self, inputs: Mapping[str, Any]) -> None:
        self._loading = True
        raw_jobs = inputs.get("jobs")
        self._jobs = [
            dict(job)
            for job in (raw_jobs if isinstance(raw_jobs, list) else [])
            if isinstance(job, Mapping)
        ][:50]
        self._job_statuses = ["等待"] * len(self._jobs)
        self.stop_on_failure.setChecked(bool(inputs.get("stop_on_failure", True)))
        self._render_jobs()
        self._loading = False
        self._sync_actions()

    def collect_inputs(self) -> dict[str, Any]:
        if not self._jobs:
            raise ValueError("请至少添加一个作战任务。")
        return {
            "jobs": [dict(job) for job in self._jobs],
            "stop_on_failure": bool(self.stop_on_failure.isChecked()),
        }

    def _save_inputs(self, *_args: object) -> None:
        if self._loading:
            return
        self._settings.save_battle_inputs(
            {
                "jobs": [dict(job) for job in self._jobs],
                "stop_on_failure": bool(self.stop_on_failure.isChecked()),
            }
        )

    def _request_validation(self) -> None:
        try:
            inputs = self.collect_inputs()
        except ValueError as exc:
            QMessageBox.warning(self, "任务单为空", str(exc))
            return
        self.validateRequested.emit(inputs, 0.0)

    def _request_start(self) -> None:
        try:
            inputs = self.collect_inputs()
        except ValueError as exc:
            QMessageBox.warning(self, "任务单为空", str(exc))
            return
        if not self._target_ready:
            QMessageBox.warning(self, "目标窗口不可用", "请先启动并显示雷索纳斯 PC 游戏窗口。")
            return
        self.startRequested.emit(inputs, 0.0)

    def set_target_status(self, payload: Mapping[str, Any]) -> None:
        target = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
        visible = bool(target.get("visible", True)) if target else False
        self._target_ready = bool(payload.get("ok")) and bool(target) and visible
        if self._target_ready:
            self.target_value.setText("已连接")
            self.ready_hint.setText(str(target.get("title") or "雷索纳斯 PC 窗口已连接"))
        else:
            self.target_value.setText("未连接")
            self.ready_hint.setText(str(payload.get("error") or "未找到可用的雷索纳斯 PC 窗口"))
        self._sync_actions()

    def begin_validation(self, payload: Mapping[str, Any]) -> None:
        self._begin(payload, mode="preview")
        self.run_status_value.setText("正在校验")
        self.stage_detail.setText("正在检查任务字段与关卡兼容性")

    def begin_run(self, payload: Mapping[str, Any]) -> None:
        self._begin(payload, mode="run")
        self.run_status_value.setText("运行中")
        self.stage_detail.setText("任务已派发，正在执行作战任务单")
        self._job_statuses = ["等待"] * len(self._jobs)
        if self._job_statuses:
            self._job_statuses[0] = "执行中"
        self._render_jobs(select_row=0)

    def _begin(self, payload: Mapping[str, Any], *, mode: str) -> None:
        self._active_mode = mode
        self._current_cid = extract_run_id(payload) or str(payload.get("cid") or "")
        self.cid_value.setText(self._current_cid or "--")
        self._elapsed_seconds = 0
        self.elapsed_value.setText("00:00")
        self.result_view.clear()
        self.set_busy(True)
        self._elapsed_timer.start()

    def update_run(self, payload: Mapping[str, Any]) -> None:
        status = extract_status(payload)
        if status:
            labels = {
                "queued": "排队中",
                "running": "运行中",
                "success": "成功",
                "failed": "失败",
                "error": "错误",
                "timeout": "超时",
                "cancelled": "已取消",
            }
            self.run_status_value.setText(labels.get(status, status))
        run_id = extract_run_id(payload)
        if run_id:
            self._current_cid = run_id
            self.cid_value.setText(run_id)

    def finish_validation(self, payload: Mapping[str, Any]) -> None:
        status = extract_status(payload)
        self.run_status_value.setText("校验通过" if status == "success" else "校验失败")
        self.stage_detail.setText("任务单参数校验已结束")
        self.result_view.setPlainText(render_result_text(payload))
        self._finish_common()

    def finish_run(self, payload: Mapping[str, Any]) -> None:
        status = extract_status(payload)
        success = status == "success"
        self.run_status_value.setText("已完成" if success else "执行结束")
        self.stage_detail.setText("已返回结果；具体跳过或失败原因见下方详情")
        if success:
            self._job_statuses = ["完成"] * len(self._jobs)
        else:
            next_index = next(
                (index for index, value in enumerate(self._job_statuses) if value != "完成"),
                0,
            )
            self._job_statuses = [
                "完成" if index < next_index else ("失败" if index == next_index else "未执行")
                for index in range(len(self._jobs))
            ]
        self._render_jobs(select_row=len(self._jobs) - 1)
        self.result_view.setPlainText(render_result_text(payload))
        self._finish_common()

    def show_failure(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("异常")
        self.stage_detail.setText(str(payload.get("error") or "任务执行异常"))
        self.result_view.setPlainText(pretty_json(payload))
        if not bool(payload.get("recoverable")):
            self._finish_common()

    def show_history_result(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("历史记录")
        self.stage_detail.setText("只读历史执行结果")
        self.cid_value.setText(extract_run_id(payload) or "--")
        self.result_view.setPlainText(render_result_text(payload))

    def cancel_requested(self, payload: Mapping[str, Any]) -> None:
        del payload
        self.run_status_value.setText("正在停止")
        self.stage_detail.setText("取消请求已发送，等待运行器确认")

    def _finish_common(self) -> None:
        self._elapsed_timer.stop()
        self.set_busy(False)
        self._active_mode = ""

    def _tick_elapsed(self) -> None:
        self._elapsed_seconds += 1
        minutes, seconds = divmod(self._elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        self.elapsed_value.setText(
            f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes:02d}:{seconds:02d}"
        )

    def _sync_actions(self) -> None:
        row = self._selected_row()
        has_selection = 0 <= row < len(self._jobs)
        editable = not self._busy
        self.move_up_button.setEnabled(editable and has_selection and row > 0)
        self.move_down_button.setEnabled(editable and has_selection and row < len(self._jobs) - 1)
        self.duplicate_button.setEnabled(editable and has_selection and len(self._jobs) < 50)
        self.delete_button.setEnabled(editable and has_selection)
        self.add_button.setEnabled(editable and self._selected_route() is not None and len(self._jobs) < 50)
        self.validate_button.setEnabled(editable and bool(self._jobs))
        self.start_button.setEnabled(editable and bool(self._jobs) and self._target_ready)
        self.cancel_button.setEnabled(self._busy)
        self.stop_on_failure.setEnabled(editable)
        self.job_table.setEnabled(editable or bool(self._jobs))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (
            self.category_buttons["ct"],
            self.category_buttons["gp"],
            self.subcategory_combo,
            self.route_combo,
            self.stage_spin,
            self.difficulty_combo,
            self.threat_spin,
            self.formation_combo,
            self.capture_spin,
        ):
            widget.setEnabled(not self._busy)
        self._sync_actions()

    def is_busy(self) -> bool:
        return self._busy

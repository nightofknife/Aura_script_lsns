"""Weekday battle plans with ordinary runs before native exploration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
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
    MISSION_LABELS,
    SUBCATEGORY_LABELS,
    BattleRoute,
    battle_job_summary,
    load_battle_routes,
    STRUCTURAL_STAGE_LABELS,
)
from packages.resonance_shared.battle_schedule import WEEKDAYS, WEEKDAY_LABELS, battle_game_day
from ..battle_plan import normalize_battle_editor_inputs
from ..config_repository import ResonanceConfigRepository
from .toggle_button import ToggleButton as QCheckBox
from .compact_parameter_grid import CompactParameterGrid
from .disclosure_section import DisclosureSection
from ..logic import (extract_run_id, extract_status, extract_final_result,
                     extract_battle_cleanup_evidence, pretty_json, render_result_text)


class BattlePage(QWidget):
    startRequested = Signal(object, float)
    cancelRequested = Signal()
    refreshTargetRequested = Signal()
    planChanged = Signal(object)

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._routes = load_battle_routes()
        self._weekday = battle_game_day()[1]
        self._days = {day: {"jobs": [], "structural_types": []} for day in WEEKDAYS}
        self._structural_types = []
        self._execution_weekday = None
        self._day_statuses = {}
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
        root.addWidget(self._build_week_panel())

        root.addWidget(self._build_builder_panel())
        body = QHBoxLayout()
        body.setContentsMargins(16, 12, 16, 4)
        body.setSpacing(12)
        body.addWidget(self._build_job_panel(), 7)
        body.addWidget(self._build_structural_panel(), 3)
        root.addLayout(body, 1)
        root.addWidget(self._build_result_panel())
        root.addWidget(self._build_action_bar())

    def _build_status_band(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("statusBand")
        layout = QHBoxLayout(band)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(28)
        self.target_value = self._status_pair(layout, "目标窗口", "检查中")
        self.run_status_value = self._status_pair(layout, "任务状态", "待命")
        self.job_count_value = self._status_pair(layout, "普通作战次数", "0 次")
        self.cid_value = self._status_pair(layout, "CID", "--")
        self.elapsed_value = self._status_pair(layout, "运行时长", "00:00")
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

    def _build_builder_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("parameterPanel")
        panel.setMinimumWidth(0)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(8)
        title = QLabel("新增普通关卡", panel)
        title.setObjectName("sectionTitle")
        content = panel
        category_row = QHBoxLayout()
        category_row.addWidget(title)
        category_row.addStretch(1)
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
        outer.addLayout(category_row)

        self.job_form = CompactParameterGrid(panel)
        self.subcategory_combo = QComboBox(content)
        self.mission_combo = QComboBox(content)
        self.route_combo = QComboBox(content)
        self.subcategory_combo.currentIndexChanged.connect(self._sync_missions)
        self.mission_combo.currentIndexChanged.connect(self._sync_routes)
        self.route_combo.currentIndexChanged.connect(self._sync_dynamic_fields)
        self.route_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.route_combo.setMinimumContentsLength(12)
        self.job_form.add_field("作战分类", self.subcategory_combo)
        self.job_form.add_field("任务类型", self.mission_combo)
        self.job_form.add_field("城市 / 关卡", self.route_combo)

        self.stage_spin = QSpinBox(content)
        self.stage_spin.setRange(1, 3)
        self.difficulty_combo = QComboBox(content)
        for value, label in DIFFICULTY_LABELS.items():
            self.difficulty_combo.addItem(label, value)
        self.threat_spin = QSpinBox(content)
        self.threat_spin.setRange(1, 999)
        self.threat_spin.setValue(1)
        self.formation_combo = QComboBox(content)
        self.formation_combo.addItem("游戏默认队伍", None)
        for index in range(1, 5):
            self.formation_combo.addItem(f"队伍 {index}", index)
        self.capture_spin = QSpinBox(content)
        self.capture_spin.setRange(0, 99)
        self.capture_spin.setSpecialValueText("默认")
        self.run_count_spin = QSpinBox(content)
        self.run_count_spin.setRange(1, 9999)
        self.run_count_spin.setSuffix(" 次")
        self.job_form.add_field("难度", self.difficulty_combo)
        self.job_form.add_field("关卡", self.stage_spin)
        self.job_form.add_field("威胁等级", self.threat_spin)
        self.job_form.add_field("战斗队伍", self.formation_combo)
        self.job_form.add_field("抓捕数量", self.capture_spin)
        self.job_form.add_field("作战次数", self.run_count_spin)
        outer.addWidget(self.job_form)

        actions = QHBoxLayout()
        availability = QLabel("优先扫荡；无法扫荡时进入实战。", content)
        availability.setWordWrap(True)
        availability.setProperty("caption", True)
        actions.addWidget(availability, 1)

        self.add_button = QPushButton("加入当天安排", panel)
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self._add_job)
        actions.addWidget(self.add_button)
        outer.addLayout(actions)

        self.category_buttons["ct"].setChecked(True)
        self._sync_subcategories()
        return panel

    def _build_job_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("普通关卡 · 按顺序执行", panel)
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.job_table = QTableWidget(0, 6, panel)
        self.job_table.setHorizontalHeaderLabels(["#", "关卡", "参数", "队伍", "次数", "状态"])
        self.job_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.job_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.job_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.job_table.setAlternatingRowColors(False)
        self.job_table.setMinimumHeight(156)
        self.job_table.verticalHeader().setVisible(False)
        header = self.job_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.itemSelectionChanged.connect(self._sync_actions)
        layout.addWidget(self.job_table, 3)
        self.empty_hint = QLabel("这天未安排普通关卡，可仅执行构析或直接跳过作战。", panel)
        self.empty_hint.setProperty("caption", True)
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

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

        return panel

    def _build_result_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(4)
        self.stop_rule_label = QLabel("战败或体力不足时结束作战，收尾后继续后续流程。", panel)
        self.stop_rule_label.setProperty("caption", True)
        self.stop_rule_label.setWordWrap(True)
        layout.addWidget(self.stop_rule_label)
        self.stage_detail = QLabel("计划会自动保存；未安排的日期直接跳过作战。", panel)
        self.stage_detail.setWordWrap(True)
        layout.addWidget(self.stage_detail)
        self.result_section = DisclosureSection("执行详情", panel, expanded=False)
        self.result_view = QTextBrowser(self.result_section.body)
        self.result_view.setMinimumHeight(130)
        self.result_view.setMaximumHeight(220)
        self.result_view.setPlaceholderText("执行后显示胜败、扫荡和收尾结果。")
        self.result_section.body_layout.addWidget(self.result_view)
        layout.addWidget(self.result_section)
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
        self.start_button = QPushButton("执行今日安排", band)
        self.start_button.setObjectName("primaryButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_button.clicked.connect(self._request_start)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.start_button)
        return band

    def _build_week_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        title = QLabel("每周作战计划", panel)
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.day_summary = QLabel(panel)
        self.day_summary.setProperty("caption", True)
        title_row.addWidget(self.day_summary)
        layout.addLayout(title_row)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.weekday_group = QButtonGroup(self)
        self.weekday_group.setExclusive(True)
        self.weekday_buttons = {}
        for index, day in enumerate(WEEKDAYS):
            button = QPushButton(WEEKDAY_LABELS[index], panel)
            button.setCheckable(True)
            button.setProperty("segment", True)
            button.setMinimumHeight(48)
            button.setToolTip(f"编辑{WEEKDAY_LABELS[index]}的关卡、次数和构析优先级")
            self.weekday_group.addButton(button, index)
            self.weekday_buttons[day] = button
            row.addWidget(button, 1)
        self.weekday_group.idClicked.connect(lambda index: self._select_day(WEEKDAYS[index]))
        layout.addLayout(row)
        note = QLabel("每天 05:00 换日；开始时执行今天的安排，切换星期仅用于编辑。", panel)
        note.setProperty("caption", True)
        note.setWordWrap(True)
        layout.addWidget(note)
        return panel

    def _build_structural_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(230)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        title = QLabel("构析勘探 · 最后执行", panel)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.structural_buttons = {}
        row = QHBoxLayout()
        for index, (kind, label) in enumerate(STRUCTURAL_STAGE_LABELS.items()):
            if index == 2:
                layout.addLayout(row)
                row = QHBoxLayout()
            button = QCheckBox(label, panel)
            button.setToolTip(f"由游戏自动讨伐{label}，可在下方调整优先级")
            button.toggled.connect(lambda checked, key=kind: self._toggle_structural(key, checked))
            self.structural_buttons[kind] = button
            row.addWidget(button)
        layout.addLayout(row)
        self.structural_list = QListWidget(panel)
        self.structural_list.setMinimumHeight(94)
        self.structural_list.currentRowChanged.connect(lambda _row: self._sync_actions())
        layout.addWidget(self.structural_list, 1)
        actions = QHBoxLayout()
        self.structural_up_button = QPushButton("优先级上移", panel)
        self.structural_down_button = QPushButton("优先级下移", panel)
        self.structural_up_button.clicked.connect(lambda: self._move_structural(-1))
        self.structural_down_button.clicked.connect(lambda: self._move_structural(1))
        actions.addWidget(self.structural_up_button)
        actions.addWidget(self.structural_down_button)
        layout.addLayout(actions)
        note = QLabel("仅选择类型和顺序，不设次数；普通关卡结束后交给游戏自动讨伐。", panel)
        note.setProperty("caption", True)
        note.setWordWrap(True)
        layout.addWidget(note)
        return panel

    def _toggle_structural(self, kind: str, checked: bool) -> None:
        if self._loading or self._busy:
            return
        if checked and kind not in self._structural_types:
            self._structural_types.append(kind)
        elif not checked and kind in self._structural_types:
            self._structural_types.remove(kind)
        self._render_structural()
        self._save_inputs()

    def _move_structural(self, offset: int) -> None:
        row = self.structural_list.currentRow()
        target = row+offset
        if self._busy or row < 0 or not 0 <= target < len(self._structural_types):
            return
        self._structural_types[row], self._structural_types[target] = self._structural_types[target], self._structural_types[row]
        self._render_structural(select_row=target)
        self._save_inputs()

    def _render_structural(self, *, select_row: int = -1) -> None:
        for kind, button in self.structural_buttons.items():
            button.blockSignals(True)
            button.setChecked(kind in self._structural_types)
            button.blockSignals(False)
        self.structural_list.clear()
        for index, kind in enumerate(self._structural_types):
            item = QListWidgetItem(f"{index+1}  {STRUCTURAL_STAGE_LABELS[kind]}")
            item.setData(Qt.ItemDataRole.UserRole, kind)
            self.structural_list.addItem(item)
        if not self._structural_types:
            item = QListWidgetItem("未选择类型 · 这天不执行构析")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.structural_list.addItem(item)
        self.structural_list.setCurrentRow(select_row)
        self._refresh_week_summary()
        self._sync_actions()

    def _select_day(self, weekday: str, *, force: bool = False) -> None:
        if (self._busy and not force) or weekday not in WEEKDAYS:
            return
        self._weekday = weekday
        self._jobs = self._days[weekday]["jobs"]
        self._structural_types = self._days[weekday]["structural_types"]
        self._job_statuses = list(self._day_statuses.get(weekday, ["等待"]*len(self._jobs)))
        if len(self._job_statuses) != len(self._jobs):
            self._job_statuses = ["等待"]*len(self._jobs)
        self.weekday_buttons[weekday].setChecked(True)
        self._sync_routes()
        self._render_jobs()
        self._render_structural()

    def _refresh_week_summary(self) -> None:
        today = battle_game_day()[1]
        for index, day in enumerate(WEEKDAYS):
            data = self._days[day]
            runs = sum(job.get("run_count", 1) for job in data["jobs"])
            types = len(data["structural_types"])
            # Tabs stay a fixed seven-day strip even with large run counts.
            # The adjacent day summary and table show the exact run counts.
            summary = f"{len(data['jobs'])} 关" if runs else "未安排"
            if types:
                summary = (f"{len(data['jobs'])} 关 · " if runs else "")+"构析"
            self.weekday_buttons[day].setText(WEEKDAY_LABELS[index]+(" · 今日" if day == today else "")+"\n"+summary)
        runs = sum(job.get("run_count", 1) for job in self._jobs)
        self.job_count_value.setText(f"{runs} 次")
        self.day_summary.setText(f"{WEEKDAY_LABELS[WEEKDAYS.index(self._weekday)]}：普通 {runs} 次 / 构析 {len(self._structural_types)} 类")

    def _set_run_count(self, row: int, value: int) -> None:
        if self._loading or self._busy or not 0 <= row < len(self._jobs):
            return
        self._jobs[row]["run_count"] = value
        self._job_statuses[row] = "等待"
        self.job_table.item(row, 5).setText("等待")
        self.job_table.item(row, 5).setToolTip("等待")
        self.job_table.item(row, 4).setToolTip(str(value))
        self.job_table.cellWidget(row, 4).setToolTip(f"作战 {value} 次")
        self._save_inputs()

    def _selected_main_category(self) -> str:
        return "gp" if self.category_buttons["gp"].isChecked() else "ct"

    def _sync_subcategories(self, *_args: object) -> None:
        category = self._selected_main_category()
        values = (
            ("tie_an", "regional_ops_center")
            if category == "ct"
            else ("action_summary",)
        )
        previous = self.subcategory_combo.currentData()
        self.subcategory_combo.blockSignals(True)
        self.subcategory_combo.clear()
        for value in values:
            self.subcategory_combo.addItem(SUBCATEGORY_LABELS[value], value)
        selected = self.subcategory_combo.findData(previous)
        self.subcategory_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.subcategory_combo.blockSignals(False)
        self._sync_missions()

    def _sync_missions(self, *_args: object) -> None:
        is_tie_an = (
            self._selected_main_category() == "ct"
            and str(self.subcategory_combo.currentData() or "") == "tie_an"
        )
        previous = self.mission_combo.currentData()
        self.mission_combo.blockSignals(True)
        self.mission_combo.clear()
        if is_tie_an:
            for value in ("expel", "bounty"):
                self.mission_combo.addItem(MISSION_LABELS[value], value)
            selected = self.mission_combo.findData(previous)
            self.mission_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.mission_combo.blockSignals(False)
        self._set_form_field_visible(self.mission_combo, is_tie_an)
        self._sync_routes()

    def _sync_routes(self, *_args: object) -> None:
        category = self._selected_main_category()
        subcategory = str(self.subcategory_combo.currentData() or "")
        mission_type = str(self.mission_combo.currentData() or "")
        selected_route = self.route_combo.currentData()
        self.route_combo.blockSignals(True)
        self.route_combo.clear()
        for route in self._routes:
            if route.main_category != category or route.subcategory != subcategory:
                continue
            if subcategory == "tie_an" and route.mission_type != mission_type:
                continue
            if not route.available_on(self._weekday):
                continue
            self.route_combo.addItem(
                route.detail if subcategory == "tie_an" else route.title,
                route.route_id,
            )
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
        self.job_form.set_field_visible(field, visible)

    def _job_from_controls(self) -> dict[str, Any]:
        route = self._selected_route()
        if route is None:
            raise ValueError("请选择一个有效的作战关卡。")
        job: dict[str, Any] = {"route_id": route.route_id, "run_count": self.run_count_spin.value()}
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
        if self._busy:
            return
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
        if self._busy or row < 0 or target < 0 or target >= len(self._jobs):
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
        if self._busy or row < 0 or len(self._jobs) >= 50:
            return
        self._jobs.insert(row + 1, dict(self._jobs[row]))
        self._job_statuses.insert(row + 1, "等待")
        self._render_jobs(select_row=row + 1)
        self._save_inputs()

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if self._busy or row < 0:
            return
        del self._jobs[row]
        del self._job_statuses[row]
        self._render_jobs(select_row=min(row, len(self._jobs) - 1))
        self._save_inputs()

    def _render_jobs(self, *, select_row: int = -1) -> None:
        self.job_table.blockSignals(True)
        for row in range(self.job_table.rowCount()):
            old_spin = self.job_table.cellWidget(row, 4)
            if old_spin is not None:
                old_spin.hide()
                self.job_table.removeCellWidget(row, 4)
        self.job_table.setRowCount(len(self._jobs))
        for row, job in enumerate(self._jobs):
            title, params, formation = battle_job_summary(job, self._routes)
            route = next((route for route in self._routes if route.route_id == job.get("route_id")), None)
            # The route identity gets the space: a category prefix must not
            # crowd the stage name out of the narrow embedded editor.
            identity = route.title.replace(" · ", "\n") if route else title
            parameters = params.replace(" · ", "\n") if params else "默认"
            values = (str(row + 1), identity, parameters, formation, "", self._job_statuses[row])
            tooltips = (str(row+1), title+"\n"+str(job.get("route_id") or ""),
                        params or "默认", formation, str(job.get("run_count", 1)), self._job_statuses[row])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(tooltips[column])
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.job_table.setItem(row, column, item)
            spin = QSpinBox(self.job_table)
            spin.setRange(1, 9999)
            spin.setValue(int(job.get("run_count", 1)))
            spin.setMinimumWidth(70)
            spin.setMaximumWidth(90)
            spin.setEnabled(not self._busy)
            spin.setToolTip(f"作战 {job.get('run_count', 1)} 次")
            spin.valueChanged.connect(lambda value, index=row: self._set_run_count(index, value))
            self.job_table.setCellWidget(row, 4, spin)
            text_lines = max(identity.count("\n"), parameters.count("\n"))+1
            self.job_table.setRowHeight(row, max(36, spin.sizeHint().height()+6,
                                                self.job_table.fontMetrics().height()*text_lines+28))
        if 0 <= select_row < len(self._jobs):
            self.job_table.selectRow(select_row)
        self.job_table.blockSignals(False)
        self.empty_hint.setVisible(not self._jobs)
        self._refresh_week_summary()
        self._sync_actions()

    def set_inputs(self, inputs: Mapping[str, Any]) -> None:
        if self._busy:
            return
        canonical = normalize_battle_editor_inputs(inputs)
        self._loading = True
        self._days = deepcopy(canonical["weekly_plan"]["days"])
        self._day_statuses.clear()
        self._select_day(self._weekday)
        self._loading = False
        self._sync_actions()

    def collect_inputs(self) -> dict[str, Any]:
        return normalize_battle_editor_inputs({
            "weekly_plan": {"schema_version": 1, "days": deepcopy(self._days)},
        })

    def _save_inputs(self, *_args: object) -> None:
        if self._loading or self._busy:
            return
        self._day_statuses.pop(self._weekday, None)
        snapshot = self.collect_inputs()
        self._settings.save_battle_inputs(snapshot)
        self._refresh_week_summary()
        self.planChanged.emit(snapshot)


    def _request_start(self) -> None:
        if self._busy:
            return
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


    def begin_run(self, payload: Mapping[str, Any]) -> None:
        self._execution_weekday = battle_game_day()[1]
        self._select_day(self._execution_weekday, force=True)
        self._begin(payload, mode="run")
        self.run_status_value.setText("运行中")
        self.stage_detail.setText("正在执行今日安排；实际游戏日以后台开始时确定的日期为准。")
        self._job_statuses = ["等待"] * len(self._jobs)
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
        result = extract_battle_cleanup_evidence(payload, expected_cid=self._current_cid)
        if result:
            day = result.get("weekday")
            if day in WEEKDAYS:
                self._execution_weekday = day
                self.stage_detail.setText(f"执行 {result.get('effective_date', '')} {WEEKDAY_LABELS[WEEKDAYS.index(day)]}的安排")
                summaries = result.get("job_results") or []
                if day == self._weekday and len(summaries) == len(self._jobs):
                    status_labels = {"completed": "完成", "running": "执行中", "stopped": "已停止",
                                     "cancelled": "已取消", "failed": "失败", "not_started": "未执行"}
                    self._job_statuses = [status_labels.get(item.get("status"), "等待") for item in summaries]
                    for row, text in enumerate(self._job_statuses):
                        self.job_table.item(row, 5).setText(text)
                        self.job_table.item(row, 5).setToolTip(text)


    def finish_run(self, payload: Mapping[str, Any]) -> None:
        result = extract_final_result(payload)
        if result.get("schema") == "resonance_pc.battle_plan_result.v1":
            # A framework success may still mean an early business stop.
            state = result.get("status")
            reason = result.get("stop_reason")
            self.run_status_value.setText({
                "completed": "已完成", "stopped": "提前结束", "skipped": "今日未安排",
                "cancelled": "已取消",
            }.get(state, "执行结束"))
            self.stage_detail.setText(
                "已取消作战，后续流程不会执行" if state == "cancelled" else
                "体力不足，已结束作战" if reason == "insufficient_stamina" else
                "战败，已结束作战" if reason == "defeat" else
                "今日未安排作战" if state == "skipped" else "作战已结束，具体结果见下方详情"
            )
            day = result.get("weekday")
            if day in WEEKDAYS:
                self.stage_detail.setText(f"{result.get('effective_date', '')} {WEEKDAY_LABELS[WEEKDAYS.index(day)]}："+self.stage_detail.text())
            self._execution_weekday = result.get("weekday")
            ordinary = iter(result.get("job_results") or [])
            structural = result.get("structural") or {}
            statuses = []
            for job in self._jobs:
                if str(job.get("route_id") or "").startswith("gp.structural_exploration."):
                    statuses.append("完成" if structural.get("outcome") == "completed" else
                                    "已取消" if structural.get("outcome") == "cancelled" else
                                    "已停止" if structural else "未执行")
                else:
                    summary = next(ordinary, {})
                    statuses.append({
                        "completed": "完成", "stopped": "已停止", "not_started": "未执行",
                        "failed": "失败", "cancelled": "已取消",
                    }.get(summary.get("status"), "未执行"))
            if self._execution_weekday == self._weekday:
                self._job_statuses = statuses
                self._day_statuses[self._weekday] = list(statuses)
                self._render_jobs(select_row=max(0, len(self._jobs) - 1))
            counts = result.get("counts") or {}
            self.stage_detail.setText(self.stage_detail.text()+f"；扫荡 {counts.get('swept', 0)} 次，实战胜 {counts.get('won', 0)} / 败 {counts.get('lost', 0)}")
            self.result_view.setPlainText(render_result_text(payload))
            self._finish_common()
            return
        status = extract_status(payload)
        success = status == "success"
        self.run_status_value.setText("已完成" if success else "执行结束")
        self.stage_detail.setText("已返回结果；具体跳过或失败原因见下方详情")
        if not success:
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
        self.result_section.toggle.setChecked(True)
        if not bool(payload.get("recoverable")):
            self._finish_common()

    def show_history_result(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("历史记录")
        self.stage_detail.setText("只读历史执行结果")
        self.cid_value.setText(extract_run_id(payload) or "--")
        self.result_view.setPlainText(render_result_text(payload))
        self.result_section.toggle.setChecked(True)

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
        self.start_button.setEnabled(editable and self._target_ready)
        self.cancel_button.setEnabled(self._busy)
        structural_row = self.structural_list.currentRow()
        self.structural_up_button.setEnabled(editable and 0 < structural_row < len(self._structural_types))
        self.structural_down_button.setEnabled(editable and 0 <= structural_row < len(self._structural_types)-1)
        self.job_table.setEnabled(editable or bool(self._jobs))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (
            self.category_buttons["ct"],
            self.category_buttons["gp"],
            self.subcategory_combo,
            self.mission_combo,
            self.route_combo,
            self.stage_spin,
            self.difficulty_combo,
            self.threat_spin,
            self.formation_combo,
            self.capture_spin,
            self.run_count_spin,
            *self.weekday_buttons.values(),
            *self.structural_buttons.values(),
        ):
            widget.setEnabled(not self._busy)
        for row in range(self.job_table.rowCount()):
            self.job_table.cellWidget(row, 4).setEnabled(not self._busy)
        self._sync_actions()

    def is_busy(self) -> bool:
        return self._busy

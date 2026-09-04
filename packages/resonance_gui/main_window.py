"""Main window for the Resonance desktop GUI."""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .bridge import RunnerBridge
from .config_repository import GuiPreferences, ResonanceConfigRepository
from .logic import (
    PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF,
    PC_GAME_NAME,
    PC_PLAYER_DATA_LATEST_TASK_REF,
    PC_PLAYER_DATA_REFRESH_TASK_REF,
    PC_TEAM_RECOMMENDATION_TASK_REF,
    extract_final_result,
    extract_run_id,
    extract_status,
    normalize_trade_task_inputs,
    parse_inputs_json,
    pretty_json,
    render_result_text,
    trade_result_summary,
)
from .style import APP_STYLE
from .task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS, TaskSpec
from .update_checker import current_version_label, find_available_update
from .widgets import BattlePage, CommercePage, SettingsHubPage, SmallTasksPage, WorkflowPage
from .widgets.run_detail import RunDetailView


class ResonanceMainWindow(QMainWindow):
    WORKFLOW_PAGE_INDEX = 0
    COMMERCE_PAGE_INDEX = 1
    BATTLE_PAGE_INDEX = 2
    WORKBENCH_PAGE_INDEX = 3
    HISTORY_PAGE_INDEX = 4
    SETTINGS_PAGE_INDEX = 5
    SMALL_TASKS_PAGE_INDEX = 6

    updateCheckCompleted = Signal(str)
    requestInitialize = Signal()
    requestRefreshTasks = Signal()
    requestRefreshHistory = Signal()
    requestRefreshTarget = Signal()
    requestRunNow = Signal(str, object, object, float)
    requestRunPcTask = Signal(str, object, object, float)
    requestRunPcTrade = Signal(object, float)
    requestPreviewPcTrade = Signal(object, float)
    requestRunPcPassenger = Signal(object, float)
    requestRunPcCombinedCommerce = Signal(object, float)
    requestRunPcBattle = Signal(object, float)
    requestValidatePcBattle = Signal(object, float)
    requestEnqueueTask = Signal(str, object, object, float)
    requestClearQueue = Signal()
    requestCancelCurrent = Signal()
    requestBridgeClose = Signal()

    def __init__(
        self,
        *,
        bridge: RunnerBridge | None = None,
        settings: ResonanceConfigRepository | None = None,
        initialize_on_startup: bool = True,
        update_checker: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings or ResonanceConfigRepository()
        self._preferences = self._settings.load_preferences()
        self._bridge = bridge or RunnerBridge()
        self._update_checker = update_checker if update_checker is not None else find_available_update
        self._update_check_started = False
        self._bridge_thread = QThread(self)
        self._task_items: dict[str, QTreeWidgetItem] = {}
        self._history_rows: list[dict[str, Any]] = []
        self._busy = False
        self._active_game_name = ""
        self._active_kind = ""
        self._commerce_active = False
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self._commerce_pending: list[str] = []
        self._commerce_inputs: dict[str, dict[str, Any]] = {}
        self._workflow_active = False
        self._workflow_stopping = False
        self._workflow_pending: list[dict[str, Any]] = []
        self._workflow_current: dict[str, Any] | None = None
        self._workflow_failed_message = ""
        self._small_task_active_ref = ""
        self._current_task: TaskSpec = TASKS_BY_ID.get(self._preferences.last_task_id) or WORKBENCH_TASKS[0]

        self._base_window_title = "Aura 雷索纳斯控制台"
        self.setWindowTitle(self._base_window_title)
        self.setMinimumSize(1180, 720)
        self.resize(1440, 860)
        self._build_ui()
        self._wire_bridge()
        self.updateCheckCompleted.connect(self._show_available_update)
        self._select_task(self._current_task.task_id)
        if initialize_on_startup:
            QTimer.singleShot(0, self.requestInitialize.emit)
            QTimer.singleShot(0, self._start_update_check)

    def _start_update_check(self) -> None:
        if self._update_check_started:
            return
        self._update_check_started = True

        def run() -> None:
            try:
                latest_tag = str(self._update_checker() or "").strip()
            except Exception:
                latest_tag = ""
            try:
                self.updateCheckCompleted.emit(latest_tag)
            except RuntimeError:
                return

        threading.Thread(target=run, name="aura-update-check", daemon=True).start()

    def _show_available_update(self, latest_tag: str) -> None:
        normalized = str(latest_tag or "").strip()
        if normalized:
            self.setWindowTitle(f"{self._base_window_title} · 发现新版本 {normalized}")

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("appRoot")
        root.setStyleSheet(APP_STYLE)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        top_bar = QFrame(root)
        top_bar.setObjectName("commerceHeader")
        top_layout = QVBoxLayout(top_bar)
        top_layout.setContentsMargins(18, 9, 18, 8)
        top_layout.setSpacing(7)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(7)
        brand = QLabel("AURA", top_bar)
        brand.setObjectName("brandTitle")
        title = QLabel("雷索纳斯控制台", top_bar)
        title.setObjectName("commerceTitle")
        self.version_badge = QLabel(current_version_label(), top_bar)
        self.version_badge.setObjectName("versionBadge")
        title_row.addWidget(brand)
        title_row.addWidget(title)
        title_row.addWidget(self.version_badge)
        title_row.addStretch(1)
        self.back_to_workflow_button = QPushButton("← 返回任务流程", top_bar)
        self.back_to_workflow_button.clicked.connect(lambda: self._switch_page(self.WORKFLOW_PAGE_INDEX))
        self.back_to_workflow_button.hide()
        title_row.addWidget(self.back_to_workflow_button)
        self.refresh_target_button = QPushButton("刷新目标", top_bar)
        self.refresh_target_button.setObjectName("quietButton")
        self.refresh_target_button.clicked.connect(self.requestRefreshTarget.emit)
        title_row.addWidget(self.refresh_target_button)
        self.global_target_label = QLabel("● 未连接窗口", top_bar)
        self.global_target_label.setProperty("caption", True)
        self.global_target_label.setProperty("status", "warning")
        title_row.addWidget(self.global_target_label)
        top_layout.addLayout(title_row)

        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(6)
        self.primary_nav_group = QButtonGroup(self)
        self.primary_nav_group.setExclusive(True)
        self.primary_nav_buttons: dict[int, QPushButton] = {}
        for page_index, text in (
            (self.WORKFLOW_PAGE_INDEX, "工作流程"),
            (self.SMALL_TASKS_PAGE_INDEX, "小任务"),
            (self.SETTINGS_PAGE_INDEX, "设置"),
        ):
            button = QPushButton(text, top_bar)
            button.setCheckable(True)
            button.setProperty("primaryNav", True)
            button.clicked.connect(
                lambda checked=False, target=page_index: self._switch_page(target)
            )
            self.primary_nav_group.addButton(button, page_index)
            self.primary_nav_buttons[page_index] = button
            nav_row.addWidget(button)
        nav_row.addStretch(1)
        top_layout.addLayout(nav_row)
        layout.addWidget(top_bar)

        self.page_stack = QStackedWidget(root)
        self.workflow_page = WorkflowPage(self._settings, self.page_stack)
        self.commerce_page = CommercePage(self._settings, self.page_stack)
        self.trade_page = self.commerce_page.trade_page
        self.passenger_page = self.commerce_page.passenger_page
        self.workflow_page.attach_parameter_editors(
            self.trade_page.parameter_panel,
            self.passenger_page.parameter_panel,
            self.trade_page.execution_panel,
        )
        self.workflow_page.tradeEndCityAvailabilityChanged.connect(
            self.trade_page.set_end_city_constraint_available
        )
        self.trade_page.autoBookChanged.connect(self.workflow_page.set_auto_book)
        self.workflow_page.autoBookChanged.connect(self.trade_page.set_auto_book)
        self.battle_page = BattlePage(self._settings, self.page_stack)
        self.workbench_page = self._build_workbench_page()
        self.history_page = self._build_history_page()
        self.settings_page = SettingsHubPage(self._settings, self.page_stack)
        self.small_tasks_page = SmallTasksPage(self._settings, self.page_stack)
        for page in (
            self.workflow_page,
            self.commerce_page,
            self.battle_page,
            self.workbench_page,
            self.history_page,
            self.settings_page,
            self.small_tasks_page,
        ):
            self.page_stack.addWidget(page)
        layout.addWidget(self.page_stack, 1)
        self.timeout_spin = QDoubleSpinBox(root)
        self.timeout_spin.setRange(0.0, 7200.0)
        self.timeout_spin.setValue(float(self._preferences.timeout_sec))
        self.timeout_spin.hide()
        self.setCentralWidget(root)
        self.statusBar().showMessage("雷索纳斯 GUI 就绪")
        self._switch_page(self.WORKFLOW_PAGE_INDEX)

        self.workflow_page.runRequested.connect(self._start_workflow)
        self.workflow_page.stopRequested.connect(self._stop_workflow)
        self.workflow_page.openTradeRequested.connect(self._open_trade_editor)
        self.workflow_page.openPassengerRequested.connect(self._open_passenger_editor)
        self.workflow_page.openBattleRequested.connect(lambda: self._switch_page(self.BATTLE_PAGE_INDEX))
        self.workflow_page.previewTradeRequested.connect(self._preview_workflow_trade)
        self.small_tasks_page.runPlayerDataRequested.connect(
            self._run_small_task_player_data
        )
        self.small_tasks_page.runTeamRecommendationRequested.connect(
            self._run_small_task_team_recommendation
        )
        self.small_tasks_page.runConsciousnessDeepDiveRequested.connect(
            self._run_small_task_consciousness_deep_dive
        )
        self.small_tasks_page.runConsciousnessDeepDiveCaptureRequested.connect(
            self._run_small_task_consciousness_deep_dive_capture
        )
        self.small_tasks_page.runConsciousnessDeepDiveSensitivityProbeRequested.connect(
            self._run_small_task_consciousness_deep_dive_sensitivity_probe
        )
        self.small_tasks_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.small_tasks_page.cacheRequested.connect(
            self._read_small_task_player_data_cache
        )
        self.workflow_page.settingsRequested.connect(lambda: self._switch_page(self.SETTINGS_PAGE_INDEX))
        self.settings_page.backRequested.connect(lambda: self._switch_page(self.WORKFLOW_PAGE_INDEX))
        self.settings_page.settingsSaved.connect(self._sync_workflow_settings)
        self.workflow_page.apply_compact_inputs(
            self._settings.load_trade_inputs(), self._settings.load_passenger_inputs()
        )
        self.trade_page.set_end_city_constraint_available(
            self.workflow_page.trade_end_city_constraint_available()
        )
        self.workflow_page.set_battle_count(len(self._settings.load_battle_inputs().get("jobs") or []))
        self._sync_workflow_settings()

        self.trade_page.startRequested.connect(self._run_pc_trade)
        self.trade_page.previewRequested.connect(self._preview_pc_trade)
        self.trade_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.passenger_page.startRequested.connect(self._run_pc_passenger)
        self.passenger_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.commerce_page.overview_page.startRequested.connect(self._start_commerce_sequence)
        self.commerce_page.overview_page.stopRequested.connect(self._stop_commerce_sequence)
        self.battle_page.startRequested.connect(self._run_pc_battle)
        self.battle_page.validateRequested.connect(self._validate_pc_battle)
        self.battle_page.cancelRequested.connect(self.requestCancelCurrent.emit)

    def _build_navigation(self) -> QWidget:
        nav = QFrame(self)
        nav.setObjectName("navigation")
        nav.setFixedWidth(168)
        layout = QVBoxLayout(nav)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)
        title = QLabel("AURA", nav)
        title.setObjectName("brandTitle")
        caption = QLabel("雷索纳斯控制台", nav)
        caption.setObjectName("brandCaption")
        layout.addWidget(title)
        layout.addWidget(caption)
        layout.addSpacing(20)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, text in enumerate(("跑商", "战斗", "任务工具", "历史", "设置")):
            button = QPushButton(text, nav)
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.clicked.connect(lambda checked=False, page=index: self._navigate_to_page(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)
        runtime = QLabel("PC runtime\nWGC + SendInput", nav)
        runtime.setObjectName("brandCaption")
        layout.addWidget(runtime)
        return nav

    def _navigate_to_page(self, index: int) -> None:
        if index == 0:
            self.commerce_page.show_overview()
        self._switch_page(index)

    def _switch_page(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        if index == self.WORKFLOW_PAGE_INDEX and hasattr(self, "workflow_page"):
            self.workflow_page.set_battle_count(len(getattr(self.battle_page, "_jobs", [])))
        if hasattr(self, "nav_buttons") and 0 <= index < len(self.nav_buttons):
            self.nav_buttons[index].setChecked(True)
        self.back_to_workflow_button.setVisible(
            index
            not in {
                self.WORKFLOW_PAGE_INDEX,
                self.SMALL_TASKS_PAGE_INDEX,
                self.SETTINGS_PAGE_INDEX,
            }
        )
        primary_index = (
            index
            if index in {self.SMALL_TASKS_PAGE_INDEX, self.SETTINGS_PAGE_INDEX}
            else self.WORKFLOW_PAGE_INDEX
        )
        self.primary_nav_buttons[primary_index].setChecked(True)
        if index == self.HISTORY_PAGE_INDEX:
            self.requestRefreshHistory.emit()

    def _build_workbench_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        title = QLabel("任务工具", root)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        splitter = QSplitter(Qt.Orientation.Horizontal, root)
        layout.addWidget(splitter, 1)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 8, 8, 0)
        self.task_tree = QTreeWidget(left)
        self.task_tree.setHeaderHidden(True)
        self.task_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._populate_task_tree()
        left_layout.addWidget(self.task_tree)
        refresh_button = QPushButton("刷新任务", left)
        refresh_button.clicked.connect(self.requestRefreshTasks.emit)
        left_layout.addWidget(refresh_button)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 0, 0)
        self.task_title = QLabel(right)
        self.task_title.setObjectName("sectionTitle")
        self.task_description = QLabel(right)
        self.task_description.setWordWrap(True)
        self.task_description.setProperty("caption", True)
        right_layout.addWidget(self.task_title)
        right_layout.addWidget(self.task_description)
        self.inputs_editor = QPlainTextEdit(right)
        self.inputs_editor.setTabChangesFocus(False)
        self.inputs_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        right_layout.addWidget(self.inputs_editor, 3)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("运行", right)
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._run_selected_now)
        self.enqueue_button = QPushButton("加入队列", right)
        self.enqueue_button.clicked.connect(self._enqueue_selected)
        self.clear_queue_button = QPushButton("清空队列", right)
        self.clear_queue_button.clicked.connect(self.requestClearQueue.emit)
        self.cancel_button = QPushButton("取消当前", right)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.requestCancelCurrent.emit)
        for button in (self.run_button, self.enqueue_button, self.clear_queue_button, self.cancel_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        right_layout.addLayout(button_row)
        self.queue_label = QLabel("队列：空", right)
        self.queue_label.setProperty("caption", True)
        right_layout.addWidget(self.queue_label)
        self.run_detail = RunDetailView(right)
        right_layout.addWidget(self.run_detail, 2)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([270, 760])
        self.task_tree.currentItemChanged.connect(self._handle_task_selection_changed)
        return root

    def _build_history_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        top = QHBoxLayout()
        title = QLabel("运行历史", root)
        title.setObjectName("pageTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.history_filter = QComboBox(root)
        self.history_filter.addItem("全部类型", "all")
        self.history_filter.addItem("跑商", "trade")
        self.history_filter.addItem("客运", "passenger")
        self.history_filter.addItem("战斗", "battle")
        self.history_filter.currentIndexChanged.connect(self._render_history)
        top.addWidget(self.history_filter)
        refresh = QPushButton("刷新历史", root)
        refresh.clicked.connect(self.requestRefreshHistory.emit)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.history_table = QTableWidget(0, 7, root)
        self.history_table.setHorizontalHeaderLabels(
            ["CID", "状态", "类型 / 摘要", "结果", "开始时间", "时长", "任务"]
        )
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.cellDoubleClicked.connect(self._open_history_row)
        layout.addWidget(self.history_table, 1)
        hint = QLabel("双击记录可在对应功能页打开只读结果", root)
        hint.setProperty("caption", True)
        layout.addWidget(hint)
        return root

    def _build_settings_page(self) -> QWidget:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 12)
        title = QLabel("设置", root)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.timeout_spin = QDoubleSpinBox(root)
        self.timeout_spin.setRange(0.0, 7200.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSpecialValueText("无限等待")
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setValue(float(self._preferences.timeout_sec))
        form.addRow("任务等待超时", self.timeout_spin)
        layout.addLayout(form)
        layout.addStretch(1)
        self.timeout_spin.valueChanged.connect(self._save_preferences)
        return root

    def _populate_task_tree(self) -> None:
        self.task_tree.clear()
        self._task_items.clear()
        by_category: dict[str, list[TaskSpec]] = {category: [] for category in CATEGORIES}
        for task in WORKBENCH_TASKS:
            by_category.setdefault(task.category, []).append(task)
        for category, tasks in by_category.items():
            category_item = QTreeWidgetItem([category])
            category_item.setFlags(category_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.task_tree.addTopLevelItem(category_item)
            for task in tasks:
                task_item = QTreeWidgetItem([task.title])
                task_item.setData(0, Qt.ItemDataRole.UserRole, task.task_id)
                category_item.addChild(task_item)
                self._task_items[task.task_id] = task_item
            category_item.setExpanded(True)

    def _wire_bridge(self) -> None:
        self._bridge.moveToThread(self._bridge_thread)
        self.requestInitialize.connect(self._bridge.initialize)
        self.requestRefreshTasks.connect(self._bridge.refresh_tasks)
        self.requestRefreshHistory.connect(self._bridge.refresh_history)
        self.requestRefreshTarget.connect(self._bridge.refresh_target)
        self.requestRunNow.connect(self._bridge.run_task_now)
        self.requestRunPcTask.connect(self._bridge.run_pc_task)
        self.requestRunPcTrade.connect(self._bridge.run_pc_trade)
        self.requestPreviewPcTrade.connect(self._bridge.preview_pc_trade)
        self.requestRunPcPassenger.connect(self._bridge.run_pc_passenger)
        self.requestRunPcCombinedCommerce.connect(self._bridge.run_pc_combined_commerce)
        self.requestRunPcBattle.connect(self._bridge.run_pc_battle)
        self.requestValidatePcBattle.connect(self._bridge.validate_pc_battle)
        self.requestEnqueueTask.connect(self._bridge.enqueue_task)
        self.requestClearQueue.connect(self._bridge.clear_queue)
        self.requestCancelCurrent.connect(self._bridge.cancel_current)
        self.requestBridgeClose.connect(self._bridge.close)

        self._bridge.tasksLoaded.connect(self._on_tasks_loaded)
        self._bridge.historyLoaded.connect(self._on_history_loaded)
        self._bridge.queueChanged.connect(self._on_queue_changed)
        self._bridge.taskStarted.connect(self._on_task_started)
        self._bridge.taskDispatched.connect(self._on_task_dispatched)
        self._bridge.runUpdated.connect(self._on_run_updated)
        self._bridge.tradeProgress.connect(self.trade_page.apply_progress)
        self._bridge.passengerProgress.connect(self.passenger_page.apply_progress)
        self._bridge.tradeProgress.connect(self._on_workflow_trade_progress)
        self._bridge.passengerProgress.connect(self._on_workflow_passenger_progress)
        self._bridge.targetStatusChanged.connect(self.trade_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.passenger_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.battle_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self._set_global_target_status)
        self._bridge.cancelRequested.connect(self.trade_page.cancel_requested)
        self._bridge.cancelRequested.connect(self.passenger_page.cancel_requested)
        self._bridge.cancelRequested.connect(self.battle_page.cancel_requested)
        self._bridge.cancelRequested.connect(self._on_commerce_cancel_requested)
        self._bridge.taskFinished.connect(self._on_task_finished)
        self._bridge.taskFailed.connect(self._on_task_failed)
        self._bridge.busyChanged.connect(self._on_busy_changed)
        self._bridge.logMessage.connect(self._on_log_message)
        self._bridge_thread.start()

    @Slot(dict)
    def _on_workflow_trade_progress(self, event: dict[str, Any]) -> None:
        self.workflow_page.apply_progress_event("trade", event)

    @Slot(dict)
    def _on_workflow_passenger_progress(self, event: dict[str, Any]) -> None:
        self.workflow_page.apply_progress_event("passenger", event)

    def _handle_task_selection_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        del previous
        if current is None:
            return
        task_id = current.data(0, Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        self._current_task = TASKS_BY_ID[str(task_id)]
        self._render_current_task()
        self._save_preferences()

    def _select_task(self, task_id: str) -> None:
        item = self._task_items.get(task_id) or next(iter(self._task_items.values()))
        self.task_tree.setCurrentItem(item)
        self._current_task = TASKS_BY_ID[str(item.data(0, Qt.ItemDataRole.UserRole))]
        self._render_current_task()

    def _render_current_task(self) -> None:
        task = self._current_task
        self.task_title.setText(f"{task.category} / {task.title}")
        self.task_description.setText(f"{task.description}\n{task.task_ref}")
        self.inputs_editor.setPlainText(pretty_json(task.default_inputs))

    def _collect_inputs(self) -> dict[str, Any] | None:
        try:
            return parse_inputs_json(self.inputs_editor.toPlainText())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "参数错误", str(exc))
            return None

    def _run_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcTrade.emit(
            normalize_trade_task_inputs(self._trade_input_mapping(inputs)),
            float(self.timeout_spin.value()),
        )

    def _preview_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        self.requestPreviewPcTrade.emit(
            normalize_trade_task_inputs(self._trade_input_mapping(inputs)),
            float(self.timeout_spin.value()),
        )

    def _preview_workflow_trade(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            return
        try:
            inputs = self.trade_page.collect_inputs()
        except ValueError as exc:
            self.workflow_page.show_trade_editor()
            QMessageBox.warning(self, "货运参数错误", str(exc))
            return
        self._settings.save_trade_inputs(inputs)
        self.requestPreviewPcTrade.emit(
            normalize_trade_task_inputs(inputs), float(self.timeout_spin.value())
        )

    def _run_pc_passenger(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcPassenger.emit(inputs, float(self.timeout_spin.value()))

    @staticmethod
    def _combined_commerce_inputs(
        *,
        order: str,
        trade: dict[str, Any],
        passenger: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "order": str(order),
            "total_fatigue_budget": int(trade.get("fatigue_budget", 0)),
            "trade_inputs": normalize_trade_task_inputs(trade),
            "passenger_inputs": dict(passenger),
        }

    @staticmethod
    def _trade_input_mapping(inputs: object) -> Mapping[str, Any]:
        return inputs if isinstance(inputs, Mapping) else {}

    def _open_trade_editor(self) -> None:
        self.workflow_page.show_trade_editor()
        self._switch_page(self.WORKFLOW_PAGE_INDEX)

    def _open_passenger_editor(self) -> None:
        self.workflow_page.show_passenger_editor()
        self._switch_page(self.WORKFLOW_PAGE_INDEX)

    def _sync_workflow_settings(self) -> None:
        startup = self.settings_page.startup_inputs()
        close = self.settings_page.close_inputs()
        self.workflow_page.startup_launch.setChecked(bool(startup["launch_if_not_running"]))
        self.workflow_page.startup_window_timeout.setValue(int(startup["window_timeout_sec"]))
        self.workflow_page.startup_rounds.setValue(int(startup["max_settle_rounds"]))
        self.workflow_page.close_timeout.setValue(int(close["graceful_timeout_sec"]))
        self.workflow_page.close_force.setChecked(bool(close["force_after_timeout"]))
        self.trade_page.arrival_timeout_minutes.setValue(
            max(self.settings_page.trade_arrival_timeout_seconds() // 60, 1)
        )

    def _start_commerce_sequence(self, run_trade: bool, run_passenger: bool) -> None:
        if self._busy or self._commerce_active or not (run_trade or run_passenger):
            return

        snapshots: dict[str, dict[str, Any]] = {}
        if run_trade:
            try:
                snapshots["trade"] = self.trade_page.collect_inputs()
            except ValueError as exc:
                self._open_trade_editor()
                QMessageBox.warning(self, "货运参数错误", str(exc))
                return
        if run_passenger:
            try:
                snapshots["passenger"] = self.passenger_page.collect_inputs()
            except ValueError as exc:
                self._open_passenger_editor()
                QMessageBox.warning(self, "客运参数错误", str(exc))
                return

        if "trade" in snapshots:
            self._settings.save_trade_inputs(snapshots["trade"])
        if "passenger" in snapshots:
            self._settings.save_passenger_inputs(snapshots["passenger"])

        run_snapshots = {kind: dict(values) for kind, values in snapshots.items()}
        if "trade" in run_snapshots:
            run_snapshots["trade"] = normalize_trade_task_inputs(
                run_snapshots["trade"]
            )

        if set(snapshots) == {"trade", "passenger"}:
            self._commerce_inputs = {
                "combined_commerce": self._combined_commerce_inputs(
                    order="trade_first",
                    trade=run_snapshots["trade"],
                    passenger=run_snapshots["passenger"],
                )
            }
            self._commerce_pending = ["combined_commerce"]
        else:
            self._commerce_inputs = run_snapshots
            self._commerce_pending = [
                kind for kind in ("trade", "passenger") if kind in snapshots
            ]
        self._commerce_active = True
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self.trade_page.set_busy(True)
        self.passenger_page.set_busy(True)
        self.run_button.setEnabled(False)
        self.enqueue_button.setEnabled(False)
        self.commerce_page.overview_page.set_running(True)
        self._dispatch_next_commerce_task()

    def _start_workflow(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            return
        steps = self.workflow_page.workflow_steps()
        if not steps:
            QMessageBox.warning(self, "流程为空", "请至少启用一个任务。")
            return
        commerce_steps = self.workflow_page.commerce_steps() if "commerce" in steps else []
        if "commerce" in steps and not commerce_steps:
            QMessageBox.warning(self, "跑商未配置", "请至少启用货运或客运。")
            return

        snapshots: dict[str, dict[str, Any]] = {}
        try:
            if "commerce" in steps:
                if "trade" in commerce_steps:
                    snapshots["trade"] = self.workflow_page.merge_trade_inputs(
                        self.trade_page.collect_inputs()
                    )
                    self._settings.save_trade_inputs(snapshots["trade"])
                if "passenger" in commerce_steps:
                    snapshots["passenger"] = self.workflow_page.merge_passenger_inputs(
                        self.passenger_page.collect_inputs()
                    )
                    self._settings.save_passenger_inputs(snapshots["passenger"])
            if "battle" in steps:
                snapshots["battle"] = self.battle_page.collect_inputs()
                self._settings.save_battle_inputs(snapshots["battle"])

            run_snapshots = {kind: dict(values) for kind, values in snapshots.items()}
            if "trade" in run_snapshots:
                run_snapshots["trade"] = normalize_trade_task_inputs(
                    run_snapshots["trade"]
                )
            if set(commerce_steps) == {"trade", "passenger"}:
                total_fatigue = int(run_snapshots["trade"].get("fatigue_budget", 0))
                passenger_fatigue = self.workflow_page.passenger_route_fatigue()
                route_city_ids = [
                    str(run_snapshots["passenger"]["passenger_city_a_id"]),
                    str(run_snapshots["passenger"]["passenger_city_b_id"]),
                ]
                available_city_ids = {
                    str(city_id)
                    for city_id in (run_snapshots["trade"].get("available_city_ids") or [])
                }
                unavailable_end_ids = [
                    city_id for city_id in route_city_ids if city_id not in available_city_ids
                ]
                if unavailable_end_ids:
                    raise ValueError(
                        "客运线路端点必须同时包含在货运的可用城市中："
                        + "、".join(unavailable_end_ids)
                    )
                trade_fatigue = total_fatigue - passenger_fatigue
                if trade_fatigue <= 0:
                    raise ValueError(
                        f"总疲劳 {total_fatigue} 不足以完成客运所需的 "
                        f"{passenger_fatigue} 疲劳，货运无法启动。"
                    )
        except ValueError as exc:
            QMessageBox.warning(self, "流程参数错误", str(exc))
            return

        pending: list[dict[str, Any]] = []
        for step in steps:
            if step == "startup":
                pending.append({
                    "step": "startup",
                    "task_ref": "tasks:game_startup_pc.yaml:enter_main",
                    "inputs": self.workflow_page.startup_inputs(),
                    "label": "进入主界面",
                    "dispatch": "pc_task",
                })
            elif step == "commerce":
                if set(commerce_steps) == {"trade", "passenger"}:
                    pending.append({
                        "step": "commerce",
                        "inputs": self._combined_commerce_inputs(
                            order=(
                                "trade_first"
                                if commerce_steps[0] == "trade"
                                else "passenger_first"
                            ),
                            trade=run_snapshots["trade"],
                            passenger=run_snapshots["passenger"],
                        ),
                        "label": "客货运组合",
                        "dispatch": "combined_commerce",
                        "commerce_steps": list(commerce_steps),
                    })
                else:
                    for kind in commerce_steps:
                        pending.append({
                            "step": kind,
                            "parent": "commerce",
                            "inputs": dict(run_snapshots[kind]),
                            "label": "货运" if kind == "trade" else "客运",
                            "dispatch": kind,
                        })
            elif step == "battle":
                pending.append({
                    "step": "battle", "inputs": dict(snapshots["battle"]),
                    "label": "自动战斗", "dispatch": "battle",
                })
            elif step == "close":
                pending.append({
                    "step": "close",
                    "task_ref": "tasks:game_startup_pc.yaml:close_game",
                    "inputs": self.workflow_page.close_inputs(),
                    "label": "关闭游戏",
                    "dispatch": "pc_task",
                })

        self._workflow_pending = pending
        self._workflow_active = True
        self._workflow_stopping = False
        self._workflow_current = None
        self._workflow_failed_message = ""
        self.workflow_page.begin_workflow(
            steps,
            commerce_steps,
            snapshots.get("trade"),
        )
        self._switch_page(self.WORKFLOW_PAGE_INDEX)
        self._dispatch_next_workflow_task()

    def _dispatch_next_workflow_task(self) -> None:
        if not self._workflow_active or self._workflow_stopping or self._busy:
            return
        if not self._workflow_pending:
            if self._workflow_failed_message:
                self._finish_workflow(False, self._workflow_failed_message)
            else:
                self._finish_workflow(True, "全部启用任务已完成。")
            return
        current = self._workflow_pending.pop(0)
        if not bool(current.get("inputs_ready", True)):
            self._abort_workflow("组合流程尚未获得可用的货运疲劳预算。")
            return
        self._workflow_current = current
        step = str(current["step"])
        parent = str(current.get("parent") or "")
        if parent and self.workflow_page.step_is_waiting(parent):
            self.workflow_page.mark_step(parent, "running", "开始跑商")
        self.workflow_page.mark_step(step, "running", f"正在执行{current['label']}")
        timeout = float(self.timeout_spin.value())
        dispatch = str(current["dispatch"])
        if dispatch == "trade":
            self.requestRunPcTrade.emit(dict(current["inputs"]), timeout)
        elif dispatch == "passenger":
            self.requestRunPcPassenger.emit(dict(current["inputs"]), timeout)
        elif dispatch == "combined_commerce":
            self.requestRunPcCombinedCommerce.emit(dict(current["inputs"]), timeout)
        elif dispatch == "battle":
            self.requestRunPcBattle.emit(dict(current["inputs"]), timeout)
        else:
            self.requestRunPcTask.emit(
                str(current["task_ref"]), dict(current["inputs"]), current["label"], timeout
            )

    def _run_small_task_player_data(self, inputs: object) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_player_data_error("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_PLAYER_DATA_REFRESH_TASK_REF
        self.small_tasks_page.begin_player_data_run()
        self.requestRunPcTask.emit(
            PC_PLAYER_DATA_REFRESH_TASK_REF,
            inputs,
            "刷新用户数据",
            float(self.timeout_spin.value()),
        )

    def _run_small_task_team_recommendation(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_team_recommendation_error(
                "当前有任务正在运行，请稍后再试。"
            )
            return
        self._small_task_active_ref = PC_TEAM_RECOMMENDATION_TASK_REF
        self.small_tasks_page.begin_team_recommendation_run()
        self.requestRunPcTask.emit(
            PC_TEAM_RECOMMENDATION_TASK_REF,
            {},
            "配队推荐",
            float(self.timeout_spin.value()),
        )

    def _run_small_task_consciousness_deep_dive(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_consciousness_deep_dive_error(
                "当前有任务正在运行，请稍后再试。"
            )
            return
        self._small_task_active_ref = PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF
        self.small_tasks_page.begin_consciousness_deep_dive_run()
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF,
            {},
            "识海深潜",
            float(self.timeout_spin.value()),
        )

    def _run_small_task_consciousness_deep_dive_capture(
        self, inputs: dict[str, Any]
    ) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_consciousness_deep_dive_capture_error(
                "当前有任务正在运行，请稍后再试。"
            )
            return
        self._small_task_active_ref = PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF
        capture_inputs = dict(inputs or {})
        self.small_tasks_page.begin_consciousness_deep_dive_capture_run(
            capture_inputs
        )
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
            capture_inputs,
            "识海深潜素材采集",
            float(self.timeout_spin.value()),
        )

    def _run_small_task_consciousness_deep_dive_sensitivity_probe(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_consciousness_deep_dive_sensitivity_probe_error(
                "当前有任务正在运行，请稍后再试。"
            )
            return
        self._small_task_active_ref = (
            PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF
        )
        self.small_tasks_page.begin_consciousness_deep_dive_sensitivity_probe_run()
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
            {},
            "识海深潜灵敏度探测素材采集",
            float(self.timeout_spin.value()),
        )

    def _read_small_task_player_data_cache(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            self.small_tasks_page.show_player_data_error("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_PLAYER_DATA_LATEST_TASK_REF
        self.requestRunPcTask.emit(
            PC_PLAYER_DATA_LATEST_TASK_REF,
            {},
            "读取用户数据缓存",
            float(self.timeout_spin.value()),
        )

    def _show_small_task_error(self, task_ref: str, message: str) -> None:
        if task_ref == PC_TEAM_RECOMMENDATION_TASK_REF:
            self.small_tasks_page.show_team_recommendation_error(message)
        elif task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF:
            self.small_tasks_page.show_consciousness_deep_dive_error(message)
        elif task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF:
            self.small_tasks_page.show_consciousness_deep_dive_capture_error(message)
        elif task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF:
            self.small_tasks_page.show_consciousness_deep_dive_sensitivity_probe_error(
                message
            )
        else:
            self.small_tasks_page.show_player_data_error(message)

    def _stop_workflow(self) -> None:
        if not self._workflow_active:
            return
        self._workflow_pending.clear()
        self._workflow_stopping = True
        if self._workflow_current is not None:
            self.workflow_page.mark_step(
                str(self._workflow_current["step"]), "cancelled", "用户请求停止流程"
            )
        if self._busy:
            self.requestCancelCurrent.emit()
        else:
            self._finish_workflow(False, "流程已停止。")

    def _abort_workflow(self, message: str) -> None:
        if not self._workflow_active:
            return
        self._workflow_failed_message = str(message or "流程执行失败。")
        current = self._workflow_current
        if current is not None:
            self.workflow_page.mark_step(str(current["step"]), "failed", message)
            parent = str(current.get("parent") or "")
            if parent:
                self.workflow_page.mark_step(parent, "failed", message)
        close_cleanup = self.settings_page.close_on_failure_enabled()
        self._workflow_pending = (
            [row for row in self._workflow_pending if row.get("step") == "close"]
            if close_cleanup else []
        )
        self._workflow_current = None
        if close_cleanup and self._workflow_pending:
            self._workflow_stopping = False
            self.workflow_page.append_log("流程失败，按设置继续执行关闭游戏。")
        else:
            self._workflow_stopping = True

    def _finish_workflow(self, success: bool, message: str) -> None:
        self._workflow_active = False
        self._workflow_stopping = False
        self._workflow_pending.clear()
        self._workflow_current = None
        self.workflow_page.finish_workflow(success=success, message=message)
        self._workflow_failed_message = ""

    def _dispatch_next_commerce_task(self) -> None:
        if not self._commerce_active or self._commerce_stopping:
            return
        if not self._commerce_pending:
            self._finish_commerce_sequence()
            return

        kind = self._commerce_pending.pop(0)
        self._commerce_current_kind = kind
        inputs = dict(self._commerce_inputs[kind])
        timeout = float(self.timeout_spin.value())
        if kind == "trade":
            self.requestRunPcTrade.emit(inputs, timeout)
        elif kind == "combined_commerce":
            self.requestRunPcCombinedCommerce.emit(inputs, timeout)
        else:
            self.requestRunPcPassenger.emit(inputs, timeout)

    def _stop_commerce_sequence(self) -> None:
        if not self._commerce_active:
            return
        self._commerce_pending.clear()
        self._commerce_stopping = True
        self.commerce_page.overview_page.set_stopping()
        if self._commerce_current_kind:
            self.requestCancelCurrent.emit()
        elif not self._busy:
            self._finish_commerce_sequence()

    def _abort_commerce_sequence(self, *, cancel_current: bool) -> None:
        if not self._commerce_active:
            return
        self._commerce_pending.clear()
        self._commerce_stopping = True
        self.commerce_page.overview_page.set_stopping()
        if cancel_current and self._commerce_current_kind:
            self.requestCancelCurrent.emit()
        if not self._busy and not self._commerce_current_kind:
            self._finish_commerce_sequence()

    def _finish_commerce_sequence(self) -> None:
        self._commerce_active = False
        self._commerce_stopping = False
        self._commerce_current_kind = ""
        self._commerce_pending.clear()
        self._commerce_inputs.clear()
        self.trade_page.set_busy(self._busy)
        self.passenger_page.set_busy(self._busy)
        self.run_button.setEnabled(not self._busy)
        self.enqueue_button.setEnabled(True)
        self.commerce_page.overview_page.set_running(False)
        self.commerce_page.overview_page.set_external_busy(self._busy)

    def _on_commerce_cancel_requested(self, _payload: dict[str, Any]) -> None:
        if self._commerce_active:
            self._abort_commerce_sequence(cancel_current=False)

    def _run_pc_battle(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcBattle.emit(inputs, float(self.timeout_spin.value()))

    def _validate_pc_battle(self, inputs: object, _unused_timeout: float) -> None:
        self.requestValidatePcBattle.emit(inputs, float(self.timeout_spin.value()))

    def _run_selected_now(self) -> None:
        inputs = self._collect_inputs()
        if inputs is not None:
            task = self._current_task
            self.requestRunNow.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _enqueue_selected(self) -> None:
        inputs = self._collect_inputs()
        if inputs is not None:
            task = self._current_task
            self.requestEnqueueTask.emit(task.task_ref, inputs, task.title, float(self.timeout_spin.value()))

    def _on_tasks_loaded(self, rows: list[dict[str, Any]]) -> None:
        refs = {str(row.get("task_ref") or "") for row in rows}
        missing = [task.task_ref for task in WORKBENCH_TASKS if task.task_ref not in refs]
        self.statusBar().showMessage(
            f"任务列表已加载，工作台缺失 {len(missing)} 个引用" if missing else f"任务列表已加载：{len(rows)} 个"
        )

    def _on_history_loaded(self, rows: list[dict[str, Any]]) -> None:
        self._history_rows = list(rows[: int(self._preferences.history_limit)])
        self._render_history()

    def _render_history(self, *_args: object) -> None:
        selected_kind = (
            str(self.history_filter.currentData() or "all")
            if hasattr(self, "history_filter")
            else "all"
        )
        visible_rows = [
            row
            for row in self._history_rows
            if selected_kind == "all" or self._history_kind(row) == selected_kind
        ]
        self._visible_history_rows = visible_rows
        self.history_table.setRowCount(0)
        for row in visible_rows:
            kind = self._history_kind(row)
            index = self.history_table.rowCount()
            self.history_table.insertRow(index)
            duration_ms = row.get("duration_ms")
            if kind == "battle":
                summary_text = "战斗任务单"
                result_text = "查看执行详情"
                type_label = "战斗"
            elif kind == "passenger":
                result = extract_final_result(row)
                route = result.get("passenger_route") if isinstance(result.get("passenger_route"), dict) else {}
                city_a = route.get("city_a") if isinstance(route.get("city_a"), dict) else {}
                city_b = route.get("city_b") if isinstance(route.get("city_b"), dict) else {}
                summary_text = " ↔ ".join(
                    value
                    for value in (
                        str(city_a.get("city_name") or ""),
                        str(city_b.get("city_name") or ""),
                    )
                    if value
                ) or "客运任务"
                result_text = f"{len(result.get('completed_legs') or [])} 个单程"
                type_label = "客运"
            else:
                summary = trade_result_summary(row)
                city_path = summary.get("city_path") or []
                summary_text = " -> ".join(str(city) for city in city_path) or "跑商任务"
                result_text = str(summary.get("expected_profit") or "--")
                type_label = "跑商"
            values = [
                extract_run_id(row),
                extract_status(row),
                f"{type_label} · {summary_text}",
                result_text,
                str(row.get("started_at") or row.get("created_at") or ""),
                self._format_duration(duration_ms),
                str(row.get("task_name") or row.get("task_ref") or ""),
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(index, col, QTableWidgetItem(value))
    def _open_history_row(self, row: int, column: int) -> None:
        del column
        visible_rows = getattr(self, "_visible_history_rows", self._history_rows)
        if 0 <= row < len(visible_rows):
            payload = visible_rows[row]
            history_kind = self._history_kind(payload)
            if history_kind == "battle":
                self.battle_page.show_history_result(payload)
                self._switch_page(self.BATTLE_PAGE_INDEX)
            elif history_kind == "passenger":
                self.passenger_page.show_history_result(payload)
                self._open_passenger_editor()
            else:
                self.trade_page.show_history_result(payload)
                self._open_trade_editor()

    @staticmethod
    def _history_kind(row: dict[str, Any]) -> str:
        task_identity = " ".join(
            str(row.get(key) or "")
            for key in ("task_name", "task_ref", "task_id")
        ).lower()
        if "passenger" in task_identity:
            return "passenger"
        return "battle" if "battle" in task_identity else "trade"

    def _on_queue_changed(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.queue_label.setText("队列：空")
            return
        labels = ", ".join(str(row.get("label") or row.get("task_ref")) for row in rows[:5])
        suffix = "" if len(rows) <= 5 else f" 等 {len(rows)} 项"
        self.queue_label.setText(f"队列：{labels}{suffix}")

    def _on_task_started(self, payload: dict[str, Any]) -> None:
        self._active_game_name = str(payload.get("game_name") or "")
        self._active_kind = str(payload.get("kind") or "")
        self.statusBar().showMessage(f"派发中：{payload.get('label')}")
        if payload.get("game_name") != PC_GAME_NAME:
            self.run_detail.show_text(pretty_json(payload))

    def _on_task_dispatched(self, payload: dict[str, Any]) -> None:
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("game_name") == PC_GAME_NAME:
            kind = str(item.get("kind") or "")
            if kind == "trade_preview":
                self.trade_page.begin_preview(payload)
                self.workflow_page.show_trade_preview()
                self._switch_page(self.WORKFLOW_PAGE_INDEX)
            elif kind == "trade_run":
                self.trade_page.begin_run(payload)
                if self._workflow_active:
                    self.workflow_page.set_active_progress_cid("trade", extract_run_id(payload))
                if not self._commerce_active and not self._workflow_active:
                    self._open_trade_editor()
            elif kind == "passenger_run":
                self.passenger_page.begin_run(payload)
                if self._workflow_active:
                    self.workflow_page.set_active_progress_cid("passenger", extract_run_id(payload))
                if not self._commerce_active and not self._workflow_active:
                    self._open_passenger_editor()
            elif kind == "combined_commerce_run":
                self.trade_page.begin_run(payload)
                self.passenger_page.begin_run(payload)
                if self._workflow_active:
                    cid = extract_run_id(payload)
                    self.workflow_page.set_active_progress_cid("trade", cid)
                    self.workflow_page.set_active_progress_cid("passenger", cid)
            elif kind == "battle_preview":
                self.battle_page.begin_validation(payload)
                self._switch_page(self.BATTLE_PAGE_INDEX)
            elif kind == "battle_run":
                self.battle_page.begin_run(payload)
                if not self._workflow_active:
                    self._switch_page(self.BATTLE_PAGE_INDEX)
            else:
                self.run_detail.show_text(pretty_json(payload))
        else:
            self.run_detail.show_text(pretty_json(payload))

    def _on_run_updated(self, payload: dict[str, Any]) -> None:
        if self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("trade_"):
            self.trade_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("passenger_"):
            self.passenger_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind == "combined_commerce_run":
            self.trade_page.update_run(payload)
            self.passenger_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("battle_"):
            self.battle_page.update_run(payload)

    @staticmethod
    def _combined_child_payload(
        payload: dict[str, Any],
        child: dict[str, Any],
        *,
        kind: str,
    ) -> dict[str, Any]:
        child_status = str(child.get("status") or "").strip().lower()
        succeeded = (
            child.get("success") is True
            and child_status == "completed"
        )
        item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
        return {
            **dict(payload),
            "status": "success" if succeeded else "failed",
            "gui_item": {**dict(item), "kind": f"{kind}_run"},
            "final_result": {"user_data": dict(child)},
        }

    def _finish_combined_pages(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        trade = result.get("trade") if isinstance(result.get("trade"), dict) else {}
        passenger = (
            result.get("passenger") if isinstance(result.get("passenger"), dict) else {}
        )
        self.trade_page.finish_run(
            self._combined_child_payload(payload, trade, kind="trade")
        )
        self.passenger_page.finish_run(
            self._combined_child_payload(payload, passenger, kind="passenger")
        )

    @staticmethod
    def _combined_reason_text(result: dict[str, Any]) -> str:
        reason = str(result.get("reason") or "客货运组合未成功完成")
        labels = {
            "passenger_route_invalid": "客运线路参数无效。",
            "passenger_endpoint_unavailable": "客运线路端点未全部包含在货运可用城市中。",
            "insufficient_trade_fatigue": "完成客运后没有可用于货运的疲劳。",
            "current_city_unknown": "无法识别当前城市，不能计算客货运组合方案。",
            "trade_preflight_no_plan": "当前行情下没有可在客运后启动的货运方案。",
            "trade_failed": "货运任务执行失败。",
            "trade_handoff_invalid": "货运结束状态不满足客货运交接条件。",
            "passenger_failed": "客运任务执行失败。",
            "passenger_handoff_invalid": "客运结束状态不满足客货运交接条件。",
            "passenger_forecast_mismatch": "客运实际终点或疲劳与运行前预测不一致。",
            "post_passenger_trade_no_plan": "客运结束后的最新行情已无可执行货运方案。",
        }
        return labels.get(reason, reason)

    def _finish_combined_workflow_result(
        self,
        *,
        result: dict[str, Any],
        succeeded: bool,
    ) -> None:
        trade = result.get("trade") if isinstance(result.get("trade"), dict) else {}
        passenger = (
            result.get("passenger") if isinstance(result.get("passenger"), dict) else {}
        )
        for kind, child, label in (
            ("trade", trade, "货运"),
            ("passenger", passenger, "客运"),
        ):
            if child.get("success") is True and str(child.get("status") or "") == "completed":
                self.workflow_page.mark_step(kind, "success", f"{label}已完成")
        if succeeded:
            self.workflow_page.mark_step("commerce", "success", "客货运组合已完成")
            self._workflow_current = None
            return

        failure_stage = str(result.get("failure_stage") or "")
        failed_kind = "passenger" if failure_stage.startswith("passenger") else "trade"
        if failure_stage == "preflight":
            order = str(result.get("order") or "trade_first")
            failed_kind = "passenger" if order == "passenger_first" else "trade"
        reason = self._combined_reason_text(result)
        self.workflow_page.mark_step(failed_kind, "failed", reason)
        if failure_stage == "preflight":
            QMessageBox.warning(self, "客货运组合不可执行", reason)
        self._abort_workflow(reason)

    def _on_task_finished(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage("任务执行结束")
        finished_kind = ""
        if self._active_game_name == PC_GAME_NAME:
            item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
            kind = str(item.get("kind") or self._active_kind)
            task_ref = str(item.get("task_ref") or "")
            player_data_result = extract_final_result(payload)
            player_data = player_data_result.get("player_data")
            small_task_owns_result = self._small_task_active_ref == task_ref
            if isinstance(player_data, dict):
                if task_ref == PC_PLAYER_DATA_REFRESH_TASK_REF:
                    self.small_tasks_page.apply_refresh_result(
                        player_data,
                        mark_complete=small_task_owns_result,
                    )
                elif task_ref == PC_PLAYER_DATA_LATEST_TASK_REF:
                    self.small_tasks_page.set_snapshot(player_data)
            elif task_ref in {
                PC_PLAYER_DATA_REFRESH_TASK_REF,
                PC_PLAYER_DATA_LATEST_TASK_REF,
            }:
                message = str(
                    player_data_result.get("reason")
                    or player_data_result.get("error")
                    or payload.get("error")
                    or "任务未成功完成。"
                )
                if small_task_owns_result:
                    self._show_small_task_error(task_ref, message)
            if small_task_owns_result:
                self._small_task_active_ref = ""
            team_result = player_data_result.get("team_recommendations")
            if task_ref == PC_TEAM_RECOMMENDATION_TASK_REF:
                if isinstance(team_result, dict):
                    self.small_tasks_page.apply_team_recommendation_result(team_result)
                else:
                    self.small_tasks_page.show_team_recommendation_error(
                        str(
                            player_data_result.get("reason")
                            or player_data_result.get("error")
                            or payload.get("error")
                            or "任务未返回可用配队推荐结果。"
                        )
                    )
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_TASK_REF:
                deep_dive_result = player_data_result.get("deep_dive")
                if isinstance(deep_dive_result, dict):
                    self.small_tasks_page.apply_consciousness_deep_dive_result(
                        deep_dive_result
                    )
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_error(
                        str(
                            player_data_result.get("reason")
                            or player_data_result.get("error")
                            or payload.get("error")
                            or "任务未返回可用识海深潜结果。"
                        )
                    )
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF:
                captures = player_data_result.get("captures")
                if isinstance(captures, list):
                    self.small_tasks_page.apply_consciousness_deep_dive_capture_result(
                        player_data_result
                    )
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_capture_error(
                        str(
                            player_data_result.get("reason")
                            or player_data_result.get("error")
                            or payload.get("error")
                            or "任务未返回识海深潜素材采集结果。"
                        )
                    )
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF:
                output_path = player_data_result.get("output_path")
                if isinstance(output_path, str) and output_path:
                    self.small_tasks_page.apply_consciousness_deep_dive_sensitivity_probe_result(
                        player_data_result
                    )
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_sensitivity_probe_error(
                        str(
                            player_data_result.get("reason")
                            or player_data_result.get("error")
                            or payload.get("error")
                            or "任务未返回灵敏度探测素材采集结果。"
                        )
                    )
                self._small_task_active_ref = ""
            finished_kind = kind
            if kind == "trade_preview":
                self.trade_page.finish_preview(payload)
            elif kind == "trade_run":
                self.trade_page.finish_run(payload)
            elif kind == "passenger_run":
                self.passenger_page.finish_run(payload)
            elif kind == "combined_commerce_run":
                self._finish_combined_pages(payload, extract_final_result(payload))
            elif kind == "battle_preview":
                self.battle_page.finish_validation(payload)
            elif kind == "battle_run":
                self.battle_page.finish_run(payload)
        if self._commerce_active and finished_kind == f"{self._commerce_current_kind}_run":
            status = extract_status(payload)
            result = extract_final_result(payload)
            result_status = str(result.get("status") or "").strip().lower()
            succeeded = (
                status == "success"
                and result.get("success") is not False
                and result_status not in {"blocked", "error", "failed", "timeout", "cancelled"}
            )
            self._commerce_current_kind = ""
            if not succeeded:
                if (
                    finished_kind == "combined_commerce_run"
                    and str(result.get("failure_stage") or "") == "preflight"
                ):
                    QMessageBox.warning(
                        self,
                        "客货运组合不可执行",
                        self._combined_reason_text(result),
                    )
                self._abort_commerce_sequence(cancel_current=False)
        if self._workflow_active and self._workflow_current is not None:
            status = extract_status(payload)
            result = extract_final_result(payload)
            result_status = str(result.get("status") or "").strip().lower()
            succeeded = (
                status == "success"
                and result.get("success") is not False
                and result_status not in {"blocked", "error", "failed", "timeout", "cancelled"}
            )
            current = self._workflow_current
            step = str(current["step"])
            if str(current.get("dispatch") or "") == "combined_commerce":
                self._finish_combined_workflow_result(result=result, succeeded=succeeded)
            elif succeeded:
                self.workflow_page.mark_step(step, "success", f"{current['label']}已完成")
                parent = str(current.get("parent") or "")
                if parent and not any(row.get("parent") == parent for row in self._workflow_pending):
                    self.workflow_page.mark_step(parent, "success", "跑商已完成")
                self._workflow_current = None
            else:
                self._abort_workflow(str(result.get("reason") or f"{current['label']}未成功完成"))
        self.run_detail.show_text(render_result_text(payload))

    def _on_task_failed(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage(f"任务异常：{payload.get('error', '')}")
        stage = str(payload.get("stage") or "")
        failed_task_ref = str(payload.get("task_ref") or "")
        if failed_task_ref and failed_task_ref == self._small_task_active_ref:
            message = str(payload.get("error") or "未知错误")
            self._show_small_task_error(failed_task_ref, message)
            self._small_task_active_ref = ""
        elif (
            self._small_task_active_ref
            and stage == "poll_run"
            and payload.get("recoverable") is False
        ):
            self._show_small_task_error(
                self._small_task_active_ref,
                str(payload.get("error") or "任务状态读取失败"),
            )
            self._small_task_active_ref = ""
        if self._commerce_active:
            self._abort_commerce_sequence(
                cancel_current=stage != "cancel_task" and bool(self._commerce_current_kind)
            )
        if self._workflow_active:
            self._abort_workflow(str(payload.get("error") or "任务执行异常"))
        if stage in {"run_pc_battle", "validate_pc_battle"}:
            self.battle_page.show_failure(payload)
        elif stage == "run_pc_passenger":
            self.passenger_page.show_failure(payload)
        elif stage == "run_pc_combined_commerce":
            self.trade_page.show_failure(payload)
            self.passenger_page.show_failure(payload)
        elif stage in {"run_pc_trade", "preview_pc_trade"}:
            self.trade_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("battle_")
        ):
            self.battle_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("passenger_")
        ):
            self.passenger_page.show_failure(payload)
        elif (
            self._active_game_name == PC_GAME_NAME
            and self._active_kind.startswith("trade_")
        ):
            self.trade_page.show_failure(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind == "combined_commerce_run":
            self.trade_page.show_failure(payload)
            self.passenger_page.show_failure(payload)
        self.run_detail.show_text(pretty_json(payload))

    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = bool(busy)
        if not busy:
            self._active_game_name = ""
            self._active_kind = ""
            if self._small_task_active_ref:
                self._show_small_task_error(
                    self._small_task_active_ref,
                    "任务已停止，但没有返回可用结果。",
                )
                self._small_task_active_ref = ""
        self.trade_page.set_busy(busy or self._commerce_active)
        self.passenger_page.set_busy(busy or self._commerce_active)
        self.battle_page.set_busy(busy)
        self.small_tasks_page.set_runner_busy(busy)
        self.commerce_page.overview_page.set_external_busy(busy)
        self.run_button.setEnabled(not busy and not self._commerce_active)
        self.enqueue_button.setEnabled(not self._commerce_active)
        self.cancel_button.setEnabled(busy)
        if self._commerce_active and not busy:
            if self._commerce_stopping:
                self._finish_commerce_sequence()
            elif self._commerce_pending:
                self._dispatch_next_commerce_task()
            else:
                self._finish_commerce_sequence()
        if self._workflow_active and not busy:
            if self._workflow_stopping:
                self._finish_workflow(
                    False,
                    self._workflow_failed_message or "流程已停止。",
                )
            elif self._workflow_pending:
                self._dispatch_next_workflow_task()
            elif self._workflow_current is None:
                if self._workflow_failed_message:
                    self._finish_workflow(False, self._workflow_failed_message)
                else:
                    self._finish_workflow(True, "全部启用任务已完成。")

    def _on_log_message(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if self._workflow_active:
            self.workflow_page.append_log(message)

    def _set_global_target_status(self, payload: dict[str, Any]) -> None:
        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        ready = bool(payload.get("ok")) and bool(target.get("visible", True))
        self.global_target_label.setText(
            f"● {target.get('title') or '已连接窗口'}" if ready else "● 未连接窗口"
        )
        self.global_target_label.setProperty("status", "success" if ready else "warning")
        self.global_target_label.style().unpolish(self.global_target_label)
        self.global_target_label.style().polish(self.global_target_label)

    def _save_preferences(self) -> None:
        self._preferences = GuiPreferences(
            timeout_sec=float(self.timeout_spin.value()) if hasattr(self, "timeout_spin") else self._preferences.timeout_sec,
            history_limit=self._preferences.history_limit,
            last_task_id=self._current_task.task_id,
        )
        self._settings.save_preferences(self._preferences)

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            seconds = int(float(value) / 1000)
        except (TypeError, ValueError):
            return "--"
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy or self._workflow_active:
            box = QMessageBox(self)
            box.setWindowTitle("任务仍在运行")
            box.setText("PC 自动化任务仍在运行。")
            stay = box.addButton("继续运行界面", QMessageBox.ButtonRole.RejectRole)
            cancel_and_exit = box.addButton("取消任务并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            if box.clickedButton() is stay:
                event.ignore()
                return
            if box.clickedButton() is cancel_and_exit:
                if self._workflow_active:
                    self._stop_workflow()
                elif self._busy:
                    self.requestCancelCurrent.emit()
            else:
                event.ignore()
                return
        self._save_preferences()
        self.requestBridgeClose.emit()
        self._bridge_thread.quit()
        self._bridge_thread.wait(5000)
        super().closeEvent(event)


def create_main_window() -> ResonanceMainWindow:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before creating ResonanceMainWindow.")
    return ResonanceMainWindow()

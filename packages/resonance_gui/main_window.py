"""Main window for the Resonance desktop GUI."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
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
    PC_GAME_NAME,
    extract_final_result,
    extract_run_id,
    extract_status,
    parse_inputs_json,
    pretty_json,
    render_result_text,
    trade_result_summary,
)
from .style import APP_STYLE
from .task_specs import CATEGORIES, TASKS_BY_ID, WORKBENCH_TASKS, TaskSpec
from .update_checker import find_available_update
from .widgets import BattlePage, CommercePage, SettingsHubPage, WorkflowPage
from .widgets.run_detail import RunDetailView


class ResonanceMainWindow(QMainWindow):
    WORKFLOW_PAGE_INDEX = 0
    COMMERCE_PAGE_INDEX = 1
    BATTLE_PAGE_INDEX = 2
    WORKBENCH_PAGE_INDEX = 3
    HISTORY_PAGE_INDEX = 4
    SETTINGS_PAGE_INDEX = 5

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
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(18, 9, 18, 9)
        brand = QLabel("AURA", top_bar)
        brand.setObjectName("brandTitle")
        title = QLabel("雷索纳斯控制台", top_bar)
        title.setObjectName("commerceTitle")
        badge = QLabel("开发中", top_bar)
        badge.setObjectName("developmentBadge")
        top_layout.addWidget(brand)
        top_layout.addWidget(title)
        top_layout.addWidget(badge)
        top_layout.addStretch(1)
        self.back_to_workflow_button = QPushButton("← 返回任务流程", top_bar)
        self.back_to_workflow_button.clicked.connect(lambda: self._switch_page(self.WORKFLOW_PAGE_INDEX))
        self.back_to_workflow_button.hide()
        top_layout.addWidget(self.back_to_workflow_button)
        self.refresh_target_button = QPushButton("刷新目标", top_bar)
        self.refresh_target_button.setObjectName("quietButton")
        self.refresh_target_button.clicked.connect(self.requestRefreshTarget.emit)
        top_layout.addWidget(self.refresh_target_button)
        self.global_target_label = QLabel("● 等待连接", top_bar)
        self.global_target_label.setProperty("caption", True)
        top_layout.addWidget(self.global_target_label)
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
        self.battle_page = BattlePage(self._settings, self.page_stack)
        self.workbench_page = self._build_workbench_page()
        self.history_page = self._build_history_page()
        self.settings_page = SettingsHubPage(self._settings, self.page_stack)
        for page in (
            self.workflow_page,
            self.commerce_page,
            self.battle_page,
            self.workbench_page,
            self.history_page,
            self.settings_page,
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
        self.workflow_page.settingsRequested.connect(lambda: self._switch_page(self.SETTINGS_PAGE_INDEX))
        self.settings_page.backRequested.connect(lambda: self._switch_page(self.WORKFLOW_PAGE_INDEX))
        self.settings_page.settingsSaved.connect(self._sync_workflow_settings)
        self.workflow_page.apply_compact_inputs(
            self._settings.load_trade_inputs(), self._settings.load_passenger_inputs()
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
            index not in {self.WORKFLOW_PAGE_INDEX, self.SETTINGS_PAGE_INDEX}
        )
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
        self._bridge.tradeProgress.connect(
            lambda event: self.workflow_page.apply_progress_event("trade", event)
        )
        self._bridge.passengerProgress.connect(
            lambda event: self.workflow_page.apply_progress_event("passenger", event)
        )
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
        self.requestRunPcTrade.emit(inputs, float(self.timeout_spin.value()))

    def _preview_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        self.requestPreviewPcTrade.emit(inputs, float(self.timeout_spin.value()))

    def _preview_workflow_trade(self) -> None:
        if self._busy or self._workflow_active or self._commerce_active:
            return
        try:
            inputs = self.trade_page.collect_inputs()
        except ValueError as exc:
            self.workflow_page.show_trade_editor()
            QMessageBox.warning(self, "货运参数错误", str(exc))
            return
        self.requestPreviewPcTrade.emit(inputs, float(self.timeout_spin.value()))

    def _run_pc_passenger(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcPassenger.emit(inputs, float(self.timeout_spin.value()))

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

        self._commerce_inputs = snapshots
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
                for kind in commerce_steps:
                    pending.append({
                        "step": kind,
                        "parent": "commerce",
                        "inputs": dict(snapshots[kind]),
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
        self.workflow_page.begin_workflow(steps, commerce_steps)
        self._switch_page(self.WORKFLOW_PAGE_INDEX)
        self._dispatch_next_workflow_task()

    def _dispatch_next_workflow_task(self) -> None:
        if not self._workflow_active or self._workflow_stopping or self._busy:
            return
        if not self._workflow_pending:
            self._finish_workflow(True, "全部启用任务已完成。")
            return
        current = self._workflow_pending.pop(0)
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
        elif dispatch == "battle":
            self.requestRunPcBattle.emit(dict(current["inputs"]), timeout)
        else:
            self.requestRunPcTask.emit(
                str(current["task_ref"]), dict(current["inputs"]), current["label"], timeout
            )

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
                summary_text = "海角城 ↔ 岚心城"
                result_text = str(result.get("total_revenue") or "--")
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
                if not self._commerce_active and not self._workflow_active:
                    self._open_trade_editor()
            elif kind == "passenger_run":
                self.passenger_page.begin_run(payload)
                if not self._commerce_active and not self._workflow_active:
                    self._open_passenger_editor()
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
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("battle_"):
            self.battle_page.update_run(payload)

    def _on_task_finished(self, payload: dict[str, Any]) -> None:
        self.statusBar().showMessage("任务执行结束")
        finished_kind = ""
        if self._active_game_name == PC_GAME_NAME:
            item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
            kind = str(item.get("kind") or self._active_kind)
            finished_kind = kind
            if kind == "trade_preview":
                self.trade_page.finish_preview(payload)
            elif kind == "trade_run":
                self.trade_page.finish_run(payload)
            elif kind == "passenger_run":
                self.passenger_page.finish_run(payload)
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
            if succeeded:
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
        self.run_detail.show_text(pretty_json(payload))

    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = bool(busy)
        if not busy:
            self._active_game_name = ""
            self._active_kind = ""
        self.trade_page.set_busy(busy or self._commerce_active)
        self.passenger_page.set_busy(busy or self._commerce_active)
        self.battle_page.set_busy(busy)
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
                self._finish_workflow(False, "流程已停止。")
            elif self._workflow_pending:
                self._dispatch_next_workflow_task()
            elif self._workflow_current is None:
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

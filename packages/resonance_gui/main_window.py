"""Main window for the Resonance desktop GUI."""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any, Callable, Mapping

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot, QSignalBlocker
from packages.aura_game.executable_locator import validate_executable_path
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from packages.aura_core.observability.logging.core_logger import logger

from .bridge import RunnerBridge
from .hotkeys import GlobalHotkeyManager
from .config_repository import GuiPreferences, ResonanceConfigRepository, TRADE_PREVIEW_INPUT_KEYS
from .logic import (
    PC_AFFIX_REFRESH_TASK_REF,
    PC_AFFIX_REFRESH_READ_TASK_REF,
    PC_BATTLE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
    PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF,
    PC_ETERNAL_SCUFFLE_TASK_REF,
    PC_GAME_NAME,
    PC_PLAYER_DATA_LATEST_TASK_REF,
    PC_PLAYER_DATA_REFRESH_TASK_REF,
    PC_TEAM_RECOMMENDATION_TASK_REF,
    extract_final_result,
    extract_deep_dive_scan_result,
    extract_deep_dive_plan_result,
    extract_deep_dive_planned_run_result,
    extract_run_id,
    extract_status,
    pretty_json,
    render_result_text,
    trade_result_summary,
    normalize_trade_task_inputs,
    validate_recovery_refresh,
    battle_continuation_detail,
)
from .style import APP_STYLE
from .update_checker import current_version_label, find_available_update
from .widgets import BattlePage, PassengerPage, TradePage, SettingsHubPage, SmallTasksPage, WorkflowPage
from .widgets.run_detail import RunDetailView


class ResonanceMainWindow(QMainWindow):

    updateCheckCompleted = Signal(str)
    requestInitialize = Signal()
    requestRefreshTasks = Signal()
    requestRefreshHistory = Signal()
    requestRefreshTarget = Signal()
    requestRunPcTask = Signal(str, object, object, float)
    requestRunPcTrade = Signal(object, float)
    requestPreviewPcTrade = Signal(object, float)
    requestRunPcPassenger = Signal(object, float)
    requestRunPcCombinedCommerce = Signal(object, float)
    requestRunPcBattle = Signal(object, float)
    requestCancelCurrent = Signal()
    requestBridgeClose = Signal()
    requestInputBridgeEnabled = Signal(bool)

    def __init__(
        self,
        *,
        bridge: RunnerBridge | None = None,
        settings: ResonanceConfigRepository | None = None,
        initialize_on_startup: bool = True,
        update_checker: Callable[[], str] | None = None,
        hotkey_backend: Any = None,
    ) -> None:
        super().__init__()
        self._settings = settings or ResonanceConfigRepository()
        self._preferences = self._settings.load_preferences()
        self._bridge = bridge or RunnerBridge()
        self._update_checker = update_checker if update_checker is not None else find_available_update
        self._update_check_started = False
        self._bridge_thread = QThread(self)
        self._closing = False
        self._close_ready = False
        self._bridge_closed = False
        self._battle_shutdown_pending = False
        self._shutdown_watchdog = QTimer(self)
        self._shutdown_watchdog.setInterval(15000)
        self._shutdown_watchdog.timeout.connect(self._on_shutdown_timeout)
        self._history_rows: list[dict[str, Any]] = []
        self._busy = False
        self._active_game_name = ""
        self._active_kind = ""
        self._workflow_active = False
        self._workflow_stopping = False
        self._workflow_pending: list[dict[str, Any]] = []
        self._workflow_current: dict[str, Any] | None = None
        self._workflow_failed_message = ""
        self._workflow_recovery_snapshot: dict[str, Any] = {}
        self._small_task_active_ref = ""

        self._base_window_title = "Aura 雷索纳斯控制台"
        self.setWindowTitle(self._base_window_title)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(1440, max(available.width() - 24, 400)) if available else 1440
        height = min(860, max(available.height() - 48, 400)) if available else 860
        self.setMinimumSize(min(760, width), min(460, height))
        self.resize(width, height)
        self._build_ui()
        self._sync_workflow_settings()
        self._wire_bridge()
        self._sync_input_bridge_setting()
        self.updateCheckCompleted.connect(self._show_available_update)
        self._setup_global_hotkeys(hotkey_backend)
        if initialize_on_startup:
            QTimer.singleShot(0, self.requestInitialize.emit)
            QTimer.singleShot(0, self._start_update_check)

    def _setup_global_hotkeys(self, backend: Any) -> None:
        self._last_hotkey_error = ""
        self._hotkeys = GlobalHotkeyManager(self, backend=backend)
        self._hotkeys.activated.connect(self._on_global_hotkey)
        self._hotkeys.errorOccurred.connect(self._on_hotkey_error)
        self.settings_page.set_hotkey_binder(self._apply_global_hotkeys)
        self.settings_page.hotkeyRecordingChanged.connect(self._hotkeys.set_suspended)
        saved = self._settings.load_hotkeys()
        try:
            self._apply_global_hotkeys(saved["start"], saved["stop"])
        except ValueError:
            # Invalid/occupied saved bindings must not prevent opening settings.
            pass

    def _apply_global_hotkeys(self, start: str, stop: str) -> Callable[[], None]:
        previous = self._hotkeys.active_shortcuts
        self._last_hotkey_error = ""
        if not self._hotkeys.configure(start, stop):
            raise ValueError(self._last_hotkey_error or "无法注册全局快捷键，请检查是否被占用。")
        self.settings_page.show_hotkey_status("全局快捷键已生效" if start or stop else "未设置快捷键")

        def undo() -> None:
            self._last_hotkey_error = ""
            if not self._hotkeys.configure(*previous):
                error = self._last_hotkey_error or "无法恢复原快捷键"
                if self._hotkeys.configure("", ""):
                    raise ValueError(f"{error}；已停用快捷键，请重新设置。")
                self._hotkeys.close()
                raise ValueError(f"{error}；快捷键监听已关闭，请重启界面。")
            self.settings_page.show_hotkey_status("全局快捷键已生效" if any(previous) else "未设置快捷键")

        return undo

    def _on_hotkey_error(self, message: str) -> None:
        self._last_hotkey_error = str(message)
        self.settings_page.show_hotkey_status(f"全局快捷键未生效：{message}", error=True)
        self.statusBar().showMessage(f"快捷键：{message}")

    def _on_global_hotkey(self, action: str) -> None:
        if (self._closing or self._close_ready or self.settings_page.start_hotkey.recording
                or self.settings_page.stop_hotkey.recording):
            return
        if action == "start":
            if (self._busy or self._workflow_active or self._small_task_active_ref
                    or QApplication.activeModalWidget() is not None):
                return
            self._start_workflow()
        elif action == "stop" and (self._busy or self._workflow_active or self._small_task_active_ref):
            if self._workflow_active:
                if not self._workflow_stopping:
                    self._stop_workflow()
            else:
                self.requestCancelCurrent.emit()

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
        self.back_to_workflow_button.clicked.connect(lambda: self._switch_page("workflow"))
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
        self.primary_nav_buttons: dict[str, QPushButton] = {}
        for page_index, text in (
            ("workflow", "工作流程"),
            ("tools", "独立功能"),
            ("history", "运行历史"),
            ("settings", "设置"),
        ):
            button = QPushButton(text, top_bar)
            button.setCheckable(True)
            button.setProperty("primaryNav", True)
            button.clicked.connect(
                lambda checked=False, target=page_index: self._switch_page(target)
            )
            self.primary_nav_group.addButton(button)
            self.primary_nav_buttons[page_index] = button
            nav_row.addWidget(button)
        nav_row.addStretch(1)
        top_layout.addLayout(nav_row)
        layout.addWidget(top_bar)

        self.page_stack = QStackedWidget(root)
        self.workflow_page = WorkflowPage(self._settings, self.page_stack)
        self.trade_page = TradePage(self._settings, self.workflow_page)
        self.passenger_page = PassengerPage(self._settings, self.workflow_page)
        self.workflow_page.attach_parameter_editors(
            self.trade_page.parameter_panel,
            self.passenger_page.parameter_panel,
        )
        self.workflow_page.tradeEndCityAvailabilityChanged.connect(
            self.trade_page.set_end_city_constraint_available
        )
        self.battle_page = BattlePage(self._settings, self.page_stack)
        self.history_page = self._build_history_page()
        self.settings_page = SettingsHubPage(self._settings, self.page_stack)
        self.small_tasks_page = SmallTasksPage(self._settings, self.page_stack)
        self.trade_preview_page = self.small_tasks_page.trade_preview_panel
        self._pages = {
            "workflow": self.workflow_page,
            "tools": self.small_tasks_page,
            "history": self.history_page,
            "settings": self.settings_page,
        }
        for page in self._pages.values():
            self.page_stack.addWidget(page)
        self.workflow_page.attach_battle_editor(self.battle_page)
        self.battle_page.layout().itemAt(0).widget().hide()
        self.battle_page.start_button.hide()
        self.battle_page.cancel_button.hide()
        self.freight_trial_button = QPushButton("打开货运试算", self.workflow_page.trade_editor_page)
        self.freight_trial_button.setToolTip("进入独立货运试算页面；只切换页面，不修改试算参数，也不开始计算。")
        self.freight_trial_button.clicked.connect(self._open_freight_trial)
        self.workflow_page.trade_editor_header.addWidget(self.freight_trial_button)
        layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("雷索纳斯 GUI 就绪")
        self._switch_page("workflow")

        self.workflow_page.runRequested.connect(self._start_workflow)
        self.workflow_page.saveParametersRequested.connect(self._save_current_task_parameters)
        self.workflow_page.stopRequested.connect(self._stop_workflow)
        self.workflow_page.openTradeRequested.connect(self._open_trade_editor)
        self.workflow_page.openPassengerRequested.connect(self._open_passenger_editor)
        self.workflow_page.openBattleRequested.connect(lambda: self._open_battle_editor())
        self.small_tasks_page.previewTradeRequested.connect(self._preview_pc_trade)
        self.small_tasks_page.returnToTradeRequested.connect(self._open_trade_editor)
        self.small_tasks_page.runPlayerDataRequested.connect(
            self._run_small_task_player_data
        )
        self.small_tasks_page.runTeamRecommendationRequested.connect(
            self._run_small_task_team_recommendation
        )
        self.small_tasks_page.runConsciousnessDeepDiveLoopRequested.connect(self._run_deep_dive_loop)
        self.small_tasks_page.runConsciousnessDeepDiveScanRequested.connect(
            self._run_deep_dive_scan
        )
        self.small_tasks_page.runConsciousnessDeepDivePlanRequested.connect(
            self._run_deep_dive_plan
        )
        self.small_tasks_page.runConsciousnessDeepDivePlannedRunRequested.connect(
            self._run_deep_dive_planned_run
        )
        self.small_tasks_page.runConsciousnessDeepDiveCaptureRequested.connect(
            self._run_small_task_consciousness_deep_dive_capture
        )
        self.small_tasks_page.runConsciousnessDeepDiveSensitivityProbeRequested.connect(
            self._run_small_task_consciousness_deep_dive_sensitivity_probe
        )
        self.small_tasks_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.small_tasks_page.runEternalScuffleRequested.connect(self._run_small_task_eternal_scuffle)
        self.small_tasks_page.readAffixRefreshRequested.connect(self._read_small_task_affix_refresh)
        self.small_tasks_page.runAffixRefreshRequested.connect(self._run_small_task_affix_refresh)
        self.small_tasks_page.cacheRequested.connect(
            self._read_small_task_player_data_cache
        )
        self.workflow_page.settingsRequested.connect(lambda: self._switch_page("settings"))
        self.settings_page.backRequested.connect(lambda: self._switch_page("workflow"))
        self.settings_page.settingsSaved.connect(self._sync_workflow_settings)
        self.settings_page.gamePathSaved.connect(
            lambda path: self.workflow_page.startup_path_label.setText(f"游戏路径：{path or '未设置'}")
        )
        self.settings_page.settingsSaved.connect(self._sync_input_bridge_setting)
        self.trade_page.auto_sparkling_water.toggled.connect(self._save_auto_sparkling_water)
        self.trade_page.auto_bento.toggled.connect(self._save_auto_bento)
        self.trade_page.auto_pickup.toggled.connect(self._save_auto_pickup)
        self.trade_page.auto_pickup_interval_sec.valueChanged.connect(self._save_auto_pickup_interval)
        self.trade_page.validationChanged.connect(self.workflow_page.set_freight_validation_error)
        self.workflow_page.set_freight_validation_error(self.trade_page._form_error())
        self.trade_page.set_end_city_constraint_available(
            self.workflow_page.trade_end_city_constraint_available()
        )
        self._sync_workflow_settings()
        self._bind_lifecycle_parameters()

        self.trade_page.startRequested.connect(self._run_pc_trade)
        self.trade_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.passenger_page.startRequested.connect(self._run_pc_passenger)
        self.passenger_page.cancelRequested.connect(self.requestCancelCurrent.emit)
        self.battle_page.startRequested.connect(self._run_pc_battle)
        self.battle_page.cancelRequested.connect(self.requestCancelCurrent.emit)


    def _switch_page(self, name: str) -> None:
        self.page_stack.setCurrentWidget(self._pages[name])
        self.primary_nav_buttons[name].setChecked(True)
        self.back_to_workflow_button.setVisible(name == "history")
        if name == "history":
            self.requestRefreshHistory.emit()

    def _open_battle_editor(self) -> None:
        self.workflow_page._select_task("battle")
        self._switch_page("workflow")

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
        self.history_filter.addItem("跑商试算", "trade_preview")
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
        self.run_detail = RunDetailView(root)
        layout.addWidget(self.run_detail, 1)
        hint = QLabel("双击记录可在下方查看只读结果", root)
        hint.setProperty("caption", True)
        layout.addWidget(hint)
        return root


    def _wire_bridge(self) -> None:
        self._bridge.moveToThread(self._bridge_thread)
        self.requestInitialize.connect(self._bridge.initialize)
        self.requestRefreshTasks.connect(self._bridge.refresh_tasks)
        self.requestRefreshHistory.connect(self._bridge.refresh_history)
        self.requestRefreshTarget.connect(self._bridge.refresh_target)
        self.requestRunPcTask.connect(self._bridge.run_pc_task)
        self.requestRunPcTrade.connect(self._bridge.run_pc_trade)
        self.requestPreviewPcTrade.connect(self._bridge.preview_pc_trade)
        self.requestRunPcPassenger.connect(self._bridge.run_pc_passenger)
        self.requestRunPcCombinedCommerce.connect(self._bridge.run_pc_combined_commerce)
        self.requestRunPcBattle.connect(self._bridge.run_pc_battle)
        self.requestCancelCurrent.connect(self._bridge.cancel_current)
        self.requestBridgeClose.connect(self._bridge.close)
        self.requestInputBridgeEnabled.connect(self._bridge.set_input_bridge_enabled)
        self._bridge.closeCompleted.connect(self._on_bridge_closed)
        self._bridge.closeFailed.connect(self._on_bridge_close_failed)
        self._bridge_thread.finished.connect(self._bridge.deleteLater)
        self._bridge_thread.finished.connect(self._on_bridge_thread_finished)

        self._bridge.tasksLoaded.connect(self._on_tasks_loaded)
        self._bridge.historyLoaded.connect(self._on_history_loaded)
        self._bridge.taskStarted.connect(self._on_task_started)
        self._bridge.taskDispatched.connect(self._on_task_dispatched)
        self._bridge.runUpdated.connect(self._on_run_updated)
        self._bridge.tradeProgress.connect(self._on_trade_progress)
        self._bridge.passengerProgress.connect(self.passenger_page.apply_progress)
        self._bridge.eternalScuffleProgress.connect(self.small_tasks_page.eternal_scuffle_panel.apply_progress)
        self._bridge.affixRefreshProgress.connect(self.small_tasks_page.affix_refresh_panel.apply_progress)
        self._bridge.deepDiveLoopProgress.connect(self.small_tasks_page.consciousness_deep_dive_panel.apply_loop_progress)
        self._bridge.deepDivePlannedRunProgress.connect(self.small_tasks_page.consciousness_deep_dive_panel.apply_planned_run_progress)
        self._bridge.tradeProgress.connect(self._on_workflow_trade_progress)
        self._bridge.passengerProgress.connect(self._on_workflow_passenger_progress)
        self._bridge.targetStatusChanged.connect(self.trade_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.passenger_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self.battle_page.set_target_status)
        self._bridge.targetStatusChanged.connect(self._set_global_target_status)
        self._bridge.cancelRequested.connect(self._on_trade_cancel_requested)
        self._bridge.cancelRequested.connect(self.passenger_page.cancel_requested)
        self._bridge.cancelRequested.connect(self.battle_page.cancel_requested)
        self._bridge.cancelRequested.connect(self._on_workflow_cancel_requested)
        self._bridge.cancelRequested.connect(self._on_affix_refresh_cancel_requested)
        self._bridge.taskFinished.connect(self._on_task_finished)
        self._bridge.taskFailed.connect(self._on_task_failed)
        self._bridge.busyChanged.connect(self._on_busy_changed)
        self._bridge.logMessage.connect(self._on_log_message)
        self._bridge_thread.start()

    @Slot(dict)
    def _on_workflow_trade_progress(self, event: dict[str, Any]) -> None:
        if self._active_kind != "trade_preview":
            self.workflow_page.apply_progress_event("trade", event)

    @Slot(dict)
    def _on_workflow_passenger_progress(self, event: dict[str, Any]) -> None:
        self.workflow_page.apply_progress_event("passenger", event)


    def _run_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            return
        trade = deepcopy(inputs if isinstance(inputs, dict) else {})
        self._start_freight_recovery_workflow(trade)

    def _save_auto_sparkling_water(self, enabled: bool) -> None:
        inputs = self._settings.load_trade_inputs()
        inputs["auto_sparkling_water"] = bool(enabled)
        self._settings.save_trade_inputs(inputs)

    def _save_auto_pickup(self, enabled: bool) -> None:
        inputs = self._settings.load_trade_inputs()
        inputs["auto_pickup"] = bool(enabled)
        self._settings.save_trade_inputs(inputs)

    def _save_auto_pickup_interval(self, interval_sec: float) -> None:
        previous = None
        try:
            inputs = self._settings.load_trade_inputs()
            previous = inputs["auto_pickup_interval_sec"]
            inputs["auto_pickup_interval_sec"] = float(interval_sec)
            self._settings.save_trade_inputs(inputs)
        except (ValueError, OSError) as exc:
            if previous is not None:
                with QSignalBlocker(self.trade_page.auto_pickup_interval_sec):
                    self.trade_page.auto_pickup_interval_sec.setValue(previous)
            QMessageBox.warning(self, "拣货间隔保存失败", str(exc))

    def _save_auto_bento(self, enabled: bool) -> None:
        inputs = self._settings.load_trade_inputs()
        # Keep incomplete drafts editable; the shared form/task validation
        # blocks execution until at least one bento type is selected.
        inputs["auto_bento"] = bool(enabled)
        self._settings.save_trade_inputs(inputs)

    @staticmethod
    def _recovery_refresh_step(trade: Mapping[str, Any]) -> dict[str, Any] | None:
        recovery_enabled = bool(trade.get("auto_sparkling_water", False) or trade.get("auto_bento", False))
        if not recovery_enabled:
            return None
        profile_sections = ["fatigue"]
        if bool(trade.get("auto_sparkling_water", False)):
            profile_sections.append("sparkling_water")
        if bool(trade.get("auto_bento", False)):
            profile_sections.append("bento_count")
        return {
            "step": "refresh_recovery",
            "task_ref": PC_PLAYER_DATA_REFRESH_TASK_REF,
            "inputs": {
                "stages": ["profile"],
                "profile_sections": profile_sections,
            },
            "label": "货运前数据刷新",
            "dispatch": "pc_task",
        }

    def _start_freight_recovery_workflow(
        self, trade: dict[str, Any], passenger: dict[str, Any] | None = None,
    ) -> None:
        kinds = ["trade"] if passenger is None else ["trade", "passenger"]
        prepare = self._recovery_refresh_step(trade)
        pending = []
        if prepare is not None:
            prepare["parent"] = "trade"
            pending.append(prepare)
        pending.append({
            "step": "trade", "label": "货运", "dispatch": "trade", "inputs": deepcopy(trade),
        })
        if passenger is not None:
            pending.append({
                "step": "passenger", "label": "客运", "dispatch": "passenger",
                "inputs": deepcopy(passenger),
            })
        self._begin_workflow(pending, kinds, [], trade)

    def _preview_pc_trade(self, inputs: object, _unused_timeout: float) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            return
        preview_inputs = dict(inputs) if isinstance(inputs, dict) else {}
        preview_inputs = {
            key: value for key, value in preview_inputs.items() if key in TRADE_PREVIEW_INPUT_KEYS
        }
        preview_inputs = {
            key: value for key, value in normalize_trade_task_inputs(preview_inputs).items()
            if key in TRADE_PREVIEW_INPUT_KEYS
        }
        self.requestPreviewPcTrade.emit(
            preview_inputs, float(self._preferences.timeout_sec)
        )

    def _open_freight_trial(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            return
        self.trade_preview_page.tabs.setCurrentIndex(0)
        self.small_tasks_page.show_trade_preview()
        self._switch_page("tools")

    def _on_trade_progress(self, event: dict[str, Any]) -> None:
        page = self.trade_preview_page if self._active_kind == "trade_preview" else self.trade_page
        page.apply_progress(event)

    def _on_trade_cancel_requested(self, payload: dict[str, Any]) -> None:
        page = self.trade_preview_page if self._active_kind == "trade_preview" else self.trade_page
        page.cancel_requested(payload)

    def _run_pc_passenger(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcPassenger.emit(inputs, float(self._preferences.timeout_sec))

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

    def _open_trade_editor(self) -> None:
        self.workflow_page.show_trade_editor()
        self._switch_page("workflow")

    def _open_passenger_editor(self) -> None:
        self.workflow_page.show_passenger_editor()
        self._switch_page("workflow")

    def _sync_input_bridge_setting(self) -> None:
        active = self._busy or self._workflow_active
        self.settings_page.use_input_bridge.setEnabled(not active)
        if active:
            return
        value = self._settings.value("game/use_input_bridge", False)
        enabled = (
            value.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(value, str) else bool(value)
        )
        self.requestInputBridgeEnabled.emit(enabled)

    def _sync_workflow_settings(self) -> None:
        path = str(self._settings.value("game/executable_path", "") or "")
        self.workflow_page.startup_path_label.setText(f"游戏路径：{path or '未设置'}")
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

    def _bind_lifecycle_parameters(self) -> None:
        """Keep lifecycle fields consistent with the global settings editor."""
        quick, settings = self.workflow_page, self.settings_page
        def sync_value(target, value, key, setter="setValue"):
            with QSignalBlocker(target):
                getattr(target, setter)(value)
            try:
                self._settings.set_value(key, value)
            except OSError as exc:
                self.statusBar().showMessage(f"设置保存失败：{exc}")
        for source, target, key in (
            (quick.startup_window_timeout, settings.window_timeout, "game/window_timeout_sec"),
            (quick.startup_rounds, settings.settle_rounds, "game/max_settle_rounds"),
            (quick.close_timeout, settings.close_timeout, "game/graceful_timeout_sec"),
        ):
            source.valueChanged.connect(lambda value, peer=target, setting=key: sync_value(peer, value, setting))
            target.valueChanged.connect(lambda value, peer=source, setting=key: sync_value(peer, value, setting))
        quick.startup_launch.toggled.connect(lambda checked: sync_value(settings.launch_if_needed, checked, "game/launch_if_not_running", "setChecked"))
        settings.launch_if_needed.toggled.connect(lambda checked: sync_value(quick.startup_launch, checked, "game/launch_if_not_running", "setChecked"))
        def set_close_mode(checked):
            with QSignalBlocker(settings.close_mode):
                settings.close_mode.setCurrentIndex(0 if checked else 1)
            sync_value(quick.close_force, checked, "game/force_after_timeout", "setChecked")
        quick.close_force.toggled.connect(set_close_mode)
        settings.close_mode.currentIndexChanged.connect(
            lambda _index: sync_value(quick.close_force, bool(settings.close_mode.currentData()), "game/force_after_timeout", "setChecked")
        )
        settings.trade_arrival_timeout.valueChanged.connect(
            lambda value: self.trade_page.arrival_timeout_minutes.setValue(value)
        )
        def sync_arrival(value):
            with QSignalBlocker(settings.trade_arrival_timeout):
                settings.trade_arrival_timeout.setValue(value)
        self.trade_page.arrival_timeout_minutes.valueChanged.connect(sync_arrival)


    def _save_current_task_parameters(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            return
        task = self.workflow_page._selected_task
        try:
            if task == "trade":
                self._settings.save_trade_inputs(self.trade_page.collect_ui_state())
            elif task == "passenger":
                self._settings.save_passenger_inputs(self.passenger_page.collect_inputs())
            elif task == "battle":
                self._settings.save_battle_inputs(self.battle_page.collect_inputs())
            elif task == "startup":
                for key, value in self.workflow_page.startup_inputs().items():
                    if key in {"executable_path", "launch_if_not_running", "window_timeout_sec", "max_settle_rounds"}:
                        self._settings.set_value(f"game/{key}", value or "" if key == "executable_path" else value)
            elif task == "close":
                for key, value in self.workflow_page.close_inputs().items():
                    self._settings.set_value(f"game/{key}", value)
            self._settings.sync_checked()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "参数未保存", str(exc))
            return
        self.statusBar().showMessage("当前任务参数已保存")

    def _start_workflow(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            return
        steps = self.workflow_page.workflow_steps()
        if not steps:
            QMessageBox.warning(self, "流程为空", "请至少启用一个任务。")
            return
        commerce_steps: list[str] = []

        snapshots: dict[str, dict[str, Any]] = {}
        validating = ""
        try:
            if "startup" in steps:
                validating = "进入主界面"
                snapshots["startup"] = self.workflow_page.startup_inputs()
                if snapshots["startup"].get("launch_if_not_running", True):
                    path = str(snapshots["startup"].get("executable_path") or "").strip()
                    if not path:
                        raise ValueError("游戏路径为空，请到设置中填写或点击检测。")
                    if validate_executable_path(path, executable_name="雷索纳斯.exe") is None:
                        raise ValueError("游戏路径无效，请选择雷索纳斯.exe。")
            if "close" in steps:
                snapshots["close"] = self.workflow_page.close_inputs()
            if "trade" in steps:
                validating = "货运"
                snapshots["trade"] = self.trade_page.collect_task_inputs()
            if "passenger" in steps:
                validating = "客运"
                snapshots["passenger"] = self.passenger_page.collect_inputs()
            if "battle" in steps:
                validating = "自动战斗"
                snapshots["battle"] = self.battle_page.collect_inputs()
            validating = "保存设置"
            if "trade" in steps:
                self._settings.save_trade_inputs(self.trade_page.collect_ui_state())
            if "passenger" in steps:
                self._settings.save_passenger_inputs(snapshots["passenger"])
            if "battle" in steps:
                self._settings.save_battle_inputs(snapshots["battle"])
            self._settings.sync_checked()

            run_snapshots = deepcopy(snapshots)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "流程参数错误", f"{validating}：{exc}")
            return

        trade = snapshots.get("trade", {})
        # Recovery is an internal freight preparation stage, not a selectable
        # task. Refresh immediately before freight, in the user's exact order.
        pending: list[dict[str, Any]] = []
        for step in steps:
            if step == "startup":
                pending.append({
                    "step": "startup",
                    "task_ref": "tasks:game_startup_pc.yaml:enter_main",
                    "inputs": deepcopy(run_snapshots["startup"]),
                    "label": "进入主界面",
                    "dispatch": "pc_task",
                })
            elif step in {"trade", "passenger"}:
                if step == "trade":
                    prepare = self._recovery_refresh_step(trade)
                    if prepare is not None:
                        prepare["parent"] = "trade"
                        pending.append(prepare)
                pending.append({
                    "step": step, "inputs": dict(run_snapshots[step]),
                    "label": "货运" if step == "trade" else "客运", "dispatch": step,
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
                    "inputs": deepcopy(run_snapshots["close"]),
                    "label": "关闭游戏",
                    "dispatch": "pc_task",
                })

        if "trade" in steps and "passenger" in steps:
            first, second = sorted((steps.index("trade"), steps.index("passenger")))
            if second != first + 1:
                QMessageBox.warning(self, "客货运组合顺序", "货运与客运共用疲劳预算，请将两项任务相邻排列。")
                return
            commerce_steps = [steps[first], steps[second]]
            order = "trade_first" if commerce_steps[0] == "trade" else "passenger_first"
            commerce_rows = [row for row in pending if row["step"] in {"trade", "passenger"}]
            first_row = next(i for i, row in enumerate(pending) if row is commerce_rows[0])
            insert_at = sum(row["step"] not in {"trade", "passenger", "refresh_recovery"} for row in pending[:first_row])
            pending = [row for row in pending if row["step"] not in {"trade", "passenger"}]
            # Recovery belongs before the combined runtime, including passenger-first.
            preparation = [row for row in pending if row["step"] == "refresh_recovery"]
            pending = [row for row in pending if row["step"] != "refresh_recovery"]
            insert_at = min(insert_at, len(pending))
            pending[insert_at:insert_at] = preparation + [{
                "step": commerce_steps[0], "label": "客货运组合", "dispatch": "combined_commerce",
                "inputs": self._combined_commerce_inputs(order=order, trade=snapshots["trade"], passenger=snapshots["passenger"]),
            }]
        self._begin_workflow(pending, steps, commerce_steps, snapshots.get("trade"))

    def _begin_workflow(
        self, pending: list[dict[str, Any]], steps: list[str],
        commerce_steps: list[str], trade_inputs: dict[str, Any] | None,
    ) -> None:
        display_steps = list(steps)
        self._workflow_pending = pending
        self._workflow_recovery_snapshot = {}
        self._workflow_active = True
        self._sync_input_bridge_setting()
        self._workflow_stopping = False
        self._workflow_current = None
        self._workflow_failed_message = ""
        self.workflow_page.begin_workflow(
            display_steps,
            commerce_steps,
            trade_inputs,
        )
        if any(row["step"] == "refresh_recovery" for row in pending):
            self.workflow_page.add_freight_preparation()
        self._switch_page("workflow")
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
            self.workflow_page.mark_step(parent, "running", "准备货运")
        self.workflow_page.mark_step(step, "running", f"正在执行{current['label']}")
        timeout = float(self._preferences.timeout_sec)
        dispatch = str(current["dispatch"])
        inputs = dict(current["inputs"])
        if dispatch in {"trade", "combined_commerce"}:
            player = self._workflow_recovery_snapshot.get("player_data")
            if player is not None and self._workflow_recovery_snapshot.get("recovery_requested") is True:
                inputs["recovery_snapshot"] = deepcopy(player)
        if dispatch == "trade":
            self.requestRunPcTrade.emit(normalize_trade_task_inputs(inputs), timeout)
        elif dispatch == "passenger":
            self.requestRunPcPassenger.emit(dict(current["inputs"]), timeout)
        elif dispatch == "combined_commerce":
            self.requestRunPcCombinedCommerce.emit(inputs, timeout)
        elif dispatch == "battle":
            # The backend owns per-battle/transition budgets. A daily plan may
            # legitimately last longer than the GUI's generic task timeout.
            self.requestRunPcBattle.emit(dict(current["inputs"]), 0.0)
        else:
            self.requestRunPcTask.emit(
                str(current["task_ref"]), dict(current["inputs"]), current["label"], timeout
            )

    def _run_small_task_player_data(self, inputs: object) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.small_tasks_page.show_player_data_error("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_PLAYER_DATA_REFRESH_TASK_REF
        self.small_tasks_page.begin_player_data_run()
        self.requestRunPcTask.emit(
            PC_PLAYER_DATA_REFRESH_TASK_REF,
            inputs,
            "刷新用户数据",
            float(self._preferences.timeout_sec),
        )

    def _run_small_task_team_recommendation(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
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
            float(self._preferences.timeout_sec),
        )

    def _read_small_task_affix_refresh(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.small_tasks_page.affix_refresh_panel.defer_auto_read()
            self.statusBar().showMessage("当前有任务正在运行，空闲后自动识别词缀界面。")
            return
        self._small_task_active_ref = PC_AFFIX_REFRESH_READ_TASK_REF
        self.small_tasks_page.begin_affix_refresh("read")
        self.requestRunPcTask.emit(PC_AFFIX_REFRESH_READ_TASK_REF, {"config": {}}, "读取词缀替换界面", 0.0)

    def _run_small_task_affix_refresh(self, inputs: dict[str, Any]) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.statusBar().showMessage("当前有任务正在运行，请稍后再试。")
            return
        try:
            # Recollect the visible target and budget; replacement is disabled.
            inputs = self.small_tasks_page.affix_refresh_panel.collect_inputs()
            self._settings.save_affix_refresh_inputs(inputs)
        except (ValueError, KeyError) as exc:
            self.small_tasks_page.show_affix_refresh_error(str(exc))
            return
        self._small_task_active_ref = PC_AFFIX_REFRESH_TASK_REF
        self.small_tasks_page.begin_affix_refresh("run", inputs)
        self.requestRunPcTask.emit(PC_AFFIX_REFRESH_TASK_REF, {"config": inputs}, "刷新词缀", 0.0)

    def _on_affix_refresh_cancel_requested(self, _payload: dict[str, Any]) -> None:
        if self._small_task_active_ref in {PC_AFFIX_REFRESH_TASK_REF, PC_AFFIX_REFRESH_READ_TASK_REF}:
            self.small_tasks_page.affix_refresh_panel.mark_stopping()

    def _run_small_task_eternal_scuffle(self, inputs: dict[str, Any]) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.small_tasks_page.show_eternal_scuffle_error("当前有任务正在运行，请稍后再试。")
            return
        try:
            self._settings.save_eternal_scuffle_inputs(inputs)
        except ValueError as exc:
            self.small_tasks_page.show_eternal_scuffle_error(str(exc))
            return
        self._small_task_active_ref = PC_ETERNAL_SCUFFLE_TASK_REF
        self.small_tasks_page.begin_eternal_scuffle_run(inputs)
        self.requestRunPcTask.emit(PC_ETERNAL_SCUFFLE_TASK_REF, dict(inputs), "无垠乱斗", float(self._preferences.timeout_sec))


    def _run_deep_dive_scan(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.statusBar().showMessage("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF
        self.small_tasks_page.begin_consciousness_deep_dive_scan()
        # The scanner owns its bounded time budget, independently of the GUI timeout.
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF, {}, "识海深潜布局扫描", 0.0
        )

    def _run_deep_dive_loop(self, inputs):
        panel=self.small_tasks_page.consciousness_deep_dive_panel
        if self._busy or self._workflow_active or self._small_task_active_ref:
            panel.show_loop_error('当前有任务正在运行，请稍后再试。')
            return
        self._small_task_active_ref=PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF
        panel.begin_loop()
        # A sustained loop must not inherit the GUI's short per-task timeout.
        self.requestRunPcTask.emit(PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF,dict(inputs),'识海深潜循环',0.)

    def _run_deep_dive_planned_run(self, inputs: dict[str, Any]) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.statusBar().showMessage("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF
        self.small_tasks_page.begin_consciousness_deep_dive_planned_run()
        # In-level execution owns its scan/planning budgets and the round safety limit.
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF,
            dict(inputs),
            "识海深潜关卡内自动运行",
            0.0,
        )

    def _run_deep_dive_plan(self, inputs: dict[str, Any]) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.statusBar().showMessage("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF
        self.small_tasks_page.begin_consciousness_deep_dive_plan()
        # The offline planner owns its search budget and shares the runner's cancellation.
        self.requestRunPcTask.emit(
            PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF,
            dict(inputs),
            "识海深潜移动规划",
            0.0,
        )


    def _run_small_task_consciousness_deep_dive_capture(
        self, inputs: dict[str, Any]
    ) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
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
            float(self._preferences.timeout_sec),
        )

    def _run_small_task_consciousness_deep_dive_sensitivity_probe(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
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
            float(self._preferences.timeout_sec),
        )

    def _read_small_task_player_data_cache(self) -> None:
        if self._busy or self._workflow_active or self._small_task_active_ref:
            self.small_tasks_page.show_player_data_error("当前有任务正在运行，请稍后再试。")
            return
        self._small_task_active_ref = PC_PLAYER_DATA_LATEST_TASK_REF
        self.requestRunPcTask.emit(
            PC_PLAYER_DATA_LATEST_TASK_REF,
            {},
            "读取用户数据缓存",
            float(self._preferences.timeout_sec),
        )

    def _show_small_task_error(self, task_ref: str, message: str) -> None:
        page = self.small_tasks_page
        handlers = {
            PC_AFFIX_REFRESH_TASK_REF: page.show_affix_refresh_error,
            PC_AFFIX_REFRESH_READ_TASK_REF: page.show_affix_refresh_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF: page.show_consciousness_deep_dive_planned_run_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF: page.consciousness_deep_dive_panel.show_loop_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF: page.show_consciousness_deep_dive_scan_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF: page.show_consciousness_deep_dive_plan_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_CAPTURE_TASK_REF: page.show_consciousness_deep_dive_capture_error,
            PC_CONSCIOUSNESS_DEEP_DIVE_SENSITIVITY_PROBE_TASK_REF: page.show_consciousness_deep_dive_sensitivity_probe_error,
            PC_TEAM_RECOMMENDATION_TASK_REF: page.show_team_recommendation_error,
            PC_ETERNAL_SCUFFLE_TASK_REF: page.show_eternal_scuffle_error,
        }
        handlers.get(task_ref, page.show_player_data_error)(message)

    def _stop_workflow(self) -> None:
        if not self._workflow_active:
            return
        self._workflow_pending.clear()
        self._workflow_stopping = True
        self.workflow_page.set_stopping()
        if self._workflow_current is not None:
            current_steps = (
                ("trade", "passenger") if self._workflow_current.get("dispatch") == "combined_commerce"
                else (str(self._workflow_current["step"]),)
            )
            for step in current_steps:
                self.workflow_page.mark_step(step, "stopping", "等待执行端停止确认")
            parent = str(self._workflow_current.get("parent") or "")
            if parent:
                self.workflow_page.mark_step(parent, "stopping", "等待执行端停止确认")
        # The dispatch signal may still be queued when the user presses Stop.
        # A pending current task must receive cancel even before busyChanged.
        if self._busy or self._workflow_current is not None:
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
        self._sync_input_bridge_setting()
        self._workflow_stopping = False
        self._workflow_pending.clear()
        self._workflow_current = None
        self.workflow_page.finish_workflow(success=success, message=message)
        self.trade_page.set_busy(False)
        self.passenger_page.set_busy(False)
        self.battle_page.set_busy(False)
        self._workflow_failed_message = ""
        self.small_tasks_page.affix_refresh_panel.set_runner_busy(self._busy)


    def _on_workflow_cancel_requested(self, _payload: dict[str, Any]) -> None:
        if self._workflow_active and not self._workflow_stopping:
            self._workflow_pending.clear()
            self._workflow_stopping = True
            self.workflow_page.set_stopping()
            if self._workflow_current is not None:
                steps = (
                    ("trade", "passenger") if self._workflow_current.get("dispatch") == "combined_commerce"
                    else (str(self._workflow_current["step"]),)
                )
                for step in steps:
                    self.workflow_page.mark_step(step, "stopping", "任务已请求停止，等待执行端确认")

    def _run_pc_battle(self, inputs: object, _unused_timeout: float) -> None:
        self.requestRunPcBattle.emit(inputs, 0.0)


    def _on_tasks_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.small_tasks_page.set_available_tasks(rows)
        self.statusBar().showMessage(f"PC 任务列表已加载：{len(rows)} 个")

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
            elif kind in {"trade", "trade_preview"}:
                summary = trade_result_summary(row)
                city_path = summary.get("city_path") or []
                summary_text = " -> ".join(str(city) for city in city_path) or "跑商任务"
                result_text = str(summary.get("expected_profit") or "--")
                type_label = "跑商试算" if kind == "trade_preview" else "跑商"
            else:
                summary_text = str(row.get("task_name") or row.get("task_ref") or "任务")
                result_text = str(extract_final_result(row).get("reason") or "查看执行详情")
                type_label = "客货运组合" if kind == "combined_commerce" else "任务"
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
            self.run_detail.show_text(render_result_text(visible_rows[row]))

    @staticmethod
    def _history_kind(row: dict[str, Any]) -> str:
        task_identity = " ".join(
            str(row.get(key) or "")
            for key in ("task_name", "task_ref", "task_id")
        ).lower()
        if "preview_trade_plan_pc" in task_identity:
            return "trade_preview"
        if "passenger" in task_identity:
            return "passenger"
        if "battle" in task_identity:
            return "battle"
        if "auto_cycle_trade" in task_identity:
            return "trade"
        if "combined_commerce" in task_identity:
            return "combined_commerce"
        return "task"


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
                self.trade_preview_page.begin_preview(payload)
            elif kind == "trade_run":
                self.trade_page.begin_run(payload)
                if self._workflow_active:
                    self.workflow_page.set_active_progress_cid("trade", extract_run_id(payload))
                if not self._workflow_active:
                    self._open_trade_editor()
            elif kind == "passenger_run":
                self.passenger_page.begin_run(payload)
                if self._workflow_active:
                    self.workflow_page.set_active_progress_cid("passenger", extract_run_id(payload))
                if not self._workflow_active:
                    self._open_passenger_editor()
            elif kind == "combined_commerce_run":
                self.trade_page.begin_run(payload)
                self.passenger_page.begin_run(payload)
                if self._workflow_active:
                    cid = extract_run_id(payload)
                    self.workflow_page.set_active_progress_cid("trade", cid)
                    self.workflow_page.set_active_progress_cid("passenger", cid)
            elif kind == "battle_run":
                self.battle_page.begin_run(payload)
                if not self._workflow_active:
                    self._open_battle_editor()
            else:
                self.run_detail.show_text(pretty_json(payload))
        else:
            self.run_detail.show_text(pretty_json(payload))

    def _on_run_updated(self, payload: dict[str, Any]) -> None:
        if (self._active_game_name == PC_GAME_NAME and
                self._small_task_active_ref == PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF):
            planned_run = extract_deep_dive_planned_run_result(payload)
            if planned_run:
                self.small_tasks_page.consciousness_deep_dive_panel.apply_planned_run_progress(planned_run)
        if self._active_game_name == PC_GAME_NAME and self._active_kind == "trade_preview":
            self.trade_preview_page.update_run(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("trade_"):
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
        self.workflow_page.update_freight_market_result(trade)
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
        if succeeded:
            missing = next((kind for kind, child in (("trade", trade), ("passenger", passenger))
                            if child.get("success") is not True or child.get("status") != "completed"), "")
            if result.get("success") is not True or missing:
                result = {**result, "failure_stage": f"{missing or 'trade'}_result",
                          "reason": "组合任务未返回完整的货运与客运完成结果。"}
                succeeded = False
        for kind, child, label in (
            ("trade", trade, "货运"),
            ("passenger", passenger, "客运"),
        ):
            if child.get("success") is True and str(child.get("status") or "") == "completed":
                self.workflow_page.mark_step(kind, "success", f"{label}已完成")
        if succeeded:
            self._workflow_current = None
            return

        failure_stage = str(result.get("failure_stage") or "")
        failed_kind = "passenger" if failure_stage.startswith("passenger") else "trade"
        if failure_stage == "preflight":
            order = str(result.get("order") or "trade_first")
            failed_kind = "passenger" if order == "passenger_first" else "trade"
        reason = self._combined_reason_text(result)
        self.workflow_page.mark_step(failed_kind, "failed", reason)
        if self._workflow_current is not None:
            self._workflow_current["step"] = failed_kind
        for kind, child in (("trade", trade), ("passenger", passenger)):
            if kind != failed_kind and not child:
                self.workflow_page.mark_step(kind, "skipped", "组合交接未通过，后续任务未执行")
        if failure_stage == "preflight":
            QMessageBox.warning(self, "客货运组合不可执行", reason)
        self._abort_workflow(reason)

    def _on_task_finished(self, payload: dict[str, Any]) -> None:
        if self._closing:
            return
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
                    if extract_status(payload) == "success" and player_data_result.get("success") is not False:
                        self.trade_preview_page.invalidate_preview("账号数据已更新")
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
            if task_ref in {PC_AFFIX_REFRESH_TASK_REF, PC_AFFIX_REFRESH_READ_TASK_REF} and small_task_owns_result:
                affix_result = player_data_result.get("affix_refresh")
                runner_status = extract_status(payload)
                if isinstance(affix_result, dict):
                    affix_result = dict(affix_result)
                    if runner_status != "success":
                        affix_result.update(success=False, status="cancelled" if runner_status == "cancelled" else "failed",
                                            framework_interrupted=True,
                                            message=payload.get("error") or "任务已取消或中断。",
                                            reason=affix_result.get("reason") or payload.get("error") or "任务已取消或中断。")
                    self.small_tasks_page.apply_affix_refresh_result(affix_result)
                else:
                    self.small_tasks_page.show_affix_refresh_error(
                        str(player_data_result.get("reason") or player_data_result.get("error") or payload.get("error") or "任务未返回完整刷新词缀结果。"),
                        cancelled=runner_status == "cancelled")
            if task_ref == PC_ETERNAL_SCUFFLE_TASK_REF and small_task_owns_result:
                scuffle_result = player_data_result.get("eternal_scuffle")
                status = extract_status(payload)
                if isinstance(scuffle_result, dict):
                    scuffle_result = dict(scuffle_result)
                    if status != "success":
                        scuffle_result.update(success=False, status="cancelled" if status == "cancelled" else "failed")
                    self.small_tasks_page.apply_eternal_scuffle_result(scuffle_result)
                else:
                    self.small_tasks_page.show_eternal_scuffle_error(
                        str(player_data_result.get("reason") or player_data_result.get("error") or payload.get("error") or "任务未返回完整无垠乱斗结果。"),
                        cancelled=status == "cancelled",
                    )
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
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_SCAN_TASK_REF and small_task_owns_result:
                scan_result = extract_deep_dive_scan_result(payload)
                runner_status = extract_status(payload)
                if runner_status != "success":
                    scan_result.update(
                        success=False,
                        status="cancelled" if runner_status == "cancelled" else "blocked",
                        reason=scan_result.get("reason") or payload.get("error") or "任务已取消或中断",
                    )
                if scan_result:
                    self.small_tasks_page.apply_consciousness_deep_dive_scan_result(scan_result)
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_scan_error("任务未返回布局扫描结果。")
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_PLANNED_RUN_TASK_REF and small_task_owns_result:
                planned_run_result = extract_deep_dive_planned_run_result(payload)
                runner_status = extract_status(payload)
                if runner_status != "success":
                    planned_run_result.update(
                        success=False,
                        status="cancelled" if runner_status == "cancelled" else "blocked",
                        reason=planned_run_result.get("reason") or payload.get("error") or "任务已取消或中断",
                    )
                if planned_run_result:
                    self.small_tasks_page.apply_consciousness_deep_dive_planned_run_result(planned_run_result)
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_planned_run_error("任务未返回关卡内自动运行结果。")
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_PLAN_TASK_REF and small_task_owns_result:
                plan_result = extract_deep_dive_plan_result(payload)
                runner_status = extract_status(payload)
                if runner_status != "success":
                    plan_result.update(
                        success=False,
                        status=("cancelled" if runner_status == "cancelled" else
                                plan_result.get("status") if plan_result.get("status") in
                                {"blocked", "search_budget_exhausted", "cancelled"} else "blocked"),
                        reason=plan_result.get("reason") or payload.get("error") or "任务已取消或中断",
                    )
                if plan_result:
                    self.small_tasks_page.apply_consciousness_deep_dive_plan_result(plan_result)
                else:
                    self.small_tasks_page.show_consciousness_deep_dive_plan_error("任务未返回移动规划结果。")
                self._small_task_active_ref = ""
            if task_ref == PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF:
                loop_result=player_data_result.get('deep_dive_loop')
                panel=self.small_tasks_page.consciousness_deep_dive_panel
                if isinstance(loop_result,dict):
                    panel.apply_loop_result(loop_result)
                else:
                    panel.show_loop_error(str(payload.get('error') or player_data_result.get('error') or '任务已取消或中断'))
                self._small_task_active_ref=''
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
                self.trade_preview_page.finish_preview(payload)
            elif kind == "trade_run":
                self.trade_page.finish_run(payload)
                self.workflow_page.update_freight_market_result(trade_result_summary(payload))
            elif kind == "passenger_run":
                self.passenger_page.finish_run(payload)
            elif kind == "combined_commerce_run":
                self._finish_combined_pages(payload, extract_final_result(payload))
            elif kind == "battle_run":
                self.battle_page.finish_run(payload)
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
            if self._workflow_stopping:
                self.workflow_page.mark_step(step, "cancelled", "执行端已确认任务结束")
                if current.get("parent"):
                    self.workflow_page.mark_step(str(current["parent"]), "cancelled", "执行端已确认任务结束")
                self._workflow_current = None
            elif step == "refresh_recovery":
                item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
                if item.get("task_ref") != PC_PLAYER_DATA_REFRESH_TASK_REF:
                    self._abort_workflow("货运前数据刷新返回了不匹配的任务结果。")
                elif payload.get("gui_timeout_cancelled"):
                    self._abort_workflow("货运前数据刷新超时，流程已停止。")
                elif not succeeded:
                    self._abort_workflow(str(
                        result.get("reason") or result.get("error")
                        or payload.get("error") or "货运前数据刷新未成功完成。"
                    ))
                else:
                    try:
                        player = validate_recovery_refresh(result, current["inputs"]["profile_sections"])
                    except ValueError as exc:
                        self._abort_workflow(str(exc))
                    else:
                        self._workflow_recovery_snapshot = {
                            "cid": extract_run_id(payload),
                            "player_data": player,
                            "recovery_requested": True,
                        }
                        recovery = player.get("recovery", {})
                        water = recovery.get("sparkling_water")
                        fatigue = player["status"]["fatigue"]
                        bento = recovery.get("bento_count")
                        resources = [f"疲劳 {fatigue['current']}/{fatigue['max']}"]
                        if water is not None:
                            resources.append(f"气泡水 {water['remaining_free_uses']}/{water['daily_free_limit']} 次")
                        if bento is not None:
                            resources.append(f"便当 {bento['count']} 份")
                        self.workflow_page.mark_step(
                            step, "success",
                            f"货运前数据已刷新：{'，'.join(resources)}，数据已保存"
                            f"（CID: {extract_run_id(payload)}）",
                        )
                        self._workflow_current = None
            elif str(current.get("dispatch") or "") == "combined_commerce":
                self._finish_combined_workflow_result(result=result, succeeded=succeeded)
            elif step == "battle":
                try:
                    gui_item = payload.get("gui_item") if isinstance(payload.get("gui_item"), dict) else {}
                    if gui_item.get("task_ref") != PC_BATTLE_TASK_REF:
                        raise ValueError("作战返回了不匹配的任务结果。")
                    if (status != "success" or payload.get("gui_timeout_cancelled")
                            or payload.get("gui_soft_stop_requested")):
                        raise ValueError(str(result.get("reason") or payload.get("error") or "作战执行异常或被取消。"))
                    state, detail = battle_continuation_detail(result)
                except ValueError as exc:
                    self._abort_workflow(str(exc))
                else:
                    self.workflow_page.mark_step(step, state, detail)
                    self.workflow_page.append_log(detail)
                    self._workflow_current = None
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
        if self._closing:
            return
        self.statusBar().showMessage(f"任务异常：{payload.get('error', '')}")
        stage = str(payload.get("stage") or "")
        if (stage == "cancel_task" and self._small_task_active_ref in
                {PC_AFFIX_REFRESH_TASK_REF, PC_AFFIX_REFRESH_READ_TASK_REF}):
            self.small_tasks_page.affix_refresh_panel.cancel_failed(str(payload.get("error") or "未知错误"))
        if stage in {"list_tasks", "list_runs"}:
            self.run_detail.show_text(pretty_json(payload))
            return
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
        if self._workflow_active:
            self._abort_workflow(str(payload.get("error") or "任务执行异常"))
        if stage == "run_pc_battle":
            self.battle_page.show_failure(payload)
        elif stage == "run_pc_passenger":
            self.passenger_page.show_failure(payload)
        elif stage == "run_pc_combined_commerce":
            self.trade_page.show_failure(payload)
            self.passenger_page.show_failure(payload)
        elif stage == "preview_pc_trade":
            self.trade_preview_page.show_failure(payload)
        elif stage == "run_pc_trade":
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
        elif self._active_game_name == PC_GAME_NAME and self._active_kind.startswith("trade_"):
            page = self.trade_preview_page if self._active_kind == "trade_preview" else self.trade_page
            page.show_failure(payload)
        elif self._active_game_name == PC_GAME_NAME and self._active_kind == "combined_commerce_run":
            self.trade_page.show_failure(payload)
            self.passenger_page.show_failure(payload)
        self.run_detail.show_text(pretty_json(payload))

    def _on_busy_changed(self, busy: bool) -> None:
        if self._closing:
            return
        self._busy = bool(busy)
        self._sync_input_bridge_setting()
        if not busy:
            self._active_game_name = ""
            self._active_kind = ""
            if self._small_task_active_ref:
                self._show_small_task_error(
                    self._small_task_active_ref,
                    "任务已停止，但没有返回可用结果。",
                )
                self._small_task_active_ref = ""
        locked = busy or self._workflow_active
        self.trade_page.set_busy(locked)
        self.passenger_page.set_busy(locked)
        self.battle_page.set_busy(locked)
        if self._workflow_active:
            self.workflow_page._set_editing_enabled(False)
        self.small_tasks_page.set_runner_busy(busy)
        if not self._workflow_active:
            self.workflow_page.run_button.setEnabled(not busy)
        if self._workflow_active and not busy:
            if (
                not self._workflow_stopping
                and self._workflow_current is not None
                and self._workflow_current.get("step") == "refresh_recovery"
            ):
                self._abort_workflow("货运前数据刷新已停止，但没有返回可用结果。")
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
            self.workflow_page.append_log(message, persist=False)

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
            timeout_sec=self._preferences.timeout_sec,
            history_limit=self._preferences.history_limit,
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
        if self._close_ready:
            self._hotkeys.close()
            super().closeEvent(event)
            return
        if self._closing:
            if self._battle_shutdown_pending:
                box = QMessageBox(self)
                box.setWindowTitle("作战仍在停止中")
                box.setText("尚未确认游戏原生自动已关闭。继续等待可让执行端完成收尾；强制退出可能让游戏继续自动作战。")
                wait = box.addButton("继续等待", QMessageBox.ButtonRole.RejectRole)
                force = box.addButton("强制退出", QMessageBox.ButtonRole.DestructiveRole)
                box.setDefaultButton(wait)
                box.exec()
                if box.clickedButton() is force:
                    self._battle_shutdown_pending = False
                    logger.warning("[NativeBattleStop] user_forced_exit=true native_stopped=unconfirmed")
                    self.statusBar().showMessage("正在强制退出；原生自动停止未确认，请在游戏内手动检查。")
                    self._bridge.request_shutdown(force=True)
                    self.requestBridgeClose.emit()
                    self._shutdown_watchdog.start(5000)
            event.ignore()
            return
        if self._busy or self._workflow_active or self._small_task_active_ref:
            box = QMessageBox(self)
            box.setWindowTitle("任务仍在运行")
            box.setText("PC 自动化任务仍在运行。")
            stay = box.addButton("继续运行界面", QMessageBox.ButtonRole.RejectRole)
            cancel_and_exit = box.addButton("取消任务并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            if box.clickedButton() is stay:
                event.ignore()
                return
            if box.clickedButton() is not cancel_and_exit:
                event.ignore()
                return
        battle_running = (
            getattr(self._bridge, "current_task_ref", "") == PC_BATTLE_TASK_REF
            or self._active_kind == "battle_run"
            or (self._workflow_current is not None and self._workflow_current.get("step") == "battle")
        )
        self._save_preferences()
        self._hotkeys.close()
        event.ignore()
        self._closing = True
        self._workflow_active = False
        self._workflow_pending.clear()
        self.centralWidget().setEnabled(False)
        self.setWindowTitle(f"{self._base_window_title} — 正在退出")
        self.statusBar().showMessage("正在停止后台任务并释放资源，请稍候…")
        logger.info("[GuiShutdown] phase=gui_close_requested")
        self._battle_shutdown_pending = battle_running
        if not battle_running:
            self._bridge.request_shutdown()
        else:
            self.statusBar().showMessage("正在关闭游戏原生自动并收尾；再次关闭窗口可选择强制退出。")
        self._shutdown_watchdog.start()
        self.requestBridgeClose.emit()

    @Slot()
    def _on_bridge_closed(self) -> None:
        self._bridge_closed = True
        logger.info("[GuiShutdown] phase=bridge_thread_quit_requested")
        self._bridge_thread.quit()

    @Slot(str)
    def _on_bridge_close_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"后台退出未完成，正在重试释放资源：{message}")
        self._shutdown_watchdog.start(5000)

    @Slot()
    def _on_shutdown_timeout(self) -> None:
        logger.warning("[GuiShutdown] phase=gui_shutdown_watchdog bridge_closed=%s", self._bridge_closed)
        self.statusBar().showMessage("后台退出超时，正在回收本次运行的工作进程…")
        if self._bridge_closed:
            self._bridge_thread.quit()
            return
        if self._battle_shutdown_pending:
            logger.warning("[NativeBattleStop] waiting_for_native_stop=true force_exit=false")
            self.statusBar().showMessage("作战仍在软停止，尚未确认原生自动关闭；继续等待，再次关闭可选择强制退出。")
            self._shutdown_watchdog.start(60000)
            return
        try:
            self._bridge.request_shutdown(force=True)
        except Exception as exc:
            logger.exception("[GuiShutdown] phase=force_worker_failed")
            self.statusBar().showMessage(f"工作进程回收失败：{exc}")
        # The queued close slot runs after pending bridge IPC unwinds.
        self.requestBridgeClose.emit()
        self._shutdown_watchdog.setInterval(5000)

    @Slot()
    def _on_bridge_thread_finished(self) -> None:
        if not self._closing:
            return
        if not self._bridge_closed:
            logger.error("[GuiShutdown] phase=thread_exited_without_worker_confirmation")
            self.statusBar().showMessage("后台线程意外退出，尚未确认工作进程已释放。")
            return
        self._shutdown_watchdog.stop()
        self._close_ready = True
        logger.info("[GuiShutdown] phase=qt_thread_finished gui_close_ready=true")
        self.close()


def create_main_window() -> ResonanceMainWindow:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before creating ResonanceMainWindow.")
    return ResonanceMainWindow()

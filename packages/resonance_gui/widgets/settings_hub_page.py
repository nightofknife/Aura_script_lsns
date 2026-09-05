"""Dedicated settings workspace for the Resonance GUI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from packages.aura_game.executable_locator import (
    find_registry_executables,
    validate_executable_path,
)

from ..config_repository import ResonanceConfigRepository


GAME_DISPLAY_NAME = "雷索纳斯"
GAME_EXECUTABLE_NAME = "雷索纳斯.exe"


class SettingsHubPage(QWidget):
    backRequested = Signal()
    settingsSaved = Signal()

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._build_ui()
        self.load_values()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        heading = QHBoxLayout()
        back = QPushButton("← 返回任务流程", self)
        back.clicked.connect(self.backRequested.emit)
        title = QLabel("设置", self)
        title.setObjectName("workflowTitle")
        heading.addWidget(back)
        heading.addWidget(title)
        heading.addStretch(1)
        root.addLayout(heading)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.categories = QListWidget(self)
        self.categories.setObjectName("settingsCategories")
        self.categories.setFixedWidth(190)
        for label in ("游戏与启动", "执行设置", "日志与历史", "更新", "关于"):
            self.categories.addItem(label)
        body.addWidget(self.categories)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_game_page())
        self.stack.addWidget(self._placeholder("执行设置", "流程失败即停止；运行时锁定参数快照。"))
        self.stack.addWidget(self._placeholder("日志与历史", "日志与运行历史沿用现有本机存储。"))
        self.stack.addWidget(self._placeholder("更新", "启动检查更新与便携更新逻辑保持不变。"))
        self.stack.addWidget(self._placeholder("关于", "AURA 雷索纳斯控制台\n开发中"))
        self.categories.currentRowChanged.connect(self.stack.setCurrentIndex)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        actions = QHBoxLayout()
        self.save_button = QPushButton("保存设置", self)
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save_values)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self._cancel)
        actions.addWidget(self.save_button)
        actions.addWidget(cancel)
        actions.addStretch(1)
        note = QLabel("设置仅保存在本机", self)
        note.setProperty("caption", True)
        actions.addWidget(note)
        root.addLayout(actions)
        self.save_result = QLabel("", self)
        self.save_result.setWordWrap(True)
        root.addWidget(self.save_result)
        self.categories.setCurrentRow(0)

    def _build_game_page(self) -> QWidget:
        page = QFrame(self)
        page.setObjectName("workflowPanel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        title = QLabel("设置 · 游戏与启动", page)
        title.setObjectName("workflowTitle")
        layout.addWidget(title)

        program = QFrame(page)
        program.setObjectName("linenInset")
        program_layout = QVBoxLayout(program)
        program_layout.addWidget(self._section("游戏程序", program))
        path_row = QHBoxLayout()
        self.executable_path = QLineEdit(program)
        self.executable_path.setPlaceholderText("选择雷索纳斯 PC 客户端程序")
        self.executable_path.textChanged.connect(self._reset_detection_status)
        browse = QPushButton("浏览…", program)
        browse.clicked.connect(self._browse_executable)
        detect = QPushButton("检测", program)
        detect.clicked.connect(self._detect_executable)
        path_row.addWidget(QLabel("游戏程序路径", program))
        path_row.addWidget(self.executable_path, 1)
        path_row.addWidget(browse)
        path_row.addWidget(detect)
        program_layout.addLayout(path_row)
        self.detect_result = QLabel("未检测", program)
        self.detect_result.setProperty("caption", True)
        self.detect_result.setWordWrap(True)
        self.executable_path.setToolTip("浏览选择或检测成功后自动保存；手动输入后请点击检测或保存设置")
        program_layout.addWidget(self.detect_result)
        layout.addWidget(program)

        two_columns = QHBoxLayout()
        startup = QFrame(page)
        startup.setObjectName("linenInset")
        startup_layout = QVBoxLayout(startup)
        startup_layout.addWidget(self._section("进入主界面", startup))
        startup_form = QFormLayout()
        self.launch_if_needed = QCheckBox("游戏未运行时自动启动", startup)
        self.window_timeout = QSpinBox(startup)
        self.window_timeout.setRange(1, 600)
        self.window_timeout.setSuffix(" 秒")
        self.settle_rounds = QSpinBox(startup)
        self.settle_rounds.setRange(1, 3600)
        startup_form.addRow("启动行为", self.launch_if_needed)
        startup_form.addRow("窗口等待上限", self.window_timeout)
        startup_form.addRow("识别轮次", self.settle_rounds)
        startup_layout.addLayout(startup_form)
        two_columns.addWidget(startup, 1)

        close = QFrame(page)
        close.setObjectName("linenInset")
        close_layout = QVBoxLayout(close)
        close_layout.addWidget(self._section("关闭游戏", close))
        close_form = QFormLayout()
        self.close_mode = QComboBox(close)
        self.close_mode.addItem("正常关闭，失败时结束进程", True)
        self.close_mode.addItem("仅正常关闭", False)
        self.close_timeout = QSpinBox(close)
        self.close_timeout.setRange(0, 120)
        self.close_timeout.setSuffix(" 秒")
        self.close_on_failure = QCheckBox("流程失败时仍执行关闭游戏", close)
        close_form.addRow("关闭方式", self.close_mode)
        close_form.addRow("等待退出时间", self.close_timeout)
        close_form.addRow("失败清理", self.close_on_failure)
        close_layout.addLayout(close_form)
        two_columns.addWidget(close, 1)
        layout.addLayout(two_columns)

        advanced = QPushButton("高级参数  ›", page)
        advanced.setObjectName("quietButton")
        advanced.setCheckable(True)
        advanced.setToolTip("调整通常无需修改的底层运行参数")
        advanced.toggled.connect(self._toggle_advanced)
        layout.addWidget(advanced)
        self.advanced_button = advanced
        self.advanced_panel = QFrame(page)
        self.advanced_panel.setObjectName("linenInset")
        advanced_form = QFormLayout(self.advanced_panel)
        self.trade_arrival_timeout = QSpinBox(self.advanced_panel)
        self.trade_arrival_timeout.setRange(1, 240)
        self.trade_arrival_timeout.setSuffix(" 分钟")
        self.trade_arrival_timeout.setToolTip(
            "超过该时间仍未识别到站按钮或城市主页时，货运任务判定为到站超时"
        )
        advanced_form.addRow("货运到站等待上限", self.trade_arrival_timeout)
        self.advanced_panel.hide()
        layout.addWidget(self.advanced_panel)
        layout.addStretch(1)
        return page

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        self.advanced_button.setText("高级参数  ﹀" if expanded else "高级参数  ›")

    @staticmethod
    def _section(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setObjectName("sectionTitle")
        return label

    def _placeholder(self, title: str, text: str) -> QWidget:
        page = QFrame(self)
        page.setObjectName("workflowPanel")
        layout = QVBoxLayout(page)
        heading = QLabel(title, page)
        heading.setObjectName("workflowTitle")
        body = QLabel(text, page)
        body.setWordWrap(True)
        body.setProperty("caption", True)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch(1)
        return page

    def _browse_executable(self) -> None:
        current = self.executable_path.text().strip()
        start = str(Path(current).parent) if current else ""
        path, _ = QFileDialog.getOpenFileName(self, "选择游戏程序", start, "程序 (*.exe);;所有文件 (*)")
        if path:
            self.executable_path.setText(path)
            self._save_game_path(path)

    def _save_game_path(self, value: str, *, message: str = "游戏路径已保存") -> bool:
        path = validate_executable_path(value, executable_name=GAME_EXECUTABLE_NAME)
        if path is None:
            self._set_detection_status("路径不存在或不是雷索纳斯.exe，未保存", "warning")
            return False
        self.executable_path.setText(str(path))
        try:
            self._settings.set_value("game/executable_path", str(path))
            self._settings.sync_checked()
        except OSError as exc:
            self._set_detection_status(f"游戏路径保存失败：{exc}", "warning")
            return False
        self._set_detection_status(message, "success")
        return True

    def _detect_executable(self) -> None:
        entered = self.executable_path.text().strip()
        if entered:
            self._save_game_path(entered, message="用户路径验证通过，已保存")
            return

        matches = find_registry_executables(
            display_name_fragment=GAME_DISPLAY_NAME,
            executable_name=GAME_EXECUTABLE_NAME,
        )
        if not matches:
            self._set_detection_status("注册表中未找到游戏，请使用“浏览…”选择", "warning")
            return
        suffix = "" if len(matches) == 1 else f"（找到 {len(matches)} 个安装记录，已使用第一个）"
        self._save_game_path(str(matches[0]), message=f"已从注册表检测到游戏并保存{suffix}")

    def _reset_detection_status(self) -> None:
        self._set_detection_status("未检测", "")

    def _set_detection_status(self, text: str, status: str) -> None:
        self.detect_result.setText(text)
        self.detect_result.setProperty("status", status)
        self.detect_result.style().unpolish(self.detect_result)
        self.detect_result.style().polish(self.detect_result)

    def load_values(self) -> None:
        self.executable_path.setText(str(self._settings.value("game/executable_path", "") or ""))
        self.launch_if_needed.setChecked(self._bool_value("game/launch_if_not_running", True))
        self.window_timeout.setValue(int(self._settings.value("game/window_timeout_sec", 90)))
        self.settle_rounds.setValue(int(self._settings.value("game/max_settle_rounds", 300)))
        force = self._bool_value("game/force_after_timeout", True)
        self.close_mode.setCurrentIndex(0 if force else 1)
        self.close_timeout.setValue(int(self._settings.value("game/graceful_timeout_sec", 10)))
        self.close_on_failure.setChecked(self._bool_value("workflow/close_on_failure", False))
        trade_inputs = self._settings.load_trade_inputs()
        self.trade_arrival_timeout.setValue(
            max(int(trade_inputs.get("arrival_timeout_seconds", 3600)) // 60, 1)
        )

    def save_values(self) -> None:
        path = self.executable_path.text().strip()
        if path and not self._save_game_path(path):
            self.save_result.setText("设置未全部保存，请检查游戏路径及保存提示。")
            return
        try:
            if not path:
                self._settings.set_value("game/executable_path", "")
            self._settings.set_value("game/launch_if_not_running", self.launch_if_needed.isChecked())
            self._settings.set_value("game/window_timeout_sec", self.window_timeout.value())
            self._settings.set_value("game/max_settle_rounds", self.settle_rounds.value())
            self._settings.set_value("game/force_after_timeout", bool(self.close_mode.currentData()))
            self._settings.set_value("game/graceful_timeout_sec", self.close_timeout.value())
            self._settings.set_value("workflow/close_on_failure", self.close_on_failure.isChecked())
            trade_inputs = self._settings.load_trade_inputs()
            trade_inputs["arrival_timeout_seconds"] = self.trade_arrival_timeout.value() * 60
            self._settings.save_trade_inputs(trade_inputs)
            self._settings.sync_checked()
        except OSError as exc:
            self.save_result.setText(f"设置保存失败：{exc}")
            return
        self.save_result.setText("设置已保存")
        self.settingsSaved.emit()

    def startup_inputs(self) -> dict[str, object]:
        return {
            "executable_path": self.executable_path.text().strip() or None,
            "launch_if_not_running": self.launch_if_needed.isChecked(),
            "window_timeout_sec": self.window_timeout.value(),
            "max_settle_rounds": self.settle_rounds.value(),
            "round_interval_sec": 1.0,
        }

    def close_inputs(self) -> dict[str, object]:
        return {
            "graceful_timeout_sec": self.close_timeout.value(),
            "force_after_timeout": bool(self.close_mode.currentData()),
        }

    def close_on_failure_enabled(self) -> bool:
        return self.close_on_failure.isChecked()

    def trade_arrival_timeout_seconds(self) -> int:
        return self.trade_arrival_timeout.value() * 60

    def _cancel(self) -> None:
        self.load_values()
        self.save_result.clear()
        self.backRequested.emit()

    def _bool_value(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

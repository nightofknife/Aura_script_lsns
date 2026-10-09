"""Dedicated PC auto-trade workspace."""

from __future__ import annotations

from typing import Any, Mapping
from decimal import Decimal

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QAbstractItemView,
    QAbstractButton,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config_repository import (
    DEFAULT_PC_TRADE_CITY_IDS,
    PC_TRADE_CITY_OPTIONS,
    TRADE_PREVIEW_INPUT_KEYS,
    ResonanceConfigRepository,
)
from ..logic import (
    validate_trade_goods_investment_level,
    TradeProgressState,
    expected_profit_per_fatigue,
    extract_run_id,
    extract_status,
    pretty_json,
    reduce_trade_progress,
    route_product_lines,
    trade_result_summary,
    trade_goods_investment_detail,
    normalize_trade_task_inputs,
    average_book_profit_text,
)
from ..trade_catalog import TradeProductGroup, load_trade_product_groups, trade_product_ids
from .compact_parameter_grid import CompactParameterGrid
from .disclosure_section import DisclosureSection
from .toggle_button import ToggleButton as QCheckBox


class CityPrestigeDialog(QDialog):
    def __init__(
        self,
        default_level: int = 20,
        overrides: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appRoot")
        self.setWindowTitle("城市声望设置")
        self.setModal(True)
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("默认城市声望", self))
        default_row.addStretch(1)
        self.default_prestige = self._prestige_spin(allow_default=False)
        self.default_prestige.setValue(max(1, min(int(default_level), 20)))
        default_row.addWidget(self.default_prestige)
        layout.addLayout(default_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        normalized_overrides = dict(overrides or {})
        self.city_prestige: dict[str, QSpinBox] = {}
        rows_per_column = (len(PC_TRADE_CITY_OPTIONS) + 1) // 2
        for index, (city_id, city_name) in enumerate(PC_TRADE_CITY_OPTIONS):
            section = index // rows_per_column
            row = index % rows_per_column
            column = section * 2
            label = QLabel(city_name, self)
            field = self._prestige_spin(allow_default=True)
            field.setValue(max(0, min(int(normalized_overrides.get(city_id, 0)), 20)))
            self.city_prestige[city_id] = field
            grid.addWidget(label, row, column)
            grid.addWidget(field, row, column + 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            parent=self,
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        restore_button = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        save_button.setText("保存")
        cancel_button.setText("取消")
        restore_button.setText("全部使用默认")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        restore_button.clicked.connect(self._restore_defaults)
        layout.addWidget(buttons)

    @staticmethod
    def _prestige_spin(*, allow_default: bool) -> QSpinBox:
        field = QSpinBox()
        field.setRange(0 if allow_default else 1, 20)
        if allow_default:
            field.setSpecialValueText("默认")
        field.setFixedWidth(88)
        return field

    def _restore_defaults(self) -> None:
        self.default_prestige.setValue(20)
        for field in self.city_prestige.values():
            field.setValue(0)

    def prestige_value(self) -> dict[str, Any]:
        return {
            "default": self.default_prestige.value(),
            "overrides": {
                city_id: field.value()
                for city_id, field in self.city_prestige.items()
                if field.value() > 0
            },
        }


class ProductUnlockDialog(QDialog):
    def __init__(
        self,
        groups: tuple[TradeProductGroup, ...],
        unlocked_product_ids: set[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appRoot")
        self.setWindowTitle("商品解锁设置")
        self.setModal(True)
        self.resize(760, 660)
        self.setMinimumSize(620, 520)
        self._groups = groups
        self._all_product_ids = set(trade_product_ids(groups))
        self._items_by_product_id: dict[str, list[QTreeWidgetItem]] = {}
        self._buttons_by_product_id: dict[str, list[QCheckBox]] = {}
        self._city_buttons: dict[str, QCheckBox] = {}
        self._city_items: list[QTreeWidgetItem] = []
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索城市或商品")
        self.search.textChanged.connect(self._filter_items)
        toolbar.addWidget(self.search, 1)
        unlock_all = QPushButton("全部解锁", self)
        lock_all = QPushButton("全部设为未解锁", self)
        unlock_all.clicked.connect(lambda: self._set_all_products(True))
        lock_all.clicked.connect(lambda: self._set_all_products(False))
        toolbar.addWidget(unlock_all)
        toolbar.addWidget(lock_all)
        layout.addLayout(toolbar)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["城市 / 商品", "解锁状态", "已解锁数量"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.header().resizeSection(1, 116)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)

        enabled = self._all_product_ids if unlocked_product_ids is None else set(unlocked_product_ids)
        for group in groups:
            city_item = QTreeWidgetItem([group.city_name, ""])
            city_item.setFlags(city_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable & ~Qt.ItemFlag.ItemIsAutoTristate)
            city_item.setData(0, Qt.ItemDataRole.UserRole, {"city_id": group.city_id})
            self.tree.addTopLevelItem(city_item)
            self._city_items.append(city_item)
            city_button = QCheckBox("整城解锁", self.tree)
            city_button.setToolTip(f"切换{group.city_name}全部声望商品；共享商品状态会同步到其它城市。")
            self._city_buttons[group.city_id] = city_button
            self.tree.setItemWidget(city_item, 1, city_button)
            city_button.toggled.connect(lambda checked, city=city_item: self._set_city_products(city, checked))
            for product in group.products:
                item = QTreeWidgetItem([product.name, ""])
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(0, Qt.ItemDataRole.UserRole, {"product_id": product.product_id})
                item.setToolTip(0, f"商品 ID: {product.product_id}")
                city_item.addChild(item)
                button = QCheckBox("已解锁", self.tree)
                button.setChecked(product.product_id in enabled)
                button.setText("已解锁" if button.isChecked() else "未解锁")
                button.setToolTip(f"{product.name}；商品 ID: {product.product_id}；点击切换解锁状态。")
                self.tree.setItemWidget(item, 1, button)
                self._buttons_by_product_id.setdefault(product.product_id, []).append(button)
                button.toggled.connect(lambda checked, product_id=product.product_id: self._on_product_toggled(product_id, checked))
                self._items_by_product_id.setdefault(product.product_id, []).append(item)
            self._update_city_count(city_item)

        self.summary = QLabel(self)
        self.summary.setProperty("caption", True)
        layout.addWidget(self.summary)
        self._update_summary()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def unlocked_product_ids(self) -> set[str]:
        return {
            product_id
            for product_id, buttons in self._buttons_by_product_id.items()
            if buttons and buttons[0].isChecked()
        }

    def _set_all_products(self, checked: bool) -> None:
        self._updating = True
        try:
            for buttons in self._buttons_by_product_id.values():
                for button in buttons:
                    button.setChecked(checked)
                    button.setText("已解锁" if checked else "未解锁")
            for city_item in self._city_items:
                self._update_city_count(city_item)
        finally:
            self._updating = False
        self._update_summary()

    def _on_product_toggled(self, product_id: str, checked: bool) -> None:
        if self._updating:
            return
        if product_id:
            self._updating = True
            try:
                for button in self._buttons_by_product_id.get(product_id, []):
                    button.setChecked(checked)
                    button.setText("已解锁" if checked else "未解锁")
                for city_item in self._city_items:
                    self._update_city_count(city_item)
            finally:
                self._updating = False
        else:
            for city_item in self._city_items:
                self._update_city_count(city_item)
        self._update_summary()

    def _set_city_products(self, city: QTreeWidgetItem, checked: bool) -> None:
        if self._updating:
            return
        for index in range(city.childCount()):
            payload = city.child(index).data(0, Qt.ItemDataRole.UserRole)
            self._on_product_toggled(str(payload["product_id"]), checked)

    def _update_city_count(self, city_item: QTreeWidgetItem) -> None:
        checked = sum(
            self._buttons_by_product_id[str(city_item.child(index).data(0, Qt.ItemDataRole.UserRole)["product_id"])][0].isChecked()
            for index in range(city_item.childCount())
        )
        city_item.setText(2, f"{checked}/{city_item.childCount()}")
        city_id = str(city_item.data(0, Qt.ItemDataRole.UserRole)["city_id"])
        button = self._city_buttons[city_id]
        previous = button.blockSignals(True)
        button.setChecked(checked == city_item.childCount() and checked > 0)
        button.setText("整城已解锁" if button.isChecked() else "整城解锁")
        button.blockSignals(previous)

    def _update_summary(self) -> None:
        self.summary.setText(
            f"已解锁 {len(self.unlocked_product_ids())} / {len(self._all_product_ids)} 个声望商品"
        )

    def _filter_items(self, text: str) -> None:
        keyword = str(text or "").strip().lower()
        for city_item in self._city_items:
            city_matches = keyword in city_item.text(0).lower()
            visible_children = 0
            for index in range(city_item.childCount()):
                child = city_item.child(index)
                product_matches = keyword in child.text(0).lower()
                hidden = bool(keyword and not city_matches and not product_matches)
                child.setHidden(hidden)
                visible_children += int(not hidden)
            city_item.setHidden(bool(keyword and not city_matches and visible_children == 0))
            if keyword and visible_children:
                city_item.setExpanded(True)


class TradePage(QWidget):
    startRequested = Signal(object, float)
    previewRequested = Signal(object, float)
    cancelRequested = Signal()
    refreshTargetRequested = Signal()

    def __init__(
        self, settings: ResonanceConfigRepository, parent: QWidget | None = None,
        *, preview_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.preview_mode = bool(preview_mode)
        self.start_city: QComboBox | None = None
        self.start_button: QPushButton | None = None
        self.preview_button: QPushButton | None = None
        self.tabs: QTabWidget | None = None
        self._settings = settings
        self._progress = TradeProgressState()
        self._current_cid = ""
        self._busy = False
        self._target_ready = False
        self._elapsed_seconds = 0
        self._last_inputs: dict[str, Any] = {}
        self._last_result: dict[str, Any] = {}
        self._current_plan: dict[str, Any] = {}
        self._plan_inputs: dict[str, Any] = {}
        self._active_mode = ""
        self._route_statuses: dict[int, str] = {}
        self._end_city_constraint_available = True
        self._product_groups = load_trade_product_groups()
        self._all_product_ids = set(trade_product_ids(self._product_groups))
        self._unlocked_product_ids: set[str] | None = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._build_ui()
        self._set_parameter_help()
        self.set_inputs(self._load_inputs())
        self.set_busy(False)

    def _set_parameter_help(self) -> None:
        descriptions = {
            "fatigue_budget": "本次货运的规划疲劳上限；固定线路的起点导航消耗也计入预算。",
            "cargo_capacity": "规划时使用的货舱总容量，请填写游戏中实际可用的容量。",
            "book_usage": "不用书、有限书数或不限书数；不限书数不会跳过单本收益阈值。",
            "book_budget": "本次完整货运最多使用的进货书数量，不是每座城市的额度。",
            "book_policy": "收益优先按新增收益选货；满仓优先按装载目标选货，仍受额度和收益阈值约束。",
            "negotiation_policy": "选择是否协商价格；快速模式要求协商，其余模式按此选项执行。",
            "target_profit": "填写预计目标收益，以万为单位，最多四位小数；不可达时只显示原因，不执行路线。",
            "automatic_end": "自动选择规划终点；关闭后可用城市按钮指定一个或多个允许的终点。",
            "reposition_to_route": "当前城市不是固定线路起点时先导航过去；关闭则报错停止，导航疲劳计入本次预算。",
            "auto_sparkling_water": "到达最后一个符合条件的城市时按实际疲劳和剩余免费次数喝水；运行前需刷新恢复数据。",
            "base_fatigue_reserve": "气泡水恢复时保留的基础疲劳下限，避免恢复后低于此值。",
            "auto_bento": "最终清仓后使用已勾选的便当，按下面的优先级尝试；完成便当收尾后才算货运完成。",
            "auto_pickup": "行车过程中启用沿途拣货，会增加行车中的操作。",
            "use_fatigue_medicine": "行车任务允许使用疲劳药；默认关闭，开启后仍受使用上限限制。",
            "fatigue_medicine_max_uses": "本次行车允许使用疲劳药的次数上限；仅在允许使用疲劳药时生效。",
            "auto_cape_island_investment": "货运到达蜃息岛时尝试投资；不会为投资额外改变货运线路。",
            "auto_rubbish_recycling": "到达符合条件的城市时尝试垃圾回收，不为回收额外改变线路。",
            "bargain_rates": "每次砍价的成功率，以逗号分隔的 bps 数值填写；10000 表示 100%。",
            "raise_rates": "每次抬价的成功率，以逗号分隔的 bps 数值填写；10000 表示 100%。",
            "bargain_step": "每次砍价的降价幅度，100 个 bps 等于 1%。",
            "raise_step": "每次抬价的涨价幅度，100 个 bps 等于 1%。",
        }
        for name, description in descriptions.items():
            getattr(self, name).setToolTip(description)
        for key, button in self.bento_type_checks.items():
            button.setToolTip("允许使用工作餐恢复疲劳。" if key == "work_meals" else "允许使用爱心便当恢复疲劳。")

    def _load_inputs(self) -> dict[str, Any]:
        if self.preview_mode:
            values = self._settings.load_trade_preview_inputs()
            return {key: values[key] for key in TRADE_PREVIEW_INPUT_KEYS if key in values}
        return self._settings.load_trade_inputs()

    def _save_inputs(self, inputs: Mapping[str, Any]) -> None:
        if self.preview_mode:
            self._settings.save_trade_preview_inputs(
                {key: inputs[key] for key in TRADE_PREVIEW_INPUT_KEYS if key in inputs}
            )
        else:
            self._settings.save_trade_inputs(dict(inputs))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_status_band())

        self.parameter_panel = self._build_parameter_panel()
        self.execution_panel = self._build_execution_panel()
        if self.preview_mode:
            self.tabs = QTabWidget(self)
            self.tabs.addTab(self.parameter_panel, "规划参数")
            self.tabs.addTab(self.execution_panel, "计算结果")
            root.addWidget(self.tabs, 1)
        else:
            splitter = QSplitter(Qt.Orientation.Horizontal, self)
            splitter.addWidget(self.parameter_panel)
            splitter.addWidget(self.execution_panel)
            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, False)
            splitter.setSizes([320, 820])
            root.addWidget(splitter, 1)
        root.addWidget(self._build_action_bar())

    def _build_status_band(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("statusBand")
        if self.preview_mode:
            grid = QGridLayout(band)
            grid.setContentsMargins(18, 10, 18, 10)
            grid.setHorizontalSpacing(16)
            self.target_value = None
            for index, (name, caption, value) in enumerate((
                ("city_value", "起始城市", "--"),
                ("snapshot_value", "市场快照", "--"),
                ("run_status_value", "任务状态", "待命"),
                ("cid_value", "CID", "--"),
                ("elapsed_value", "运行时长", "00:00"),
            )):
                cell = QHBoxLayout()
                label = self._status_pair(cell, caption, value)
                label.setMinimumWidth(0)
                label.setWordWrap(True)
                setattr(self, name, label)
                grid.addLayout(cell, index // 2, index % 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            return band
        layout = QHBoxLayout(band)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(28)
        self.target_value = self._status_pair(layout, "目标窗口", "检查中")
        self.city_value = self._status_pair(layout, "当前城市", "--")
        self.snapshot_value = self._status_pair(layout, "市场快照", "--")
        self.run_status_value = self._status_pair(layout, "任务状态", "待命")
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
        value_label.setMinimumWidth(72)
        box.addWidget(caption_label)
        box.addWidget(value_label)
        layout.addLayout(box)
        return value_label

    def _build_parameter_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("parameterPanel")
        panel.setMinimumWidth(0)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        form_stack = QVBoxLayout(content)
        form_stack.setContentsMargins(0, 8, 4, 8)
        form_stack.setSpacing(12)

        mode_row = QGridLayout()
        self.mode_buttons: dict[str, QPushButton] = {}
        self.mode_group = QButtonGroup(content)
        self.mode_group.setExclusive(True)
        for index, (mode, caption) in enumerate((("profit", "收益模式"), ("quick", "快速模式"),
                              ("fixed", "固定线路"), ("target", "指定收益"))):
            button = QCheckBox(caption, content)
            button.setCheckable(True)
            button.setProperty("cityOption", True)
            self.mode_group.addButton(button)
            button.setToolTip({"profit": "在疲劳与全任务用书额度内优先预计收益。", "quick": "固定优先满仓和必须协商；单本收益阈值仍然生效。", "fixed": "按有序线路访问，预算内自动继续；不设置执行次数。", "target": "填写正的目标收益；达标表示预计收益，不代表实测现金收益。"}[mode])
            self.mode_buttons[mode] = button
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            mode_row.addWidget(button, index // 2, index % 2)
            button.toggled.connect(self._sync_mode_controls)
        self.mode_buttons["profit"].setChecked(True)
        form_stack.addLayout(mode_row)
        self.mode_note = QLabel("", content)
        self.mode_note.setWordWrap(True)
        self.mode_note.setProperty("caption", True)
        form_stack.addWidget(self.mode_note)
        form_stack.addWidget(QLabel("规划", content))

        self.common_parameters = CompactParameterGrid(content)
        common_form = self.common_parameters
        self.fatigue_budget = self._spin(0, 100000)
        self.cargo_capacity = self._spin(1, 100000)
        self.book_budget = self._spin(0, 100000)
        self.book_usage = QComboBox(content)
        for caption, value in (("不用书", "none"), ("限量用书", "finite"), ("无限用书", "unlimited")):
            self.book_usage.addItem(caption, value)
        self.book_usage.currentIndexChanged.connect(self._sync_book_controls)
        self.book_policy = QComboBox(content)
        self.book_policy.addItem("收益优先", "profit")
        self.book_policy.addItem("优先满仓", "fill")
        self.negotiation_policy = QComboBox(content)
        for caption, value in (("自动协商", "auto"), ("必须协商", "required"), ("不协商", "disabled")):
            self.negotiation_policy.addItem(caption, value)
        self._retained_book_policy = "profit"
        self._retained_negotiation_policy = "auto"
        self.arrival_timeout_minutes = self._spin(1, 240)
        self.arrival_timeout_minutes.setParent(content)
        self.arrival_timeout_minutes.hide()
        self.arrival_timeout_minutes.setSuffix(" 分钟")
        self.arrival_timeout_minutes.setToolTip(
            "超过该时间仍未识别到站按钮或城市主页时，当前跑商任务判定为到站超时"
        )
        self.start_city = QComboBox(content)
        self.start_city.currentIndexChanged.connect(self._sync_actions)
        self.start_city.setToolTip("仅试算使用；正式运行重新识别游戏当前城市。固定线路首站由线路列表决定。")
        self.city_selector = self._build_city_selector(content)
        self.nonfixed_panel = QWidget(content)
        nonfixed_form = QFormLayout(self.nonfixed_panel)
        nonfixed_form.setContentsMargins(0, 0, 0, 0)
        nonfixed_form.addRow(self.city_selector)
        self.end_city_selector = self._build_end_city_selector(content)
        nonfixed_form.addRow("终点城市", self.end_city_selector)
        self.fixed_panel = self._build_fixed_route_selector(content)
        self.target_profit = QDoubleSpinBox(content)
        self.target_profit.setRange(0, 100_000_000)
        self.target_profit.setDecimals(4)
        self.target_profit.setSuffix(" 万")
        self.target_profit.setSpecialValueText("请填写目标收益")
        self.target_label = QLabel("目标收益", content)
        self.end_city_notice = QLabel(
            "终点约束暂不可编辑，已保留原有选择。",
            content,
        )
        self.end_city_notice.setProperty("status", "warning")
        self.end_city_notice.setWordWrap(True)
        self.end_city_notice.hide()
        common_form.add_field("疲劳预算", self.fatigue_budget)
        common_form.add_field("货舱容量", self.cargo_capacity)
        common_form.add_field("用书额度", self.book_usage)
        self.book_budget_label = QLabel("有限书数", content)
        common_form.add_field(self.book_budget_label, self.book_budget)
        self.book_profit_threshold = QDoubleSpinBox(content)
        self.book_profit_threshold.setRange(0, 1_000_000_000)
        self.book_profit_threshold.setDecimals(4)
        self.book_profit_threshold.setSuffix(" 万")
        self.book_profit_threshold.setToolTip("单本新增收益下限，所有用书模式都生效；50 万提交为 500000。")
        common_form.add_field("单本收益阈值", self.book_profit_threshold)
        common_form.add_field("用书策略", self.book_policy)
        common_form.add_field("协商策略", self.negotiation_policy)
        common_form.add_field("试算当前城市", self.start_city)
        common_form.set_field_visible(self.start_city, self.preview_mode)
        common_form.add_field(self.target_label, self.target_profit)
        form_stack.addWidget(common_form)
        form_stack.addWidget(self.fixed_panel)
        form_stack.addWidget(self.nonfixed_panel)
        form_stack.addWidget(self.end_city_notice)

        self.recovery_heading = QLabel("恢复与附加", content)
        self.recovery_heading.hide()
        self.recovery_section = DisclosureSection("恢复与附加行为", content)
        form_stack.addWidget(self.recovery_section)
        recovery_layout = self.recovery_section.body_layout
        self.auto_sparkling_water = QCheckBox("自动喝气泡水", content)
        self.auto_bento = QCheckBox("自动吃便当", content)
        self.auto_bento.toggled.connect(self._sync_bento_type_checks)
        recovery_options = QHBoxLayout()
        recovery_options.addWidget(self.auto_sparkling_water)
        recovery_options.addWidget(self.auto_bento)
        recovery_options.addStretch(1)
        recovery_layout.addLayout(recovery_options)
        self.base_fatigue_reserve = self._spin(0, 2147483647)
        self.base_fatigue_reserve.setValue(200)
        self.water_reserve_panel = QWidget(content)
        reserve_form = QFormLayout(self.water_reserve_panel)
        reserve_form.setContentsMargins(0, 0, 0, 0)
        reserve_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.base_fatigue_reserve.setMaximumWidth(220)
        reserve_form.addRow("基础疲劳保留", self.base_fatigue_reserve)
        recovery_layout.addWidget(self.water_reserve_panel)
        self.bento_priority_panel = self._build_bento_priority_panel(content)
        recovery_layout.addWidget(self.bento_priority_panel)
        self.auto_pickup = QCheckBox("自动拣货", content)
        self.use_fatigue_medicine = QCheckBox("行车使用疲劳药", content)
        self.fatigue_medicine_max_uses = self._spin(0, 100000)
        self.allowed_fatigue_medicines: list[str] = []
        self.auto_cape_island_investment = QCheckBox("蜃息岛投资", content)
        self.auto_trade_goods_investment = QCheckBox("自动交易品投资", content)
        self.auto_trade_goods_investment.toggled.connect(self._sync_goods_investment_controls)
        self.trade_goods_investment_panel = QWidget(content)
        investment_form = QFormLayout(self.trade_goods_investment_panel)
        investment_form.setContentsMargins(0, 0, 0, 0)
        investment_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.trade_goods_investment_mode = QSpinBox(self.trade_goods_investment_panel)
        self.trade_goods_investment_mode.setRange(1, 20)
        self.trade_goods_investment_mode.setValue(10)
        investment_form.addRow("目标等级", self.trade_goods_investment_mode)
        self.auto_rubbish_recycling = QCheckBox("自动倒垃圾", content)
        self.additional_options = QWidget(content)
        options_layout = QGridLayout(self.additional_options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(8)
        for index, button in enumerate((self.auto_pickup, self.use_fatigue_medicine,
                       self.auto_cape_island_investment, self.auto_rubbish_recycling,
                       self.auto_trade_goods_investment)):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            options_layout.addWidget(button, index // 2, index % 2, alignment=Qt.AlignmentFlag.AlignLeft)
        recovery_layout.addWidget(self.additional_options)
        recovery_layout.addWidget(self.trade_goods_investment_panel)
        medicine_form = QFormLayout()
        medicine_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.fatigue_medicine_max_uses.setMaximumWidth(220)
        self.medicine_limit_label = QLabel("疲劳药使用上限", content)
        medicine_form.addRow(self.medicine_limit_label, self.fatigue_medicine_max_uses)
        recovery_layout.addLayout(medicine_form)

        if self.preview_mode:
            self.recovery_section.hide()
            self.additional_options.hide()
            self.recovery_heading.hide()
            self.use_fatigue_medicine.hide()
            self.fatigue_medicine_max_uses.hide()
            self.medicine_limit_label.hide()
            self.auto_sparkling_water.hide()
            self.auto_bento.hide()
            self.bento_priority_panel.hide()
            self.water_reserve_panel.hide()
            self.auto_pickup.hide()
            self.auto_cape_island_investment.hide()
            self.auto_trade_goods_investment.hide()
            self.trade_goods_investment_panel.hide()
            self.auto_rubbish_recycling.hide()

        self.advanced_toggle = QToolButton(content)
        self.advanced_toggle.setText("账号与执行参数")
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setProperty("uiDisclosure", True)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        form_stack.addWidget(self.advanced_toggle)
        self.advanced_panel = self._build_advanced_panel(content)
        self.advanced_panel.setVisible(False)
        form_stack.addWidget(self.advanced_panel)
        form_stack.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return panel

    def _build_end_city_selector(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        layout = QGridLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.automatic_end = QCheckBox("自动终点", panel)
        self.automatic_end.setChecked(True)
        self.automatic_end.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.automatic_end.toggled.connect(self._sync_end_city_options)
        layout.addWidget(self.automatic_end, 0, 0, 1, 3, alignment=Qt.AlignmentFlag.AlignLeft)
        self.end_city_checks: dict[str, QPushButton] = {}
        for index, (city_id, name) in enumerate(PC_TRADE_CITY_OPTIONS):
            button = QCheckBox(name, panel)
            button.setCheckable(True)
            button.setProperty("cityOption", True)
            button.setToolTip(f"允许货运在{name}结束；可选择多个终点，规划器从中选择。")
            self.end_city_checks[city_id] = button
            layout.addWidget(button, 1 + index // 3, index % 3)
        return panel

    def _build_fixed_route_selector(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("点击城市追加线路，拖动调整顺序；双击移除。至少两站，可重复访问。", panel)
        note.setWordWrap(True)
        layout.addWidget(note)
        picker = DisclosureSection("添加线路城市", panel)
        grid = QGridLayout()
        for index, (city_id, name) in enumerate(PC_TRADE_CITY_OPTIONS):
            button = QPushButton(name, panel)
            button.clicked.connect(lambda _checked=False, city=city_id: self.add_route_city(city))
            grid.addWidget(button, index // 3, index % 3)
        picker.body_layout.addLayout(grid)
        self.fixed_route = QListWidget(panel)
        self.fixed_route.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.fixed_route.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.fixed_route.setMaximumHeight(160)
        self.fixed_route.itemDoubleClicked.connect(lambda item: self.fixed_route.takeItem(self.fixed_route.row(item)))
        layout.addWidget(self.fixed_route)
        layout.addWidget(picker)
        self.reposition_to_route = QCheckBox("导航到线路起点", panel)
        layout.addWidget(self.reposition_to_route)
        return panel

    def add_route_city(self, city_id: str) -> None:
        names = dict(PC_TRADE_CITY_OPTIONS)
        if city_id not in names:
            raise ValueError("未知线路城市")
        item = QListWidgetItem(names[city_id])
        item.setData(Qt.ItemDataRole.UserRole, city_id)
        self.fixed_route.addItem(item)

    def trade_mode(self) -> str:
        return next((mode for mode, button in self.mode_buttons.items() if button.isChecked()), "profit")

    def _sync_mode_controls(self, *_args: object) -> None:
        if not hasattr(self, "fixed_panel"):
            return
        mode = self.trade_mode()
        self.mode_note.setText({
            "profit": "在疲劳与用书额度内优先预计收益。",
            "quick": "固定使用优先满仓、必须协商；下方锁定项无需设置。",
            "fixed": "按线路顺序访问，预算内继续；可重复访问同一城市。",
            "target": "目标为规划预计收益，不代表实际现金收益。",
        }[mode])
        was_quick = getattr(self, "_was_quick", False)
        if mode == "quick" and not was_quick:
            self._retained_book_policy = str(self.book_policy.currentData())
            self._retained_negotiation_policy = str(self.negotiation_policy.currentData())
        self._was_quick = mode == "quick"
        self.book_policy.setCurrentIndex(self.book_policy.findData("fill" if mode == "quick" else self._retained_book_policy) if was_quick or mode == "quick" else self.book_policy.currentIndex())
        self.negotiation_policy.setCurrentIndex(self.negotiation_policy.findData("required" if mode == "quick" else self._retained_negotiation_policy) if was_quick or mode == "quick" else self.negotiation_policy.currentIndex())
        self.book_policy.setEnabled(mode != "quick" and not self._busy)
        self.negotiation_policy.setEnabled(mode != "quick" and not self._busy)
        self.nonfixed_panel.setVisible(mode != "fixed")
        self.fixed_panel.setVisible(mode == "fixed")
        self.target_label.setVisible(mode == "target")
        self.target_profit.setVisible(mode == "target")
        self.common_parameters.set_field_visible(self.target_profit, mode == "target")
        self._sync_start_city_options()

    def _sync_book_controls(self, *_args: object) -> None:
        finite = self.book_usage.currentData() == "finite"
        self.book_budget.setVisible(finite)
        self.book_budget_label.setVisible(finite)
        self.common_parameters.set_field_visible(self.book_budget, finite)
        self.book_budget.setEnabled(finite and not self._busy)

    def _build_bento_priority_panel(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        self._bento_layout = QVBoxLayout(panel)
        self._bento_layout.setContentsMargins(0, 0, 0, 0)
        self._bento_layout.addWidget(QLabel("便当类型优先级", panel))
        self._bento_rows_layout = QHBoxLayout()
        self._bento_rows_layout.setSpacing(16)
        self._bento_layout.addLayout(self._bento_rows_layout)
        self._bento_order = ["work_meals", "love_bentos"]
        self.bento_type_checks: dict[str, QCheckBox] = {}
        self._bento_rows: dict[str, QWidget] = {}
        self.bento_move_buttons: dict[tuple[str, int], QToolButton] = {}
        for key, label in (("work_meals", "工作餐"), ("love_bentos", "爱心便当")):
            row = QWidget(panel)
            row.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            check = QCheckBox(label, row)
            self.bento_type_checks[key] = check
            check.toggled.connect(self._save_bento_priority)
            layout.addWidget(check)
            for delta, arrow, caption in (
                (-1, Qt.ArrowType.UpArrow, "上移"),
                (1, Qt.ArrowType.DownArrow, "下移"),
            ):
                button = QToolButton(row)
                button.setArrowType(arrow)
                button.setFixedSize(28, 28)
                button.setToolTip(f"{caption}{label}")
                button.setAccessibleName(f"{caption}{label}")
                button.clicked.connect(
                    lambda _checked=False, key=key, delta=delta: self._move_bento_type(key, delta)
                )
                self.bento_move_buttons[key, delta] = button
                layout.addWidget(button)
            self._bento_rows[key] = row
            self._bento_rows_layout.addWidget(row)
        self._bento_rows_layout.addStretch(1)
        self._sync_bento_order()
        return panel

    def _sync_bento_order(self) -> None:
        for index, key in enumerate(self._bento_order):
            self._bento_rows_layout.removeWidget(self._bento_rows[key])
            self._bento_rows_layout.insertWidget(index, self._bento_rows[key])
            self.bento_move_buttons[key, -1].setEnabled(index > 0)
            self.bento_move_buttons[key, 1].setEnabled(index < len(self._bento_order) - 1)

    def _move_bento_type(self, key: str, delta: int) -> None:
        index = self._bento_order.index(key)
        target = index + delta
        if not 0 <= target < len(self._bento_order):
            return
        self._bento_order[index], self._bento_order[target] = (
            self._bento_order[target], self._bento_order[index]
        )
        self._sync_bento_order()
        self._save_bento_priority()

    def _selected_bento_priority(self) -> list[str]:
        return [key for key in self._bento_order if self.bento_type_checks[key].isChecked()]

    def _sync_bento_type_checks(self) -> None:
        selected = self._selected_bento_priority()
        for key, check in self.bento_type_checks.items():
            check.setEnabled(not self.auto_bento.isChecked() or len(selected) > 1 or key not in selected)

    def _save_bento_priority(self, *_args: object) -> None:
        if self.preview_mode:
            return
        self._sync_bento_type_checks()
        values = self._settings.load_trade_inputs()
        values["bento_priority"] = self._selected_bento_priority()
        self._settings.save_trade_inputs(values)

    def _build_advanced_panel(self, parent: QWidget) -> QWidget:
        panel = CompactParameterGrid(parent)
        form = panel
        self.negotiation_max_attempts = self._spin(1, 6)
        self.negotiation_max_attempts.setToolTip(
            "每次买入砍价或卖出抬价分别计数；达到上限仍未满 20% 时按当前价格继续成交"
        )
        self.bargain_rates = QLineEdit(panel)
        self.bargain_rates.setPlaceholderText("5000, 5000")
        self.bargain_step = self._spin(1, 2000)
        self.raise_rates = QLineEdit(panel)
        self.raise_rates.setPlaceholderText("5000, 5000")
        self.raise_step = self._spin(1, 2000)
        self._city_prestige_default = 20
        self._city_prestige_overrides: dict[str, int] = {}
        self.city_prestige_button = QPushButton("设置城市声望", panel)
        self.city_prestige_button.clicked.connect(self._edit_city_prestige)
        self.product_unlock_button = QPushButton("设置商品解锁", panel)
        self.product_unlock_button.clicked.connect(self._edit_product_unlocks)
        if self.preview_mode:
            self.negotiation_max_attempts.setParent(panel)
            self.negotiation_max_attempts.hide()
        else:
            form.add_field("协商尝试上限", self.negotiation_max_attempts)
        form.add_field("砍价成功率(bps)", self.bargain_rates)
        form.add_field("砍价幅度(bps)", self.bargain_step)
        form.add_field("抬价成功率(bps)", self.raise_rates)
        form.add_field("抬价幅度(bps)", self.raise_step)
        form.add_field("城市声望", self.city_prestige_button)
        form.add_field("商品解锁", self.product_unlock_button)
        if not self.preview_mode:
            form.add_field("到站等待上限", self.arrival_timeout_minutes)
        return panel

    def _build_city_selector(self, parent: QWidget) -> QWidget:
        selector = QWidget(parent)
        layout = QVBoxLayout(selector)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.city_selector_toggle = QToolButton(selector)
        self.city_selector_toggle.setText("参与规划城市 · 已选 0 城")
        self.city_selector_toggle.setCheckable(True)
        self.city_selector_toggle.setProperty("uiDisclosure", True)
        self.city_selector_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.city_selector_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.city_selector_toggle.setToolTip("展开城市按钮网格；至少选择两城。当前选项不会因收起而改变。")
        layout.addWidget(self.city_selector_toggle)
        self.city_buttons_panel = QWidget(selector)
        button_layout = QVBoxLayout(self.city_buttons_panel)
        button_layout.setContentsMargins(0, 4, 0, 0)
        self.city_selector_toggle.toggled.connect(self._toggle_city_selector)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.city_checks: dict[str, QPushButton] = {}
        for index, (city_id, city_name) in enumerate(PC_TRADE_CITY_OPTIONS):
            button = QCheckBox(city_name, selector)
            button.setCheckable(True)
            button.setProperty("cityOption", True)
            button.setMinimumHeight(30)
            button.setToolTip(f"城市 ID: {city_id}；点击切换是否参与规划")
            button.toggled.connect(self._sync_city_controls)
            self.city_checks[city_id] = button
            grid.addWidget(button, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        button_layout.addLayout(grid)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        select_all = QPushButton("全选", selector)
        clear_all = QPushButton("清空", selector)
        select_all.setToolTip("选择全部已配置地图和交易所坐标的城市")
        clear_all.setToolTip("清空城市选择")
        select_all.clicked.connect(lambda: self._set_all_cities(True))
        clear_all.clicked.connect(lambda: self._set_all_cities(False))
        actions.addWidget(select_all)
        actions.addWidget(clear_all)
        actions.addStretch(1)
        button_layout.addLayout(actions)
        layout.addWidget(self.city_buttons_panel)
        self.city_buttons_panel.hide()
        return selector

    def _toggle_city_selector(self, expanded: bool) -> None:
        self.city_buttons_panel.setVisible(expanded)
        self.city_selector_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _build_execution_panel(self) -> QWidget:
        self.execution_scroll = QScrollArea(self)
        self.execution_scroll.setWidgetResizable(True)
        self.execution_scroll.setFrameShape(QFrame.Shape.NoFrame)
        panel = QWidget(self.execution_scroll)
        self.execution_scroll.setWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(10)
        heading = QVBoxLayout() if self.preview_mode else QHBoxLayout()
        heading_box = QVBoxLayout()
        section = QLabel("当前方案路径", panel)
        section.setObjectName("pageTitle")
        self.stage_title = QLabel("等待开始", panel)
        self.stage_title.setObjectName("stageTitle")
        heading_box.addWidget(section)
        heading_box.addWidget(self.stage_title)
        heading.addLayout(heading_box)
        heading.addStretch(1)
        self.stage_detail = QLabel("" if self.preview_mode else "目标检查完成后即可开始", panel)
        self.stage_detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.stage_detail.setProperty("caption", True)
        if self.preview_mode:
            self.stage_detail.setWordWrap(True)
        heading.addWidget(self.stage_detail)
        layout.addLayout(heading)

        self.route_tree = QTreeWidget(panel)
        self.route_tree.setMinimumHeight(160)
        self.route_tree.setColumnCount(6)
        self.route_tree.setHeaderLabels(["路线", "计划买入", "疲劳 / 书", "协商", "预计收益", "状态"])
        self.route_tree.setRootIsDecorated(False)
        self.route_tree.setAlternatingRowColors(True)
        self.route_tree.setUniformRowHeights(False)
        self.route_tree.setIconSize(QSize(18, 18))
        self.route_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if self.preview_mode
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        header = self.route_tree.header()
        header.setMinimumSectionSize(42)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(5, 48)
        if self.preview_mode:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(0, 220)
            header.resizeSection(1, 240)
        layout.addWidget(self.route_tree, 3)

        self.result_band = QFrame(panel)
        self.result_band.setObjectName("resultBand")
        result_layout = QVBoxLayout(self.result_band)
        result_layout.setContentsMargins(0, 10, 0, 0)
        result_title = QLabel("方案概览", self.result_band)
        result_title.setObjectName("sectionTitle")
        result_layout.addWidget(result_title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(6)
        self.result_values: dict[str, QLabel] = {}
        self.result_captions: dict[str, QLabel] = {}
        fields = (
            ("status", "方案状态"),
            ("trade_mode", "货运模式"),
            ("planning_status", "规划状态"),
            ("expected_profit", "预计收益"),
            ("fatigue", "预计疲劳"),
            ("reposition_fatigue", "预计定位疲劳（包含在总疲劳内）"),
            ("actual_profit", "实际收益（已确认）"),
            ("actual_fatigue", "实际疲劳（已读取）"),
            ("profit_per_fatigue", "疲劳收益比"),
            ("route", "路线规模"),
            ("books", "进货书"),
            ("negotiations", "协商"),
            ("remaining_fatigue", "剩余疲劳"),
            ("average_book_profit", "平均每本进货书收益"),
        )
        for index, (key, title) in enumerate(fields):
            row, col = divmod(index, 2 if self.preview_mode else 4)
            box = QVBoxLayout()
            box.setSpacing(2)
            caption = QLabel(title, self.result_band)
            caption.setProperty("caption", True)
            caption.setWordWrap(True)
            caption.setMinimumHeight(caption.fontMetrics().height() + 4)
            value = QLabel("--", self.result_band)
            value.setProperty("value", True)
            value.setMinimumHeight(value.fontMetrics().height() + 4)
            if self.preview_mode:
                value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(caption)
            box.addWidget(value)
            grid.addLayout(box, row, col)
            self.result_values[key] = value
            self.result_captions[key] = caption
        self.result_captions["average_book_profit"].hide()
        self.result_values["average_book_profit"].hide()
        result_layout.addLayout(grid)
        self.reason_label = QLabel("", self.result_band)
        self.reason_label.setWordWrap(True)
        self.reason_label.setProperty("status", "error")
        result_layout.addWidget(self.reason_label)

        self.debug_toggle = QToolButton(self.result_band)
        self.debug_toggle.setText("调试详情")
        self.debug_toggle.setCheckable(True)
        self.debug_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.debug_toggle.toggled.connect(self._toggle_debug)
        result_layout.addWidget(self.debug_toggle)
        self.debug_view = QTextBrowser(self.result_band)
        self.debug_view.setMinimumHeight(150)
        self.debug_view.setVisible(False)
        result_layout.addWidget(self.debug_view)
        layout.addWidget(self.result_band, 2)
        return self.execution_scroll

    def _build_action_bar(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("resultBand")
        layout = QHBoxLayout(band)
        layout.setContentsMargins(18, 9, 18, 9)
        self.ready_hint = QLabel("" if self.preview_mode else "正在检查目标窗口", band)
        self.ready_hint.setProperty("caption", True)
        layout.addWidget(self.ready_hint)
        layout.addStretch(1)
        self.cancel_button = QPushButton("停止任务", band)
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.cancel_button.clicked.connect(self.cancelRequested.emit)
        layout.addWidget(self.cancel_button)
        if self.preview_mode:
            self.preview_button = QPushButton("计算方案", band)
            self.preview_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            self.preview_button.setToolTip("更新市场行情并从所选起始城市计算方案；不会操作游戏")
            self.preview_button.clicked.connect(self._request_preview)
            layout.addWidget(self.preview_button)
        else:
            self.start_button = QPushButton("开始跑商", band)
            self.start_button.setObjectName("primaryButton")
            self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            self.start_button.clicked.connect(self._request_start)
            layout.addWidget(self.start_button)
        return band

    @staticmethod
    def _spin(minimum: int, maximum: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setGroupSeparatorShown(True)
        return widget

    def _toggle_advanced(self, visible: bool) -> None:
        self.advanced_panel.setVisible(visible)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def _toggle_debug(self, visible: bool) -> None:
        self.debug_view.setVisible(visible)
        self.debug_toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def _set_all_cities(self, checked: bool) -> None:
        for checkbox in self.city_checks.values():
            checkbox.setChecked(checked)
        self._sync_city_controls()

    def _sync_city_controls(self) -> None:
        if hasattr(self, "city_selector_toggle"):
            self.city_selector_toggle.setText(f"参与规划城市 · 已选 {len(self.selected_city_ids())} 城")
        self._sync_start_city_options()
        self._sync_end_city_options()

    def _edit_city_prestige(self) -> None:
        dialog = CityPrestigeDialog(
            default_level=self._city_prestige_default,
            overrides=self._city_prestige_overrides,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        prestige = dialog.prestige_value()
        self._city_prestige_default = int(prestige["default"])
        self._city_prestige_overrides = {
            str(city_id): int(value)
            for city_id, value in dict(prestige["overrides"]).items()
        }
        self._update_city_prestige_button()
        saved_inputs = self._load_inputs()
        saved_inputs["city_prestige"] = self._city_prestige_payload()
        self._save_inputs(saved_inputs)

    def _city_prestige_payload(self) -> dict[str, Any]:
        return {
            "default": self._city_prestige_default,
            "overrides": dict(self._city_prestige_overrides),
        }

    def _update_city_prestige_button(self) -> None:
        count = len(self._city_prestige_overrides)
        suffix = f"（{count} 个自定义）" if count else ""
        self.city_prestige_button.setText(f"设置城市声望{suffix}")
        self.city_prestige_button.setToolTip(
            f"默认声望：{self._city_prestige_default}；自定义城市：{count}"
        )

    def _edit_product_unlocks(self) -> None:
        dialog = ProductUnlockDialog(
            groups=self._product_groups,
            unlocked_product_ids=self._unlocked_product_ids,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.unlocked_product_ids()
        self._unlocked_product_ids = None if selected == self._all_product_ids else selected
        self._update_product_unlock_button()
        saved_inputs = self._load_inputs()
        saved_inputs["product_unlocks"] = self._product_unlock_payload()
        self._save_inputs(saved_inputs)

    def _product_unlock_payload(self) -> dict[str, Any]:
        if self._unlocked_product_ids is None:
            return {"mode": "all", "product_ids": []}
        return {
            "mode": "only",
            "product_ids": sorted(
                self._unlocked_product_ids,
                key=lambda value: (int(value), value) if value.isdigit() else (2**31 - 1, value),
            ),
        }

    def _update_product_unlock_button(self) -> None:
        total = len(self._all_product_ids)
        enabled = total if self._unlocked_product_ids is None else len(self._unlocked_product_ids)
        suffix = "全部" if enabled == total else f"{enabled}/{total}"
        self.product_unlock_button.setText(f"设置商品解锁（{suffix}）")
        self.product_unlock_button.setToolTip(f"当前已解锁 {enabled} / {total} 个声望商品；普通商品始终可用")

    def _sync_start_city_options(self) -> None:
        if self.start_city is None:
            return
        current_city_id = str(self.start_city.currentData() or "")
        selected = set(dict(PC_TRADE_CITY_OPTIONS)) if self.trade_mode() == "fixed" else set(self.selected_city_ids())
        self.start_city.blockSignals(True)
        self.start_city.clear()
        self.start_city.addItem("请选择起始城市", "")
        for city_id, city_name in PC_TRADE_CITY_OPTIONS:
            if city_id in selected:
                self.start_city.addItem(city_name, city_id)
        index = self.start_city.findData(current_city_id)
        self.start_city.setCurrentIndex(max(index, 0))
        self.start_city.blockSignals(False)
        self._sync_actions()

    def _sync_end_city_options(self) -> None:
        if not hasattr(self, "end_city_checks"):
            return
        selected = set(self.selected_city_ids())
        self.automatic_end.setEnabled(self._end_city_constraint_available and not self._busy)
        for city_id, button in self.end_city_checks.items():
            button.setVisible(not self.automatic_end.isChecked())
            button.setEnabled(city_id in selected and not self.automatic_end.isChecked()
                              and self._end_city_constraint_available and not self._busy)
            if city_id not in selected:
                button.setChecked(False)
        self._sync_actions()

    def selected_city_ids(self) -> list[str]:
        return [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS if self.city_checks[city_id].isChecked()]

    def set_inputs(self, inputs: Mapping[str, Any]) -> None:
        values = (
            {key: inputs[key] for key in TRADE_PREVIEW_INPUT_KEYS if key in inputs}
            if self.preview_mode else dict(inputs)
        )
        self.fatigue_budget.setValue(self._input_integer(values, "fatigue_budget", 700))
        self.cargo_capacity.setValue(self._input_integer(values, "cargo_capacity", 750, minimum=1))
        budget = values.get("book_budget", 0)
        if budget is not None and (type(budget) is not int or budget < 0):
            raise ValueError("进货书必须为非负整数或无限。")
        self.book_budget.setValue(self._input_integer(values, "finite_book_budget", budget if budget is not None else 0))
        enabled = bool(values.get("books_enabled", budget is None or bool(budget)))
        unlimited = bool(values.get("books_unlimited", budget is None))
        self.book_usage.setCurrentIndex(self.book_usage.findData("unlimited" if enabled and unlimited else "finite" if enabled else "none"))
        arrival_timeout_seconds = self._input_integer(values, "arrival_timeout_seconds", 3600, minimum=1)
        self.arrival_timeout_minutes.setValue(max((arrival_timeout_seconds + 59) // 60, 1))
        self.book_profit_threshold.setValue(float(Decimal(str(values.get("book_profit_threshold", 500000))) / Decimal(10000)))
        self.target_profit.setValue(float(Decimal(str(values.get("target_profit") or 0)) / Decimal(10000)))
        self._was_quick = False
        self.book_policy.setCurrentIndex(max(self.book_policy.findData(values.get("book_policy", "profit")), 0))
        self.negotiation_policy.setCurrentIndex(max(self.negotiation_policy.findData(values.get("negotiation_policy", "auto")), 0))
        self.fixed_route.clear()
        for city in values.get("fixed_route_city_ids", []) or []:
            self.add_route_city(str(city))
        self.reposition_to_route.setChecked(bool(values.get("reposition_to_route", False)))
        self.mode_buttons.get(str(values.get("trade_mode", "profit")), self.mode_buttons["profit"]).setChecked(True)
        self.negotiation_max_attempts.setValue(self._input_integer(values, "negotiation_max_attempts", 5, minimum=1, maximum=6))
        self.bargain_rates.setText(self._join_values(values.get("bargain_success_rates_bps", [5000])))
        self.bargain_step.setValue(int(values.get("bargain_step_bps", 1000)))
        self.raise_rates.setText(self._join_values(values.get("raise_success_rates_bps", [5000])))
        self.raise_step.setValue(int(values.get("raise_step_bps", 1000)))
        selected_city_ids = {
            str(city_id)
            for city_id in (values.get("available_city_ids") or DEFAULT_PC_TRADE_CITY_IDS)
        }
        for city_id, checkbox in self.city_checks.items():
            checkbox.setChecked(city_id in selected_city_ids)
        self._sync_start_city_options()
        if self.start_city is not None:
            start_city_index = self.start_city.findData(str(values.get("start_city_id") or ""))
            self.start_city.setCurrentIndex(max(start_city_index, 0))
        required_end_city_ids = [
            str(city_id)
            for city_id in (values.get("required_end_city_ids") or [])
            if str(city_id) in selected_city_ids
        ]
        self._sync_end_city_options()
        self.automatic_end.setChecked(not required_end_city_ids)
        for city, button in self.end_city_checks.items():
            button.setChecked(city in required_end_city_ids)
        prestige = values.get("city_prestige") if isinstance(values.get("city_prestige"), Mapping) else {}
        self._city_prestige_default = max(1, min(int(prestige.get("default", 20)), 20))
        overrides = prestige.get("overrides") if isinstance(prestige.get("overrides"), Mapping) else {}
        self._city_prestige_overrides = {
            city_id: max(1, min(int(overrides[city_id]), 20))
            for city_id, _city_name in PC_TRADE_CITY_OPTIONS
            if city_id in overrides and int(overrides[city_id]) > 0
        }
        self._update_city_prestige_button()
        unlocks = values.get("product_unlocks") if isinstance(values.get("product_unlocks"), Mapping) else {}
        unlock_mode = str(unlocks.get("mode") or "all").strip().lower()
        if unlock_mode == "only":
            self._unlocked_product_ids = {
                str(product_id)
                for product_id in (unlocks.get("product_ids") or [])
                if str(product_id) in self._all_product_ids
            }
        else:
            self._unlocked_product_ids = None
        self._update_product_unlock_button()
        self.auto_sparkling_water.setChecked(bool(values.get("auto_sparkling_water", False)))
        self.auto_bento.setChecked(bool(values.get("auto_bento", False)))
        priority = values.get("bento_priority", ["work_meals", "love_bentos"])
        self._bento_order = [*priority, *(key for key in self.bento_type_checks if key not in priority)]
        for key, check in self.bento_type_checks.items():
            blocked = check.blockSignals(True)
            check.setChecked(key in priority)
            check.blockSignals(blocked)
        self._sync_bento_order()
        self._sync_bento_type_checks()
        self.base_fatigue_reserve.setValue(self._input_integer(values, "base_fatigue_reserve", 200))
        self.auto_pickup.setChecked(bool(values.get("auto_pickup", False)))
        self.use_fatigue_medicine.setChecked(bool(values.get("use_fatigue_medicine", False)))
        self.allowed_fatigue_medicines = list(values.get("allowed_fatigue_medicines") or [])
        self.fatigue_medicine_max_uses.setValue(self._input_integer(values, "fatigue_medicine_max_uses", 4))
        self.auto_cape_island_investment.setChecked(
            bool(values.get("auto_cape_island_investment", True))
        )
        investment_mode = validate_trade_goods_investment_level(values.get("trade_goods_investment_mode", 10))
        self.trade_goods_investment_mode.setValue(investment_mode)
        self.auto_trade_goods_investment.setChecked(bool(values.get("auto_trade_goods_investment", False)))
        self._sync_goods_investment_controls()
        self.auto_rubbish_recycling.setChecked(
            bool(values.get("auto_rubbish_recycling", True))
        )
        self._sync_city_controls()
        self._sync_book_controls()
        self._sync_mode_controls()

    def restore_inputs(self, inputs: Mapping[str, Any]) -> None:
        self.set_inputs(inputs)

    @staticmethod
    def _input_integer(values: Mapping[str, Any], key: str, default: int,
                       *, minimum: int = 0, maximum: int | None = None) -> int:
        value = values.get(key, default)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} 必须为范围内的整数，不能使用布尔或小数。")
        return value

    def collect_inputs(self, *, require_start_city: bool = False) -> dict[str, Any]:
        bargain_rates = self._parse_int_list(self.bargain_rates.text(), "砍价成功率", 0, 10000)
        raise_rates = self._parse_int_list(self.raise_rates.text(), "抬价成功率", 0, 10000)
        selected_city_ids = self.selected_city_ids()
        mode = self.trade_mode()
        if mode != "fixed" and len(selected_city_ids) < 2:
            raise ValueError("参与规划城市至少需要选择两个")
        if self.preview_mode or require_start_city:
            start_city_id = str(self.start_city.currentData() or "") if self.start_city is not None else ""
            if not start_city_id:
                raise ValueError("请选择起始城市")
            if mode != "fixed" and start_city_id not in selected_city_ids:
                raise ValueError("起始城市必须属于参与规划城市")
        required_end_city_ids = None if self.automatic_end.isChecked() else [city for city, button in self.end_city_checks.items() if button.isChecked()]
        if mode != "fixed" and required_end_city_ids == []:
            raise ValueError("请至少指定一个终点，或选择自动终点")
        inputs = {
            "fatigue_budget": self.fatigue_budget.value(),
            "cargo_capacity": self.cargo_capacity.value(),
            "trade_mode": mode,
            "book_budget": None if self.book_usage.currentData() == "unlimited" else self.book_budget.value() if self.book_usage.currentData() == "finite" else 0,
            "book_policy": str(self.book_policy.currentData()),
            "negotiation_policy": str(self.negotiation_policy.currentData()),
            "book_profit_threshold": self._amount_base_units(self.book_profit_threshold),
            "bargain_success_rates_bps": bargain_rates,
            "bargain_step_bps": self.bargain_step.value(),
            "raise_success_rates_bps": raise_rates,
            "raise_step_bps": self.raise_step.value(),
            "available_city_ids": selected_city_ids,
            "required_end_city_ids": required_end_city_ids,
            "city_prestige": self._city_prestige_payload(),
            "product_unlocks": self._product_unlock_payload(),
        }
        if mode == "fixed":
            route = [str(self.fixed_route.item(index).data(Qt.ItemDataRole.UserRole)) for index in range(self.fixed_route.count())]
            if len(route) < 2:
                raise ValueError("固定线路至少需要两个城市")
            inputs.update(fixed_route_city_ids=route, reposition_to_route=self.reposition_to_route.isChecked())
        if mode == "target":
            target = self._amount_base_units(self.target_profit)
            if target <= 0:
                raise ValueError("指定收益模式请填写正的目标收益")
            inputs["target_profit"] = target
        if self.preview_mode or require_start_city:
            inputs["start_city_id"] = start_city_id
        if self.auto_bento.isChecked() and not self._selected_bento_priority():
            raise ValueError("自动吃便当开启时，便当类型至少选择一种。")
        inputs.update({
            "negotiation_max_attempts": self.negotiation_max_attempts.value(),
            "arrival_timeout_seconds": self.arrival_timeout_minutes.value() * 60,
            "auto_sparkling_water": self.auto_sparkling_water.isChecked(),
            "auto_bento": self.auto_bento.isChecked(),
            "bento_priority": self._selected_bento_priority(),
            "base_fatigue_reserve": self.base_fatigue_reserve.value(),
            "auto_pickup": self.auto_pickup.isChecked(),
            "use_fatigue_medicine": self.use_fatigue_medicine.isChecked(),
            "allowed_fatigue_medicines": list(self.allowed_fatigue_medicines),
            "fatigue_medicine_max_uses": self.fatigue_medicine_max_uses.value(),
            "auto_cape_island_investment": self.auto_cape_island_investment.isChecked(),
            "auto_trade_goods_investment": self.auto_trade_goods_investment.isChecked(),
            "trade_goods_investment_mode": self.trade_goods_investment_mode.value(),
            "auto_rubbish_recycling": self.auto_rubbish_recycling.isChecked(),
        })
        return normalize_trade_task_inputs(inputs)

    @staticmethod
    def _amount_base_units(field: QDoubleSpinBox) -> int:
        # Formatting the four-decimal control first avoids binary float multiplication.
        return int(Decimal(format(field.value(), ".4f")) * Decimal(10000))

    def collect_task_inputs(self, *, preview: bool = False) -> dict[str, Any]:
        inputs = self.collect_inputs(require_start_city=preview)
        if not preview:
            inputs.pop("start_city_id", None)
        return inputs

    def collect_ui_state(self) -> dict[str, Any]:
        """Preserve inactive mode fields without dispatching them to a task."""
        state = self.collect_inputs()
        state.update(books_enabled=self.book_usage.currentData() != "none",
                     books_unlimited=self.book_usage.currentData() == "unlimited",
                     finite_book_budget=self.book_budget.value(),
                     available_city_ids=self.selected_city_ids(),
                     required_end_city_ids=None if self.automatic_end.isChecked() else [city for city, button in self.end_city_checks.items() if button.isChecked()],
                     fixed_route_city_ids=[str(self.fixed_route.item(index).data(Qt.ItemDataRole.UserRole)) for index in range(self.fixed_route.count())],
                     reposition_to_route=self.reposition_to_route.isChecked(),
                     target_profit=self._amount_base_units(self.target_profit) or None,
                     book_policy=self._retained_book_policy if self.trade_mode() == "quick" else self.book_policy.currentData(),
                     negotiation_policy=self._retained_negotiation_policy if self.trade_mode() == "quick" else self.negotiation_policy.currentData(),
                     start_city_id=str(self.start_city.currentData() or ""))
        return state

    def save_ui_state(self) -> None:
        self._save_inputs(self.collect_ui_state())

    def _request_start(self) -> None:
        if not self.preview_mode:
            self._request_action(self.startRequested)

    def _request_preview(self) -> None:
        if self.preview_mode:
            self._request_action(self.previewRequested, require_start_city=True)

    def _request_action(self, signal: Signal, *, require_start_city: bool = False) -> None:
        try:
            inputs = self.collect_inputs(require_start_city=require_start_city)
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        self._save_inputs(self.collect_ui_state())
        self._last_inputs = normalize_trade_task_inputs(inputs)
        if self.preview_mode:
            self._last_inputs = {
                key: value for key, value in self._last_inputs.items()
                if key in TRADE_PREVIEW_INPUT_KEYS
            }
        signal.emit(dict(self._last_inputs), 0.0)

    def _sync_goods_investment_controls(self) -> None:
        self.trade_goods_investment_mode.setEnabled(
            not self._busy and self.auto_trade_goods_investment.isChecked()
        )

    def set_target_status(self, payload: Mapping[str, Any]) -> None:
        if self.target_value is None:
            self._sync_actions()
            return
        data = dict(payload)
        target = data.get("target") if isinstance(data.get("target"), Mapping) else {}
        title = str(target.get("title") or target.get("window_title") or "")
        visible = target.get("visible")
        self._target_ready = bool(data.get("ok")) and bool(title or target.get("hwnd")) and visible is not False
        if self._target_ready:
            self.target_value.setText(title or "已连接")
            self.target_value.setProperty("status", "success")
            capture = data.get("capture") if isinstance(data.get("capture"), Mapping) else {}
            profile = self._find_capture_profile(capture)
            profile_label = {
                "performance": "高性能",
                "compatible": "兼容",
            }.get(profile, "自动")
            self.ready_hint.setText(f"PC / WGC {profile_label}模式 / SendInput")
        else:
            self.target_value.setText("未连接")
            self.target_value.setProperty("status", "error")
            self.ready_hint.setText("开始跑商需连接客户端")
        self.target_value.style().unpolish(self.target_value)
        self.target_value.style().polish(self.target_value)
        self._sync_actions()

    @staticmethod
    def _find_capture_profile(payload: Mapping[str, Any]) -> str:
        current: Any = payload
        for _ in range(4):
            if not isinstance(current, Mapping):
                break
            value = str(current.get("capture_profile_effective") or "").strip().lower()
            if value:
                return value
            current = current.get("health")
        return ""

    def begin_run(self, payload: Mapping[str, Any]) -> None:
        self._active_mode = "run"
        self._current_cid = str(payload.get("cid") or extract_run_id(payload))
        self._progress = TradeProgressState(cid=self._current_cid)
        self._route_statuses = {
            index: "pending" for index in range(self.route_tree.topLevelItemCount())
        }
        self._last_result = {}
        self._elapsed_seconds = 0
        self.elapsed_value.setText("00:00")
        self.cid_value.setText(self._short_cid(self._current_cid))
        self.run_status_value.setText("运行中")
        self.stage_title.setText("准备目标")
        self.stage_detail.setText("执行任务将重新确认当前方案")
        self._apply_route_statuses()
        self._elapsed_timer.start()
        self.set_busy(True)
        self._refresh_debug()

    def begin_preview(self, payload: Mapping[str, Any]) -> None:
        if self.tabs is not None:
            self.tabs.setCurrentWidget(self.execution_panel)
        self.route_tree.clear()
        self._route_statuses = {}
        self._current_plan = {}
        self._plan_inputs = {}
        self._last_result = {}
        self._clear_result()
        self.snapshot_value.setText("--")
        self.city_value.setText(self.start_city.currentText() if self.start_city is not None else "--")
        self._active_mode = "preview"
        self._current_cid = str(payload.get("cid") or extract_run_id(payload))
        self._progress = TradeProgressState(cid=self._current_cid)
        self._elapsed_seconds = 0
        self.elapsed_value.setText("00:00")
        self.cid_value.setText(self._short_cid(self._current_cid))
        self.run_status_value.setText("计算中")
        self.stage_title.setText("计算方案")
        self.stage_detail.setText("正在更新行情，期间不会操作游戏")
        self._elapsed_timer.start()
        self.set_busy(True)
        self._refresh_debug()

    def apply_progress(self, event: Mapping[str, Any]) -> None:
        previous_sequence = self._progress.sequence
        self._progress = reduce_trade_progress(self._progress, event, expected_cid=self._current_cid)
        if self._progress.sequence == previous_sequence:
            return
        self.stage_title.setText(self._progress.stage_label)
        self.stage_detail.setText(self._progress_detail())
        if self._progress.current_city:
            self.city_value.setText(self._progress.current_city)
        if self._progress.snapshot_id:
            self.snapshot_value.setText(self._progress.snapshot_id)
        if self._progress.route:
            if self._progress.stage == "planning":
                self._route_statuses = {
                    index: "pending" for index in range(len(self._progress.route))
                }
            self._render_route(self._progress.route)
        if self._progress.leg_index is not None:
            status = self._progress.state
            if self._progress.stage not in {"leg", "arrival"} and status == "completed":
                status = "active"
            self._route_statuses[self._progress.leg_index] = status
            self._apply_route_statuses()
        if self._progress.summary:
            self._render_planning_summary(self._progress.summary)
        self._refresh_debug()

    def update_run(self, payload: Mapping[str, Any]) -> None:
        status = extract_status(payload)
        if status:
            self.run_status_value.setText(self._status_label(status))

    def cancel_requested(self, payload: Mapping[str, Any]) -> None:
        self.run_status_value.setText("超时取消中" if payload.get("timeout") else "取消中")
        self.stage_title.setText("取消中")
        self.cancel_button.setEnabled(False)

    def finish_run(self, payload: Mapping[str, Any]) -> None:
        self._elapsed_timer.stop()
        self._last_result = dict(payload)
        status = extract_status(payload)
        summary = trade_result_summary(payload)
        self._current_plan = dict(summary)
        self._plan_inputs = dict(self._last_inputs)
        business_status = str(summary.get("status") or status)
        self.run_status_value.setText(self._status_label(business_status))
        self.stage_title.setText(self._status_label(business_status))
        self.stage_detail.setText(str(summary.get("reason") or "路线执行结束"))
        if summary.get("snapshot_id"):
            self.snapshot_value.setText(str(summary["snapshot_id"]))
        if summary.get("final_city"):
            self.city_value.setText(str(summary["final_city"]))
        if summary.get("route"):
            self._render_route(summary["route"])
        if business_status in {"success", "completed", "ok"}:
            for index in range(len(summary.get("route") or [])):
                self._route_statuses[index] = "completed"
        self._apply_route_statuses()
        self._render_result(summary)
        self.set_busy(False)
        self._active_mode = ""
        self._refresh_debug()

    def finish_preview(self, payload: Mapping[str, Any]) -> None:
        if self.tabs is not None:
            self.tabs.setCurrentWidget(self.execution_panel)
        self._elapsed_timer.stop()
        self._last_result = dict(payload)
        summary = trade_result_summary(payload)
        runner_status = extract_status(payload)
        business_status = str(summary.get("status") or runner_status)
        failure_status = next(
            (status for status in (runner_status, business_status)
             if status in {"failed", "error", "timeout", "cancelled", "blocked"}),
            "",
        )
        if failure_status:
            reason = str(summary.get("reason") or payload.get("error") or "方案计算未完成")
            self.run_status_value.setText(self._status_label(failure_status))
            self.stage_title.setText(self._status_label(failure_status))
            self.stage_detail.setText(reason)
            self.route_tree.clear()
            self._route_statuses = {}
            self._clear_result()
            self._render_result({**summary, "status": failure_status, "reason": reason, "route": []})
            self.set_busy(False)
            self._active_mode = ""
            self._refresh_debug()
            return
        self._current_plan = dict(summary)
        self._plan_inputs = dict(self._last_inputs)
        no_plan = business_status in {"no_plan", "no_positive_profit_route"} or not summary.get("route")
        if no_plan:
            summary = {**summary, "status": "no_plan"}
            self._current_plan = dict(summary)
        self.run_status_value.setText("无可执行路线" if no_plan else "方案就绪")
        self.stage_title.setText("无可执行路线" if no_plan else "方案已计算")
        self.stage_detail.setText(
            str(summary.get("reason") or "当前规划参数下无可执行路线") if no_plan
            else "行情更新失败，已使用本地市场快照"
            if summary.get("market_source") == "fallback_cache"
            else "行情已更新，本方案使用最新快照"
        )
        if summary.get("snapshot_id"):
            self.snapshot_value.setText(str(summary["snapshot_id"]))
        if summary.get("initial_city"):
            self.city_value.setText(str(summary["initial_city"]))
        self._render_route(summary.get("route") or [])
        self._route_statuses = {
            index: "pending" for index in range(len(summary.get("route") or []))
        }
        self._apply_route_statuses()
        self._render_result(summary)
        self.set_busy(False)
        self._active_mode = ""
        self._refresh_debug()

    def show_history_result(self, payload: Mapping[str, Any]) -> None:
        if self._busy:
            return
        if self.tabs is not None:
            self.tabs.setCurrentWidget(self.execution_panel)
        self._last_result = dict(payload)
        self._current_cid = extract_run_id(payload)
        summary = trade_result_summary(payload)
        self.cid_value.setText(self._short_cid(self._current_cid) or "--")
        self.run_status_value.setText(self._status_label(summary.get("status")))
        self.snapshot_value.setText(str(summary.get("snapshot_id") or "--"))
        self.city_value.setText(str(summary.get("final_city") or summary.get("initial_city") or "--"))
        self.stage_title.setText("历史方案")
        self.stage_detail.setText("历史结果，只读")
        self._render_route(summary.get("route") or [])
        self._route_statuses = {index: "completed" for index in range(len(summary.get("route") or []))}
        self._apply_route_statuses()
        self._render_result(summary)
        self._refresh_debug()

    def show_error(self, error: str | Mapping[str, Any]) -> None:
        self.show_failure(error if isinstance(error, Mapping) else {"error": str(error)})

    def show_failure(self, payload: Mapping[str, Any]) -> None:
        if payload.get("recoverable"):
            return
        self._elapsed_timer.stop()
        self.run_status_value.setText("失败")
        self.stage_title.setText("任务失败")
        self.stage_detail.setText(str(payload.get("error") or "未知错误"))
        self.reason_label.setText(str(payload.get("error") or "未知错误"))
        self.set_busy(False)
        self._active_mode = ""
        self._last_result = dict(payload)
        self._refresh_debug()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (
            self.fatigue_budget,
            self.cargo_capacity,
            self.arrival_timeout_minutes,
            self.auto_sparkling_water,
            self.auto_bento,
            self.bento_priority_panel,
            self.base_fatigue_reserve,
            self.auto_pickup,
            self.auto_cape_island_investment,
            self.auto_trade_goods_investment,
            self.trade_goods_investment_panel,
            self.auto_rubbish_recycling,
            self.advanced_panel,
            self.start_city,
            self.book_usage,
            self.book_profit_threshold,
            self.fixed_panel,
            self.target_profit,
            self.use_fatigue_medicine,
            self.fatigue_medicine_max_uses,
            *self.mode_buttons.values(),
        ):
            if widget is not None:
                widget.setEnabled(not busy)
        for widget in self.city_selector.findChildren(QAbstractButton):
            if widget is not self.city_selector_toggle:
                widget.setEnabled(not busy)
        self._sync_book_controls()
        self._sync_end_city_options()
        self._sync_mode_controls()
        self._sync_goods_investment_controls()
        self._sync_actions()

    def set_end_city_constraint_available(self, available: bool) -> None:
        self._end_city_constraint_available = bool(available)
        self._sync_end_city_options()
        self.end_city_notice.setVisible(not self._end_city_constraint_available)

    def is_busy(self) -> bool:
        return self._busy

    def _sync_actions(self) -> None:
        if self.start_button is not None:
            self.start_button.setEnabled(self._target_ready and not self._busy)
        if self.preview_button is not None:
            self.preview_button.setEnabled(
                self.start_city is not None and bool(self.start_city.currentData()) and not self._busy
            )
        if hasattr(self, "cancel_button"):
            self.cancel_button.setEnabled(
                self._busy and (not self.preview_mode or self._active_mode == "preview")
            )

    def _render_route(self, route: list[dict[str, Any]]) -> None:
        self.route_tree.clear()
        for index, leg in enumerate(route):
            products = ", ".join(route_product_lines(leg)) or "仅迁移"
            fatigue = self._display(leg.get("expected_fatigue_cost"))
            resources = f"疲劳 {fatigue} / 书 {int(leg.get('books_used') or 0)}"
            negotiations = []
            if leg.get("bargain_to_cap"):
                negotiations.append("买入砍价")
            if leg.get("raise_to_cap"):
                negotiations.append("到站抬价")
            item = QTreeWidgetItem(
                [
                    f"{index + 1}. {leg.get('from_city', '--')}  ->  {leg.get('to_city', '--')}",
                    products,
                    resources,
                    " / ".join(negotiations) or "--",
                    self._display(leg.get("expected_profit")),
                    "",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(0, f"{leg.get('from_city', '--')} -> {leg.get('to_city', '--')}")
            item.setToolTip(1, products)
            item.setTextAlignment(5, Qt.AlignmentFlag.AlignCenter)
            self.route_tree.addTopLevelItem(item)
        self._apply_route_statuses()

    def _apply_route_statuses(self) -> None:
        labels = {
            "pending": "待执行",
            "started": "进行中",
            "active": "进行中",
            "completed": "已完成",
            "blocked": "已阻断",
            "failed": "失败",
        }
        pixmaps = {
            "pending": QStyle.StandardPixmap.SP_MediaPause,
            "started": QStyle.StandardPixmap.SP_MediaPlay,
            "active": QStyle.StandardPixmap.SP_MediaPlay,
            "completed": QStyle.StandardPixmap.SP_DialogApplyButton,
            "blocked": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "failed": QStyle.StandardPixmap.SP_MessageBoxCritical,
        }
        active_item: QTreeWidgetItem | None = None
        for row in range(self.route_tree.topLevelItemCount()):
            item = self.route_tree.topLevelItem(row)
            status = self._route_statuses.get(row, "pending")
            item.setText(5, "")
            item.setIcon(5, self.style().standardIcon(pixmaps.get(status, pixmaps["pending"])))
            item.setToolTip(5, labels.get(status, "待执行"))
            item.setTextAlignment(5, Qt.AlignmentFlag.AlignCenter)
            active = status in {"started", "active"}
            background = QBrush(QColor("#e1e6d8")) if active else QBrush()
            for column in range(self.route_tree.columnCount()):
                item.setBackground(column, background)
            if active:
                active_item = item
        if active_item is not None:
            self.route_tree.scrollToItem(active_item)

    def _render_planning_summary(self, summary: Mapping[str, Any]) -> None:
        self._render_overview(summary, route=self._progress.route)

    def _render_result(self, summary: Mapping[str, Any]) -> None:
        status = str(summary.get("status") or "")
        self.result_values["status"].setText(self._status_label(status))
        self.result_values["status"].setProperty(
            "status",
            "success" if status in {"success", "completed", "ok", "planned"} else "warning" if status in {"blocked", "stopped", "no_plan"} else "error",
        )
        self._render_overview(summary, route=summary.get("route") or [])
        messages = []
        if summary.get("reason"):
            messages.append(str(summary["reason"]))
        error = summary.get("error")
        if isinstance(error, Mapping):
            messages.append(f"{error.get('code', '')}：{error.get('message', '')}".strip("："))
        if summary.get("planning_status") == "target_unreachable":
            messages.append("指定收益不可达；诊断路线不是可执行方案。")
        messages.extend(str(item) for item in (summary.get("warnings") or []) if str(item).strip())
        self.reason_label.setText("\n".join(messages))
        self.result_values["status"].style().unpolish(self.result_values["status"])
        self.result_values["status"].style().polish(self.result_values["status"])

    def _render_overview(self, summary: Mapping[str, Any], *, route: list[dict[str, Any]]) -> None:
        self.result_values["trade_mode"].setText({"profit": "收益", "quick": "快速", "fixed": "固定线路", "target": "指定收益"}.get(str(summary.get("trade_mode")), "--"))
        self.result_values["planning_status"].setText({"ok": "可执行", "no_plan": "无可行方案", "target_unreachable": "目标不可达", "fixed_route_infeasible": "固定线路不可行"}.get(str(summary.get("planning_status")), str(summary.get("planning_status") or "--")))
        reposition = summary.get("reposition")
        self.result_values["reposition_fatigue"].setText(self._display(reposition.get("expected_fatigue") if isinstance(reposition, Mapping) else summary.get("reposition_expected_fatigue")))
        for field in ("actual_profit", "actual_fatigue"):
            self.result_values[field].setText(self._display(summary.get(field)) if summary.get(field) is not None else "未知")
        average = average_book_profit_text(summary)
        self.result_captions["average_book_profit"].setVisible(average is not None)
        self.result_values["average_book_profit"].setVisible(average is not None)
        self.result_values["average_book_profit"].setText(average or "--")
        self.result_values["expected_profit"].setText(self._display(summary.get("expected_profit")))
        self.result_values["fatigue"].setText(self._display(summary.get("expected_fatigue_used")))
        ratio = expected_profit_per_fatigue(summary)
        self.result_values["profit_per_fatigue"].setText(
            f"{ratio:,.2f} / 疲劳" if ratio is not None else "--"
        )
        city_count = len(route) + 1 if route else 0
        self.result_values["route"].setText(f"{len(route)} 段 / {city_count} 城" if route else "--")
        books = self._display(summary.get("books_used"))
        self.result_values["books"].setText(
            f"计划共 {books} 本" if summary.get("auto_book") else books
        )
        self.result_values["negotiations"].setText(
            f"砍 {self._display(summary.get('full_bargain_count'))} / 抬 {self._display(summary.get('full_raise_count'))}"
        )
        self.result_values["remaining_fatigue"].setText(
            self._display(summary.get("remaining_expected_fatigue"))
        )

    def _clear_result(self) -> None:
        for label in self.result_values.values():
            label.setText("--")
        self.reason_label.clear()
        self.result_captions["average_book_profit"].hide()
        self.result_values["average_book_profit"].hide()

    def _refresh_debug(self) -> None:
        self.debug_view.setPlainText(
            pretty_json(
                {
                    "inputs": self._last_inputs,
                    "progress_events": self._progress.events,
                    "result": self._last_result,
                }
            )
        )

    def _progress_detail(self) -> str:
        progress = self._progress
        if progress.stage == "trade_goods_investment":
            return trade_goods_investment_detail(progress.last_data, progress.state)
        if progress.stage == "market":
            source = str(progress.last_data.get("source") or "")
            if progress.state == "started":
                return "正在刷新市场行情"
            if progress.state == "completed":
                return "刷新失败，使用本地快照" if source == "fallback_cache" else "市场行情已更新"
        if progress.from_city or progress.to_city:
            leg = ""
            if progress.leg_index is not None and progress.leg_count:
                leg = f"第 {min(progress.leg_index + 1, progress.leg_count)}/{progress.leg_count} 段  "
            return f"{leg}{progress.from_city} -> {progress.to_city}".strip()
        return {"started": "正在处理", "completed": "已完成", "blocked": "已阻断", "failed": "失败"}.get(
            progress.state,
            progress.state,
        )

    def _tick_elapsed(self) -> None:
        self._elapsed_seconds += 1
        minutes, seconds = divmod(self._elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        self.elapsed_value.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}")

    @staticmethod
    def _parse_text_list(text: str) -> list[str]:
        normalized = str(text or "").replace("，", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    @classmethod
    def _parse_int_list(cls, text: str, label: str, minimum: int, maximum: int) -> list[int]:
        values = cls._parse_text_list(text)
        if not values:
            raise ValueError(f"{label}至少需要一个值。")
        try:
            parsed = [int(value) for value in values]
        except ValueError as exc:
            raise ValueError(f"{label}必须是用逗号分隔的整数。") from exc
        if any(value < minimum or value > maximum for value in parsed):
            raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间。")
        return parsed

    @staticmethod
    def _join_values(values: Any) -> str:
        return ", ".join(str(item) for item in (values or []))

    @staticmethod
    def _short_cid(cid: str) -> str:
        value = str(cid or "")
        return value if len(value) <= 12 else f"{value[:8]}...{value[-4:]}"

    @staticmethod
    def _display(value: Any) -> str:
        if value in (None, ""):
            return "--"
        if isinstance(value, float):
            return f"{value:,.2f}"
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    @staticmethod
    def _status_label(status: Any) -> str:
        value = str(status or "").lower()
        return {
            "queued": "排队中",
            "running": "运行中",
            "success": "完成",
            "completed": "完成",
            "ok": "可执行",
            "planned": "方案已计算（未执行）",
            "stopped": "已停止",
            "no_positive_profit_route": "无可执行路线",
            "no_plan": "无可执行路线",
            "blocked": "已阻断",
            "failed": "失败",
            "error": "失败",
            "timeout": "超时",
            "cancelled": "已取消",
        }.get(value, value or "--")

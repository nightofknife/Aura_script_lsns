"""Dedicated PC auto-trade workspace."""

from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyle,
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
    ResonanceConfigRepository,
)
from ..logic import (
    TradeProgressState,
    expected_profit_per_fatigue,
    extract_run_id,
    extract_status,
    pretty_json,
    reduce_trade_progress,
    route_product_lines,
    trade_result_summary,
)
from ..trade_catalog import TradeProductGroup, load_trade_product_groups, trade_product_ids


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
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["城市 / 商品（勾选即解锁）", "已解锁数量"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)

        enabled = self._all_product_ids if unlocked_product_ids is None else set(unlocked_product_ids)
        for group in groups:
            city_item = QTreeWidgetItem([group.city_name, ""])
            city_item.setData(0, Qt.ItemDataRole.UserRole, {"city_id": group.city_id})
            city_item.setFlags(
                city_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate
            )
            self.tree.addTopLevelItem(city_item)
            self._city_items.append(city_item)
            for product in group.products:
                item = QTreeWidgetItem([product.name, ""])
                item.setData(0, Qt.ItemDataRole.UserRole, {"product_id": product.product_id})
                item.setToolTip(0, f"商品 ID: {product.product_id}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if product.product_id in enabled
                    else Qt.CheckState.Unchecked,
                )
                city_item.addChild(item)
                self._items_by_product_id.setdefault(product.product_id, []).append(item)
            self._update_city_count(city_item)
        self.tree.itemChanged.connect(self._on_item_changed)

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
            for product_id, items in self._items_by_product_id.items()
            if items and items[0].checkState(0) == Qt.CheckState.Checked
        }

    def _set_all_products(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._updating = True
        try:
            for items in self._items_by_product_id.values():
                for item in items:
                    item.setCheckState(0, state)
            for city_item in self._city_items:
                self._update_city_count(city_item)
        finally:
            self._updating = False
        self._update_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating:
            return
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        product_id = str(payload.get("product_id") or "") if isinstance(payload, dict) else ""
        if product_id:
            self._updating = True
            try:
                for sibling in self._items_by_product_id.get(product_id, []):
                    if sibling is not item:
                        sibling.setCheckState(0, item.checkState(0))
                for city_item in self._city_items:
                    self._update_city_count(city_item)
            finally:
                self._updating = False
        else:
            for city_item in self._city_items:
                self._update_city_count(city_item)
        self._update_summary()

    def _update_city_count(self, city_item: QTreeWidgetItem) -> None:
        checked = sum(
            city_item.child(index).checkState(0) == Qt.CheckState.Checked
            for index in range(city_item.childCount())
        )
        city_item.setText(1, f"{checked}/{city_item.childCount()}")

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

    def __init__(self, settings: ResonanceConfigRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.set_inputs(self._settings.load_trade_inputs())
        self.set_busy(False)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_status_band())

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.parameter_panel = self._build_parameter_panel()
        splitter.addWidget(self.parameter_panel)
        self.execution_panel = self._build_execution_panel()
        splitter.addWidget(self.execution_panel)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setSizes([320, 820])
        root.addWidget(splitter, 1)
        root.addWidget(self._build_action_bar())

    def _build_status_band(self) -> QWidget:
        band = QFrame(self)
        band.setObjectName("statusBand")
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
        panel.setMinimumWidth(290)
        panel.setMaximumWidth(390)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        title = QLabel("跑商参数", panel)
        title.setObjectName("pageTitle")
        outer.addWidget(title)

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        form_stack = QVBoxLayout(content)
        form_stack.setContentsMargins(0, 8, 4, 8)
        form_stack.setSpacing(12)

        common_form = QFormLayout()
        common_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fatigue_budget = self._spin(0, 100000)
        self.cargo_capacity = self._spin(1, 100000)
        self.book_budget = self._spin(0, 100000)
        self.arrival_timeout_minutes = self._spin(1, 240)
        self.arrival_timeout_minutes.setParent(content)
        self.arrival_timeout_minutes.hide()
        self.arrival_timeout_minutes.setSuffix(" 分钟")
        self.arrival_timeout_minutes.setToolTip(
            "超过该时间仍未识别到站按钮或城市主页时，当前跑商任务判定为到站超时"
        )
        self.start_city = QComboBox(content)
        self.start_city.currentIndexChanged.connect(self._sync_actions)
        common_form.addRow("起始城市", self.start_city)
        self.city_selector = self._build_city_selector(content)
        common_form.addRow("参与规划城市", self.city_selector)
        self.end_city = QComboBox(content)
        self.end_city.currentIndexChanged.connect(self._sync_actions)
        self.end_city.setToolTip("选择“否”时由算法自由选择终点；指定城市必须属于参与规划城市")
        common_form.addRow("终点城市", self.end_city)
        self.end_city_notice = QLabel(
            "货运在客运前执行时，终点必须衔接客运线路，因此该参数暂不可用。",
            content,
        )
        self.end_city_notice.setProperty("status", "warning")
        self.end_city_notice.setWordWrap(True)
        self.end_city_notice.hide()
        common_form.addRow("", self.end_city_notice)
        common_form.addRow("疲劳预算", self.fatigue_budget)
        common_form.addRow("货舱容量", self.cargo_capacity)
        common_form.addRow("进货书", self.book_budget)
        form_stack.addLayout(common_form)

        self.use_medicine = QCheckBox("允许使用疲劳药", content)
        self.use_medicine.toggled.connect(self._sync_medicine_controls)
        form_stack.addWidget(self.use_medicine)
        self.medicine_box = QWidget(content)
        medicine_form = QFormLayout(self.medicine_box)
        medicine_form.setContentsMargins(0, 0, 0, 0)
        self.allowed_medicines = QLineEdit(self.medicine_box)
        self.allowed_medicines.setPlaceholderText("药品名称，使用逗号分隔")
        self.medicine_max_uses = self._spin(0, 100)
        medicine_form.addRow("允许药品", self.allowed_medicines)
        medicine_form.addRow("最大次数", self.medicine_max_uses)
        form_stack.addWidget(self.medicine_box)

        self.auto_cape_island_investment = QCheckBox("是否自动进行蜃息岛投资", content)
        form_stack.addWidget(self.auto_cape_island_investment)
        self.auto_rubbish_recycling = QCheckBox("是否自动倒垃圾", content)
        form_stack.addWidget(self.auto_rubbish_recycling)

        self.advanced_toggle = QToolButton(content)
        self.advanced_toggle.setText("高级规划参数")
        self.advanced_toggle.setCheckable(True)
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

    def _build_advanced_panel(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        form = QFormLayout(panel)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.book_profit_threshold = QDoubleSpinBox(panel)
        self.book_profit_threshold.setRange(0, 1_000_000_000)
        self.book_profit_threshold.setDecimals(2)
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
        self.trade_level = self._spin(1, 20)
        self._city_prestige_default = 20
        self._city_prestige_overrides: dict[str, int] = {}
        self.city_prestige_button = QPushButton("设置城市声望", panel)
        self.city_prestige_button.clicked.connect(self._edit_city_prestige)
        self.product_unlock_button = QPushButton("设置商品解锁", panel)
        self.product_unlock_button.clicked.connect(self._edit_product_unlocks)
        self.active_events = QLineEdit(panel)
        self.active_events.setPlaceholderText("活动 ID，使用逗号分隔")
        form.addRow("进货书收益阈值", self.book_profit_threshold)
        form.addRow("单次协商最大尝试次数", self.negotiation_max_attempts)
        form.addRow("砍价成功率(bps)", self.bargain_rates)
        form.addRow("砍价幅度(bps)", self.bargain_step)
        form.addRow("抬价成功率(bps)", self.raise_rates)
        form.addRow("抬价幅度(bps)", self.raise_step)
        form.addRow("贸易等级", self.trade_level)
        form.addRow("城市声望", self.city_prestige_button)
        form.addRow("商品解锁", self.product_unlock_button)
        form.addRow("活动", self.active_events)
        return panel

    def _build_city_selector(self, parent: QWidget) -> QWidget:
        selector = QWidget(parent)
        layout = QVBoxLayout(selector)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.city_checks: dict[str, QPushButton] = {}
        for index, (city_id, city_name) in enumerate(PC_TRADE_CITY_OPTIONS):
            button = QPushButton(city_name, selector)
            button.setCheckable(True)
            button.setProperty("cityOption", True)
            button.setMinimumHeight(30)
            button.setToolTip(f"城市 ID: {city_id}；点击切换是否参与规划")
            button.toggled.connect(self._sync_city_controls)
            self.city_checks[city_id] = button
            grid.addWidget(button, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

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
        layout.addLayout(actions)
        return selector

    def _build_execution_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading_box = QVBoxLayout()
        section = QLabel("当前方案路径", panel)
        section.setObjectName("pageTitle")
        self.stage_title = QLabel("等待开始", panel)
        self.stage_title.setObjectName("stageTitle")
        heading_box.addWidget(section)
        heading_box.addWidget(self.stage_title)
        heading.addLayout(heading_box)
        heading.addStretch(1)
        self.stage_detail = QLabel("目标检查完成后即可开始", panel)
        self.stage_detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.stage_detail.setProperty("caption", True)
        heading.addWidget(self.stage_detail)
        layout.addLayout(heading)

        self.route_tree = QTreeWidget(panel)
        self.route_tree.setColumnCount(6)
        self.route_tree.setHeaderLabels(["路线", "计划买入", "疲劳 / 书", "协商", "预计收益", "状态"])
        self.route_tree.setRootIsDecorated(False)
        self.route_tree.setAlternatingRowColors(True)
        self.route_tree.setUniformRowHeights(False)
        self.route_tree.setIconSize(QSize(18, 18))
        self.route_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        fields = (
            ("status", "方案状态"),
            ("expected_profit", "预计收益"),
            ("fatigue", "预计疲劳"),
            ("profit_per_fatigue", "疲劳收益比"),
            ("route", "路线规模"),
            ("books", "进货书"),
            ("negotiations", "协商"),
            ("remaining_fatigue", "剩余疲劳"),
        )
        for index, (key, title) in enumerate(fields):
            row, col = divmod(index, 4)
            box = QVBoxLayout()
            caption = QLabel(title, self.result_band)
            caption.setProperty("caption", True)
            value = QLabel("--", self.result_band)
            value.setProperty("value", True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(caption)
            box.addWidget(value)
            grid.addLayout(box, row, col)
            self.result_values[key] = value
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
        self.preview_button = QPushButton("计算方案", band)
        self.preview_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.preview_button.setToolTip("更新市场行情并从所选起始城市计算方案；不会操作游戏")
        self.preview_button.clicked.connect(self._request_preview)
        self.start_button = QPushButton("开始跑商", band)
        self.start_button.setObjectName("primaryButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_button.clicked.connect(self._request_start)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.preview_button)
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

    def _sync_medicine_controls(self) -> None:
        self.medicine_box.setVisible(self.use_medicine.isChecked())

    def _set_all_cities(self, checked: bool) -> None:
        for checkbox in self.city_checks.values():
            checkbox.setChecked(checked)
        self._sync_city_controls()

    def _sync_city_controls(self) -> None:
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
        saved_inputs = self._settings.load_trade_inputs()
        saved_inputs["city_prestige"] = self._city_prestige_payload()
        self._settings.save_trade_inputs(saved_inputs)

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
        saved_inputs = self._settings.load_trade_inputs()
        saved_inputs["product_unlocks"] = self._product_unlock_payload()
        self._settings.save_trade_inputs(saved_inputs)

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
        if not hasattr(self, "start_city"):
            return
        current_city_id = str(self.start_city.currentData() or "")
        selected = set(self.selected_city_ids())
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
        if not hasattr(self, "end_city"):
            return
        current_city_id = str(self.end_city.currentData() or "")
        selected = set(self.selected_city_ids())
        self.end_city.blockSignals(True)
        self.end_city.clear()
        self.end_city.addItem("否", "")
        for city_id, city_name in PC_TRADE_CITY_OPTIONS:
            if city_id in selected:
                self.end_city.addItem(city_name, city_id)
        index = self.end_city.findData(current_city_id)
        self.end_city.setCurrentIndex(max(index, 0))
        self.end_city.blockSignals(False)
        self._sync_actions()

    def selected_city_ids(self) -> list[str]:
        return [city_id for city_id, _name in PC_TRADE_CITY_OPTIONS if self.city_checks[city_id].isChecked()]

    def set_inputs(self, inputs: Mapping[str, Any]) -> None:
        values = dict(inputs)
        self.fatigue_budget.setValue(int(values.get("fatigue_budget", 700)))
        self.cargo_capacity.setValue(int(values.get("cargo_capacity", 750)))
        self.book_budget.setValue(int(values.get("book_budget", 0)))
        arrival_timeout_seconds = max(int(values.get("arrival_timeout_seconds", 3600)), 1)
        self.arrival_timeout_minutes.setValue(max((arrival_timeout_seconds + 59) // 60, 1))
        self.book_profit_threshold.setValue(float(values.get("book_profit_threshold", 15000)))
        self.negotiation_max_attempts.setValue(int(values.get("negotiation_max_attempts", 5)))
        self.bargain_rates.setText(self._join_values(values.get("bargain_success_rates_bps", [5000])))
        self.bargain_step.setValue(int(values.get("bargain_step_bps", 1000)))
        self.raise_rates.setText(self._join_values(values.get("raise_success_rates_bps", [5000])))
        self.raise_step.setValue(int(values.get("raise_step_bps", 1000)))
        self.trade_level.setValue(int(values.get("trade_level", 20)))
        selected_city_ids = {
            str(city_id)
            for city_id in (values.get("available_city_ids") or DEFAULT_PC_TRADE_CITY_IDS)
        }
        for city_id, checkbox in self.city_checks.items():
            checkbox.setChecked(city_id in selected_city_ids)
        self._sync_start_city_options()
        start_city_index = self.start_city.findData(str(values.get("start_city_id") or ""))
        self.start_city.setCurrentIndex(max(start_city_index, 0))
        required_end_city_ids = [
            str(city_id)
            for city_id in (values.get("required_end_city_ids") or [])
            if str(city_id) in selected_city_ids
        ]
        self._sync_end_city_options()
        if required_end_city_ids:
            end_city_index = self.end_city.findData(required_end_city_ids[0])
            self.end_city.setCurrentIndex(max(end_city_index, 0))
        else:
            self.end_city.setCurrentIndex(0)
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
        self.active_events.setText(self._join_values(values.get("active_events", [])))
        self.use_medicine.setChecked(bool(values.get("use_fatigue_medicine", False)))
        self.allowed_medicines.setText(self._join_values(values.get("allowed_fatigue_medicines", [])))
        self.medicine_max_uses.setValue(int(values.get("fatigue_medicine_max_uses", 4)))
        self.auto_cape_island_investment.setChecked(
            bool(values.get("auto_cape_island_investment", True))
        )
        self.auto_rubbish_recycling.setChecked(
            bool(values.get("auto_rubbish_recycling", True))
        )
        self._sync_city_controls()
        self._sync_medicine_controls()

    def collect_inputs(self, *, require_start_city: bool = False) -> dict[str, Any]:
        bargain_rates = self._parse_int_list(self.bargain_rates.text(), "砍价成功率", 0, 10000)
        raise_rates = self._parse_int_list(self.raise_rates.text(), "抬价成功率", 0, 10000)
        selected_city_ids = self.selected_city_ids()
        if len(selected_city_ids) < 2:
            raise ValueError("参与规划城市至少需要选择两个")
        start_city_id = str(self.start_city.currentData() or "")
        if require_start_city and not start_city_id:
            raise ValueError("请选择起始城市")
        if start_city_id and start_city_id not in selected_city_ids:
            raise ValueError("起始城市必须属于参与规划城市")
        end_city_id = str(self.end_city.currentData() or "")
        if end_city_id and end_city_id not in selected_city_ids:
            raise ValueError("终点城市必须属于参与规划城市")
        required_end_city_ids = [end_city_id] if end_city_id else None
        return {
            "start_city_id": start_city_id,
            "fatigue_budget": self.fatigue_budget.value(),
            "cargo_capacity": self.cargo_capacity.value(),
            "book_budget": self.book_budget.value(),
            "arrival_timeout_seconds": self.arrival_timeout_minutes.value() * 60,
            "book_profit_threshold": self.book_profit_threshold.value(),
            "negotiation_max_attempts": self.negotiation_max_attempts.value(),
            "bargain_success_rates_bps": bargain_rates,
            "bargain_step_bps": self.bargain_step.value(),
            "raise_success_rates_bps": raise_rates,
            "raise_step_bps": self.raise_step.value(),
            "trade_level": self.trade_level.value(),
            "available_city_ids": selected_city_ids,
            "required_end_city_ids": required_end_city_ids,
            "city_prestige": self._city_prestige_payload(),
            "product_unlocks": self._product_unlock_payload(),
            "active_events": self._parse_text_list(self.active_events.text()),
            "use_fatigue_medicine": self.use_medicine.isChecked(),
            "allowed_fatigue_medicines": self._parse_text_list(self.allowed_medicines.text()),
            "fatigue_medicine_max_uses": self.medicine_max_uses.value(),
            "auto_cape_island_investment": self.auto_cape_island_investment.isChecked(),
            "auto_rubbish_recycling": self.auto_rubbish_recycling.isChecked(),
        }

    def _request_start(self) -> None:
        self._request_action(self.startRequested)

    def _request_preview(self) -> None:
        self._request_action(self.previewRequested, require_start_city=True)

    def _request_action(self, signal: Signal, *, require_start_city: bool = False) -> None:
        try:
            inputs = self.collect_inputs(require_start_city=require_start_city)
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        self._settings.save_trade_inputs(inputs)
        self._last_inputs = inputs
        signal.emit(inputs, 0.0)

    def set_target_status(self, payload: Mapping[str, Any]) -> None:
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
            self.ready_hint.setText("计算方案无需游戏；开始跑商需连接客户端")
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
        self._elapsed_timer.stop()
        self._last_result = dict(payload)
        summary = trade_result_summary(payload)
        runner_status = extract_status(payload)
        if runner_status in {"failed", "error", "timeout", "cancelled"} and not summary.get("route"):
            self.run_status_value.setText(self._status_label(runner_status))
            self.stage_title.setText(self._status_label(runner_status))
            self.stage_detail.setText("方案计算未完成，保留上一份方案")
            self.set_busy(False)
            self._active_mode = ""
            self._refresh_debug()
            return
        self._current_plan = dict(summary)
        self._plan_inputs = dict(self._last_inputs)
        self.run_status_value.setText("方案就绪")
        self.stage_title.setText("方案已计算")
        self.stage_detail.setText(
            "行情更新失败，已使用本地市场快照"
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
            self.book_budget,
            self.arrival_timeout_minutes,
            self.use_medicine,
            self.auto_cape_island_investment,
            self.auto_rubbish_recycling,
            self.advanced_toggle,
            self.advanced_panel,
            self.city_selector,
            self.start_city,
        ):
            widget.setEnabled(not busy)
        self.end_city.setEnabled(not busy and self._end_city_constraint_available)
        self._sync_actions()

    def set_end_city_constraint_available(self, available: bool) -> None:
        self._end_city_constraint_available = bool(available)
        self.end_city.setEnabled(not self._busy and self._end_city_constraint_available)
        self.end_city_notice.setVisible(not self._end_city_constraint_available)
        if self._end_city_constraint_available:
            self.end_city.setToolTip(
                "选择“否”时由算法自由选择终点；指定城市必须属于参与规划城市"
            )
        else:
            self.end_city.setToolTip(
                "货运在客运前执行时，货运终点由客运线路决定，无法手动指定"
            )

    def is_busy(self) -> bool:
        return self._busy

    def _sync_actions(self) -> None:
        self.start_button.setEnabled(self._target_ready and not self._busy)
        self.preview_button.setEnabled(bool(self.start_city.currentData()) and not self._busy)
        self.cancel_button.setEnabled(self._busy)

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
            background = QBrush(QColor("#dff3f2")) if active else QBrush()
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
            "success" if status in {"success", "completed", "ok", "no_plan"} else "warning" if status == "blocked" else "error",
        )
        self._render_overview(summary, route=summary.get("route") or [])
        messages = []
        if summary.get("reason"):
            messages.append(str(summary["reason"]))
        messages.extend(str(item) for item in (summary.get("warnings") or []) if str(item).strip())
        self.reason_label.setText("\n".join(messages))
        self.result_values["status"].style().unpolish(self.result_values["status"])
        self.result_values["status"].style().polish(self.result_values["status"])

    def _render_overview(self, summary: Mapping[str, Any], *, route: list[dict[str, Any]]) -> None:
        self.result_values["expected_profit"].setText(self._display(summary.get("expected_profit")))
        self.result_values["fatigue"].setText(self._display(summary.get("expected_fatigue_used")))
        ratio = expected_profit_per_fatigue(summary)
        self.result_values["profit_per_fatigue"].setText(
            f"{ratio:,.2f} / 疲劳" if ratio is not None else "--"
        )
        city_count = len(route) + 1 if route else 0
        self.result_values["route"].setText(f"{len(route)} 段 / {city_count} 城" if route else "--")
        self.result_values["books"].setText(self._display(summary.get("books_used")))
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
            "no_positive_profit_route": "无可执行路线",
            "no_plan": "无可执行路线",
            "blocked": "已阻断",
            "failed": "失败",
            "error": "失败",
            "timeout": "超时",
            "cancelled": "已取消",
        }.get(value, value or "--")

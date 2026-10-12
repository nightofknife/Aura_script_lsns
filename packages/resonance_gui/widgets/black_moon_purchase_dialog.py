"""Transactional, filterable selector for canonical Black Moon products."""

from __future__ import annotations

from collections import Counter
from typing import Callable, Mapping

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRect, QSize, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QStyle, QStyledItemDelegate, QToolTip, QVBoxLayout, QWidget,
)

from ..black_moon_catalog import BlackMoonCatalog, CATEGORY_NAMES, QUALITY_NAMES, normalize_purchase_table
from .toggle_button import ToggleButton


PRODUCT_ROLE = Qt.ItemDataRole.UserRole + 1
PURCHASE_HELP = "保存设置不会购买；勾选共用商品的折扣、不同包装库存卡片，不是数量上限。"


class PurchaseTableModel(QAbstractListModel):
    tableChanged = Signal()

    def __init__(self, catalog: BlackMoonCatalog, table: Mapping[str, bool], parent: QWidget) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self._table = normalize_purchase_table(table, catalog)

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.catalog.products)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        product = self.catalog.products[index.row()]
        if role == PRODUCT_ROLE:
            return product
        if role == Qt.ItemDataRole.DisplayRole:
            return product.name
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if self._table[product.item_id] else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole):
            return (f"{product.name} · {CATEGORY_NAMES[product.category]} · {QUALITY_NAMES.get(product.quality, product.quality)}\n"
                    f"{'已选' if self._table[product.item_id] else '未选'}\n{PURCHASE_HELP}")
        return None

    def purchase_table(self) -> dict[str, bool]:
        return dict(self._table)

    def apply_selection(self, item_ids: tuple[str, ...], checked: bool) -> None:
        changed = False
        for item_id in item_ids:
            if self._table[item_id] != checked:
                self._table[item_id] = checked
                changed = True
        if changed:
            self.dataChanged.emit(self.index(0), self.index(self.rowCount() - 1),
                                  [Qt.ItemDataRole.CheckStateRole, Qt.ItemDataRole.ToolTipRole,
                                   Qt.ItemDataRole.AccessibleTextRole])
            self.tableChanged.emit()


class PurchaseFilter(QSortFilterProxyModel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.category = ""
        self.search = ""
        self.quality = ""
        self.selected_only = False

    def matches_without_quality(self, product, checked: bool) -> bool:
        return (not self.category or product.category == self.category) and (
            not self.search or self.search in product.name.casefold()
        ) and (not self.selected_only or checked)

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        index = self.sourceModel().index(row, 0, parent)
        product = index.data(PRODUCT_ROLE)
        return self.matches_without_quality(product, index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked) and (
            not self.quality or product.quality == self.quality
        )

    def matching_ids(self) -> tuple[str, ...]:
        return tuple(self.index(row, 0).data(PRODUCT_ROLE).item_id for row in range(self.rowCount()))


class ProductDelegate(QStyledItemDelegate):
    """Paint tiles instead of constructing one widget for every product."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._icons: dict[str, QIcon] = {}

    def sizeHint(self, option, index):  # noqa: N802
        # IconMode otherwise centers the default one-line item inside the cell,
        # leaving the painted "button" only as tall as a line of text.
        return self.parent().gridSize()

    def paint(self, painter: QPainter, option, index) -> None:
        product = index.data(PRODUCT_ROLE)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        painter.setBrush(QColor("#66745c" if checked else "#eee5d5" if hovered else "#f8f3e8"))
        painter.setPen(QPen(QColor("#52634a" if checked else "#c9b99d")))
        painter.drawRoundedRect(rect, 4, 4)
        icon_rect = QRect(rect.left() + 10, rect.top() + (rect.height() - 40) // 2, 40, 40)
        if product.item_id not in self._icons:
            self._icons[product.item_id] = QIcon(str(product.icon_path)) if product.icon_path else QIcon()
        icon = self._icons[product.item_id]
        if not icon.isNull():
            icon.paint(painter, icon_rect)
        else:
            painter.setPen(QColor("#fffaf0" if checked else "#9b958b"))
            painter.drawRoundedRect(icon_rect.adjusted(5, 5, -5, -5), 3, 3)
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, "—")
        text_rect = QRect(icon_rect.right() + 10, rect.top() + 8, max(1, rect.width() - 82), rect.height() - 16)
        painter.setFont(option.font)
        painter.setPen(QColor("#fffaf0" if checked else "#38342e"))
        metrics = QFontMetrics(option.font)
        # Two lines, with explicit final-line elision; tooltip retains the full name.
        first = product.name
        split = len(first)
        while split > 1 and metrics.horizontalAdvance(first[:split]) > text_rect.width():
            split -= 1
        if split < len(first):
            painter.drawText(text_rect.left(), text_rect.top() + metrics.ascent(), first[:split])
            name = metrics.elidedText(first[split:], Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect.left(), text_rect.top() + metrics.ascent() + metrics.height(), name)
        else:
            painter.drawText(text_rect.left(), text_rect.top() + metrics.ascent(), first)
        painter.setPen(QColor("#e0e6d8" if checked else "#716a5e"))
        painter.drawText(text_rect.left(), rect.bottom() - 9, QUALITY_NAMES.get(product.quality, product.quality))
        if checked:
            # Draw the check rather than relying on a symbol-font fallback.
            painter.setPen(QPen(QColor("#fffaf0"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            x, y = rect.right() - 20, rect.top() + 11
            painter.drawLine(x, y + 5, x + 4, y + 9)
            painter.drawLine(x + 4, y + 9, x + 12, y)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#fffaf0" if checked else "#383f32"), 2))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
        painter.restore()


class ProductGrid(QListView):
    toggleRequested = Signal(QModelIndex)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setAccessibleName("黑月商品选择网格")
        self.setStyleSheet("QListView { background: #f2ebdd; border: 0; }")
        self.clicked.connect(self.toggleRequested)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(0)
        self._layout_timer.timeout.connect(self.update_grid_size)
        self._tooltip_index = QModelIndex()
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.setInterval(700)
        self._tooltip_timer.timeout.connect(self._show_tooltip)
        self.viewport().installEventFilter(self)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.update_grid_size()

    def update_grid_size(self) -> None:
        width = max(self.viewport().width() - 4, 1)
        columns = max(1, width // 216)
        size = QSize(width // columns, max(78, self.fontMetrics().height() * 3 + 20))
        if self.gridSize() != size:
            self.setGridSize(size)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.toggleRequested.emit(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.viewport():
            if event.type() == QEvent.Type.Resize:
                # Scrollbars can resize the viewport without resizing QListView.
                self._layout_timer.start()
            if event.type() == QEvent.Type.ToolTip:
                return True
            if event.type() == QEvent.Type.MouseMove:
                index = self.indexAt(event.position().toPoint())
                if index != self._tooltip_index:
                    self._tooltip_timer.stop()
                    QToolTip.hideText()
                    self._tooltip_index = index
                    if index.isValid():
                        self._tooltip_timer.start()
            elif event.type() in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                self._tooltip_timer.stop()
                QToolTip.hideText()
                self._tooltip_index = QModelIndex()
        return super().eventFilter(watched, event)

    def _show_tooltip(self) -> None:
        if self.isVisible() and self._tooltip_index.isValid():
            QToolTip.showText(QCursor.pos(), self._tooltip_index.data(Qt.ItemDataRole.ToolTipRole), self)

    def reset_tooltip(self) -> None:
        self._tooltip_timer.stop()
        self._tooltip_index = QModelIndex()
        QToolTip.hideText()


class BlackMoonPurchaseDialog(QDialog):
    def __init__(self, catalog: BlackMoonCatalog, table: Mapping[str, bool], parent: QWidget | None = None,
                 *, save: Callable[[dict[str, bool]], None] | None = None, read_only: bool = False):
        super().__init__(parent)
        self.setObjectName("appRoot")
        self.setWindowTitle("黑月本地采购商品")
        self.setModal(True)
        self._save = save
        self._saved_table: dict[str, bool] | None = None
        self._read_only = read_only
        self.product_model = PurchaseTableModel(catalog, table, self)
        self.proxy = PurchaseFilter(self)
        self.proxy.setSourceModel(self.product_model)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        workspace = QHBoxLayout()
        self.categories = QListWidget(self)
        self.categories.setFixedWidth(158)
        self.categories.setStyleSheet(
            "QListWidget { background: #f2ebdd; border: 0; }"
            "QListWidget::item { padding: 9px 6px; }"
            "QListWidget::item:selected { background: #d9dfcf; color: #38342e; }"
        )
        self.categories.setAccessibleName("商品分类")
        for key in ("", *CATEGORY_NAMES):
            item = QListWidgetItem(self.categories)
            item.setData(Qt.ItemDataRole.UserRole, key)
        workspace.addWidget(self.categories)
        content = QVBoxLayout()
        filters = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索商品名称")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索商品名称")
        self.quality = QComboBox(self)
        self.quality.setAccessibleName("商品品质")
        self.quality.addItem("全部品质", "")
        qualities = {product.quality for product in catalog.products}
        for key in (*QUALITY_NAMES, *sorted(qualities - set(QUALITY_NAMES))):
            if key in qualities:
                self.quality.addItem(QUALITY_NAMES.get(key, key), key)
        self.selected_only = ToggleButton("仅看已选", self)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.quality)
        filters.addWidget(self.selected_only)
        content.addLayout(filters)
        self.grid = ProductGrid(self)
        self.grid.setModel(self.proxy)
        self.grid.setItemDelegate(ProductDelegate(self.grid))
        content.addWidget(self.grid, 1)
        self.empty_label = QLabel("没有匹配商品", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.empty_label)
        workspace.addLayout(content, 1)
        root.addLayout(workspace, 1)
        self.count_label = QLabel(self)
        root.addWidget(self.count_label)
        footer = QHBoxLayout()
        self.select_all = QPushButton(self)
        self.clear_all = QPushButton(self)
        for button in (self.select_all, self.clear_all):
            button.setAutoDefault(False)
            footer.addWidget(button)
        footer.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setAutoDefault(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        footer.addWidget(self.buttons)
        root.addLayout(footer)
        self.categories.currentRowChanged.connect(self._refilter)
        self.search.textChanged.connect(self._refilter)
        self.quality.currentIndexChanged.connect(self._refilter)
        self.selected_only.toggled.connect(self._refilter)
        self.product_model.tableChanged.connect(self._refilter)
        self.grid.toggleRequested.connect(self._toggle_item)
        self.select_all.clicked.connect(lambda: self._set_matching(True))
        self.clear_all.clicked.connect(lambda: self._set_matching(False))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.categories.setCurrentRow(0)
        self._refilter()
        self.set_read_only(read_only)
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(660, int(available.width() * .9)), min(400, int(available.height() * .9)))
        self.resize(min(1000, int(available.width() * .9)), min(680, int(available.height() * .9)))

    def _refilter(self, *_args) -> None:
        modern_filter_api = hasattr(self.proxy, "beginFilterChange") and hasattr(self.proxy, "endFilterChange")
        if modern_filter_api:
            self.proxy.beginFilterChange()
        item = self.categories.currentItem()
        self.proxy.category = item.data(Qt.ItemDataRole.UserRole) if item else ""
        self.proxy.search = self.search.text().strip().casefold()
        self.proxy.quality = self.quality.currentData() or ""
        self.proxy.selected_only = self.selected_only.isChecked()
        if modern_filter_api:
            self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.proxy.invalidateFilter()
        self.grid.reset_tooltip()
        table = self.product_model.purchase_table()
        products = self.product_model.catalog.products
        for row in range(self.categories.count()):
            item = self.categories.item(row)
            key = item.data(Qt.ItemDataRole.UserRole)
            members = [product for product in products if not key or product.category == key]
            item.setText(f"{CATEGORY_NAMES.get(key, '全部')}  {sum(table[p.item_id] for p in members)}/{len(members)}")
        counts = Counter(product.quality for product in products
                         if self.proxy.matches_without_quality(product, table[product.item_id]))
        was_blocked = self.quality.blockSignals(True)
        for row in range(self.quality.count()):
            key = self.quality.itemData(row)
            self.quality.setItemText(row, f"{QUALITY_NAMES.get(key, key) if key else '全部品质'} ({counts[key] if key else sum(counts.values())})")
        self.quality.blockSignals(was_blocked)
        count = self.proxy.rowCount()
        self.count_label.setText(f"匹配 {count} 项 · 已选 {sum(table.values())} / {len(table)}")
        self.select_all.setText(f"全选筛选结果 ({count})")
        self.clear_all.setText(f"清空筛选结果 ({count})")
        for button in (self.select_all, self.clear_all):
            button.setEnabled(not self._read_only and count > 0)
            button.setToolTip("作用于当前分类和筛选的所有匹配商品，包含未滚动到的项目；不影响筛选外商品。")
        self.empty_label.setVisible(count == 0)

    def _toggle_item(self, index: QModelIndex) -> None:
        if not self._read_only and index.isValid():
            item_id = index.data(PRODUCT_ROLE).item_id
            self.product_model.apply_selection((item_id,), not self.product_model.purchase_table()[item_id])

    def _set_matching(self, checked: bool) -> None:
        if not self._read_only:
            # Snapshot IDs before changing the selected-only filter's contents.
            self.product_model.apply_selection(self.proxy.matching_ids(), checked)

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self.save_button.setEnabled(not read_only)
        self.notice.setText("运行期间仅可查看，不能修改采购商品。" if read_only else "")
        self.notice.setVisible(read_only)
        self.grid.setCursor(Qt.CursorShape.ArrowCursor if read_only else Qt.CursorShape.PointingHandCursor)
        self._refilter()

    def saved_table(self) -> dict[str, bool] | None:
        return dict(self._saved_table) if self._saved_table is not None else None

    def accept(self) -> None:
        if self._read_only:
            return
        table = normalize_purchase_table(self.product_model.purchase_table(), self.product_model.catalog)
        try:
            if self._save is not None:
                self._save(dict(table))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "采购设置保存失败", str(exc))
            return
        self._saved_table = dict(table)
        super().accept()

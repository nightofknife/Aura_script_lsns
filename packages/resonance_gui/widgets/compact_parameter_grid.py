"""Responsive, inline label/editor pairs for dense desktop forms."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QLayout, QSizePolicy, QWidget


class CompactParameterGrid(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        # The previous multi-column minimum must not prevent narrowing the form.
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(8)
        self._fields: list[tuple[QWidget, QWidget]] = []
        self._enabled_fields: dict[QWidget, bool] = {}
        self.column_count = 1

    def minimumSizeHint(self) -> QSize:
        width = max((cell.minimumSizeHint().width() for _editor, cell in self._fields), default=240)
        return QSize(width, self._grid.minimumSize().height())

    def add_field(self, caption: str | QLabel, editor: QWidget) -> None:
        cell = QWidget(self)
        row = QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel(caption, cell) if isinstance(caption, str) else caption
        label.setWordWrap(True)
        label.setMinimumWidth(88)
        label.setMaximumWidth(144)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        editor.setMinimumWidth(120)
        editor.setMaximumWidth(220)
        row.addWidget(label)
        row.addWidget(editor)
        row.addStretch(1)
        label.show()
        editor.show()
        self._fields.append((editor, cell))
        self._enabled_fields[editor] = True
        self._reflow()

    def set_field_visible(self, editor: QWidget, visible: bool) -> None:
        if self._enabled_fields.get(editor) != visible:
            self._enabled_fields[editor] = visible
            self._reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        columns = 3 if self.width() >= 1080 else 2 if self.width() >= 640 else 1
        if columns != self.column_count:
            self.column_count = columns
            self._reflow()

    def _reflow(self) -> None:
        visible_index = 0
        for editor, cell in self._fields:
            self._grid.removeWidget(cell)
            visible = self._enabled_fields[editor]
            cell.setVisible(visible)
            if visible:
                row, column = divmod(visible_index, self.column_count)
                self._grid.addWidget(cell, row, column, alignment=Qt.AlignmentFlag.AlignTop)
                visible_index += 1
        for column in range(3):
            self._grid.setColumnStretch(column, 1 if column < self.column_count else 0)
        self.setMinimumHeight(self._grid.minimumSize().height())
        self.updateGeometry()

"""Layout components for the freight parameter editor."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .compact_parameter_grid import CompactParameterGrid
from .toggle_button import ToggleButton


class FreightSwitch(ToggleButton):
    def __init__(self, name: str, parent: QWidget) -> None:
        super().__init__("未开启", parent)
        self.setAccessibleName(name)
        self.setProperty("freightSwitch", True)
        self.setMinimumWidth(76)
        self.toggled.connect(self._update_caption)

    def setChecked(self, checked: bool) -> None:
        super().setChecked(checked)
        self._update_caption(self.isChecked())

    def _update_caption(self, checked: bool) -> None:
        self.setText("已开启" if checked else "未开启")


def parameter_divider(layout: QVBoxLayout) -> None:
    line = QFrame()
    line.setObjectName("freightDivider")
    line.setFixedHeight(1)
    layout.addWidget(line)


def parameter_heading(layout: QVBoxLayout, text: str, *, divider: bool = True) -> None:
    if divider and layout.count():
        parameter_divider(layout)
    heading = QLabel(text)
    heading.setObjectName("freightSection")
    layout.addWidget(heading)


def parameter_fields(layout: QVBoxLayout, *fields: tuple[str, QWidget]) -> CompactParameterGrid:
    grid = CompactParameterGrid(layout.parentWidget())
    for label, editor in fields:
        grid.add_field(label, editor)
    layout.addWidget(grid)
    return grid


def parameter_row(layout: QVBoxLayout, title: str, editor: QWidget, summary: QLabel | None = None) -> None:
    row = QHBoxLayout()
    row.setContentsMargins(0, 3, 0, 3)
    row.setSpacing(14)
    labels = QVBoxLayout()
    labels.setSpacing(3)
    caption = QLabel(title)
    caption.setObjectName("freightSetting")
    labels.addWidget(caption)
    if summary is not None:
        summary.setObjectName("freightValue")
        summary.setWordWrap(True)
        labels.addWidget(summary)
    row.addLayout(labels, 1)
    row.addWidget(editor, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addLayout(row)


def parameter_button(text: str, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.setProperty("freightEdit", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button

"""Offline freight quick start with a link to the freight settings."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from ..tutorial_content import TUTORIAL_CHAPTERS


def _tutorial_help_icon() -> QIcon:
    """Draw a balanced help glyph at native and high-DPI toolbar sizes."""
    icon = QIcon()
    for mode, color in (
        (QIcon.Mode.Normal, "#68775d"),
        (QIcon.Mode.Active, "#45583c"),
        (QIcon.Mode.Disabled, "#a39f92"),
    ):
        for scale in (1, 2, 3):
            pixmap = QPixmap(24 * scale, 24 * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(scale, scale)
            pen = QPen(QColor(color), 1.65)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(12, 12), 9.25, 9.25)
            question = QPainterPath(QPointF(9.2, 9))
            question.cubicTo(9.5, 6.1, 14.9, 6.1, 14.9, 9.2)
            question.cubicTo(14.9, 11.3, 12, 11.3, 12, 13.4)
            painter.drawPath(question)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(12, 16.8), 0.95, 0.95)
            painter.end()
            pixmap.setDevicePixelRatio(scale)
            icon.addPixmap(pixmap, mode)
    return icon


class TutorialHelpButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tutorialHelpButton")
        self.setFixedSize(34, 34)
        self.setIcon(_tutorial_help_icon())
        self.setIconSize(QSize(22, 22))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(True)
        self.setAccessibleName("跑商教程")
        self.setToolTip("跑商教程（F1）")
        self.setShortcut("F1")


class TutorialPage(QWidget):
    destinationRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        chapter = TUTORIAL_CHAPTERS[0]
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        heading = QLabel(chapter.title, self)
        heading.setObjectName("workflowTitle")
        root.addWidget(heading)
        hint = QLabel("按下面五步准备并开始跑商。", self)
        hint.setWordWrap(True)
        hint.setProperty("caption", True)
        root.addWidget(hint)

        self.reader = QTextBrowser(self)
        self.reader.setObjectName("tutorialReader")
        self.reader.setAccessibleName("跑商教程正文")
        self.reader.setOpenLinks(False)
        self.reader.setOpenExternalLinks(False)
        self.reader.document().setDefaultStyleSheet(
            "body { color: #38342e; font-family: 'Microsoft YaHei UI', 'Segoe UI'; "
            "font-size: 14px; } "
            "h2 { font-size: 16px; margin-top: 20px; margin-bottom: 8px; } "
            "p, li { line-height: 150%; } li { margin-bottom: 10px; }"
        )
        self.reader.document().setDocumentMargin(20)
        self.reader.setHtml(f"<html><body>{chapter.body}</body></html>")
        root.addWidget(self.reader, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.action_button = QPushButton(chapter.action_label, self)
        self.action_button.setObjectName("primaryButton")
        self.action_button.setToolTip("打开货运参数页面。")
        self.action_button.clicked.connect(self._open_destination)
        footer.addWidget(self.action_button)
        root.addLayout(footer)

    def _open_destination(self) -> None:
        self.destinationRequested.emit(TUTORIAL_CHAPTERS[0].destination)

"""A wrapped warning that splitters cannot squeeze down to a single text line."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy


class MarketWarningLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("status", "warning")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self._fit_height()

    def clear(self):
        self.setText("")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        width = self.width()
        parent = self.parentWidget()
        if self.isHidden() and parent is not None and parent.layout() is not None:
            margins = parent.layout().contentsMargins()
            width = parent.contentsRect().width() - margins.left() - margins.right()
        height = max(self.fontMetrics().height(), self.heightForWidth(max(width, 1))) if self.text() else 0
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)

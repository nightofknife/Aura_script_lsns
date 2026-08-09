"""Application styling for the Resonance operator console."""

APP_STYLE = """
QMainWindow, QWidget#appRoot {
    background: #f4f6f8;
    color: #20252b;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QFrame#navigation {
    background: #20252b;
    border: 0;
}
QLabel#brandTitle { color: #ffffff; font-size: 18px; font-weight: 700; }
QLabel#brandCaption { color: #91a0ad; font-size: 11px; }
QPushButton[nav="true"] {
    background: transparent;
    color: #cbd3da;
    border: 0;
    border-left: 3px solid transparent;
    padding: 10px 14px;
    text-align: left;
    min-height: 24px;
}
QPushButton[nav="true"]:hover { background: #2a3037; color: #ffffff; }
QPushButton[nav="true"]:checked {
    background: #30383f;
    color: #ffffff;
    border-left-color: #19a5a5;
    font-weight: 600;
}
QFrame#commerceHeader {
    background: #ffffff;
    border-bottom: 1px solid #d7dfe3;
}
QLabel#commerceTitle { color: #20252b; font-size: 18px; font-weight: 700; }
QLabel#commerceCaption { color: #6c7780; font-size: 11px; }
QPushButton[commerceNav="true"] {
    min-width: 72px;
    padding: 7px 18px;
    background: #f4f7f8;
    border-color: #cdd7dc;
}
QPushButton[commerceNav="true"]:hover { background: #e9f1f2; border-color: #8eacad; }
QPushButton[commerceNav="true"]:checked {
    background: #087f82;
    border-color: #087f82;
    color: #ffffff;
    font-weight: 700;
}
QFrame#commerceOverviewPanel {
    background: #ffffff;
    border: 1px solid #d8e1e4;
    border-radius: 8px;
}
QLabel#commerceOverviewTitle { color: #20252b; font-size: 22px; font-weight: 700; }
QLabel#developmentBadge {
    color: #805d09;
    background: #fff1bf;
    border: 1px solid #e2c45f;
    border-radius: 4px;
    padding: 4px 9px;
    font-size: 11px;
    font-weight: 700;
}
QCheckBox[commerceSwitch="true"] {
    color: #273038;
    font-size: 16px;
    font-weight: 600;
    spacing: 12px;
    padding: 8px 4px;
}
QPushButton[commerceRun="true"] {
    min-height: 36px;
    margin-top: 6px;
    font-size: 15px;
    font-weight: 700;
}
QPushButton[commerceRunState="run"] {
    color: #ffffff;
    background: #087f82;
    border-color: #087f82;
}
QPushButton[commerceRunState="run"]:hover { background: #066e71; }
QPushButton[commerceRunState="stop"] {
    color: #a52a24;
    background: #fff7f6;
    border-color: #cfaaa7;
}
QFrame#statusBand { background: #ffffff; border-bottom: 1px solid #dfe4e8; }
QFrame#contentPanel { background: #f7f9fa; }
QFrame#actionBar { background: #ffffff; border-top: 1px solid #dfe4e8; }
QLabel[caption="true"] { color: #6c7780; font-size: 11px; }
QLabel[value="true"] { color: #20252b; font-weight: 600; }
QLabel#stageTitle { color: #20252b; font-size: 19px; font-weight: 700; }
QLabel#pageTitle { color: #20252b; font-size: 17px; font-weight: 700; }
QLabel#sectionTitle { color: #2b333a; font-size: 14px; font-weight: 700; }
QFrame#parameterPanel { background: #ffffff; border-right: 1px solid #dfe4e8; }
QLabel#passengerRouteTitle { color: #0b6f72; font-size: 20px; font-weight: 700; padding-top: 4px; }
QLabel#passengerRouteNote { padding-bottom: 2px; }
QLabel[badge="true"] {
    color: #7d5b08;
    background: #fff3ce;
    border: 1px solid #ead487;
    border-radius: 3px;
    padding: 2px 6px;
    font-size: 10px;
    font-weight: 600;
}
QLabel#passengerEstimate {
    color: #173f40;
    background: #e7f3f3;
    border-left: 3px solid #148f91;
    padding: 12px 14px;
    font-size: 14px;
    font-weight: 700;
}
QLabel#passengerPolicy {
    color: #68737c;
    background: #f4f6f8;
    border: 1px solid #e0e5e8;
    border-radius: 4px;
    padding: 10px 12px;
    font-size: 11px;
}
QFrame#passengerRouteBand {
    background: #ffffff;
    border: 1px solid #d7e1e3;
    border-radius: 5px;
}
QLabel#passengerTimeline { color: #087f82; font-size: 16px; font-weight: 700; }
QLabel#passengerStageDetail { color: #4f5b64; font-size: 12px; }
QFrame#passengerMetrics {
    background: transparent;
    border-top: 1px solid #dfe5e8;
    border-bottom: 1px solid #dfe5e8;
}
QLabel[metricValue="true"] { color: #20252b; font-size: 14px; font-weight: 700; }
QTextBrowser#passengerDetails { background: #ffffff; color: #4d5962; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextBrowser {
    background: #ffffff;
    border: 1px solid #cfd6dc;
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: #148f91;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextBrowser:focus { border-color: #148f91; }
QPushButton {
    background: #ffffff;
    color: #273038;
    border: 1px solid #c9d1d7;
    border-radius: 4px;
    padding: 7px 12px;
    min-height: 22px;
}
QPushButton:hover { background: #f0f4f5; border-color: #9eabb4; }
QPushButton:disabled { color: #9ba4aa; background: #edf0f2; border-color: #dce1e4; }
QPushButton#primaryButton { background: #087f82; border-color: #087f82; color: #ffffff; font-weight: 700; }
QPushButton#primaryButton:hover { background: #066e71; }
QPushButton#dangerButton { color: #a52a24; border-color: #cfaaa7; }
QPushButton#dangerButton:hover { background: #fff1f0; }
QPushButton[segment="true"] { border-radius: 0; padding: 6px 10px; }
QPushButton[segment="true"]:first { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }
QPushButton[segment="true"]:checked { background: #dceff0; color: #075f61; border-color: #66aaac; font-weight: 700; }
QToolButton { color: #47535c; border: 0; padding: 4px 0; font-weight: 600; }
QTreeWidget, QTableWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fa;
    border: 1px solid #d9dfe3;
    border-radius: 4px;
    gridline-color: #e8ecef;
    outline: 0;
}
QHeaderView::section {
    background: #edf1f3;
    color: #4a555d;
    border: 0;
    border-bottom: 1px solid #d7dde1;
    padding: 7px;
    font-weight: 600;
}
QTreeWidget::item, QTableWidget::item { padding: 6px; }
QTreeWidget::item:selected, QTableWidget::item:selected { background: #d9eeee; color: #173f40; }
QFrame#resultBand { background: #ffffff; border-top: 1px solid #dfe4e8; }
QLabel[status="success"] { color: #287a3c; }
QLabel[status="warning"] { color: #a45f00; }
QLabel[status="error"] { color: #b3261e; }
QScrollBar:vertical { background: #eef1f3; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #bbc5cb; min-height: 28px; border-radius: 4px; }
QStatusBar { background: #ffffff; color: #59656e; border-top: 1px solid #dfe4e8; }
"""

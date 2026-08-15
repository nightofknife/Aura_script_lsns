"""Application styling for the Resonance operator console."""

APP_STYLE = """
QMainWindow, QWidget#appRoot {
    background: #f2ebdd;
    color: #38342e;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}
QFrame#navigation {
    background: #e8decc;
    border: 0;
}
QLabel#brandTitle { color: #66745c; font-size: 18px; font-weight: 700; }
QLabel#brandCaption { color: #716a5e; font-size: 11px; }
QPushButton[nav="true"] {
    background: transparent;
    color: #554f46;
    border: 0;
    border-left: 3px solid transparent;
    padding: 10px 14px;
    text-align: left;
    min-height: 24px;
}
QPushButton[nav="true"]:hover { background: #ddd2bf; color: #38342e; }
QPushButton[nav="true"]:checked {
    background: #d9dfcf;
    color: #38342e;
    border-left-color: #77866b;
    font-weight: 600;
}
QFrame#commerceHeader {
    background: #f8f3e8;
    border-bottom: 1px solid #c9b99d;
}
QLabel#commerceTitle { color: #20252b; font-size: 18px; font-weight: 700; }
QLabel#commerceCaption { color: #6c7780; font-size: 11px; }
QPushButton[commerceNav="true"] {
    min-width: 72px;
    padding: 7px 18px;
    background: #eee5d5;
    border-color: #c9b99d;
}
QPushButton[commerceNav="true"]:hover { background: #e2e6d9; border-color: #9aa58e; }
QPushButton[commerceNav="true"]:checked {
    background: #77866b;
    border-color: #77866b;
    color: #ffffff;
    font-weight: 700;
}
QFrame#commerceOverviewPanel {
    background: #f8f3e8;
    border: 1px solid #c9b99d;
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
    background: #77866b;
    border-color: #77866b;
}
QPushButton[commerceRunState="run"]:hover { background: #66745c; }
QPushButton[commerceRunState="stop"] {
    color: #a52a24;
    background: #fff7f6;
    border-color: #cfaaa7;
}
QFrame#statusBand { background: #f8f3e8; border-bottom: 1px solid #c9b99d; }
QFrame#contentPanel { background: #f2ebdd; }
QFrame#actionBar { background: #f8f3e8; border-top: 1px solid #c9b99d; }
QLabel[caption="true"] { color: #6c7780; font-size: 11px; }
QLabel[value="true"] { color: #20252b; font-weight: 600; }
QLabel#stageTitle { color: #20252b; font-size: 19px; font-weight: 700; }
QLabel#pageTitle { color: #20252b; font-size: 17px; font-weight: 700; }
QLabel#sectionTitle { color: #2b333a; font-size: 14px; font-weight: 700; }
QFrame#parameterPanel { background: #f8f3e8; border-right: 1px solid #c9b99d; }
QLabel#passengerRouteTitle { color: #66745c; font-size: 20px; font-weight: 700; padding-top: 4px; }
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
    color: #45523f;
    background: #e1e6d8;
    border-left: 3px solid #77866b;
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
QLabel#passengerTimeline { color: #66745c; font-size: 16px; font-weight: 700; }
QLabel#passengerStageDetail { color: #4f5b64; font-size: 12px; }
QFrame#passengerMetrics {
    background: transparent;
    border-top: 1px solid #dfe5e8;
    border-bottom: 1px solid #dfe5e8;
}
QLabel[metricValue="true"] { color: #20252b; font-size: 14px; font-weight: 700; }
QTextBrowser#passengerDetails { background: #ffffff; color: #4d5962; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextBrowser {
    background: #fffaf0;
    border: 1px solid #c9b99d;
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: #77866b;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextBrowser:focus { border-color: #77866b; }
QPushButton {
    background: #f8f3e8;
    color: #38342e;
    border: 1px solid #c9b99d;
    border-radius: 4px;
    padding: 7px 12px;
    min-height: 22px;
}
QPushButton:hover { background: #e9e8dc; border-color: #8e9a82; }
QPushButton:disabled { color: #9ba4aa; background: #edf0f2; border-color: #dce1e4; }
QPushButton#primaryButton { background: #77866b; border-color: #77866b; color: #ffffff; font-weight: 700; }
QPushButton#primaryButton:hover { background: #66745c; }
QPushButton#dangerButton { color: #9b503e; border-color: #b9785d; }
QPushButton#dangerButton:hover { background: #f3dfd5; }
QPushButton[segment="true"] { border-radius: 0; padding: 6px 10px; }
QPushButton[segment="true"]:first { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }
QPushButton[segment="true"]:checked { background: #dce3d5; color: #4e6048; border-color: #77866b; font-weight: 700; }
QToolButton { color: #47535c; border: 0; padding: 4px 0; font-weight: 600; }
QTreeWidget, QTableWidget {
    background: #fffaf0;
    alternate-background-color: #f3ecdf;
    border: 1px solid #c9b99d;
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
QTreeWidget::item:selected, QTableWidget::item:selected { background: #dce3d5; color: #384335; }
QFrame#resultBand { background: #f8f3e8; border-top: 1px solid #c9b99d; }
QLabel[status="success"] { color: #287a3c; }
QLabel[status="warning"] { color: #a45f00; }
QLabel[status="error"] { color: #b3261e; }
QScrollBar:vertical { background: #e8decc; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #b6aa94; min-height: 28px; border-radius: 4px; }
QStatusBar { background: #f8f3e8; color: #716a5e; border-top: 1px solid #c9b99d; }

QFrame#workflowPanel {
    background: #f8f3e8;
    border: 1px solid #c9b99d;
    border-radius: 6px;
}
QLabel#workflowTitle { color: #38342e; font-size: 19px; font-weight: 700; }
QLabel#workflowProgress { color: #66745c; font-size: 16px; font-weight: 700; padding: 7px 0; }
QListWidget#workflowTaskList, QListWidget#commerceOrderList, QListWidget#settingsCategories {
    background: transparent;
    border: 0;
    outline: 0;
}
QListWidget#workflowTaskList::item {
    background: #fffaf0;
    border: 1px solid #d4c6ad;
    border-radius: 5px;
    min-height: 46px;
}
QListWidget#workflowTaskList::item:selected {
    background: #e1e6d8;
    border-color: #77866b;
}
QFrame#workflowTaskRow, QFrame#commerceStepRow {
    background: #fffaf0;
    border: 1px solid #d4c6ad;
    border-radius: 5px;
}
QFrame#workflowTaskRow[selected="true"] {
    background: #e1e6d8;
    border-color: #77866b;
}
QLabel#commerceStepNumber {
    color: #716a5e;
    font-weight: 700;
    min-width: 18px;
}
QLabel#taskNumber {
    color: #716a5e;
    background: #eee5d5;
    border: 1px solid #c9b99d;
    border-radius: 3px;
    padding: 2px 5px;
}
QLabel#taskName { color: #38342e; font-weight: 600; }
QLabel#taskStatus { color: #9c9589; font-size: 16px; font-weight: 700; }
QLabel#taskStatus[runState="running"] { color: #77866b; }
QLabel#taskStatus[runState="success"] { color: #6f8a5e; }
QLabel#taskStatus[runState="failed"], QLabel#taskStatus[runState="cancelled"] { color: #b9785d; }
QFrame#linenInset, QLabel#linenInsetLabel {
    background: #f2ebdd;
    border: 1px solid #d4c6ad;
    border-radius: 5px;
    padding: 9px;
}
QTreeWidget#workflowRunTree { background: #fffaf0; }
QTextBrowser#workflowLog { background: #f4edde; font-family: "Cascadia Mono", "Consolas"; font-size: 11px; }
QPushButton#quietButton { background: transparent; border-color: #c9b99d; }
QListWidget#settingsCategories::item { padding: 12px 10px; border-left: 3px solid transparent; }
QListWidget#settingsCategories::item:selected {
    background: #dce3d5;
    color: #45523f;
    border-left-color: #77866b;
}
"""

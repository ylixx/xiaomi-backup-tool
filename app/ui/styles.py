# -*- coding: utf-8 -*-
"""暗色 QSS 样式表（时间允许的话可在 UI 中切换亮色）。"""

DARK_QSS = """
QWidget {
    background-color: #1e1f22;
    color: #d7dade;
    font-size: 13px;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
}
QMainWindow, QDialog { background-color: #1e1f22; }
QGroupBox {
    border: 1px solid #3a3d42;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #8ab4f8;
}
QPushButton {
    background-color: #34373c;
    border: 1px solid #4a4d52;
    border-radius: 6px;
    padding: 6px 16px;
    min-height: 26px;
}
QPushButton:hover { background-color: #41454b; }
QPushButton:pressed { background-color: #2c2f34; }
QPushButton:disabled { color: #6b6f76; background-color: #2a2c30; }
QPushButton#primary {
    background-color: #2b6cb0;
    border: 1px solid #3b82f6;
    color: white;
    font-weight: bold;
}
QPushButton#primary:hover { background-color: #3b82f6; }
QPushButton#danger { background-color: #7f1d1d; border-color: #b91c1c; }
QPushButton#danger:hover { background-color: #b91c1c; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget,
QTableWidget {
    background-color: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: #2b6cb0;
}
QTabWidget::pane { border: 1px solid #3a3d42; border-radius: 8px; top: -1px; }
QTabBar::tab {
    background: #26282c;
    border: 1px solid #3a3d42;
    border-bottom: none;
    padding: 8px 22px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 4px;
}
QTabBar::tab:selected { background: #2b6cb0; color: white; }
QProgressBar {
    background-color: #26282c;
    border: 1px solid #3a3d42;
    border-radius: 6px;
    text-align: center;
    height: 20px;
}
QProgressBar::chunk { background-color: #2b6cb0; border-radius: 5px; }
QLabel#deviceName { font-size: 17px; font-weight: bold; color: #ffffff; }
QLabel#hint { color: #9aa0a6; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QTableWidget { gridline-color: #3a3d42; }
QHeaderView::section {
    background-color: #26282c;
    border: none;
    border-right: 1px solid #3a3d42;
    padding: 5px;
    font-weight: bold;
}
QScrollBar:vertical { background: #1e1f22; width: 12px; }
QScrollBar::handle:vertical { background: #4a4d52; border-radius: 6px; min-height: 30px; }
QStatusBar { background-color: #26282c; border-top: 1px solid #3a3d42; }
"""

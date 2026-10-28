#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
程序入口：python main.py

环境要求：Python 3.10+，PySide6（见 requirements.txt）。
日志写入程序目录下 logs/（同时实时显示在界面日志面板）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证以脚本方式运行时也能找到 app 包
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("缺少 PySide6，请先执行：pip install -r requirements.txt")
        return 1

    from app.ui.main_window import MainWindow
    from app.ui.styles import DARK_QSS
    from app.utils import setup_logging

    # 日志目录：优先程序目录下 logs/，不可写时回退到用户目录
    log_dir = BASE_DIR / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
    except OSError:
        log_dir = Path.home() / "MiBackups" / "logs"
    setup_logging(log_dir)

    app = QApplication(sys.argv)
    app.setApplicationName("XiaomiUsbBackup")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

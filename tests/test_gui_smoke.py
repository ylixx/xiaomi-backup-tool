#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI 冒烟自测（需要 PySide6，无显示环境也可运行）：
  QT_QPA_PLATFORM=offscreen python3 tests/test_gui_smoke.py

验证：主窗口可构建、Tab/控件齐全、设备轮询与日志信号不崩溃、
进度面板节流刷新正常（F2/F6 的基础冒烟）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    print("跳过：未安装 PySide6（pip install -r requirements.txt 后可运行）")
    sys.exit(0)

from app.ui.main_window import MainWindow  # noqa: E402
from app.utils import setup_logging  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


def main() -> None:
    setup_logging(HERE / ".tmp_gui" / "logs")
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    check("主窗口构建成功", win is not None)
    check("Tab 数量 = 3", win.tabs.count() == 3, str(win.tabs.count()))
    check("设备卡存在", win.device_card is not None)
    check("日志面板存在", win.log_panel is not None)
    check("备份类别控件齐全",
          len(win.bk_cat_checks) == 6, str(len(win.bk_cat_checks)))
    check("备份/还原进度面板存在",
          win.bk_progress is not None and win.rs_progress is not None)
    check("状态栏状态机初始化",
          "状态" in win.state_label.text(), win.state_label.text())

    # 进度面板刷新（含节流后的 UI 更新路径）
    win._task_kind = "backup"
    win._on_progress({"label": "备份", "percent": 42.5, "done_bytes": 425,
                      "total_bytes": 1000, "total_files": 10, "done_files": 4,
                      "failed_files": 0, "skipped_files": 1, "speed": 1024,
                      "eta": 12.5, "current_file": "/sdcard/a.jpg",
                      "current_file_bytes": 100, "current_file_size": 200})
    check("进度面板更新",
          abs(win.bk_progress.total_bar.value() - 425) <= 1,
          str(win.bk_progress.total_bar.value()))
    check("日志面板可写入",
          (win.log_panel.append("INFO", "测试日志"), True)[1])

    # 处理若干轮事件循环，覆盖定时器/信号路径
    for _ in range(5):
        app.processEvents()

    check("设备轮询后无崩溃", True)
    win.close()
    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

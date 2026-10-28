# -*- coding: utf-8 -*-
"""
主窗口（PySide6）。

结构：
  ┌─ 设备信息卡（型号 / Android 版本 / 存储 / 连接状态）──────────┐
  ├─ Tab：备份 │ 还原 │ 校验                                     │
  │    - 分类勾选 / 自定义目录 / 目标路径 / 并发 / 限速           │
  │    - 开始 / 暂停 / 取消，总进度 + 当前文件进度 + 速度/ETA     │
  ├─ 实时日志区                                                  │
  └─ 状态栏（状态机：未连接→已连接→备份中→完成/失败/取消）        │

线程模型（F2/F6）：
  - 所有 IO / 传输都在 python 工作线程 + 线程池（≤3）执行
  - 主线程只处理信号槽刷新；进度由引擎侧 100ms 节流后上报
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from ..adb_manager import Adb, DeviceInfo, find_adb
from ..backup_engine import BackupEngine, BackupOptions, verify_backup_dir
from ..categories import CATEGORIES, MEDIA_KEYS
from ..manifest import Manifest, STATUS_SUCCESS
from ..restore_engine import RestoreEngine, RestoreOptions
from ..utils import (AdbError, ControlToken, TaskError, add_gui_log_handler,
                     format_eta, get_logger, human_size, human_speed)
from .styles import DARK_QSS

log = get_logger("ui")

STATE_TEXT = {
    "idle": "待机",
    "scanning": "扫描中…",
    "running": "任务执行中…",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "no_device": "未连接设备",
}


# ---------------------------------------------------------------- 桥接
class EngineBridge(QObject):
    """工作线程 → GUI 的信号桥（跨线程信号均为队列连接，线程安全）。"""

    log = Signal(str, str)          # level, message
    progress = Signal(object)       # 进度快照 dict
    scan = Signal(object)           # 扫描/预览信息
    state = Signal(str)             # 状态机
    finished = Signal(object)       # 结束信息
    confirm = Signal(object)        # 请求用户确认
    device_status = Signal(object)  # 设备列表轮询结果

    def __init__(self) -> None:
        super().__init__()
        self._confirm_event = threading.Event()
        self._confirm_result = False

    # 工作线程调用：等待 UI 确认
    def await_confirm(self, info: dict, token: ControlToken) -> bool:
        self._confirm_event.clear()
        self._confirm_result = False
        self.confirm.emit(info)
        while not self._confirm_event.wait(0.2):
            if token.cancelled:
                return False
        return self._confirm_result

    # UI 线程调用：回填确认结果
    def resolve_confirm(self, ok: bool) -> None:
        self._confirm_result = ok
        self._confirm_event.set()

    # NullBridge 兼容接口（供引擎统一调用）
    def emit_log(self, level: str, msg: str) -> None:
        self.log.emit(level, msg)

    def emit_progress(self, data: dict) -> None:
        self.progress.emit(data)

    def emit_scan(self, data: dict) -> None:
        self.scan.emit(data)

    def emit_state(self, state: str) -> None:
        self.state.emit(state)

    def emit_finished(self, data: dict) -> None:
        self.finished.emit(data)


# ---------------------------------------------------------------- 设备卡
class DeviceCard(QGroupBox):
    def __init__(self) -> None:
        super().__init__("设备信息")
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color:#ef4444; font-size:18px;")
        self.name = QLabel("未连接设备")
        self.name.setObjectName("deviceName")
        self.detail = QLabel("请用 USB 数据线连接小米手机，并开启「开发者选项 → USB 调试」")
        self.detail.setObjectName("hint")
        self.detail.setWordWrap(True)
        self.storage = QProgressBar()
        self.storage.setFormat("存储：%v / %m")
        self.btn_refresh = QPushButton("刷新")
        self.btn_guide = QPushButton("USB 调试开启指引")

        top = QHBoxLayout()
        top.addWidget(self.status_dot)
        top.addWidget(self.name)
        top.addStretch(1)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_guide)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.detail)
        lay.addWidget(self.storage)

    def set_device(self, dev: Optional[DeviceInfo], extra_hint: str = "") -> None:
        if dev is None:
            self.status_dot.setStyleSheet("color:#ef4444; font-size:18px;")
            self.name.setText("未连接设备")
            self.detail.setText(
                "请用 USB 数据线连接小米手机，并开启「开发者选项 → USB 调试」" +
                (f"\n{extra_hint}" if extra_hint else ""))
            self.storage.setRange(0, 1)
            self.storage.setValue(0)
            self.storage.setFormat("存储：-")
            return
        if dev.state == "device":
            self.status_dot.setStyleSheet("color:#22c55e; font-size:18px;")
        elif dev.state == "unauthorized":
            self.status_dot.setStyleSheet("color:#eab308; font-size:18px;")
        else:
            self.status_dot.setStyleSheet("color:#ef4444; font-size:18px;")
        self.name.setText(dev.display_name)
        info = (f"序列号：{dev.serial}    Android：{dev.android or '?'}    "
                f"SDK：{dev.sdk or '?'}")
        if dev.state == "unauthorized":
            info += ("\n⚠ 设备未授权 USB 调试：请在手机弹窗中点「允许」，"
                     "并勾选「始终允许」（A3）")
        elif dev.state != "device":
            info += f"\n⚠ 设备状态异常：{dev.state}，请重新插拔数据线（A4）"
        if extra_hint:
            info += "\n" + extra_hint
        self.detail.setText(info)
        if dev.storage_total:
            used = dev.storage_total - dev.storage_free
            self.storage.setRange(0, dev.storage_total // (1024 * 1024))
            self.storage.setValue(used // (1024 * 1024))
            self.storage.setFormat(
                f"存储：已用 {human_size(used)} / 共 {human_size(dev.storage_total)}"
                f"（剩余 {human_size(dev.storage_free)}）")
        else:
            self.storage.setRange(0, 1)
            self.storage.setValue(0)
            self.storage.setFormat("存储：读取失败")


# ---------------------------------------------------------------- 进度区
class ProgressPane(QWidget):
    """总进度 + 当前文件进度 + 速度 / ETA。"""

    def __init__(self, title: str = "任务进度") -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.total_bar = QProgressBar()
        self.total_bar.setRange(0, 1000)
        self.total_label = QLabel(f"{title}：等待开始")
        self.cur_bar = QProgressBar()
        self.cur_bar.setRange(0, 1000)
        self.cur_label = QLabel("当前文件：-")
        self.speed_label = QLabel("速度：-    剩余时间：-")
        self.speed_label.setObjectName("hint")
        lay.addWidget(self.total_label)
        lay.addWidget(self.total_bar)
        lay.addWidget(self.cur_label)
        lay.addWidget(self.cur_bar)
        lay.addWidget(self.speed_label)

    def update_progress(self, d: dict) -> None:
        pct = d.get("percent", 0)
        self.total_bar.setValue(int(pct * 10))
        self.total_label.setText(
            f"{d.get('label', '任务')}进度：{pct:.1f}%    "
            f"文件 {d.get('done_files', 0)}/{d.get('total_files', 0)}"
            f"（跳过 {d.get('skipped_files', 0)}，失败 {d.get('failed_files', 0)}）    "
            f"{human_size(d.get('done_bytes', 0))} / {human_size(d.get('total_bytes', 0))}")
        cur_bytes = d.get("current_file_bytes", 0)
        cur_size = d.get("current_file_size", 0) or 0
        name = d.get("current_file", "") or "-"
        if cur_size > 0:
            self.cur_bar.setRange(0, 1000)
            self.cur_bar.setValue(int(min(1.0, cur_bytes / cur_size) * 1000))
            self.cur_label.setText(
                f"当前文件：{name}    {human_size(cur_bytes)} / {human_size(cur_size)}")
        else:
            self.cur_bar.setRange(0, 0)   # 忙碌指示（大小未知）
            self.cur_label.setText(f"当前文件：{name}    {human_size(cur_bytes)}")
        self.speed_label.setText(
            f"速度：{human_speed(d.get('speed', 0))}    "
            f"剩余时间：{format_eta(d.get('eta', -1))}")

    def reset(self) -> None:
        self.total_bar.setRange(0, 1000)
        self.total_bar.setValue(0)
        self.cur_bar.setRange(0, 1000)
        self.cur_bar.setValue(0)
        self.total_label.setText("任务进度：等待开始")
        self.cur_label.setText("当前文件：-")
        self.speed_label.setText("速度：-    剩余时间：-")


# ---------------------------------------------------------------- 日志面板
class LogPanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("实时日志")
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(3000)   # 限制内存，防长任务膨胀
        self.btn_clear = QPushButton("清空")
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(self.btn_clear)
        lay.addLayout(bar)
        lay.addWidget(self.view)
        self.btn_clear.clicked.connect(self.view.clear)

    def append(self, level: str, msg: str) -> None:
        color = {"ERROR": "#ef4444", "WARN": "#eab308", "INFO": "#d7dade",
                 "DEBUG": "#9aa0a6"}.get(level, "#d7dade")
        self.view.appendHtml(
            f'<span style="color:{color}">[{level}] {msg}</span>')
        self.view.moveCursor(QTextCursor.MoveOperation.End)


# ---------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("小米 USB 一键换机 / 数据备份还原工具")
        self.resize(1080, 860)

        self.bridge = EngineBridge()
        self.adb: Optional[Adb] = None
        self.adb_error: str = ""
        self.device: Optional[DeviceInfo] = None
        self.token: Optional[ControlToken] = None
        self.worker: Optional[threading.Thread] = None
        self._polling = False
        self._task_kind = ""       # backup / restore / verify

        self._build_ui()
        self._connect_signals()
        self._init_adb()

        # 设备轮询（2.5s）：检测掉线/重连（A4）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_device)
        self.timer.start(2500)
        self._poll_device()

        # GUI 日志回调 → 信号（线程安全）
        add_gui_log_handler(lambda lv, m: self.bridge.log.emit(lv, m))

    # -------------------------------------------------- UI 构建
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        self.device_card = DeviceCard()
        root.addWidget(self.device_card)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_backup_tab(), "备份（手机 → 电脑）")
        self.tabs.addTab(self._build_restore_tab(), "还原 / 换机（电脑 → 手机）")
        self.tabs.addTab(self._build_verify_tab(), "校验备份完整性")
        root.addWidget(self.tabs, 1)

        self.log_panel = LogPanel()
        root.addWidget(self.log_panel, 1)

        self.state_label = QLabel("状态：待机")
        self.statusBar().addPermanentWidget(self.state_label)
        self.setCentralWidget(central)
        self.setStyleSheet(DARK_QSS)

    # ---------- 备份页 ----------
    def _build_backup_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 分类
        cat_box = QGroupBox("备份类别（可多选）")
        grid = QGridLayout(cat_box)
        self.bk_cat_checks: Dict[str, QCheckBox] = {}
        for i, key in enumerate(MEDIA_KEYS + ("contacts", "appdata")):
            cat = CATEGORIES[key]
            cb = QCheckBox(cat.label)
            cb.setToolTip(cat.desc)
            if key in ("contacts", "appdata"):
                cb.setChecked(False)
            else:
                cb.setChecked(True)
            self.bk_cat_checks[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        btns = QHBoxLayout()
        b_all = QPushButton("全选")
        b_none = QPushButton("全不选")
        btns.addWidget(b_all)
        btns.addWidget(b_none)
        btns.addStretch(1)
        grid.addLayout(btns, 3, 0, 1, 2)
        b_all.clicked.connect(lambda: [c.setChecked(True)
                                       for c in self.bk_cat_checks.values()])
        b_none.clicked.connect(lambda: [c.setChecked(False)
                                        for c in self.bk_cat_checks.values()])
        lay.addWidget(cat_box)

        # 自定义目录
        dir_box = QGroupBox("自定义手机目录（可选，例如 /sdcard/MyFolder）")
        dl = QHBoxLayout(dir_box)
        self.custom_list = QListWidget()
        self.custom_list.setMaximumHeight(76)
        dl.addWidget(self.custom_list, 1)
        b_add = QPushButton("添加目录")
        b_del = QPushButton("删除选中")
        dl.addWidget(b_add)
        dl.addWidget(b_del)
        b_add.clicked.connect(self._add_custom_dir)
        b_del.clicked.connect(lambda: self.custom_list.takeItem(
            self.custom_list.currentRow()))
        lay.addWidget(dir_box)

        # 目标与选项
        opt_box = QGroupBox("备份选项")
        gl = QGridLayout(opt_box)
        gl.addWidget(QLabel("备份到："), 0, 0)
        self.bk_target = QLineEdit(str(Path.home() / "MiBackups"))
        gl.addWidget(self.bk_target, 0, 1)
        b_browse = QPushButton("浏览…")
        gl.addWidget(b_browse, 0, 2)
        b_browse.clicked.connect(self._pick_backup_target)

        self.bk_resume_cb = QCheckBox("继续之前的备份（断点续传，仅补传未完成文件）")
        gl.addWidget(self.bk_resume_cb, 1, 0, 1, 2)
        self.bk_incremental_cb = QCheckBox(
            "增量备份（对比基准备份，仅传输变更/新增，节省空间）")
        gl.addWidget(self.bk_incremental_cb, 2, 0, 1, 2)
        self.bk_resume_dir = QLineEdit()
        self.bk_resume_dir.setPlaceholderText("基准备份目录（Backup_ 时间戳目录）")
        self.bk_resume_dir.setEnabled(False)
        gl.addWidget(self.bk_resume_dir, 3, 0, 1, 2)
        self.b_base_browse = QPushButton("浏览…")
        gl.addWidget(self.b_base_browse, 3, 2)
        self.b_base_browse.clicked.connect(self._pick_base_dir)
        self.bk_resume_cb.toggled.connect(
            lambda on: self._on_mode_toggled("resume", on))
        self.bk_incremental_cb.toggled.connect(
            lambda on: self._on_mode_toggled("incremental", on))

        self.bk_appdata = QCheckBox("备份应用数据（adb backup，需手机端确认，未 root 不完整）")
        self.bk_appdata.setChecked(False)
        gl.addWidget(self.bk_appdata, 4, 0, 1, 3)
        self.bk_apk = QCheckBox("含 APK")
        self.bk_shared = QCheckBox("含共享存储")
        self.bk_system = QCheckBox("含系统应用")
        self.bk_shared.setChecked(True)
        sub = QHBoxLayout()
        sub.addWidget(self.bk_apk)
        sub.addWidget(self.bk_shared)
        sub.addWidget(self.bk_system)
        sub.addStretch(1)
        gl.addLayout(sub, 5, 0, 1, 3)

        self.bk_src_hash = QCheckBox("传输后与手机端哈希比对（更安全，速度略慢）")
        gl.addWidget(self.bk_src_hash, 6, 0, 1, 3)

        gl.addWidget(QLabel("并发线程："), 7, 0)
        self.bk_workers = QSpinBox()
        self.bk_workers.setRange(1, 4)      # 上限保护，避免 adb 过载（F3）
        self.bk_workers.setValue(3)
        gl.addWidget(self.bk_workers, 7, 1)
        gl.addWidget(QLabel("限速（MB/s，0=不限）："), 8, 0)
        self.bk_limit = QDoubleSpinBox()
        self.bk_limit.setRange(0, 1024)
        self.bk_limit.setDecimals(1)
        gl.addWidget(self.bk_limit, 8, 1)

        self.bk_deepdoc = QCheckBox(
            "深度扫描整机文档（按类型过滤，慢但绝不遗漏，清理前推荐）")
        self.bk_deepdoc.setToolTip(
            "遍历整个手机存储，仅收文档类（Office/PDF/OFD发票/文本/电子书），"
            "微信/QQ/网盘/各类 App 里的文档都能找回；手机端无权限的 App 私有目录仍无法读取。")
        gl.addWidget(self.bk_deepdoc, 9, 0, 1, 3)
        lay.addWidget(opt_box)

        # 控制按钮
        ctl = QHBoxLayout()
        self.bk_start = QPushButton("开始备份")
        self.bk_start.setObjectName("primary")
        self.bk_pause = QPushButton("暂停")
        self.bk_cancel = QPushButton("取消")
        self.bk_cancel.setObjectName("danger")
        self.bk_pause.setEnabled(False)
        self.bk_cancel.setEnabled(False)
        ctl.addWidget(self.bk_start)
        ctl.addWidget(self.bk_pause)
        ctl.addWidget(self.bk_cancel)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        self.bk_progress = ProgressPane("备份")
        lay.addWidget(self.bk_progress)
        return w

    # ---------- 还原页 ----------
    def _build_restore_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        box = QGroupBox("备份目录")
        gl = QGridLayout(box)
        gl.addWidget(QLabel("选择备份："), 0, 0)
        self.rs_dir = QLineEdit()
        self.rs_dir.setPlaceholderText("选择包含 manifest.json 的 Backup_ 目录")
        gl.addWidget(self.rs_dir, 0, 1)
        b_browse = QPushButton("浏览…")
        gl.addWidget(b_browse, 0, 2)
        b_browse.clicked.connect(self._pick_restore_dir)
        self.rs_preview = QPlainTextEdit()
        self.rs_preview.setReadOnly(True)
        self.rs_preview.setMaximumHeight(110)
        self.rs_preview.setPlaceholderText("点击「读取清单」查看将还原的类别 / 文件数 / 大小（D1）")
        gl.addWidget(self.rs_preview, 1, 0, 1, 3)
        b_preview = QPushButton("读取清单")
        gl.addWidget(b_preview, 2, 0)
        b_preview.clicked.connect(self._preview_manifest)
        lay.addWidget(box)

        opt = QGroupBox("还原选项")
        g2 = QGridLayout(opt)
        self.rs_cat_checks: Dict[str, QCheckBox] = {}
        for i, key in enumerate(MEDIA_KEYS):
            cb = QCheckBox(CATEGORIES[key].label.split("（")[0])
            cb.setChecked(True)
            self.rs_cat_checks[key] = cb
            g2.addWidget(cb, 0, i)
        g2.addWidget(QLabel("还原到自定义前缀（可选）："), 1, 0)
        self.rs_prefix = QLineEdit()
        self.rs_prefix.setPlaceholderText("留空 = 还原到手机原始路径")
        g2.addWidget(self.rs_prefix, 1, 1, 1, 2)
        self.rs_overwrite = QCheckBox("强制覆盖已存在文件（默认跳过大小一致的文件）")
        g2.addWidget(self.rs_overwrite, 2, 0, 1, 3)
        self.rs_appdata = QCheckBox("还原应用数据（adb restore，需手机端确认）")
        g2.addWidget(self.rs_appdata, 3, 0, 1, 3)
        g2.addWidget(QLabel("并发线程："), 4, 0)
        self.rs_workers = QSpinBox()
        self.rs_workers.setRange(1, 4)
        self.rs_workers.setValue(3)
        g2.addWidget(self.rs_workers, 4, 1)
        g2.addWidget(QLabel("抽样校验文件数："), 5, 0)
        self.rs_sample = QSpinBox()
        self.rs_sample.setRange(0, 100)
        self.rs_sample.setValue(10)
        g2.addWidget(self.rs_sample, 5, 1)
        lay.addWidget(opt)

        ctl = QHBoxLayout()
        self.rs_start = QPushButton("开始还原")
        self.rs_start.setObjectName("primary")
        self.rs_pause = QPushButton("暂停")
        self.rs_cancel = QPushButton("取消")
        self.rs_cancel.setObjectName("danger")
        self.rs_pause.setEnabled(False)
        self.rs_cancel.setEnabled(False)
        ctl.addWidget(self.rs_start)
        ctl.addWidget(self.rs_pause)
        ctl.addWidget(self.rs_cancel)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        self.rs_progress = ProgressPane("还原")
        lay.addWidget(self.rs_progress)
        return w

    # ---------- 校验页 ----------
    def _build_verify_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("选择备份目录")
        hb = QHBoxLayout(box)
        self.vf_dir = QLineEdit()
        self.vf_dir.setPlaceholderText("选择需要校验的 Backup_ 目录")
        hb.addWidget(self.vf_dir, 1)
        b_browse = QPushButton("浏览…")
        hb.addWidget(b_browse)
        b_browse.clicked.connect(self._pick_verify_dir)
        lay.addWidget(box)

        ctl = QHBoxLayout()
        self.vf_start = QPushButton("开始校验（大小 + SHA256）")
        self.vf_start.setObjectName("primary")
        self.vf_rebuild = QPushButton("重建清单（manifest 丢失/损坏时）")
        self.vf_retransfer = QPushButton("重传不一致文件")
        self.vf_retransfer.setEnabled(False)
        self.vf_cancel = QPushButton("取消")
        self.vf_cancel.setObjectName("danger")
        self.vf_cancel.setEnabled(False)
        ctl.addWidget(self.vf_start)
        ctl.addWidget(self.vf_rebuild)
        ctl.addWidget(self.vf_retransfer)
        ctl.addWidget(self.vf_cancel)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        self.vf_table = QTableWidget(0, 3)
        self.vf_table.setHorizontalHeaderLabels(["文件", "结果", "原因"])
        self.vf_table.horizontalHeader().setStretchLastSection(True)
        self.vf_table.setColumnWidth(0, 520)
        self.vf_table.setColumnWidth(1, 110)
        lay.addWidget(self.vf_table, 1)

        self.vf_summary = QLabel("校验结果：-")
        lay.addWidget(self.vf_summary)
        return w

    # -------------------------------------------------- 信号连接
    def _connect_signals(self) -> None:
        b = self.bridge
        b.log.connect(self.log_panel.append)
        b.progress.connect(self._on_progress)
        b.scan.connect(self._on_scan)
        b.state.connect(self._on_state)
        b.finished.connect(self._on_finished)
        b.confirm.connect(self._on_confirm)
        b.device_status.connect(self._on_device_status)

        self.device_card.btn_refresh.clicked.connect(self._poll_device)
        self.device_card.btn_guide.clicked.connect(self._show_guide)

        self.bk_start.clicked.connect(self._start_backup)
        self.bk_pause.clicked.connect(self._toggle_pause)
        self.bk_cancel.clicked.connect(self._cancel_task)
        self.rs_start.clicked.connect(self._start_restore)
        self.rs_pause.clicked.connect(self._toggle_pause)
        self.rs_cancel.clicked.connect(self._cancel_task)
        self.vf_start.clicked.connect(self._start_verify)
        self.vf_cancel.clicked.connect(self._cancel_task)
        self.vf_rebuild.clicked.connect(self._rebuild_manifest)
        self.vf_retransfer.clicked.connect(self._retransfer_bad)

    # -------------------------------------------------- adb 初始化
    def _init_adb(self) -> None:
        try:
            self.adb = Adb()
            self.adb_error = ""
            log.info("adb 就绪：%s", self.adb.adb_path)
        except AdbError as e:
            self.adb = None
            self.adb_error = str(e)
            self.log_panel.append("ERROR", str(e))
            self.device_card.set_device(None, extra_hint=str(e))

    def _poll_device(self) -> None:
        if self._polling:
            return
        if self.adb is None:
            self._init_adb()
            if self.adb is None:
                return
        self._polling = True

        def work() -> None:
            try:
                devs = self.adb.devices()
                dev = None
                for d in devs:
                    if d.state == "device":
                        dev = d
                        break
                if dev is None and devs:
                    dev = devs[0]
                if dev is not None and dev.state == "device":
                    try:
                        dev = self.adb.device_info(dev.serial)
                    except AdbError:
                        pass
                self.bridge.device_status.emit(dev)
            except AdbError as e:
                self.bridge.device_status.emit(None)
                log.debug("设备轮询失败：%s", e)
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def _on_device_status(self, dev: Optional[DeviceInfo]) -> None:
        prev_serial = self.device.serial if self.device else None
        self.device = dev
        self.device_card.set_device(dev, extra_hint=self.adb_error)
        if dev is None or dev.state != "device":
            if self.worker and self.worker.is_alive():
                self.log_panel.append(
                    "WARN", "⚠ 检测到设备掉线：任务已自动暂停，重新插线后将继续（C2/A4）")
                self._set_state("paused")
        else:
            if prev_serial != dev.serial and self.worker and self.worker.is_alive():
                self.log_panel.append("INFO", "设备已重新连接，任务继续执行")

    # -------------------------------------------------- 任务控制
    def _require_device(self) -> Optional[DeviceInfo]:
        if self.adb is None:
            self._init_adb()
        if self.adb is None:
            QMessageBox.warning(self, "缺少 adb", self.adb_error or "未找到 adb")
            return None
        if self.device is None or self.device.state != "device":
            QMessageBox.warning(
                self, "未连接设备",
                "没有检测到可用的小米手机。\n\n"
                "请确认：\n1. USB 数据线已连接且为「传输文件」模式；\n"
                "2. 开发者选项中已开启「USB 调试」；\n"
                "3. 手机弹窗中点击了「允许 USB 调试」。")
            return None
        return self.device

    def _start_task(self, kind: str, target) -> bool:
        """启动工作线程的通用入口（调用前需先创建 self.token）。"""
        if self.worker and self.worker.is_alive():
            QMessageBox.information(self, "任务进行中", "请先等待当前任务完成或点击「取消」。")
            return False
        if self.token is None:
            self.token = ControlToken()
        self._task_kind = kind
        self.log_panel.view.clear()
        self._set_state("scanning")
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        for btn in (self.bk_start, self.rs_start, self.vf_start):
            btn.setEnabled(False)
        self.bk_cancel.setEnabled(True)
        self.rs_cancel.setEnabled(True)
        self.vf_cancel.setEnabled(True)
        self.bk_pause.setEnabled(kind == "backup")
        self.rs_pause.setEnabled(kind == "restore")
        return True

    def _on_mode_toggled(self, which: str, on: bool) -> None:
        """「继续」与「增量」互斥：只能选一种基线模式。"""
        if on:
            if which == "resume":
                self.bk_incremental_cb.setChecked(False)
            else:
                self.bk_resume_cb.setChecked(False)
        self.bk_resume_dir.setEnabled(
            self.bk_resume_cb.isChecked() or self.bk_incremental_cb.isChecked())
        self.b_base_browse.setEnabled(self.bk_resume_dir.isEnabled())

    def _start_backup(self) -> None:
        dev = self._require_device()
        if dev is None:
            return
        cats = [k for k, c in self.bk_cat_checks.items() if c.isChecked()
                and k in MEDIA_KEYS]
        do_contacts = self.bk_cat_checks["contacts"].isChecked()
        do_appdata = self.bk_cat_checks["appdata"].isChecked() or self.bk_appdata.isChecked()
        custom = [self.custom_list.item(i).text()
                  for i in range(self.custom_list.count())]
        if self.bk_deepdoc.isChecked() and "docs" not in cats:
            cats.append("docs")  # 深度扫描文档时确保文档类别参与
        if not cats and not custom and not do_contacts and not do_appdata:
            QMessageBox.information(self, "未选择", "请至少勾选一个备份类别或自定义目录。")
            return
        resume_dir = None
        if self.bk_resume_cb.isChecked() or self.bk_incremental_cb.isChecked():
            resume_dir = self.bk_resume_dir.text().strip()
            if not resume_dir or not Path(resume_dir).is_dir():
                QMessageBox.warning(
                    self, "路径无效",
                    "请选择已存在的 Backup_ 目录作为基线（续传/增量备份）。")
                return
        else:
            target = self.bk_target.text().strip()
            if not target:
                QMessageBox.warning(self, "路径无效", "请选择备份目标文件夹。")
                return
            try:
                from ..utils import ensure_writable_dir
                ensure_writable_dir(target)
            except TaskError as e:
                QMessageBox.warning(self, "目录不可用", str(e))
                return

        opts = BackupOptions(
            target_parent=self.bk_target.text().strip(),
            categories=cats,
            custom_dirs=custom,
            include_apk=self.bk_apk.isChecked(),
            include_shared=self.bk_shared.isChecked(),
            include_system=self.bk_system.isChecked(),
            do_contacts=do_contacts,
            do_appdata=do_appdata,
            concurrency=self.bk_workers.value(),
            speed_limit=self.bk_limit.value() * 1024 * 1024,
            resume_dir=resume_dir,
            incremental=self.bk_incremental_cb.isChecked(),
            deep_doc_scan=self.bk_deepdoc.isChecked(),
            verify_src_hash=self.bk_src_hash.isChecked(),
            wait_confirm=True,
        )
        self.token = ControlToken()
        engine = BackupEngine(self.adb, dev, opts, self.bridge, self.token)
        self.bk_progress.reset()
        self._start_task("backup", engine.run)

    def _start_restore(self) -> None:
        dev = self._require_device()
        if dev is None:
            return
        d = self.rs_dir.text().strip()
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, "路径无效", "请选择包含 manifest.json 的备份目录。")
            return
        cats = [k for k, c in self.rs_cat_checks.items() if c.isChecked()]
        opts = RestoreOptions(
            backup_dir=d,
            categories=cats,
            target_prefix=self.rs_prefix.text().strip(),
            overwrite=self.rs_overwrite.isChecked(),
            concurrency=self.rs_workers.value(),
            sample_verify=self.rs_sample.value(),
            do_appdata_restore=self.rs_appdata.isChecked(),
            wait_confirm=True,
        )
        self.token = ControlToken()
        engine = RestoreEngine(self.adb, dev, opts, self.bridge, self.token)
        self.rs_progress.reset()
        self._start_task("restore", engine.run)

    def _start_verify(self) -> None:
        d = self.vf_dir.text().strip()
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, "路径无效", "请选择需要校验的备份目录。")
            return
        self.vf_table.setRowCount(0)
        self.vf_summary.setText("校验结果：校验中…")
        self.vf_retransfer.setEnabled(False)
        self.token = ControlToken()
        token = self.token

        def target() -> None:
            try:
                report = verify_backup_dir(d, bridge=self.bridge, token=token)
                self.bridge.finished.emit({"state": "completed",
                                           "verify_report": report})
            except Exception as e:  # noqa: BLE001
                self.bridge.emit_log("ERROR", f"校验失败：{e}")
                self.bridge.finished.emit({"state": "failed", "error": str(e)})

        self._start_task("verify", target)

    def _preview_manifest(self) -> None:
        d = self.rs_dir.text().strip()
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, "路径无效", "请选择备份目录。")
            return

        def work() -> None:
            try:
                m = Manifest.load(d)
            except TaskError:
                self.bridge.emit_log("WARN", "清单缺失/损坏，尝试从目录重建预览")
                m = Manifest.rebuild(d)
            st = m.stats()
            lines = [f"备份目录：{d}",
                     f"设备：{m.device.get('brand', '')} {m.device.get('model', '')}"
                     f"（{m.device.get('serial', '')}）",
                     f"创建时间：{m.created}", "-" * 46]
            for cat, s in st["by_category"].items():
                lines.append(f"  {cat:<10} {s['count']:>6} 个文件，"
                             f"{human_size(s['size'])}")
            lines.append("-" * 46)
            lines.append(f"  合计 {st['count']} 个文件，{human_size(st['size'])}")
            self.bridge.scan.emit({"preview_text": "\n".join(lines)})

        threading.Thread(target=work, daemon=True).start()

    def _toggle_pause(self) -> None:
        if not self.token:
            return
        if self.token.paused:
            self.token.resume()
            self.log_panel.append("INFO", "任务已继续")
            self._set_state("running")
            self.bk_pause.setText("暂停")
            self.rs_pause.setText("暂停")
        else:
            self.token.pause()
            self.log_panel.append("WARN", "任务已暂停（当前文件停在安全断点）")
            self._set_state("paused")
            self.bk_pause.setText("继续")
            self.rs_pause.setText("继续")

    def _cancel_task(self) -> None:
        if self.token:
            self.token.cancel("用户取消")
            self.log_panel.append("WARN", "正在取消任务…")

    # -------------------------------------------------- 信号槽
    def _on_progress(self, d: dict) -> None:
        if self._task_kind == "backup":
            self.bk_progress.update_progress(d)
        elif self._task_kind == "restore":
            self.rs_progress.update_progress(d)

    def _on_scan(self, d: dict) -> None:
        if "preview_text" in d:
            self.rs_preview.setPlainText(d["preview_text"])
            return
        if d.get("scanning"):
            self._set_state("scanning")
            self.log_panel.append(
                "DEBUG", f"扫描进度：已遍历 {d.get('scanned', 0)}，"
                         f"命中 {d.get('found', 0)}")
            return
        # 扫描结果 / 预览结果
        cats = d.get("by_category", {})
        lines = [f"预计传输 {d.get('count', 0)} 个文件，共 {human_size(d.get('size', 0))}"]
        if d.get("skipped"):
            lines.append(f"续传跳过已完成文件 {d['skipped']} 个")
        if d.get("missing"):
            lines.append(f"备份目录缺失文件 {d['missing']} 个（已跳过）")
        for cat, s in cats.items():
            lines.append(f"  - {cat}: {s['count']} 个，{human_size(s['size'])}")
        if self._task_kind == "restore":
            self.rs_preview.setPlainText("\n".join(lines))
        self.log_panel.append("INFO", " / ".join(lines[:1]))
        for ln in lines[1:]:
            self.log_panel.append("INFO", ln)

    def _on_state(self, state: str) -> None:
        self._set_state(state)

    def _set_state(self, state: str) -> None:
        self.state_label.setText(f"状态：{STATE_TEXT.get(state, state)}")

    def _on_finished(self, d: dict) -> None:
        state = d.get("state", "")
        for btn in (self.bk_start, self.rs_start, self.vf_start):
            btn.setEnabled(True)
        for btn in (self.bk_cancel, self.rs_cancel, self.vf_cancel):
            btn.setEnabled(False)
        self.bk_pause.setEnabled(False)
        self.rs_pause.setEnabled(False)
        self.bk_pause.setText("暂停")
        self.rs_pause.setText("暂停")
        self._set_state(state)

        if "verify_report" in d:
            report = d["verify_report"]
            self._fill_verify_table(report)
            bad = len(report.get("bad", []))
            self.vf_summary.setText(
                f"校验结果：通过 {len(report.get('ok', []))} 个，"
                f"不一致/缺失 {bad} 个" +
                (f"（可点击「重传不一致文件」修复）" if bad else ""))
            self.vf_retransfer.setEnabled(bad > 0)
            return

        if state == "completed":
            ok = d.get("success", 0)
            fail = d.get("failed", 0)
            pending = d.get("pending", 0)
            msg = f"任务完成：成功 {ok} 个"
            if fail:
                msg += f"，失败 {fail} 个"
            if pending:
                msg += f"，未完成 {pending} 个（可续传）"
            if d.get("backup_dir"):
                msg += f"\n备份目录：{d['backup_dir']}"
            QMessageBox.information(self, "完成", msg)
        elif state == "cancelled":
            QMessageBox.information(
                self, "已取消", "任务已取消。已传输的文件已记录，可稍后继续（断点续传）。")
        elif state == "failed":
            QMessageBox.warning(self, "失败", d.get("error", "任务失败，请查看日志。"))

    def _on_confirm(self, info: dict) -> None:
        size = human_size(info.get("size", 0))
        ret = QMessageBox.question(
            self, "确认开始",
            f"预计传输 {info.get('count', 0)} 个文件，共 {size}。\n\n"
            f"目标空间检查已通过。是否开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        self.bridge.resolve_confirm(ret == QMessageBox.StandardButton.Yes)

    def _fill_verify_table(self, report: dict) -> None:
        rows = report.get("bad", []) + [
            {"id": i, "reason": "校验通过"} for i in report.get("ok", [])[:200]]
        self.vf_table.setRowCount(len(rows))
        for r, item in enumerate(rows):
            ok = item["reason"] == "校验通过"
            self.vf_table.setItem(r, 0, QTableWidgetItem(str(item["id"])))
            self.vf_table.setItem(r, 1,
                                  QTableWidgetItem("通过" if ok else "失败"))
            self.vf_table.setItem(r, 2, QTableWidgetItem(str(item["reason"])))

    # -------------------------------------------------- 小工具
    def _add_custom_dir(self) -> None:
        text, ok = QInputDialog.getText(
            self, "添加手机目录", "请输入手机上的绝对路径（如 /sdcard/MyFolder）：")
        if ok and text.strip().startswith("/"):
            self.custom_list.addItem(text.strip())
        elif ok:
            QMessageBox.warning(self, "路径无效", "请输入以 / 开头的手机端绝对路径。")

    def _pick_backup_target(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择备份目标文件夹",
                                             self.bk_target.text())
        if d:
            self.bk_target.setText(d)
            if self.bk_resume_cb.isChecked() or self.bk_incremental_cb.isChecked():
                self.bk_resume_dir.setText(d)

    def _pick_restore_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择备份目录", self.rs_dir.text())
        if d:
            self.rs_dir.setText(d)
            self._preview_manifest()

    def _pick_verify_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择备份目录", self.vf_dir.text())
        if d:
            self.vf_dir.setText(d)

    def _pick_base_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择基准备份目录",
                                             self.bk_resume_dir.text())
        if d:
            self.bk_resume_dir.setText(d)

    def _rebuild_manifest(self) -> None:
        d = self.vf_dir.text().strip()
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, "路径无效", "请选择备份目录。")
            return

        def work() -> None:
            try:
                m = Manifest.rebuild(d)
                self.bridge.emit_log(
                    "INFO", f"清单已重建：{len(m.entries)} 个文件（哈希将在下次校验时补算）")
                self.bridge.finished.emit({"state": "completed"})
            except TaskError as e:
                self.bridge.emit_log("ERROR", f"重建清单失败：{e}")
                self.bridge.finished.emit({"state": "failed", "error": str(e)})

        self._start_task("verify", work)

    def _retransfer_bad(self) -> None:
        """E3：把校验不一致的文件标记为待传并重新备份（覆盖修复）。"""
        d = self.vf_dir.text().strip()
        dev = self._require_device()
        if dev is None or not d:
            return
        try:
            m = Manifest.load(d)
        except TaskError as e:
            QMessageBox.warning(self, "无法读取清单", str(e))
            return
        bad_ids = [self.vf_table.item(r, 0).text()
                   for r in range(self.vf_table.rowCount())
                   if self.vf_table.item(r, 1)
                   and self.vf_table.item(r, 1).text() == "失败"]
        if not bad_ids:
            QMessageBox.information(self, "无需重传", "没有不一致的文件。")
            return
        opts = BackupOptions(
            target_parent=str(Path(d).parent),
            categories=list(MEDIA_KEYS),
            resume_dir=d,
            only_ids=bad_ids,
            concurrency=self.bk_workers.value(),
            wait_confirm=True,
        )
        self.token = ControlToken()
        engine = BackupEngine(self.adb, dev, opts, self.bridge, self.token)
        self.bk_progress.reset()
        self._start_task("backup", engine.run)

    def _show_guide(self) -> None:
        QMessageBox.information(
            self, "小米手机开启 USB 调试",
            "1. 设置 → 我的设备 → 全部参数与信息 → 连续点击「OS 版本」7 次开启开发者模式\n"
            "2. 设置 → 更多设置 → 开发者选项 → 打开「USB 调试」\n"
            "3. 同时打开「USB 调试（安全设置）」和「USB 安装」（部分功能需要）\n"
            "4. 连接电脑后，手机通知栏选择 USB 用途为「传输文件 (MTP)」\n"
            "5. 首次连接会弹出「允许 USB 调试吗？」→ 勾选「始终允许」并确认\n"
            "6. 建议：设置 → 息屏与锁屏 → 充电时屏幕不休眠（避免息屏中断传输）\n\n"
            "HyperOS/MIUI 提示：若提示「无法连接」，请更换数据线/USB 口，"
            "并关闭「MIUI 优化」后重启再试（部分机型）。")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker and self.worker.is_alive():
            ret = QMessageBox.question(
                self, "任务进行中",
                "当前任务仍在执行，退出会中断传输（已传文件可续传）。确定退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.token:
                self.token.cancel("程序退出")
        event.accept()

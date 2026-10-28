# -*- coding: utf-8 -*-
"""
公共工具模块：
  - 日志（文件 + GUI 实时面板）
  - 哈希 / 体积 / 速度格式化
  - 取消 / 暂停控制令牌
  - 节流器（避免 UI 信号风暴）
  - 指数退避重试、原子化 JSON 写入

设计原则：本模块不依赖 Qt，方便在无 GUI 环境下做自测。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

# 流式读写块大小（1MB），大文件按此块顺序读写，内存占用恒定
CHUNK = 1024 * 1024

APP_NAME = "XiaomiUsbBackup"
APP_VERSION = "1.0.0"


# ---------------------------------------------------------------- exceptions
class TaskCancelled(Exception):
    """用户主动取消任务。"""


class TaskError(Exception):
    """可展示给用户的友好错误（message 为中文说明）。"""


class DeviceGoneError(TaskError):
    """设备掉线 / 被拔出。"""


class AdbError(TaskError):
    """adb 命令执行失败。"""


# ---------------------------------------------------------------- logging
_logger = logging.getLogger(APP_NAME)
_logger.setLevel(logging.DEBUG)
_gui_handler: Optional[logging.Handler] = None


def setup_logging(log_dir: str | os.PathLike) -> logging.Logger:
    """初始化日志：滚动文件（5MB x 3）+ 控制台。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{APP_NAME}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s", "%H:%M:%S"
    )

    fh = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    # 防止重复初始化时 handler 叠加
    _logger.handlers.clear()
    _logger.addHandler(fh)
    _logger.addHandler(ch)
    _logger.info("日志初始化完成 -> %s", log_file)
    return _logger


def get_logger(name: str = "") -> logging.Logger:
    return _logger.getChild(name) if name else _logger


def add_gui_log_handler(fn: Callable[[str, str], None]) -> None:
    """注册 GUI 日志回调 fn(level, message)，由 UI 转成 Qt 信号（线程安全）。"""
    global _gui_handler

    class _GuiHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                fn(record.levelname, self.format(record))
            except Exception:  # GUI 回调异常不能影响主流程
                pass

    if _gui_handler is not None:
        _logger.removeHandler(_gui_handler)
    _gui_handler = _GuiHandler()
    _gui_handler.setFormatter(logging.Formatter("%(message)s"))
    _gui_handler.setLevel(logging.INFO)
    _logger.addHandler(_gui_handler)


# ---------------------------------------------------------------- format
def human_size(n: float) -> str:
    """字节数 → 人类可读体积。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def human_speed(bps: float) -> str:
    return f"{human_size(bps)}/s" if bps and bps > 0 else "-"


def format_eta(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- hash
def hash_file(path: str | os.PathLike, algo: str = "sha256",
              cancel: Optional["ControlToken"] = None,
              progress: Optional[Callable[[int], None]] = None) -> str:
    """流式计算文件哈希，内存占用恒定（1MB 块）。"""
    h = hashlib.new(algo)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None:
                cancel.check()
            buf = f.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if progress:
                progress(done)
    return h.hexdigest()


def sha256_file(path: str | os.PathLike, **kw) -> str:
    return hash_file(path, "sha256", **kw)


def md5_file(path: str | os.PathLike, **kw) -> str:
    return hash_file(path, "md5", **kw)


# ---------------------------------------------------------------- control
class ControlToken:
    """跨线程的 取消 / 暂停 控制令牌。

    - cancel():  置取消标记，并唤醒所有暂停中的线程（让它们立刻退出）
    - pause():   进入暂停（工作线程在块边界自旋等待）
    - resume():  恢复
    - check():   若已取消抛 TaskCancelled
    - wait_if_paused(): 暂停中阻塞，恢复或取消后返回
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.reason: str = ""  # 取消/中断原因（展示用）

    def cancel(self, reason: str = "") -> None:
        if reason:
            self.reason = reason
        self._cancel.set()
        self._resume.set()  # 唤醒暂停线程，让其尽快退出

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise TaskCancelled(self.reason or "任务已取消")

    def wait_if_paused(self, timeout: float = 0.2) -> None:
        while not self._resume.wait(timeout):
            self.check()
        self.check()


class Throttler:
    """最小间隔节流器：100ms 内只放行一次，避免 UI 信号风暴（F6）。"""

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self._last = 0.0
        self._lock = threading.Lock()

    def ready(self, force: bool = False) -> bool:
        with self._lock:
            now = time.monotonic()
            if force or (now - self._last) >= self.interval:
                self._last = now
                return True
            return False


# ---------------------------------------------------------------- retry
def retry_call(fn: Callable[[], Any], tries: int = 3, base_delay: float = 1.0,
               max_delay: float = 15.0,
               on_retry: Optional[Callable[[int, Exception], None]] = None,
               cancel: Optional[ControlToken] = None) -> Any:
    """指数退避重试（默认 3 次）。

    fn 抛出 TaskCancelled 直接透传；其它异常按 1s/2s/4s... 退避重试。
    """
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        if cancel is not None:
            cancel.check()
        try:
            return fn()
        except TaskCancelled:
            raise
        except Exception as e:  # noqa: BLE001 —— 统一重试处理
            last = e
            if attempt >= tries:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= (1 + random.uniform(0, 0.25))  # 抖动，避免同时重试
            if on_retry:
                on_retry(attempt, e)
            get_logger("retry").warning(
                "第 %d 次失败（%s），%.1fs 后重试", attempt, e, delay
            )
            # 可取消的睡眠
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if cancel is not None:
                    cancel.check()
                time.sleep(0.1)
    assert last is not None
    raise last


# ---------------------------------------------------------------- fs
def atomic_write_json(path: str | os.PathLike, data: Any) -> None:
    """先写 .tmp 再 os.replace，保证 manifest 不会因崩溃写坏（E2）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: str | os.PathLike) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dir_free_bytes(path: str | os.PathLike) -> int:
    """本地目录所在磁盘剩余空间（字节）。目录不存在时向上找存在的父目录。"""
    p = Path(path)
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return shutil.disk_usage(str(p)).free


def ensure_writable_dir(path: str | os.PathLike) -> Path:
    """校验目录可写，不可用时抛出中文友好异常（B2）。"""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TaskError(f"无法创建目录：{p}\n原因：{e}") from e
    probe = p / f".write_test_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise TaskError(f"目录没有写入权限：{p}\n原因：{e}") from e
    return p


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

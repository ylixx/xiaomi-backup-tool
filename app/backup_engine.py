# -*- coding: utf-8 -*-
"""
备份引擎（手机 → 电脑）。

流程：
  1. 扫描：按类别流式遍历手机目录，生成文件清单（路径/大小/mtime）
  2. 预检：目标盘剩余空间、续传清单合并（跳过已完成且校验一致的文件）
  3. 确认：把「预计文件数 / 总大小」交给 UI 确认（B3）
  4. 传输：线程池并发（默认 ≤3），大文件分块流式，失败指数退避重试 3 次
  5. 记录：每个文件完成即写 manifest.json（原子化），支持任意时刻中断续传

所有耗时操作都在本引擎线程/线程池中执行，UI 仅通过桥接信号刷新（F2/F6）。
"""
from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adb_manager import (STREAM_THRESHOLD, Adb, DeviceInfo, DeviceMonitor)
from .categories import CATEGORIES, DOC_EXT
from .manifest import (INFO_NAME, STATUS_FAILED, STATUS_PENDING,
                       STATUS_REMOVED, STATUS_SKIPPED, STATUS_SUCCESS,
                       Entry, Manifest)
from .utils import (ControlToken, TaskCancelled, TaskError, DeviceGoneError,
                    AdbError, Throttler, dir_free_bytes, ensure_writable_dir,
                    format_eta, get_logger, human_size, now_stamp,
                    retry_call, sha256_file)

log = get_logger("backup")

# 增量备份跳过前做本地 sha256 校验的体积上限；超过则改用「大小+mtime」快速比对，
# 避免每次增量都对超大文件重算哈希（仍保证未变化文件不被重传）。
INCREMENTAL_LOCAL_VERIFY_MAX = 200 * 1024 * 1024


# ---------------------------------------------------------------- 桥接
class NullBridge:
    """无 GUI 时的空桥接（自测/命令行用）。"""

    def emit_log(self, level: str, msg: str) -> None:
        print(f"[{level}] {msg}")

    def emit_progress(self, data: dict) -> None:
        pass

    def emit_scan(self, data: dict) -> None:
        pass

    def emit_state(self, state: str) -> None:
        pass

    def emit_finished(self, data: dict) -> None:
        pass

    def await_confirm(self, info: dict, token: ControlToken) -> bool:
        return True


# ---------------------------------------------------------------- 进度聚合
class ProgressAggregator:
    """多线程进度聚合 + 节流上报（100ms 合并一次，F6）。

    done_bytes = 已完成文件字节 + 当前各活动文件已传字节
    """

    def __init__(self, total_bytes: int, total_files: int,
                 bridge, label: str = "备份") -> None:
        self.total_bytes = max(total_bytes, 1)
        self.total_files = total_files
        self.bridge = bridge
        self.label = label
        self.done_files = 0
        self.failed_files = 0
        self.skipped_files = 0
        self._finished_bytes = 0
        self._active: Dict[str, int] = {}
        self._sizes: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._throttler = Throttler(0.1)
        self._start = time.monotonic()
        self._speeds: List[Tuple[float, int]] = []
        self.current_file = ""

    # -- 更新入口（线程安全） ------------------------------------
    def file_start(self, name: str, size: int = 0) -> None:
        with self._lock:
            self.current_file = name
            self._active[name] = 0
            self._sizes[name] = size

    def file_progress(self, name: str, nbytes: int) -> None:
        with self._lock:
            self._active[name] = nbytes

    def file_done(self, name: str, nbytes: int, ok: bool = True,
                  skipped: bool = False) -> None:
        with self._lock:
            self._active.pop(name, None)
            if skipped:
                self.skipped_files += 1
            elif ok:
                self.done_files += 1
                self._finished_bytes += nbytes
            else:
                self.failed_files += 1
                self._finished_bytes += nbytes  # 失败也按已尝试字节计入避免倒退

    # -- 快照 ------------------------------------------------------
    def snapshot(self, force: bool = False) -> Optional[dict]:
        if not self._throttler.ready(force=force):
            return None
        with self._lock:
            active_bytes = sum(self._active.values())
            done = self._finished_bytes + active_bytes
        elapsed = max(time.monotonic() - self._start, 0.001)
        self._speeds.append((time.monotonic(), done))
        # 只保留最近 30 秒样本算平均速度
        cutoff = time.monotonic() - 30
        self._speeds = [s for s in self._speeds if s[0] >= cutoff]
        if len(self._speeds) >= 2:
            dt = self._speeds[-1][0] - self._speeds[0][0]
            db = self._speeds[-1][1] - self._speeds[0][1]
            speed = db / dt if dt > 0 else 0
        else:
            speed = done / elapsed
        remain = max(self.total_bytes - done, 0)
        eta = remain / speed if speed > 0 else -1
        return {
            "label": self.label,
            "total_bytes": self.total_bytes,
            "done_bytes": done,
            "percent": min(100.0, done * 100.0 / self.total_bytes),
            "total_files": self.total_files,
            "done_files": self.done_files,
            "failed_files": self.failed_files,
            "skipped_files": self.skipped_files,
            "speed": speed,
            "eta": eta,
            "current_file": self.current_file,
            "current_file_bytes": self._active.get(self.current_file, 0),
            "current_file_size": self._sizes.get(self.current_file, 0),
        }


# ---------------------------------------------------------------- 网络韧性
def transfer_with_resilience(fn: Callable[[], None], adb: Adb, serial: str,
                             bridge, token: ControlToken,
                             label: str = "") -> None:
    """带断线自动恢复的传输执行器：

    - 普通错误：指数退避重试最多 3 次（C3）
    - 设备掉线（DeviceGoneError）：不计入重试次数，等待重连后从断点继续（C2/A4）
    - 用户取消：直接抛出
    """
    tries = 3
    attempt = 0
    while True:
        try:
            fn()
            return
        except TaskCancelled:
            raise
        except DeviceGoneError as e:
            bridge.emit_log("WARN", f"{label}设备断开：{e}\n请重新插拔 USB 数据线，"
                                    f"程序会自动检测重连并从断点继续…")
            bridge.emit_state("paused")
            mon = DeviceMonitor(adb, serial, interval=2.0)
            if mon.wait_reconnect(timeout=600, cancel=token):
                bridge.emit_log("INFO", f"{label}设备已重连，从断点继续传输")
                bridge.emit_state("running")
                continue
            raise TaskError("设备超过 10 分钟未重连，任务已中止。\n"
                            "已传输的文件状态已保存，重新插入设备后可继续备份。")
        except Exception as e:  # noqa: BLE001
            attempt += 1
            if attempt >= tries:
                raise
            delay = min(15.0, 1.0 * (2 ** (attempt - 1)))
            bridge.emit_log("WARN", f"{label}第 {attempt} 次失败：{e}，"
                                    f"{delay:.0f}s 后自动重试")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                token.check()
                time.sleep(0.1)


# ---------------------------------------------------------------- 备份引擎
@dataclass
class BackupOptions:
    target_parent: str                 # 用户选择的目标目录（其下建 Backup_时间戳）
    categories: List[str] = field(default_factory=list)   # photos/audio/video/docs...
    custom_dirs: List[str] = field(default_factory=list)  # 自定义手机目录
    include_apk: bool = False
    include_shared: bool = True
    include_system: bool = False
    do_contacts: bool = False          # 联系人/短信/通话记录导出
    do_appdata: bool = False           # adb backup 应用数据
    concurrency: int = 3
    speed_limit: float = 0.0           # 字节/秒，0=不限速
    resume_dir: Optional[str] = None   # 续传：已存在的 Backup_ 目录
    incremental: bool = False          # 增量备份：基于 resume_dir 对比，仅传变更/新增
    deep_doc_scan: bool = False        # 深度扫描整机文档（遍历 /sdcard，按类型过滤，避免遗漏）
    verify_src_hash: bool = False      # 传输后与设备端哈希比对（较慢）
    wait_confirm: bool = True          # 扫描后等待 UI 确认
    only_ids: Optional[List[str]] = None  # 仅重传指定条目（E3 修复不一致文件）


class BackupEngine:
    """执行一次备份任务。run() 应在工作线程中调用。"""

    def __init__(self, adb: Adb, device: DeviceInfo, opts: BackupOptions,
                 bridge=None, token: Optional[ControlToken] = None) -> None:
        self.adb = adb
        self.device = device
        self.opts = opts
        self.bridge = bridge or NullBridge()
        self.token = token or ControlToken()
        self.manifest: Optional[Manifest] = None
        self.backup_dir: Optional[Path] = None
        self._manifest_lock = threading.Lock()
        self._save_throttler = Throttler(2.0)   # manifest 落盘节流

    # -------------------------------------------------- 主流程
    def run(self) -> None:
        try:
            self._run()
        except TaskCancelled:
            self._finalize("cancelled")
        except TaskError as e:
            log.error("备份失败：%s", e)
            self.bridge.emit_log("ERROR", str(e))
            self._finalize("failed", error=str(e))
        except Exception as e:  # noqa: BLE001 —— 兜底，禁止裸异常炸 UI（G1）
            log.exception("备份发生未预期异常")
            self.bridge.emit_log("ERROR", f"发生未预期异常：{e}")
            self._finalize("failed", error=str(e))

    def _run(self) -> None:
        opts = self.opts
        # ---------- 目标准备 ----------
        if opts.resume_dir:
            self.backup_dir = Path(opts.resume_dir)
            ensure_writable_dir(self.backup_dir)
            try:
                self.manifest = Manifest.load(self.backup_dir)
                self.manifest.device = self.device.__dict__
                self.bridge.emit_log(
                    "INFO", f"继续备份：{self.backup_dir}"
                    f"（已完成 {len(self.manifest.by_status(STATUS_SUCCESS))} 个文件将跳过）")
            except TaskError:
                self.bridge.emit_log("WARN", "原清单不可用，将重新扫描并重建清单")
                self.manifest = Manifest(self.backup_dir, self.device.__dict__)
        else:
            parent = ensure_writable_dir(opts.target_parent)
            self.backup_dir = parent / f"Backup_{now_stamp()}"
            self.backup_dir.mkdir(parents=True, exist_ok=False)
            self.manifest = Manifest(self.backup_dir, self.device.__dict__)
            self.manifest.save_info({"categories": opts.categories,
                                     "custom_dirs": opts.custom_dirs})
            self.bridge.emit_log("INFO", f"备份目录：{self.backup_dir}")

        # ---------- 扫描 ----------
        self.bridge.emit_state("scanning")
        entries = self._scan()
        if not entries:
            raise TaskError("没有扫描到任何可备份的文件。\n"
                            "请检查：手机是否已授权 USB 调试、所选类别目录是否为空。")

        # ---------- 与续传清单合并：跳过已完成且一致的文件 ----------
        if self.opts.incremental:
            entries = self._merge_incremental(entries)
        else:
            entries = self._merge_resume(entries)

        # ---------- E3：仅重传指定的不一致文件 ----------
        if self.opts.only_ids:
            wanted = set(self.opts.only_ids)
            entries = [e for e in entries if e.id in wanted]
            for e in entries:      # 强制重置为待传
                e.status = STATUS_PENDING
                e.sha256 = ""
                with self._manifest_lock:
                    self.manifest.update(e.id, status=STATUS_PENDING, sha256="")
            self.bridge.emit_log("INFO", f"修复模式：将重新传输 {len(entries)} 个不一致文件")

        pending = [e for e in entries if e.status != STATUS_SUCCESS]
        total_bytes = sum(e.size for e in pending)
        total_files = len(pending)
        stats = self.manifest.stats()
        self.bridge.emit_scan({
            "count": total_files,
            "size": total_bytes,
            "by_category": stats["by_category"],
            "backup_dir": str(self.backup_dir),
            "skipped": len(entries) - total_files,
        })

        # ---------- 空间预检（B3） ----------
        free = dir_free_bytes(self.backup_dir)
        if free < total_bytes * 1.05:
            raise TaskError(
                f"目标磁盘剩余空间不足，已拦截！\n"
                f"预计需要 {human_size(total_bytes)}，"
                f"目标盘剩余 {human_size(free)}。\n"
                f"请更换目录或清理磁盘后重试。")

        # ---------- UI 确认 ----------
        if opts.wait_confirm and not self.bridge.await_confirm(
                {"count": total_files, "size": total_bytes}, self.token):
            raise TaskCancelled("用户取消")

        if not pending:
            self.bridge.emit_log("INFO", "所有文件均已备份且校验一致，无需重复传输（C4）")
            self._finalize("completed")
            return

        # ---------- 传输 ----------
        self.bridge.emit_state("running")
        progress = ProgressAggregator(total_bytes, total_files, self.bridge)
        self._transfer_all(pending, progress)

        # ---------- 可选：联系人/短信/通话记录 ----------
        if self.opts.do_contacts and not self.token.cancelled:
            self._export_contacts()

        # ---------- 可选：应用数据（adb backup） ----------
        if self.opts.do_appdata and not self.token.cancelled:
            self._appdata_backup()

        self._finalize("completed")

    # -------------------------------------------------- 扫描
    def _scan(self) -> List[Entry]:
        """流式扫描手机目录，产出去重后的 Entry 列表。"""
        opts = self.opts
        seen: Dict[str, Entry] = {}
        scanned = 0
        last_emit = time.monotonic()
        cancel = self.token

        def _resolve(r: str) -> str:
            # 解析软链接：HyperOS 上 /sdcard 是软链，find 不跟随「起始」软链，
            # 必须用 realpath 拿到真实目录（如 /storage/emulated/0）才能下钻，
            # 否则整机深度扫描会静默产出 0 文件。
            try:
                return self.adb.realpath(r, serial=self.device.serial)
            except Exception:
                return r

        jobs: List[Tuple[str, str, Optional[set]]] = []  # (category, root, exts)
        # 深度扫描：按文档类型遍历整机存储（/sdcard 内部存储 + /sdcard1 外置 SD），
        # 散落在任意目录的文档都不会遗漏；与常规分类扫描结果按手机端路径去重。
        if opts.deep_doc_scan:
            for root in ("/sdcard", "/sdcard1"):
                jobs.append(("docs", _resolve(root), DOC_EXT))
        for key in opts.categories:
            cat = CATEGORIES.get(key)
            if cat is None or not cat.dirs:
                continue
            for root in cat.dirs:
                jobs.append((cat.key, _resolve(root), cat.exts))
        for i, d in enumerate(opts.custom_dirs):
            d = d.strip()
            if d:
                jobs.append((f"custom", d, None))

        for category, root, exts in jobs:
            if cancel.cancelled:
                cancel.check()
            self.bridge.emit_log("INFO", f"扫描目录：{root}")
            # 先探测目录是否存在，不存在则友好跳过（G1）
            if not self.adb.remote_exists(root, serial=self.device.serial):
                self.bridge.emit_log("WARN", f"跳过目录 {root}：手机上不存在该目录")
                continue
            try:
                stream = self.adb.list_files(
                    root, serial=self.device.serial, cancel=cancel,
                    warn=lambda m: self.bridge.emit_log("WARN", m))
                for path, size, mtime in stream:
                    scanned += 1
                    name = path.rsplit("/", 1)[-1]
                    if exts is not None:
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        if ext not in exts:
                            continue
                    if path in seen:
                        continue
                    rel = path[len(root.rstrip("/")):].lstrip("/")
                    if category == "custom":
                        rel = path.lstrip("/")     # 自定义目录保留完整层级
                    if not rel:
                        rel = name
                    target = f"{category}/{rel}"
                    entry = Entry(
                        id=target, category=category, source=path,
                        relpath=rel, target=target, size=size, mtime=mtime,
                        status=STATUS_PENDING)
                    seen[path] = entry
                    now = time.monotonic()
                    if now - last_emit > 0.2:
                        last_emit = now
                        self.bridge.emit_scan(
                            {"scanning": True, "scanned": scanned,
                             "found": len(seen), "current_root": root})
                        self.bridge.emit_log(
                            "DEBUG", f"已扫描 {scanned} 个文件，命中 {len(seen)} 个")
            except AdbError as e:
                # 目录不存在/无权限 → 跳过，不中断整体扫描
                self.bridge.emit_log("WARN", f"跳过目录 {root}：{e}")

        entries = list(seen.values())
        # 写入清单（批量，一次落盘）
        with self._manifest_lock:
            for e in entries:
                self.manifest.add(e)
            self.manifest.save()
        self.bridge.emit_log(
            "INFO", f"扫描完成：共遍历 {scanned} 个文件，"
                    f"命中备份 {len(entries)} 个（{human_size(sum(e.size for e in entries))}）")
        return entries

    # -------------------------------------------------- 续传合并
    def _merge_resume(self, scanned: List[Entry]) -> List[Entry]:
        """续传：已 success 且本地文件大小一致 → 标记跳过（详细哈希校验交给校验页）。"""
        if not self.opts.resume_dir or self.manifest is None:
            return scanned
        out: List[Entry] = []
        for e in scanned:
            old = self.manifest.get(e.id)
            if old and old.status == STATUS_SUCCESS:
                local = self.backup_dir / e.target
                if local.exists() and local.stat().st_size == old.size:
                    e.status = STATUS_SUCCESS
                    e.sha256 = old.sha256
                    e.tries = old.tries
            out.append(e)
        return out

    # -------------------------------------------------- 增量备份合并
    def _merge_incremental(self, scanned: List[Entry]) -> List[Entry]:
        """增量合并：对比基准备份清单，仅重传「变更/损坏/新增」文件，跳过未变文件（省空间）。

        安全机制：
          - 跳过前先校验本地备份文件的 sha256；若本地损坏则强制重传，绝不基于脏数据跳过；
          - 变更判定用「大小 + mtime」快速比对（与 rsync 默认一致），命中即重传；
          - 手机端已删除的文件：保留备份数据，仅在清单中标记 removed，不参与后续还原/续传。
        """
        if not self.opts.resume_dir or self.manifest is None:
            return scanned
        scanned_ids = {e.id for e in scanned}
        out: List[Entry] = []
        n_skip = n_change = n_new = 0
        for e in scanned:
            base = self.manifest.get(e.id)
            if base and base.status == STATUS_SUCCESS:
                if (base.size == e.size and base.mtime == e.mtime
                        and self._base_file_ok(base, self.backup_dir / e.target)):
                    # 未变化且本地备份完好 → 直接复用，不复制（节省空间）
                    e.status = STATUS_SUCCESS
                    e.sha256 = base.sha256
                    e.src_hash = base.src_hash
                    e.tries = base.tries
                    n_skip += 1
                else:
                    # 大小/时间变化或本地备份损坏 → 视为变更，重传
                    e.status = STATUS_PENDING
                    e.sha256 = ""
                    e.src_hash = ""
                    n_change += 1
            else:
                # 基准备份无此文件 → 新增（或基准备份中该条曾失败，也重传）
                e.status = STATUS_PENDING
                e.sha256 = ""
                e.src_hash = ""
                if base is None:
                    n_new += 1
                else:
                    n_change += 1
            out.append(e)

        # 手机端已删除的文件：保留数据，标记 removed（不参与还原/续传）
        n_removed = 0
        for old in self.manifest.all_entries():
            if old.status == STATUS_SUCCESS and old.id not in scanned_ids:
                self.manifest.update(old.id, status=STATUS_REMOVED,
                                     error="手机端已删除")
                n_removed += 1

        if n_skip:
            self.bridge.emit_log(
                "INFO", f"增量备份：{n_skip} 个文件未变化已跳过（节省空间），"
                        f"{n_change} 个变更/损坏需重传，{n_new} 个新增")
        if n_removed:
            self.bridge.emit_log(
                "INFO", f"增量备份：{n_removed} 个文件手机端已删除，"
                        f"已保留备份数据并标记为 removed")
        return out

    def _base_file_ok(self, base: Entry, local_path: Path) -> bool:
        """跳过前的安全校验：本地备份文件是否完好、可信任复用。

        - 文件不存在或大小不符 → 不信任；
        - 有哈希且体积 ≤ 阈值：重算本地 sha256 比对（防脏数据/静默损坏）；
        - 大文件（>阈值）或缺失哈希：信任「大小+mtime」快速比对，避免每次重算大文件哈希。
        """
        if not local_path.exists():
            return False
        if local_path.stat().st_size != base.size:
            return False
        if base.sha256 and local_path.stat().st_size <= INCREMENTAL_LOCAL_VERIFY_MAX:
            try:
                if sha256_file(str(local_path), cancel=self.token) != base.sha256:
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    # -------------------------------------------------- 并发传输
    def _transfer_all(self, entries: List[Entry],
                      progress: ProgressAggregator) -> None:
        opts = self.opts
        workers = max(1, min(int(opts.concurrency), 4))   # 上限保护（F3）
        self.bridge.emit_log(
            "INFO", f"开始传输：{len(entries)} 个文件，线程并发 {workers}")

        future_map: Dict[Any, Entry] = {}
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="backup") as pool:
            for e in entries:
                if self.token.cancelled:
                    break
                fut = pool.submit(self._transfer_entry, e, progress)
                future_map[fut] = e

            pending = set(future_map)
            while pending:
                done_set, pending = wait(pending, timeout=0.5,
                                         return_when=FIRST_COMPLETED)
                if self.token.cancelled:
                    for f in pending:
                        f.cancel()
                    break
                for fut in done_set:
                    e = future_map[fut]
                    try:
                        fut.result()
                    except TaskCancelled:
                        pass
                    except Exception as ex:  # noqa: BLE001
                        self.bridge.emit_log(
                            "ERROR", f"文件失败：{e.source} → {ex}")
                snap = progress.snapshot()
                if snap:
                    self.bridge.emit_progress(snap)

        snap = progress.snapshot(force=True)
        if snap:
            self.bridge.emit_progress(snap)
        if self.token.cancelled:
            self.bridge.emit_log("WARN", "任务已取消，已传文件状态已保存，可稍后继续备份")
        with self._manifest_lock:
            self.manifest.save()

    def _transfer_entry(self, entry: Entry,
                        progress: ProgressAggregator) -> None:
        """传输单个文件（含重试 / 哈希校验 / manifest 记录）。"""
        assert self.manifest is not None and self.backup_dir is not None
        self.token.check()
        dest = self.backup_dir / entry.target
        dest.parent.mkdir(parents=True, exist_ok=True)
        progress.file_start(entry.source, entry.size)
        serial = self.device.serial

        def do_pull() -> None:
            # 大文件：分块流式（支持断点续传）；普通文件：adb pull（更快）
            if entry.size > STREAM_THRESHOLD:
                self.adb.pull_stream(
                    entry.source, str(dest), entry.size, serial=serial,
                    cancel=self.token,
                    progress=lambda n: progress.file_progress(entry.source, n),
                    speed_limit=self.opts.speed_limit)
            else:
                self.adb.pull(
                    entry.source, str(dest), serial=serial, cancel=self.token,
                    progress=lambda n: progress.file_progress(entry.source, n))

        def on_retry(attempt: int, err: Exception) -> None:
            self.bridge.emit_log(
                "WARN", f"第 {attempt} 次传输失败（{entry.source}）：{err}，准备重试")

        try:
            transfer_with_resilience(do_pull, self.adb, self.device.serial,
                                     self.bridge, self.token,
                                     label=f"[{entry.target}] ")
        except TaskCancelled:
            progress.file_start("")
            progress.file_progress(entry.source, 0)
            self._mark(entry, STATUS_PENDING, error="用户取消")
            raise
        except Exception as e:  # noqa: BLE001
            progress.file_done(entry.source, entry.size, ok=False)
            self._mark(entry, STATUS_FAILED, error=str(e), tries=entry.tries + 3)
            return

        # ---- 校验：本地 sha256（还原/完整性基准）----
        try:
            digest = sha256_file(str(dest), cancel=self.token)
        except TaskCancelled:
            self._mark(entry, STATUS_PENDING, error="用户取消")
            raise

        src_hash = ""
        if self.opts.verify_src_hash:
            got = self.adb.remote_hash(entry.source, serial=serial)
            if got:
                src_hash = got[1]
                if got[0] == "sha256" and got[1] != digest:
                    progress.file_done(entry.source, entry.size, ok=False)
                    self._mark(entry, STATUS_FAILED,
                               error="与设备端 sha256 不一致", tries=entry.tries + 3)
                    return

        progress.file_done(entry.source, entry.size, ok=True)
        self._mark(entry, STATUS_SUCCESS, sha256=digest, src_hash=src_hash,
                   error=None)
        self.bridge.emit_log(
            "INFO", f"完成：{entry.target}（{human_size(entry.size)}）")

    def _mark(self, entry: Entry, status: str, **fields: Any) -> None:
        # size/mtime 随实际传输结果同步进清单，避免「改大小/重传」后清单元数据滞后
        with self._manifest_lock:
            self.manifest.update(entry.id, status=status,
                                 size=entry.size, mtime=entry.mtime, **fields)
            if self._save_throttler.ready():
                self.manifest.save()
        # 失败/取消立即落盘，保证断点信息准确
        if status != STATUS_SUCCESS:
            with self._manifest_lock:
                self.manifest.save()

    # -------------------------------------------------- 联系人/短信/通话
    def _export_contacts(self) -> None:
        assert self.backup_dir is not None
        out_dir = self.backup_dir / "contacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        queries = {
            "contacts.txt": "content query --uri content://com.android.contacts/phones",
            "sms.txt": "content query --uri content://sms",
            "call_log.txt": "content query --uri content://call_log/calls",
        }
        for fname, q in queries.items():
            self.token.check()
            self.bridge.emit_log("INFO", f"导出 {fname}（部分 MIUI 版本可能限制读取）…")
            try:
                res = self.adb.shell(q, serial=self.device.serial, timeout=120)
                if res.ok and "No result" not in res.out and res.out.strip():
                    (out_dir / fname).write_text(res.out, encoding="utf-8")
                    self.bridge.emit_log("INFO", f"{fname} 导出完成")
                else:
                    self.bridge.emit_log(
                        "WARN", f"{fname} 导出为空或被系统拒绝（未 root 时属正常），"
                                f"请使用系统自带的云同步/换机助手")
            except AdbError as e:
                self.bridge.emit_log("WARN", f"{fname} 导出失败：{e}")

    # -------------------------------------------------- 应用数据
    def _appdata_backup(self) -> None:
        assert self.backup_dir is not None
        ab = self.backup_dir / "appdata" / "app_backup.ab"
        ab.parent.mkdir(parents=True, exist_ok=True)
        flags = ["backup"]
        flags.append("-apk" if self.opts.include_apk else "-noapk")
        flags.append("-shared" if self.opts.include_shared else "-noshared")
        flags.append("-system" if self.opts.include_system else "-nosystem")
        flags += ["-all", "-f", str(ab)]
        self.bridge.emit_log(
            "INFO", "开始 adb backup：请立刻在手机上确认备份弹窗，"
                    "并保持屏幕常亮（未 root 时部分应用数据无法导出）")
        try:
            self.adb.run(flags, serial=self.device.serial, timeout=900)
            if ab.exists() and ab.stat().st_size > 0:
                self.bridge.emit_log(
                    "INFO", f"应用数据已保存：{ab.name}"
                            f"（{human_size(ab.stat().st_size)}）")
            else:
                self.bridge.emit_log(
                    "WARN", "应用数据备份为空（可能未在手机上确认，或系统不支持）")
        except AdbError as e:
            self.bridge.emit_log("WARN", f"应用数据备份失败：{e}")

    # -------------------------------------------------- 结束
    def _finalize(self, state: str, error: str = "") -> None:
        if self.manifest is not None:
            try:
                self.manifest.save()
            except Exception:  # noqa: BLE001
                pass
        info = {
            "state": state,
            "error": error,
            "backup_dir": str(self.backup_dir) if self.backup_dir else "",
            "success": len(self.manifest.by_status(STATUS_SUCCESS)) if self.manifest else 0,
            "failed": len(self.manifest.by_status(STATUS_FAILED)) if self.manifest else 0,
            "pending": len(self.manifest.by_status(STATUS_PENDING)) if self.manifest else 0,
        }
        self.bridge.emit_state(state)
        self.bridge.emit_finished(info)


# ---------------------------------------------------------------- 校验
def verify_backup_dir(backup_dir: str, bridge=None, token: Optional[ControlToken] = None,
                      on_progress: Optional[Callable[[int, int], None]] = None
                      ) -> Dict[str, Any]:
    """E1：全量校验备份目录（大小 + sha256），返回报告。

    {"ok": [...], "bad": [{"id","reason"}], "fixed_sha": [...]}
    """
    bridge = bridge or NullBridge()
    token = token or ControlToken()
    backup_path = Path(backup_dir)
    try:
        manifest = Manifest.load(backup_path)
    except TaskError:
        bridge.emit_log("WARN", "清单缺失或损坏，尝试从目录重建（E2）")
        manifest = Manifest.rebuild(backup_path)

    entries = manifest.all_entries()
    ok, bad, fixed = [], [], []
    for i, e in enumerate(entries, 1):
        token.check()
        r = manifest.verify_entry(e, backup_path, cancel=token)
        if r["ok"]:
            ok.append(e.id)
            if not e.sha256:
                fixed.append(e.id)
        else:
            bad.append({"id": e.id, "reason": r["reason"]})
            bridge.emit_log("WARN", f"校验不通过：{e.target} — {r['reason']}")
        if on_progress:
            on_progress(i, len(entries))
    try:
        manifest.save()   # 写回补算的 sha256
    except Exception:  # noqa: BLE001
        pass
    bridge.emit_log(
        "INFO", f"校验完成：通过 {len(ok)}，不一致/缺失 {len(bad)}")
    return {"ok": ok, "bad": bad, "fixed_sha": fixed, "manifest": manifest}

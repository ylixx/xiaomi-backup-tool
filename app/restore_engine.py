# -*- coding: utf-8 -*-
"""
还原 / 换机引擎（电脑 → 手机）。

流程：
  1. 读取 manifest.json（损坏时自动重建降级）
  2. 预览：将还原的类别 / 文件数 / 大小（D1）
  3. 预检：目标手机剩余空间（D2）
  4. 传输：adb push（大文件分块流式 + 1MB 对齐续传），并发 ≤3，指数退避重试
  5. 抽样校验：随机抽 N 个文件比对设备端哈希/大小（D5）

断点续传（D4）：设备端已存在且大小一致的文件直接跳过；
manifest 中标 success 的条目默认跳过（可在 UI 选择「强制覆盖」）。
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adb_manager import STREAM_THRESHOLD, Adb, DeviceInfo
from .backup_engine import (NullBridge, ProgressAggregator,
                            transfer_with_resilience)
from .manifest import (STATUS_FAILED, STATUS_PENDING, STATUS_REMOVED,
                       STATUS_SUCCESS, Entry, Manifest)
from .utils import (AdbError, ControlToken, TaskCancelled, TaskError,
                    Throttler, get_logger, human_size, retry_call, sha256_file)

log = get_logger("restore")


@dataclass
class RestoreOptions:
    backup_dir: str                       # 备份目录（含 manifest.json）
    categories: List[str] = field(default_factory=list)  # 空 = 全部
    target_prefix: str = ""               # 还原到自定义前缀（如 /sdcard/Restored）
    overwrite: bool = False               # True=强制覆盖已存在且一致的文件
    concurrency: int = 3
    speed_limit: float = 0.0
    sample_verify: int = 10               # 还原后抽样校验数量
    do_appdata_restore: bool = False      # adb restore（需手机端确认）
    wait_confirm: bool = True


class RestoreEngine:
    def __init__(self, adb: Adb, device: DeviceInfo, opts: RestoreOptions,
                 bridge=None, token: Optional[ControlToken] = None) -> None:
        self.adb = adb
        self.device = device
        self.opts = opts
        self.bridge = bridge or NullBridge()
        self.token = token or ControlToken()
        self.manifest: Optional[Manifest] = None
        self.backup_dir = Path(opts.backup_dir)
        self._lock = threading.Lock()

    # -------------------------------------------------- 主流程
    def run(self) -> None:
        try:
            self._run()
        except TaskCancelled:
            self.bridge.emit_state("cancelled")
            self.bridge.emit_finished({"state": "cancelled"})
        except TaskError as e:
            self.bridge.emit_log("ERROR", str(e))
            self.bridge.emit_state("failed")
            self.bridge.emit_finished({"state": "failed", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("还原发生未预期异常")
            self.bridge.emit_log("ERROR", f"发生未预期异常：{e}")
            self.bridge.emit_state("failed")
            self.bridge.emit_finished({"state": "failed", "error": str(e)})

    def _run(self) -> None:
        opts = self.opts
        if not self.backup_dir.is_dir():
            raise TaskError(f"备份目录不存在：{self.backup_dir}")

        # ---------- 1. 读取 / 重建清单 ----------
        self.bridge.emit_state("scanning")
        try:
            self.manifest = Manifest.load(self.backup_dir)
        except TaskError:
            self.bridge.emit_log("WARN", "manifest 缺失或损坏，尝试从目录重建（E2）")
            self.manifest = Manifest.rebuild(self.backup_dir)

        entries = self.manifest.all_entries()
        # 跳过增量备份中已标记「手机端删除」的文件，避免把已删数据还原回去
        entries = [e for e in entries if e.status != STATUS_REMOVED]
        if opts.categories:
            entries = [e for e in entries if e.category in opts.categories]
        # 只还原本地存在的文件；缺失文件明确提示
        available, missing = [], []
        for e in entries:
            if (self.backup_dir / e.target).exists():
                available.append(e)
            else:
                missing.append(e)
        if missing:
            self.bridge.emit_log(
                "WARN", f"{len(missing)} 个文件在备份目录中缺失，将跳过"
                        f"（可在「校验」页查看详情）")
        if not available:
            raise TaskError("所选类别下没有可还原的文件（可能备份目录不完整）。")

        total_bytes = sum(e.size for e in available)
        by_cat: Dict[str, Dict[str, int]] = {}
        for e in available:
            s = by_cat.setdefault(e.category, {"count": 0, "size": 0})
            s["count"] += 1
            s["size"] += e.size
        self.bridge.emit_scan({
            "count": len(available), "size": total_bytes,
            "by_category": by_cat, "missing": len(missing),
            "backup_dir": str(self.backup_dir),
        })

        # ---------- 2. 空间预检（D2） ----------
        self.adb.check_target_space(self.device.serial, total_bytes)
        free = self.adb.free_space("/sdcard", serial=self.device.serial)[1]
        if free:
            self.bridge.emit_log(
                "INFO", f"手机剩余空间 {human_size(free)}，"
                        f"本次需要约 {human_size(total_bytes)}")

        # ---------- 3. 确认 ----------
        if opts.wait_confirm and not self.bridge.await_confirm(
                {"count": len(available), "size": total_bytes}, self.token):
            raise TaskCancelled("用户取消")

        # ---------- 4. 传输 ----------
        self.bridge.emit_state("running")
        to_send = self._filter_resumable(available)
        progress = ProgressAggregator(total_bytes, len(available),
                                      self.bridge, label="还原")
        self._push_all(to_send, progress)

        # ---------- 5. 抽样校验（D5） ----------
        if not self.token.cancelled:
            self._sample_verify(to_send)

        # ---------- 6. 应用数据还原 ----------
        if opts.do_appdata_restore and not self.token.cancelled:
            self._appdata_restore()

        self.bridge.emit_state("completed")
        self.bridge.emit_finished({"state": "completed"})

    # -------------------------------------------------- 断点续传过滤
    def _filter_resumable(self, entries: List[Entry]) -> List[Entry]:
        """设备端已存在且大小一致 → 跳过（除非强制覆盖）。"""
        if self.opts.overwrite:
            return entries
        to_send: List[Entry] = []
        skipped = 0
        for e in entries:
            self.token.check()
            target = self._device_target(e)
            size = self.adb.remote_size(target, serial=self.device.serial)
            if size == e.size:
                skipped += 1
                continue
            to_send.append(e)
        if skipped:
            self.bridge.emit_log(
                "INFO", f"续传：{skipped} 个文件设备端已存在且大小一致，跳过（D4/C4）")
        return to_send

    def _device_target(self, e: Entry) -> str:
        """设备端目标路径：默认还原到原路径，可选自定义前缀。"""
        if self.opts.target_prefix:
            prefix = self.opts.target_prefix.rstrip("/")
            return f"{prefix}/{e.source.lstrip('/')}"
        return e.source

    # -------------------------------------------------- 并发推送
    def _push_all(self, entries: List[Entry],
                  progress: ProgressAggregator) -> None:
        workers = max(1, min(int(self.opts.concurrency), 4))
        self.bridge.emit_log("INFO", f"开始还原 {len(entries)} 个文件，"
                                     f"线程并发 {workers}")
        future_map: Dict[Any, Entry] = {}
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="restore") as pool:
            for e in entries:
                if self.token.cancelled:
                    break
                future_map[pool.submit(self._push_entry, e, progress)] = e
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
                        self.bridge.emit_log("ERROR",
                                             f"还原失败：{e.target} → {ex}")
                snap = progress.snapshot()
                if snap:
                    self.bridge.emit_progress(snap)
        snap = progress.snapshot(force=True)
        if snap:
            self.bridge.emit_progress(snap)

    def _push_entry(self, entry: Entry, progress: ProgressAggregator) -> None:
        assert self.manifest is not None
        local = self.backup_dir / entry.target
        target = self._device_target(entry)
        serial = self.device.serial
        progress.file_start(entry.target, entry.size)

        # 本地文件哈希与 manifest 不一致 → 明确报错（不推送坏数据）
        if entry.sha256:
            actual = sha256_file(str(local), cancel=self.token)
            if actual != entry.sha256:
                progress.file_done(entry.target, entry.size, ok=False)
                self.bridge.emit_log(
                    "ERROR", f"本地备份文件已损坏，跳过推送：{entry.target}")
                return

        def do_push() -> None:
            if entry.size > STREAM_THRESHOLD:
                self.adb.push_stream(
                    str(local), target, serial=serial, cancel=self.token,
                    progress=lambda n: progress.file_progress(entry.target, n),
                    speed_limit=self.opts.speed_limit)
            else:
                self.adb.push(
                    str(local), target, serial=serial, cancel=self.token,
                    progress=lambda n: progress.file_progress(entry.target, n))

        def on_retry(attempt: int, err: Exception) -> None:
            self.bridge.emit_log(
                "WARN", f"第 {attempt} 次推送失败（{entry.target}）：{err}，准备重试")

        try:
            transfer_with_resilience(do_push, self.adb, self.device.serial,
                                     self.bridge, self.token,
                                     label=f"[{entry.target}] ")
        except TaskCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            progress.file_done(entry.target, entry.size, ok=False)
            with self._lock:
                self.manifest.update(entry.id, status=STATUS_FAILED,
                                     error=f"还原失败：{e}")
                self.manifest.save()
            return

        progress.file_done(entry.target, entry.size, ok=True)
        self.bridge.emit_log("INFO", f"已还原：{target}（{human_size(entry.size)}）")

    # -------------------------------------------------- 抽样校验
    def _sample_verify(self, entries: List[Entry]) -> None:
        n = min(int(self.opts.sample_verify), len(entries))
        if n <= 0:
            return
        self.bridge.emit_log("INFO", f"开始抽样校验 {n} 个文件（大小/哈希）…")
        # 保证样本里包含大文件（覆盖「大视频」场景，B8/D5）
        entries_sorted = sorted(entries, key=lambda e: e.size, reverse=True)
        sample = entries_sorted[: max(1, n // 3)]
        rest = entries_sorted[max(1, n // 3):]
        sample += random.sample(rest, min(n - len(sample), len(rest)))

        ok = bad = 0
        for e in sample:
            if self.token.cancelled:
                return
            target = self._device_target(e)
            size = self.adb.remote_size(target, serial=self.device.serial)
            if size != e.size:
                bad += 1
                self.bridge.emit_log(
                    "ERROR", f"抽样校验失败：{target} 大小不一致"
                             f"（期望 {e.size}，实际 {size}）")
                continue
            got = self.adb.remote_hash(target, serial=self.device.serial)
            if got and e.sha256 and got[0] == "sha256" and got[1] != e.sha256:
                bad += 1
                self.bridge.emit_log(
                    "ERROR", f"抽样校验失败：{target} 哈希不一致")
                continue
            ok += 1
            self.bridge.emit_log("INFO", f"抽样校验通过：{target}")
        self.bridge.emit_log("INFO", f"抽样校验结果：通过 {ok}，失败 {bad}")

    # -------------------------------------------------- 应用数据还原
    def _appdata_restore(self) -> None:
        ab = self.backup_dir / "appdata" / "app_backup.ab"
        if not ab.exists():
            self.bridge.emit_log("WARN", "备份目录中没有应用数据（appdata/app_backup.ab），跳过")
            return
        self.bridge.emit_log(
            "INFO", "开始 adb restore：请在手机上确认恢复弹窗，并保持屏幕常亮")
        try:
            self.adb.run(["restore", str(ab)], serial=self.device.serial,
                         timeout=900)
            self.bridge.emit_log("INFO", "应用数据恢复命令已执行（结果以手机端为准）")
        except AdbError as e:
            self.bridge.emit_log("WARN", f"应用数据恢复失败：{e}")

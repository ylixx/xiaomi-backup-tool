#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增量备份自测（模拟 adb，不需要真机）：

  python3 tests/test_incremental.py

验证：
  - 未变化文件被跳过（不重写本地文件 → 节省空间）
  - 变更文件（大小/mtime 变化）被重传
  - 新增文件被备份
  - 手机端删除的文件在清单中标记 removed，且备份数据保留
  - 安全：基准备份本地文件损坏（同大小）→ 增量时被强制重传，绝不信任脏数据跳过
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app.adb_manager import Adb  # noqa: E402
from app.backup_engine import BackupEngine, BackupOptions, verify_backup_dir  # noqa: E402
from app.manifest import (STATUS_REMOVED, STATUS_SUCCESS, Manifest)  # noqa: E402
from app.utils import ControlToken  # noqa: E402

TMP = HERE / ".tmp_incremental"
DEVICE = TMP / "device"
BACKUP_ROOT = TMP / "pc_backup"
PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


class CaptureBridge:
    def __init__(self) -> None:
        self.logs = []
        self.finished = []

    def emit_log(self, level, msg):
        self.logs.append((level, msg))

    def emit_progress(self, d):
        pass

    def emit_scan(self, d):
        pass

    def emit_state(self, s):
        pass

    def emit_finished(self, d):
        self.finished.append(d)

    def await_confirm(self, info, token):
        return True

    def text(self) -> str:
        return "\n".join(m for _, m in self.logs)


def setup_fake_adb() -> Adb:
    if os.name == "nt":
        wrapper = TMP / "fake_adb.bat"
        wrapper.write_text(f'@echo off\r\npython "{HERE / "mock_adb.py"}" %*\r\n')
    else:
        wrapper = TMP / "fake_adb"
        wrapper.write_text(f'#!/bin/sh\nexec python3 "{HERE / "mock_adb.py"}" "$@"\n')
        wrapper.chmod(0o755)
    return Adb(str(wrapper))


def make_device_tree(root: Path) -> None:
    files = {
        "sdcard/DCIM/Camera/IMG_0001.jpg": os.urandom(2048),
        "sdcard/DCIM/Camera/photo2.jpg": os.urandom(4096),
        "sdcard/Music/demo_song.mp3": os.urandom(300000),
        "sdcard/Download/report.pdf": os.urandom(50000),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def sha_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_backup(adb, dev, **kw) -> CaptureBridge:
    opts = BackupOptions(
        target_parent=str(BACKUP_ROOT),
        categories=["photos", "audio", "video", "docs"],
        concurrency=2, wait_confirm=False, **kw)
    bridge = CaptureBridge()
    BackupEngine(adb, dev, opts, bridge, ControlToken()).run()
    return bridge


def main() -> None:
    global PASS, FAIL
    if TMP.exists():
        shutil.rmtree(TMP)
    for d in (DEVICE, BACKUP_ROOT):
        d.mkdir(parents=True)
    os.environ["MOCK_ADB_ROOT"] = str(DEVICE)
    make_device_tree(DEVICE)
    adb = setup_fake_adb()
    dev = adb.device_info("MOCK123")

    # ---- 基线全量备份 ----
    print("\n[1] 基线全量备份")
    run_backup(adb, dev)
    backups = list(BACKUP_ROOT.glob("Backup_*"))
    check("生成 Backup_ 目录", len(backups) == 1, str(backups))
    bdir = backups[0]
    m = Manifest.load(bdir)
    check("基线全部 success", all(e.status == STATUS_SUCCESS for e in m.entries),
          str([e.status for e in m.entries]))

    # ---- 变更手机端：改一个、加一个、删一个，保留一个不变 ----
    print("\n[2] 变更手机端（改/增/删/不变）")
    (DEVICE / "sdcard/DCIM/Camera/IMG_0001.jpg").write_bytes(os.urandom(9000))  # 改（变大）
    (DEVICE / "sdcard/DCIM/Camera/NEW_0003.jpg").write_bytes(os.urandom(1234))  # 增
    (DEVICE / "sdcard/Download/report.pdf").unlink()                            # 删
    # photo2.jpg / demo_song.mp3 保持不变

    unchanged_backup = bdir / "photos" / "Camera" / "photo2.jpg"
    unchanged_mtime_before = unchanged_backup.stat().st_mtime

    print("\n[3] 增量备份")
    bridge = run_backup(adb, dev, resume_dir=str(bdir), incremental=True)
    m = Manifest.load(bdir)
    by_id = {e.id: e for e in m.entries}

    check("未变化文件 photo2.jpg 仍 success（被跳过）",
          by_id["photos/Camera/photo2.jpg"].status == STATUS_SUCCESS)
    check("未变化文件未被重写（本地 mtime 不变 → 省空间）",
          abs(unchanged_backup.stat().st_mtime - unchanged_mtime_before) < 1.0,
          f"{unchanged_mtime_before} vs {unchanged_backup.stat().st_mtime}")
    check("变更文件 IMG_0001.jpg 重传后与手机一致",
          sha_of(bdir / "photos/Camera/IMG_0001.jpg") ==
          sha_of(DEVICE / "sdcard/DCIM/Camera/IMG_0001.jpg"))
    check("新增文件 NEW_0003.jpg 已备份",
          (bdir / "photos/Camera/NEW_0003.jpg").is_file())
    check("删除文件 report.pdf 标记 removed 且数据保留",
          by_id.get("docs/report.pdf") is not None
          and by_id["docs/report.pdf"].status == STATUS_REMOVED
          and (bdir / "docs" / "report.pdf").is_file())
    check("日志中出现「跳过」字样（节省空间）",
          "跳过" in bridge.text(), bridge.text()[-300:])

    # ---- 安全：损坏基准备份本地文件（同大小）→ 必须被强制重传 ----
    print("\n[4] 安全：本地备份损坏应被强制重传（不信任脏数据）")
    victim = bdir / "photos/Camera/photo2.jpg"
    data = victim.read_bytes()
    victim.write_bytes(b"\x00" * len(data))           # 同大小写入垃圾
    # 手机端无变化，正常增量应跳过；但本地损坏 → 必须重传修复
    run_backup(adb, dev, resume_dir=str(bdir), incremental=True)
    m = Manifest.load(bdir)
    by_id = {e.id: e for e in m.entries}
    check("损坏文件 photo2.jpg 被强制重传修复",
          sha_of(victim) == sha_of(DEVICE / "sdcard/DCIM/Camera/photo2.jpg")
          and by_id["photos/Camera/photo2.jpg"].status == STATUS_SUCCESS)

    # ---- 整库校验 ----
    print("\n[5] 整库校验")
    rep = verify_backup_dir(str(bdir), bridge=CaptureBridge())
    check("增量后全量校验通过（无 bad）", not rep["bad"], str(rep["bad"][:2]))

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

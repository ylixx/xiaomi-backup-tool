#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档备份自测（模拟 adb，不需要真机）：

  python3 tests/test_documents.py

验证：
  - 文档类别按类型过滤：Office/PDF/OFD 等被收，.apk/.mp4 等非文档被排除
  - 默认扫描仅覆盖常规目录，散落文件夹的文档会被遗漏
  - 深度扫描整机文档：任意目录的文档都能找回（不遗漏），仍排除非文档
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
from app.backup_engine import BackupEngine, BackupOptions  # noqa: E402
from app.manifest import Manifest  # noqa: E402
from app.utils import ControlToken  # noqa: E402

TMP = HERE / ".tmp_documents"
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
    def emit_log(self, level, msg): pass
    def emit_progress(self, d): pass
    def emit_scan(self, d): pass
    def emit_state(self, s): pass
    def emit_finished(self, d): pass
    def await_confirm(self, info, token): return True


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
        "sdcard/Documents/normal.docx": os.urandom(2000),
        "sdcard/Download/invoice.pdf": os.urandom(4000),
        "sdcard/WeirdDeep/sub/secret.ofd": os.urandom(1500),   # 散落目录
        "sdcard/WeirdDeep/junk.apk": os.urandom(8000),         # 非文档（应排除）
        "sdcard/DCIM/notadoc.mp4": os.urandom(9000),           # 非文档（应排除）
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def run_backup(adb, dev, **kw) -> Manifest:
    opts = BackupOptions(
        target_parent=str(BACKUP_ROOT),
        categories=["docs"], concurrency=2, wait_confirm=False, **kw)
    BackupEngine(adb, dev, opts, CaptureBridge(), ControlToken()).run()
    bdir = list(BACKUP_ROOT.glob("Backup_*"))[-1]
    return Manifest.load(bdir)


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

    print("\n[1] 默认文档扫描（按类型过滤，仅常规目录）")
    m = run_backup(adb, dev)
    ok = {e.target for e in m.entries}
    check("常规目录 docx 被备份", "docs/normal.docx" in ok)
    check("常规目录 pdf(发票) 被备份", "docs/invoice.pdf" in ok)
    check("非文档 .apk 被排除", "docs/WeirdDeep/junk.apk" not in ok)
    check("非文档 .mp4 被排除", "docs/DCIM/notadoc.mp4" not in ok)
    check("散落目录 ofd 被遗漏（默认不扫）",
          "docs/WeirdDeep/sub/secret.ofd" not in ok, str(ok))

    print("\n[2] 深度扫描整机文档（不遗漏）")
    # 新目录，避免与上一轮 Backup_ 混淆
    BACKUP_ROOT2 = TMP / "pc_backup2"
    BACKUP_ROOT2.mkdir()
    opts = BackupOptions(
        target_parent=str(BACKUP_ROOT2), categories=["docs"],
        deep_doc_scan=True, concurrency=2, wait_confirm=False)
    BackupEngine(adb, dev, opts, CaptureBridge(), ControlToken()).run()
    bdir2 = list(BACKUP_ROOT2.glob("Backup_*"))[-1]
    m2 = Manifest.load(bdir2)
    ok2 = {e.target for e in m2.entries}
    check("散落目录 ofd 被深度扫描找回（不遗漏）",
          "docs/WeirdDeep/sub/secret.ofd" in ok2, str(ok2))
    check("常规 docx 仍在", "docs/Documents/normal.docx" in ok2)
    check("常规 pdf 仍在", "docs/Download/invoice.pdf" in ok2)
    check("非文档 .apk 仍被排除", "docs/WeirdDeep/junk.apk" not in ok2)
    check("非文档 .mp4 仍被排除", "docs/DCIM/notadoc.mp4" not in ok2)
    check("深度扫描仅收文档类（3 个）", len(ok2) == 3, str(ok2))

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

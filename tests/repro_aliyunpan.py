#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
复现验证：用户报告文件
  sdcard/AliYunPan/备份文件/27-《心理大师》：我是心理医生，818最近的变态病人-沈非.pdf
未被备份。本脚本在模拟手机中复现该场景，对比两种扫描模式。

  python3 tests/repro_aliyunpan.py
"""
from __future__ import annotations

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

TMP = HERE / ".tmp_repro_aliyunpan"
DEVICE = TMP / "device"
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
        # 常规文档（默认根目录内）
        "sdcard/Download/normal.docx": os.urandom(2000),
        "sdcard/Documents/invoice.pdf": os.urandom(4000),
        # 用户报告的文件：散落在 AliYunPan 备份文件 下
        "sdcard/AliYunPan/备份文件/"
        "27-《心理大师》：我是心理医生，818最近的变态病人-沈非.pdf": os.urandom(3000),
        # 同目录下的非文档，验证深度扫描仍会按类型过滤
        "sdcard/AliYunPan/备份文件/cover.jpg": os.urandom(5000),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def run_backup(adb, dev, backup_root: Path, **kw) -> Manifest:
    opts = BackupOptions(
        target_parent=str(backup_root), categories=["docs"],
        concurrency=2, wait_confirm=False, **kw)
    BackupEngine(adb, dev, opts, CaptureBridge(), ControlToken()).run()
    bdir = list(backup_root.glob("Backup_*"))[-1]
    return Manifest.load(bdir)


def main() -> None:
    global PASS, FAIL
    if TMP.exists():
        shutil.rmtree(TMP)
    for d in (DEVICE,):
        d.mkdir(parents=True)
    os.environ["MOCK_ADB_ROOT"] = str(DEVICE)
    make_device_tree(DEVICE)
    adb = setup_fake_adb()
    dev = adb.device_info("MOCK123")

    target = "docs/AliYunPan/备份文件/" \
             "27-《心理大师》：我是心理医生，818最近的变态病人-沈非.pdf"
    # 默认根目录直接扫 /sdcard/AliYunPan 时，根前缀被剥离，落到 docs/备份文件/ 下
    target_default = "docs/备份文件/" \
                     "27-《心理大师》：我是心理医生，818最近的变态病人-沈非.pdf"

    print("\n[模式A] 默认文档扫描（未勾「深度扫描整机文档」）")
    ba = TMP / "pc_a"
    ba.mkdir()
    ma = run_backup(adb, dev, ba)
    oka = {e.target for e in ma.entries}
    check("常规 Download/docx 被备份", "docs/normal.docx" in oka)
    check("常规 Documents/pdf 被备份", "docs/invoice.pdf" in oka)
    check("AliYunPan 下 pdf 【被找回】（已加入默认根目录，定向扫描命中）",
          target_default in oka, str(oka))

    print("\n[模式B] 深度扫描整机文档（勾选「深度扫描整机文档」）")
    bb = TMP / "pc_b"
    bb.mkdir()
    mb = run_backup(adb, dev, bb, deep_doc_scan=True)
    okb = {e.target for e in mb.entries}
    check("常规 Download/docx 仍被备份", "docs/Download/normal.docx" in okb)
    check("常规 Documents/pdf 仍被备份", "docs/Documents/invoice.pdf" in okb)
    check("AliYunPan 下 pdf 【被找回】（深度扫描不遗漏）",
          target in okb, str(okb))
    check("AliYunPan 下 jpg 仍被排除（按类型过滤）",
          "docs/AliYunPan/备份文件/cover.jpg" not in okb, str(okb))

    print(f"\n结论：通过 {PASS}，失败 {FAIL}")
    print("→ 该文件漏备份的根因（真机确认）：")
    print("  ① 默认文档扫描不含 /sdcard/AliYunPan；")
    print("  ② /sdcard 是软链接，find /sdcard 不跟随起始软链 → 整机深度扫描静默 0 文件；")
    print("  ③ toybox find -exec + 受 ARG_MAX 限制会中途中止（13.9 万文件只扫 ~8900）。")
    print("  现已修复：补默认根目录（定向扫描直接命中）+ realpath 解析软链 + "
          "find -print0|xargs 规避 ARG_MAX。真机验证整机深度扫描已能扫到该文件。")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

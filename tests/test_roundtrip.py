#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端自测（使用模拟 adb，不需要真机）：
  python3 tests/test_roundtrip.py

模拟两台手机（设备A → 备份到电脑 → 还原到设备B），验证：
  备份分类扫描 / manifest 记录 / 哈希校验 / 分块流式传输 / 断点续传 /
  损坏检测 / 还原预检与抽样校验 / 重复备份跳过（C4）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app.adb_manager import Adb  # noqa: E402
from app.backup_engine import (BackupEngine, BackupOptions,  # noqa: E402
                               verify_backup_dir)
from app.manifest import (STATUS_SUCCESS, Manifest,  # noqa: E402
                          ManifestError)
from app.restore_engine import RestoreEngine, RestoreOptions  # noqa: E402
from app.utils import ControlToken, sha256_file  # noqa: E402

TMP = HERE / ".tmp_roundtrip"
DEVICE_A = TMP / "device_a"       # 模拟手机A文件系统
DEVICE_B = TMP / "device_b"       # 模拟手机B文件系统
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
    """收集引擎输出，方便断言。"""

    def __init__(self) -> None:
        self.logs = []
        self.states = []
        self.finished = []

    def emit_log(self, level, msg):
        self.logs.append((level, msg))

    def emit_progress(self, d):
        pass

    def emit_scan(self, d):
        self.logs.append(("SCAN", str(d)))

    def emit_state(self, s):
        self.states.append(s)

    def emit_finished(self, d):
        self.finished.append(d)

    def await_confirm(self, info, token):
        return True

    def text(self) -> str:
        return "\n".join(m for _, m in self.logs)


def setup_fake_adb() -> Adb:
    """生成可执行的 adb 包装脚本，指向 tests/mock_adb.py。"""
    if os.name == "nt":
        wrapper = TMP / "fake_adb.bat"
        wrapper.write_text(f'@echo off\r\npython "{HERE / "mock_adb.py"}" %*\r\n')
    else:
        wrapper = TMP / "fake_adb"
        wrapper.write_text(f'#!/bin/sh\nexec python3 "{HERE / "mock_adb.py"}" "$@"\n')
        wrapper.chmod(0o755)
    return Adb(str(wrapper))


def make_device_tree(root: Path) -> dict:
    """构造模拟手机数据：照片(含中文/空格文件名)、视频(>分块阈值)、文档。"""
    files = {
        "sdcard/DCIM/Camera/IMG_0001.jpg": os.urandom(2048),
        "sdcard/DCIM/Camera/照片 02（测试）.jpg": os.urandom(4096),
        "sdcard/DCIM/Camera/VID_0002.mp4": os.urandom(5 * 1024 * 1024 + 777),
        "sdcard/Pictures/Screenshots/截屏 2025.png": os.urandom(1024),
        "sdcard/Music/demo_song.mp3": os.urandom(300000),
        "sdcard/Download/report 最终版.pdf": os.urandom(50000),
        "sdcard/Download/sub dir/notes.txt": b"hello backup",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return {"/" + k: v for k, v in files.items()}


def sha_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    global PASS, FAIL
    if TMP.exists():
        shutil.rmtree(TMP)
    for d in (DEVICE_A, DEVICE_B, BACKUP_ROOT):
        d.mkdir(parents=True)

    os.environ["MOCK_ADB_ROOT"] = str(DEVICE_A)
    src_files = make_device_tree(DEVICE_A)

    # 让「大文件分块流式」路径被真正覆盖到（正常阈值 512MB）
    import app.adb_manager as adb_mod
    import app.backup_engine as bk_mod
    import app.restore_engine as rs_mod
    adb_mod.STREAM_CHUNK = 2 * 1024 * 1024      # 2MB 一块，5MB 文件走 3 块
    bk_mod.STREAM_THRESHOLD = 1024              # >1KB 即走分块流式
    rs_mod.STREAM_THRESHOLD = 1024

    adb = setup_fake_adb()
    dev = adb.device_info("MOCK123")
    print(f"[准备] 模拟设备：{dev.display_name} Android {dev.android}")

    print("\n[1] 设备识别（A2）")
    check("识别到设备", dev is not None and dev.model != "", str(dev))
    check("Android 版本读取", dev.android == "14", dev.android)
    check("存储容量读取", dev.storage_total > 0, str(dev.storage_total))

    print("\n[2] 分类备份（B1/B4/B7/B9）")
    bridge = CaptureBridge()
    opts = BackupOptions(
        target_parent=str(BACKUP_ROOT),
        categories=["photos", "audio", "video", "docs"],
        concurrency=3, wait_confirm=False,
    )
    BackupEngine(adb, dev, opts, bridge, ControlToken()).run()
    backups = list(BACKUP_ROOT.glob("Backup_*"))
    check("生成 Backup_时间戳 目录", len(backups) == 1, str(backups))
    bdir = backups[0]
    for sub in ("photos", "audio", "video", "docs"):
        check(f"目录结构 {sub}/", (bdir / sub).is_dir())
    check("manifest.json 存在", (bdir / "manifest.json").is_file())
    check("backup_info.json 存在", (bdir / "backup_info.json").is_file())

    m = Manifest.load(bdir)
    ok_entries = [e for e in m.entries if e.status == STATUS_SUCCESS]
    check("全部文件传输成功", len(ok_entries) == len(src_files),
          f"{len(ok_entries)}/{len(src_files)}")
    check("中文/空格文件名保留（B9）",
          (bdir / "photos" / "Camera" / "照片 02（测试）.jpg").is_file())
    check("视频归类正确",
          (bdir / "video" / "Camera" / "VID_0002.mp4").is_file())
    # 内容一致性
    bad = []
    for e in ok_entries:
        local = bdir / e.target
        if sha_of(local) != sha_of(DEVICE_A / e.source.lstrip("/")):
            bad.append(e.source)
        if e.status == STATUS_SUCCESS and (not e.sha256 or e.size != local.stat().st_size):
            bad.append(e.source)
    check("备份内容与手机端逐字节一致 + manifest 记录完整（B7/B8）",
          not bad, str(bad[:3]))

    print("\n[3] 完整性校验（E1）")
    rep = verify_backup_dir(str(bdir), bridge=bridge)
    check("全量校验通过", not rep["bad"] and len(rep["ok"]) == len(src_files),
          str(rep["bad"][:2]))

    print("\n[4] 损坏检测与重传（E3）")
    victim = bdir / "photos" / "Camera" / "IMG_0001.jpg"
    victim.write_bytes(b"corrupted!!")
    rep = verify_backup_dir(str(bdir), bridge=bridge)
    check("篡改被检出", any("photos" in b["id"] for b in rep["bad"]),
          str(rep["bad"]))
    bad_ids = [b["id"] for b in rep["bad"]]
    bridge2 = CaptureBridge()
    opts2 = BackupOptions(
        target_parent=str(BACKUP_ROOT),
        categories=["photos", "audio", "video", "docs"],
        resume_dir=str(bdir), only_ids=bad_ids,
        concurrency=3, wait_confirm=False)
    BackupEngine(adb, dev, opts2, bridge2, ControlToken()).run()
    check("不一致文件被单独重传修复", sha_of(victim) == sha_of(
        DEVICE_A / "sdcard/DCIM/Camera/IMG_0001.jpg"))
    rep = verify_backup_dir(str(bdir), bridge=bridge)
    check("修复后校验全通过", not rep["bad"], str(rep["bad"]))

    print("\n[5] 断点续传 / 跳过已完成（C1/C4）")
    (bdir / "docs" / "sub dir" / "notes.txt").unlink(missing_ok=True)
    m = Manifest.load(bdir)
    bridge3 = CaptureBridge()
    opts3 = BackupOptions(
        target_parent=str(BACKUP_ROOT),
        categories=["photos", "audio", "video", "docs"],
        resume_dir=str(bdir), concurrency=3, wait_confirm=False)
    BackupEngine(adb, dev, opts3, bridge3, ControlToken()).run()
    check("缺失文件被补传",
          (bdir / "docs" / "sub dir" / "notes.txt").is_file())
    check("日志显示跳过已完成文件", "跳过" in bridge3.text()
          or "无需重复传输" in bridge3.text(), bridge3.text()[-300:])
    m = Manifest.load(bdir)
    check("续传后清单全部 success",
          all(e.status == STATUS_SUCCESS for e in m.entries))

    print("\n[6] 大文件分块流式 + 中断续传（F4/C2）")
    big_src = DEVICE_A / "sdcard/DCIM/Camera/VID_0002.mp4"
    partial = TMP / "partial.mp4"
    data = big_src.read_bytes()
    partial.write_bytes(data[:2 * 1024 * 1024 + 512])   # 半成品（非对齐尾巴）
    adb.pull_stream("/sdcard/DCIM/Camera/VID_0002.mp4", str(partial),
                    len(data), serial="MOCK123")
    check("分块拉取 + 断点续传后哈希一致",
          sha_of(partial) == sha_of(big_src))

    big_local = TMP / "push_src.bin"
    big_local.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
    adb.push_stream(str(big_local), "/sdcard/test_push.bin", serial="MOCK123")
    check("分块推送后设备端一致",
          sha_of(DEVICE_A / "sdcard/test_push.bin") == sha_of(big_local))

    print("\n[7] 还原 / 换机到第二台设备（D1-D5）")
    os.environ["MOCK_ADB_ROOT"] = str(DEVICE_B)
    bridge4 = CaptureBridge()
    ropts = RestoreOptions(
        backup_dir=str(bdir),
        categories=["photos", "audio", "video", "docs"],
        concurrency=3, sample_verify=10, wait_confirm=False)
    RestoreEngine(adb, dev, ropts, bridge4, ControlToken()).run()
    moved = []
    for e in Manifest.load(bdir).entries:
        p = DEVICE_B / e.source.lstrip("/")
        if not p.is_file() or sha_of(p) != sha_of(bdir / e.target):
            moved.append(e.source)
    check("全部文件还原到新手机且内容一致（D3）", not moved, str(moved[:3]))
    check("抽样校验执行并全部通过（D5）",
          "抽样校验通过" in bridge4.text()
          and "抽样校验结果：通过" in bridge4.text(),
          bridge4.text()[-300:])
    check("还原状态完成", bridge4.states[-1] == "completed", str(bridge4.states))

    print("\n[8] 异常处理（G1）")
    os.environ["MOCK_ADB_ROOT"] = str(DEVICE_A)
    bridge5 = CaptureBridge()
    o = BackupOptions(target_parent=str(BACKUP_ROOT),
                      categories=["photos"], custom_dirs=["/sdcard/not_exist"],
                      concurrency=2, wait_confirm=False)
    BackupEngine(adb, dev, o, bridge5, ControlToken()).run()
    check("不存在目录仅告警不崩溃",
          any("跳过目录" in m for _, m in bridge5.logs), bridge5.text()[-200:])

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

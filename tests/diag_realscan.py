#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真机验证：确认修复后，深度扫描用的 find 能真正扫到 AliYunPan 下的 pdf。
仅做只读扫描（list_files），不传输任何文件。需要手机通过 adb 连接。

  python tests/diag_realscan.py
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app.adb_manager import Adb  # noqa: E402

SERIAL = "c6d06ce8"
ADB_BIN = os.path.abspath("platform-tools/adb.exe")
TARGET_NAME = "27-《心理大师》：我是心理医生，818最近的变态病人-沈非.pdf"


def main() -> None:
    adb = Adb(ADB_BIN)

    # 1) 软链接解析：/sdcard -> /storage/emulated/0
    rp = adb.realpath("/sdcard", serial=SERIAL)
    print(f"realpath(/sdcard) = {rp}")
    if rp != "/storage/emulated/0":
        print(f"[FAIL] 预期 /storage/emulated/0，实际 {rp}")
        sys.exit(1)

    # 2) 定向 list_files（解析后的 AliYunPan 根）—— 应快速命中
    found_targeted = False
    for path, size, _m in adb.list_files(
            adb.realpath("/sdcard/AliYunPan", serial=SERIAL), serial=SERIAL):
        if path.endswith(TARGET_NAME):
            found_targeted = True
            print(f"[OK] 定向扫描命中：{path}  ({size} 字节)")
            break
    print(f"定向扫描命中目标文件？ {found_targeted}")

    # 3) 整机 list_files（解析后的 /sdcard，即深度扫描实际执行的命令）
    found_whole = False
    count = 0
    for path, size, _m in adb.list_files(rp, serial=SERIAL):
        count += 1
        if "AliYunPan/备份文件" in path and path.endswith(TARGET_NAME):
            found_whole = True
            print(f"[OK] 整机深度扫描命中：{path}  ({size} 字节)")
            break
    print(f"整机遍历了 {count} 个文件；命中目标文件？ {found_whole}")

    ok = found_targeted and found_whole
    print("结果：", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

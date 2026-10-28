#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线单元自测（不依赖 adb / 手机 / GUI）：
  python3 tests/selftest.py

覆盖：哈希、节流、取消令牌、manifest 原子写/损坏重建、校验逻辑、进度聚合。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app.backup_engine import ProgressAggregator  # noqa: E402
from app.manifest import (STATUS_SUCCESS, Entry, Manifest,  # noqa: E402
                          ManifestError)
from app.utils import (ControlToken, Throttler, atomic_write_json,  # noqa: E402
                       hash_file, human_size, read_json, sha256_file)

TMP = HERE / ".tmp_selftest"
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
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    print("[1] 哈希 / 格式化")
    f = TMP / "data.bin"
    payload = os.urandom(3 * 1024 * 1024 + 17)
    f.write_bytes(payload)
    check("sha256 与 hashlib 一致",
          sha256_file(f) == hashlib.sha256(payload).hexdigest())
    check("md5 分块计算一致",
          hash_file(f, "md5") == hashlib.md5(payload).hexdigest())
    check("human_size 格式", human_size(1536).startswith("1.5K"),
          human_size(1536))

    print("[2] 节流器 / 取消令牌")
    th = Throttler(0.05)
    first = th.ready()
    second = th.ready()
    check("节流：100ms 内仅放行一次", first and not second)
    time.sleep(0.06)
    check("节流：间隔后再次放行", th.ready())
    tok = ControlToken()
    tok.pause()
    done = []

    def waiter():
        tok.wait_if_paused()
        done.append(1)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    check("暂停时线程被阻塞", not done)
    tok.resume()
    t.join(1)
    check("恢复后线程继续", bool(done))
    tok.cancel("测试取消")
    raised = False
    try:
        tok.check()
    except Exception:
        raised = True
    check("取消后 check() 抛出异常", raised)

    print("[3] JSON 原子写")
    js = TMP / "x.json"
    atomic_write_json(js, {"中文": "测试", "n": 1})
    check("原子写 + 读取", read_json(js)["中文"] == "测试")
    check("无 .tmp 残留", not (TMP / "x.json.tmp").exists())

    print("[4] manifest：保存 / 加载 / 续传标记")
    bdir = TMP / "Backup_20250101_000000"
    (bdir / "photos").mkdir(parents=True)
    m = Manifest(bdir, {"serial": "TEST", "model": "Mock"})
    src = bdir / "photos" / "a.jpg"
    src.write_bytes(b"hello world")
    e = Entry(id="photos/a.jpg", category="photos", source="/sdcard/a.jpg",
              relpath="a.jpg", target="photos/a.jpg",
              size=src.stat().st_size, mtime=int(time.time()),
              sha256=sha256_file(src), status=STATUS_SUCCESS)
    m.add(e)
    m.save()
    m2 = Manifest.load(bdir)
    check("清单往返一致",
          len(m2.entries) == 1 and m2.entries[0].sha256 == e.sha256)

    print("[5] 校验：一致 / 损坏 / 缺失")
    r = m2.verify_entry(m2.entries[0])
    check("一致文件校验通过", r["ok"], r.get("reason", ""))
    src.write_bytes(b"hello worlD")           # 篡改内容
    r = m2.verify_entry(m2.entries[0])
    check("篡改后校验失败", not r["ok"] and "sha256" in r["reason"], r["reason"])
    (bdir / "photos" / "a.jpg").unlink()
    r = m2.verify_entry(m2.entries[0])
    check("缺失文件校验失败", not r["ok"] and "缺失" in r["reason"], r["reason"])

    print("[6] manifest 损坏降级（E2）")
    (bdir / "manifest.json").write_text("{broken json", encoding="utf-8")
    (bdir / "manifest.json.bak").unlink(missing_ok=True)
    raised = False
    try:
        Manifest.load(bdir)
    except ManifestError:
        raised = True
    check("损坏清单报错并给出提示", raised)
    (bdir / "photos" / "a.jpg").write_bytes(b"hello world")
    m3 = Manifest.rebuild(bdir)
    check("从目录重建清单成功", len(m3.entries) == 1)

    print("[7] 进度聚合与节流")
    bridge_logs = []

    class B:
        def emit_progress(self, d):
            bridge_logs.append(d)

    pa = ProgressAggregator(1000, 2, B())
    pa.file_start("f1", 1000)
    for i in range(50):
        pa.file_progress("f1", i * 20)
        pa.snapshot()
    pa.file_done("f1", 1000, ok=True)
    check("进度 50 次调用被节流（≤10 次上报）", len(bridge_logs) <= 10,
          str(len(bridge_logs)))
    snap = pa.snapshot(force=True)
    check("快照统计正确",
          snap["done_files"] == 1 and snap["done_bytes"] == 1000)

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

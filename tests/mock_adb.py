#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟 adb（仅用于本地自测，不需要真机/模拟器）。

把手机端文件系统映射到环境变量 MOCK_ADB_ROOT 指定的本地目录，
按真实 adb 的调用格式实现本工具用到的子命令：
  devices -l / get-state / shell(getprop|df|find|stat|sha256sum|md5sum|mkdir|test|content)
  exec-out dd（分块流式拉取）/ exec-in dd（分块流式推送）/ pull / push / backup / restore

用法示例：
  MOCK_ADB_ROOT=/tmp/fake_phone python3 mock_adb.py devices -l
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import sys
import time

ROOT = os.environ.get("MOCK_ADB_ROOT", "/tmp/mock_device")


def mapp(remote: str) -> str:
    return os.path.join(ROOT, remote.lstrip("/"))


def sh(*args) -> int:
    """shell 命令分发。"""
    cmdline = args[0] if args else ""
    tokens = [t for t in shlex.split(cmdline)
              if not re.match(r"^\d?>", t) and ">/dev/null" not in t]
    if not tokens:
        return 0
    op = tokens[0]

    if op == "getprop":
        props = {
            "ro.product.model": "MockPhone 14 Pro",
            "ro.product.manufacturer": "Xiaomi",
            "ro.build.version.release": "14",
            "ro.build.version.sdk": "34",
        }
        for k, v in props.items():
            print(f"[{k}]: [{v}]")
        return 0

    if op == "df":
        print("Filesystem     1K-blocks    Used Available Use% Mounted on")
        print("/dev/fuse      240000000 4000000 236000000   2% /storage/emulated")
        return 0

    if op == "find":
        root = tokens[1]
        base = mapp(root)
        if not os.path.isdir(base):
            print(f"find: {root}: No such file or directory", file=sys.stderr)
            return 1
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                local = os.path.join(dirpath, fn)
                remote = "/" + os.path.relpath(local, ROOT).replace(os.sep, "/")
                st = os.stat(local)
                print(f"{st.st_size}|{int(st.st_mtime)}|{remote}")
        return 0

    if op == "stat":
        # stat -c %s <path>
        path = tokens[-1]
        try:
            print(os.stat(mapp(path)).st_size)
            return 0
        except OSError as e:
            print(f"stat: {path}: {e.strerror}", file=sys.stderr)
            return 1

    if op == "realpath":
        # 模拟环境无软链接，原样返回路径
        print(tokens[-1])
        return 0

    if op == "readlink":
        print(tokens[-1])
        return 0

    if op in ("sha256sum", "md5sum"):
        algo = "sha256" if op == "sha256sum" else "md5"
        path = tokens[-1]
        try:
            h = hashlib.new(algo)
            with open(mapp(path), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            print(f"{h.hexdigest()}  {path}")
            return 0
        except OSError as e:
            print(f"{op}: {path}: {e.strerror}", file=sys.stderr)
            return 1

    if op == "mkdir":
        os.makedirs(mapp(tokens[-1]), exist_ok=True)
        return 0

    if op == "test":
        # test -e <path> && echo Y
        path = tokens[2] if len(tokens) > 2 and tokens[1] == "-e" else tokens[-1]
        if os.path.exists(mapp(path)):
            print("Y")
        return 0

    if op == "content":
        print("No result found.")
        return 0

    print(f"mock-adb: unknown shell op: {op}", file=sys.stderr)
    return 1


def parse_dd(dd_str: str) -> dict:
    out = {}
    for t in shlex.split(dd_str):
        if "=" in t and t.split("=", 1)[0] in ("if", "of", "bs", "skip",
                                              "seek", "count", "conv"):
            k, v = t.split("=", 1)
            out[k] = v
    return out


def exec_out(dd_str: str) -> int:
    d = parse_dd(dd_str)
    src = mapp(d.get("if", ""))
    bs = int(d.get("bs", 512))
    skip = int(d.get("skip", 0))
    count = int(d.get("count", 0))
    start = skip * bs
    length = count * bs if count else -1
    with open(src, "rb") as f:
        f.seek(start)
        data = f.read(length) if length > 0 else f.read()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


def exec_in(dd_str: str) -> int:
    d = parse_dd(dd_str)
    dst = mapp(d.get("of", ""))
    bs = int(d.get("bs", 512))
    seek = int(d.get("seek", 0))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    data = sys.stdin.buffer.read()
    mode = "r+b" if os.path.exists(dst) else "w+b"
    with open(dst, mode) as f:
        f.seek(seek * bs)
        f.write(data)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "-s":
        args = args[2:]
    if not args:
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "devices":
        print("List of devices attached")
        print("MOCK123\tdevice product:mock model:Mock_Phone device:mock "
              "transport_id:1")
        return 0
    if cmd == "get-state":
        print("device")
        return 0
    if cmd == "shell":
        return sh(rest[0] if rest else "")
    if cmd == "exec-out":
        return exec_out(rest[0] if rest else "")
    if cmd == "exec-in":
        return exec_in(rest[0] if rest else "")
    if cmd == "pull":
        shutil.copyfile(mapp(rest[0]), rest[1])
        return 0
    if cmd == "push":
        dst = mapp(rest[1])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(rest[0], dst)
        return 0
    if cmd in ("backup", "restore"):
        # 生成一个占位 .ab 文件，模拟手机端确认后的产物
        for a in rest:
            if a.endswith(".ab"):
                os.makedirs(os.path.dirname(a) or ".", exist_ok=True)
                with open(a, "wb") as f:
                    f.write(b"ANDROID BACKUP\nmock-data\n")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

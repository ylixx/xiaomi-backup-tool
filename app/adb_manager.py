# -*- coding: utf-8 -*-
"""
adb 通信层。

职责：
  1. 自动定位 adb 可执行文件（内置 platform-tools / 环境变量 / PATH / 常见安装位置）
     → 满足 A1：用户无需手动安装 adb
  2. 设备检测与信息读取（型号 / Android 版本 / 存储容量 / 授权状态）→ A2/A3
  3. 目录遍历（分批流式返回，避免百万文件一次性进内存）→ F5
  4. 文件传输：
       - 普通文件：adb pull / adb push（sync 协议，速度快）
       - 大文件（> STREAM_THRESHOLD）：exec-out/exec-in + dd 分块流式传输，
         1MB 对齐断点续传，内存占用恒定 → F4 / C2
  5. 设备端哈希（sha256sum/md5sum，按可用性回退）
  6. 统一异常翻译为中文友好提示 → G1

并发说明：本层自身无线程安全假设；上层通过线程池（≤3）调用，
每个传输使用独立子进程，互不干扰。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple

from .utils import (AdbError, ControlToken, DeviceGoneError, TaskCancelled,
                    TaskError, Throttler, get_logger)

log = get_logger("adb")

# 单文件超过该阈值时使用分块流式传输（可断点续传、内存恒定）
STREAM_THRESHOLD = 512 * 1024 * 1024
# 流式传输分块大小（每个子进程传输 256MB，失败可在块边界续传）
STREAM_CHUNK = 256 * 1024 * 1024
# dd 的块单位（1MB），续传偏移必须按它对齐
DD_BS = 1024 * 1024
# 设备遍历 stat 输出分片（每 find -exec 批量条数由设备端决定，这里只做流式读取）
STALL_TIMEOUT = 180.0   # 传输停滞超时（秒），超过视为 adb 卡死并重试


# ---------------------------------------------------------------- adb 定位
def find_adb() -> str:
    """定位 adb：内置 → 环境变量 → PATH → 常见安装位置。找不到抛中文提示。"""
    candidates: List[Path] = []

    # 1) 打包后的内置 platform-tools（exe 同目录 / 脚本同目录）
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        exe_dir = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
        exe_dir = base
    for root in (exe_dir, base):
        candidates.append(root / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))

    # 2) 环境变量
    env = os.environ.get("XIAOMI_BACKUP_ADB")
    if env:
        candidates.append(Path(env))

    # 3) PATH
    which = shutil.which("adb")
    if which:
        candidates.append(Path(which))

    # 4) Windows 常见位置
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates += [
            Path(local) / r"Android\Sdk\platform-tools\adb.exe",
            Path(pf) / r"Android\platform-tools\adb.exe",
            Path(pf86) / r"Android\platform-tools\adb.exe",
            Path(r"C:\adb\adb.exe"),
            Path(r"C:\platform-tools\adb.exe"),
        ]

    for c in candidates:
        try:
            if c and c.is_file():
                return str(c.resolve())
        except OSError:
            continue

    raise AdbError(
        "未找到 adb 程序。\n"
        "解决方法（任选其一）：\n"
        "1. 把官方 platform-tools 目录放到本程序目录下的 platform-tools 文件夹；\n"
        "2. 设置环境变量 XIAOMI_BACKUP_ADB 指向 adb 可执行文件；\n"
        "3. 安装 Android SDK Platform-Tools 并加入系统 PATH。"
    )


def _decode(raw: bytes) -> str:
    """设备输出解码：优先 UTF-8，失败降级替换（文件名含中文时保持正确）。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 命令结果
@dataclass
class CmdResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


# ---------------------------------------------------------------- 设备
@dataclass
class DeviceInfo:
    serial: str
    state: str = "device"          # device / unauthorized / offline / ...
    model: str = ""
    brand: str = ""
    product: str = ""
    android: str = ""
    sdk: int = 0
    storage_total: int = 0
    storage_free: int = 0

    @property
    def authorized(self) -> bool:
        return self.state == "device"

    @property
    def display_name(self) -> str:
        return f"{self.brand} {self.model}".strip() or self.serial


class Adb:
    """adb 命令封装（单个 adb 可执行文件）。"""

    def __init__(self, adb_path: Optional[str] = None) -> None:
        self.adb_path = adb_path or find_adb()
        self._base = [self.adb_path]
        log.debug("使用 adb：%s", self.adb_path)

    # -------------------------------------------------- 基础执行
    def run(self, args: List[str], serial: Optional[str] = None,
            timeout: float = 30.0, check: bool = False) -> CmdResult:
        """执行 adb 命令；超时/找不到设备翻译为中文异常。"""
        cmd = list(self._base)
        if serial:
            cmd += ["-s", serial]
        cmd += args
        try:
            p = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as e:
            raise AdbError(
                f"adb 命令超时（{timeout:.0f}s）：{' '.join(args[:4])}…\n"
                f"可能是设备繁忙或 adb 卡死，任务会自动重试。"
            ) from e
        except FileNotFoundError as e:
            raise AdbError(f"adb 程序不可用：{self.adb_path}") from e
        res = CmdResult(p.returncode, _decode(p.stdout), _decode(p.stderr))
        if check and not res.ok:
            raise AdbError(f"adb 命令失败：{' '.join(args)}\n{res.err or res.out}")
        return res

    def shell(self, cmdline: str, serial: Optional[str] = None,
              timeout: float = 30.0, check: bool = False) -> CmdResult:
        """在设备上执行 shell 命令（整条命令字符串，注意自行转义）。"""
        return self.run(["shell", cmdline], serial=serial, timeout=timeout,
                        check=check)

    # -------------------------------------------------- 设备列表 / 信息
    def devices(self) -> List[DeviceInfo]:
        """解析 adb devices -l。"""
        res = self.run(["devices", "-l"], timeout=15)
        out: List[DeviceInfo] = []
        for line in res.out.splitlines():
            line = line.strip()
            if (not line or line.startswith("List of devices")
                    or line.startswith("*")):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            info = DeviceInfo(serial=serial, state=state)
            for kv in parts[2:]:
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    if k == "model":
                        info.model = v.replace("_", " ")
                    elif k == "product":
                        info.product = v
            out.append(info)
        return out

    def first_device(self) -> Optional[DeviceInfo]:
        devs = self.devices()
        for d in devs:
            if d.state == "device":
                return d
        return devs[0] if devs else None

    def get_props(self, serial: str) -> Dict[str, str]:
        """一次性读取 getprop，减少往返。"""
        res = self.shell("getprop", serial=serial, timeout=20)
        props: Dict[str, str] = {}
        for m in re.finditer(r"\[([^\]]+)\]:\s*\[([^\]]*)\]", res.out):
            props[m.group(1)] = m.group(2)
        return props

    def device_info(self, serial: str) -> DeviceInfo:
        """读取型号 / Android 版本 / SDK / 存储容量。"""
        devs = {d.serial: d for d in self.devices()}
        base = devs.get(serial) or DeviceInfo(serial=serial)
        try:
            props = self.get_props(serial)
        except AdbError:
            props = {}
        base.model = base.model or props.get("ro.product.model", "")
        base.brand = props.get("ro.product.manufacturer", "") or base.brand
        base.android = props.get("ro.build.version.release", "")
        try:
            base.sdk = int(props.get("ro.build.version.sdk", "0") or 0)
        except ValueError:
            base.sdk = 0
        total, free = self.free_space("/data", serial=serial)
        base.storage_total, base.storage_free = total, free
        return base

    # -------------------------------------------------- 存储空间
    def free_space(self, path: str, serial: Optional[str] = None) -> Tuple[int, int]:
        """返回 (总量, 剩余) 字节；解析失败返回 (0, 0)。"""
        res = self.shell(f"df -k {shlex.quote(path)}", serial=serial, timeout=20)
        for line in res.out.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[1].isdigit() and fields[3].isdigit():
                return int(fields[1]) * 1024, int(fields[3]) * 1024
        # /sdcard 的 df 在部分机型上要查挂载点
        if path != "/sdcard":
            return self.free_space("/sdcard", serial=serial) if path == "/data" else (0, 0)
        return 0, 0

    def check_target_space(self, serial: str, need_bytes: int,
                           target_dir: str = "/sdcard") -> None:
        """还原前空间预检（D2）：不足时抛中文提示。"""
        total, free = self.free_space(target_dir, serial=serial)
        if free <= 0:
            return  # 无法检测时不拦截，但会在日志提示
        if free < need_bytes * 1.05:
            from .utils import human_size
            raise TaskError(
                f"手机剩余空间不足，无法还原！\n"
                f"需要约 {human_size(need_bytes)}，手机剩余 {human_size(free)}。\n"
                f"请先清理手机空间或更换目标设备。"
            )

    # -------------------------------------------------- 目录遍历
    def list_files(self, root: str, serial: Optional[str] = None,
                   cancel: Optional[ControlToken] = None,
                   warn: Optional[Callable[[str], None]] = None
                   ) -> Generator[Tuple[str, int, int], None, None]:
        """流式遍历目录，产出 (绝对路径, 大小, mtime)。

        用 `find -exec stat` 在设备端批量 stat，输出流式按行读取，
        十万级文件也不会一次性占用内存（F5）。

        健壮性修复：
        - 必须并发排空 stderr，否则 find 在遭遇大量无权限目录
          （如 /sdcard/Android/data）时 stderr 管道写满会阻塞 find，
          导致整机扫描卡死。
        - 检查 find 退出码；遍历不完整时通过 warn 回调显式告警，
          不再静默吞掉（曾经因此让深度扫描整树 0 文件却无任何提示）。
        """
        qroot = shlex.quote(root)
        # 注意：不能用 `find ... -exec stat ... +`。toybox 的 find 在 -exec + 形态下
        # 会把全部匹配文件塞进一个 argv，超过 ARG_MAX 后 execve 失败并直接中止遍历
        # （实测整机 13.9 万文件只扫出 ~8900 就停）。改用 find -print0 | xargs -0 -n N
        # 流式分批：xargs 本就负责按 ARG_MAX 拆分，且单文件 stat 失败不影响继续。
        cmd = (f"find {qroot} -type f -print0 | "
               f"xargs -0 -n 256 stat -c '%s|%Y|%n' 2>/dev/null || true")
        popen = subprocess.Popen(
            [self.adb_path] + (["-s", serial] if serial else []) + ["shell", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert popen.stdout is not None and popen.stderr is not None
        err_buf: List[str] = []

        def _drain_stderr() -> None:
            try:
                for line in popen.stderr:
                    err_buf.append(_decode(line))
            except Exception:
                pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()
        yielded = 0
        try:
            for raw in popen.stdout:
                if cancel is not None:
                    cancel.check()
                line = _decode(raw).rstrip("\r\n")
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                size_s, mtime_s, path = parts
                try:
                    size, mtime = int(size_s), int(mtime_s)
                except ValueError:
                    continue
                yield path, size, mtime
                yielded += 1
        finally:
            try:
                popen.stdout.close()
            except OSError:
                pass
            try:
                popen.kill()
            except OSError:
                pass
            t.join(timeout=2)
            try:
                popen.wait(timeout=5)
            except Exception:
                pass
        # 遍历完整性检查
        if popen.returncode not in (0, None):
            err_text = "".join(err_buf)[-1500:]
            if yielded == 0:
                raise AdbError(
                    f"遍历 {root} 失败（find 退出码 {popen.returncode}）：{err_text}")
            if warn is not None:
                warn(f"目录 {root} 扫描可能不完整（find 退出码 "
                     f"{popen.returncode}，可能存在无权限子目录）。"
                     f"已收录 {yielded} 个文件。{err_text[-400:]}")

    # -------------------------------------------------- 设备端哈希
    def remote_hash(self, path: str, serial: Optional[str] = None
                    ) -> Optional[Tuple[str, str]]:
        """设备端计算哈希，sha256sum 不可用时回退 md5sum；都失败返回 None。"""
        q = shlex.quote(path)
        for algo, bin_name in (("sha256", "sha256sum"), ("md5", "md5sum")):
            res = self.shell(f"{bin_name} {q}", serial=serial, timeout=300)
            if res.ok and res.out.strip():
                digest = res.out.strip().split()[0]
                if re.fullmatch(r"[0-9a-fA-F]{16,64}", digest):
                    return algo, digest.lower()
        return None

    # -------------------------------------------------- 远端文件操作
    def remote_size(self, path: str, serial: Optional[str] = None) -> Optional[int]:
        res = self.shell(f"stat -c %s {shlex.quote(path)}", serial=serial, timeout=20)
        if res.ok and res.out.strip().isdigit():
            return int(res.out.strip())
        return None

    def remote_exists(self, path: str, serial: Optional[str] = None) -> bool:
        res = self.shell(f"test -e {shlex.quote(path)} && echo Y", serial=serial,
                         timeout=20)
        return "Y" in res.out

    def realpath(self, path: str, serial: Optional[str] = None) -> str:
        """返回设备端路径的规范绝对路径（解析软链接）。

        关键修复：部分 Android（尤其 HyperOS）上 /sdcard 是软链接
        （-> /storage/self/primary -> /storage/emulated/0）。
        `find /sdcard` 默认不跟随「起始」软链接，会静默产出 0 文件，
        导致整机深度扫描完全失效。先用 realpath 拿到真实目录再 find 即可下钻。
        """
        res = self.shell(
            f"realpath {shlex.quote(path)} 2>/dev/null || "
            f"readlink -f {shlex.quote(path)} 2>/dev/null",
            serial=serial, timeout=20)
        out = res.out.strip()
        return out or path

    def makedirs(self, remote_path: str, serial: Optional[str] = None) -> None:
        parent = remote_path.rsplit("/", 1)[0]
        if parent:
            self.shell(f"mkdir -p {shlex.quote(parent)}", serial=serial, timeout=30)

    def device_state(self, serial: str) -> str:
        res = self.run(["get-state"], serial=serial, timeout=8)
        if not res.ok:
            return "unknown"
        return res.out.strip() or "unknown"

    # -------------------------------------------------- 传输：普通 pull/push
    def pull(self, remote: str, local: str, serial: Optional[str] = None,
             cancel: Optional[ControlToken] = None,
             progress: Optional[Callable[[int], None]] = None) -> None:
        """adb pull（sync 协议）。本地轮询文件大小做进度上报。"""
        local_path = Path(local)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            local_path.unlink()  # 不复用半成品，避免脏数据
        tmp = Path(str(local_path) + ".pulling")
        self._transfer_cmd(["pull", remote, str(tmp)], tmp, serial,
                           cancel, progress)
        os.replace(tmp, local_path)

    def push(self, local: str, remote: str, serial: Optional[str] = None,
             cancel: Optional[ControlToken] = None,
             progress: Optional[Callable[[int], None]] = None) -> None:
        """adb push（sync 协议）。"""
        self.makedirs(remote, serial=serial)
        self._transfer_cmd(["push", local, remote], Path(local), serial,
                           cancel, progress)

    def _transfer_cmd(self, args: List[str], watch_file: Path,
                      serial: Optional[str],
                      cancel: Optional[ControlToken],
                      progress: Optional[Callable[[int], None]]) -> None:
        cmd = self._base + (["-s", serial] if serial else []) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        last_size, last_move = -1, time.monotonic()
        try:
            while True:
                if cancel is not None and cancel.cancelled:
                    proc.kill()
                    raise TaskCancelled(cancel.reason or "任务已取消")
                if proc.poll() is not None:
                    break
                size = watch_file.stat().st_size if watch_file.exists() else 0
                if size != last_size:          # 有进展 → 刷新进度 & 停滞计时
                    if progress:
                        progress(size)
                    last_size = size
                    last_move = time.monotonic()
                if time.monotonic() - last_move > STALL_TIMEOUT:
                    proc.kill()
                    raise AdbError("传输停滞超时，已中断本次尝试（将自动重试）")
                time.sleep(0.2)
            out, err = proc.communicate()
            if proc.returncode != 0:
                msg = _decode(err) or _decode(out)
                raise self._translate_error(msg, args)
            if progress:
                size = watch_file.stat().st_size if watch_file.exists() else 0
                progress(size)
        finally:
            if proc.poll() is None:
                proc.kill()

    # -------------------------------------------------- 传输：分块流式
    def pull_stream(self, remote: str, local: str, size: int,
                    serial: Optional[str] = None,
                    cancel: Optional[ControlToken] = None,
                    progress: Optional[Callable[[int], None]] = None,
                    speed_limit: float = 0.0) -> None:
        """大文件分块拉取（exec-out + dd），1MB 对齐断点续传。

        - 每个子进程最多拉 STREAM_CHUNK（256MB），进程失败只重拉当前块
        - 输出边收边写盘并流式计数，内存占用恒定（F4）
        - 已存在的本地半成品：截断到 1MB 边界后从该偏移续传（C2）
        """
        local_path = Path(local)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        resume = 0
        if local_path.exists():
            resume = local_path.stat().st_size
            if resume > size:             # 半成品比源还大 → 重新开始
                resume = 0
            resume -= resume % DD_BS      # 截断到 1MB 边界
            with open(local_path, "r+b") as f:
                f.truncate(resume)
        else:
            # 预分配同名占位，便于断点判断
            local_path.touch()

        done = resume
        if progress and done:
            progress(done)

        while done < size:
            if cancel is not None:
                cancel.check()
            this_chunk = min(STREAM_CHUNK, size - done)   # 本次分块字节数
            blocks = (this_chunk + DD_BS - 1) // DD_BS    # 按 1MB 向上取整
            skip = done // DD_BS
            dd = (f"dd if={shlex.quote(remote)} bs={DD_BS} "
                  f"skip={skip} count={blocks} 2>/dev/null")
            self._stream_blocks(["exec-out", dd], local_path, resume_at=done,
                                expect=blocks * DD_BS, size=size,
                                serial=serial, cancel=cancel, progress=progress,
                                speed_limit=speed_limit, write_mode="r+b")
            done = min(size, local_path.stat().st_size)
            if done < size and done % DD_BS:
                done -= done % DD_BS
            if done >= size:
                break
            # 块结束后按文件实际大小推进（dd 可能少返回字节，例如文件末尾）
            if progress:
                progress(done)

        # 最终大小以真实文件为准
        final = local_path.stat().st_size
        if final != size:
            raise AdbError(
                f"文件大小不匹配：期望 {size} 字节，实际拉取 {final} 字节（{remote}）"
            )

    def push_stream(self, local: str, remote: str,
                    serial: Optional[str] = None,
                    cancel: Optional[ControlToken] = None,
                    progress: Optional[Callable[[int], None]] = None,
                    speed_limit: float = 0.0) -> None:
        """大文件分块推送（exec-in + dd seek），1MB 对齐断点续传。"""
        size = os.path.getsize(local)
        self.makedirs(remote, serial=serial)
        remote_size = self.remote_size(remote, serial=serial) or 0
        resume = remote_size if 0 < remote_size <= size else 0
        resume -= resume % DD_BS

        done = resume
        if progress and done:
            progress(done)

        with open(local, "rb") as f:
            f.seek(done)
            while done < size:
                if cancel is not None:
                    cancel.check()
                chunk_len = min(STREAM_CHUNK, size - done)
                skip_blocks = done // DD_BS
                dd = (f"dd of={shlex.quote(remote)} bs={DD_BS} "
                      f"seek={skip_blocks} conv=notrunc 2>/dev/null")
                self._push_blocks(["exec-in", dd], f, chunk_len,
                                  serial=serial, cancel=cancel,
                                  progress=progress, speed_limit=speed_limit,
                                  done_base=done)
                done += chunk_len

        # 大小复核
        actual = self.remote_size(remote, serial=serial)
        if actual != size:
            raise AdbError(
                f"推送后大小不匹配：期望 {size} 字节，设备端 {actual} 字节（{remote}）"
            )

    def _stream_blocks(self, args: List[str], local_path: Path,
                       resume_at: int, expect: int, size: int,
                       serial: Optional[str],
                       cancel: Optional[ControlToken],
                       progress: Optional[Callable[[int], None]],
                       speed_limit: float, write_mode: str) -> None:
        """执行一条 exec-out dd，把 stdout 流式写入本地文件。"""
        cmd = self._base + (["-s", serial] if serial else []) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        assert proc.stdout is not None
        written = 0
        last_data = time.monotonic()
        throttler = Throttler(0.1)
        start = time.monotonic()
        try:
            with open(local_path, write_mode) as out:
                out.seek(resume_at)
                while True:
                    if cancel is not None:
                        if cancel.cancelled:
                            proc.kill()
                            raise TaskCancelled(cancel.reason or "任务已取消")
                        cancel.wait_if_paused()   # 暂停 = 不读管道，天然背压
                    buf = proc.stdout.read(DD_BS)
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
                    last_data = time.monotonic()
                    if progress and throttler.ready():
                        progress(resume_at + written)
                    self._maybe_sleep(start, resume_at + written, speed_limit)
                    if time.monotonic() - last_data > STALL_TIMEOUT:
                        proc.kill()
                        raise AdbError("传输停滞超时（设备无响应），本次尝试中断")
            out_err = proc.communicate(timeout=30)[1]
            if proc.returncode not in (0, None):
                # dd 在文件末尾正常退出为 0；非 0 才算失败
                raise self._translate_error(_decode(out_err), args)
        finally:
            if proc.poll() is None:
                proc.kill()
        if written == 0 and expect > 0 and resume_at >= size:
            return
        if written == 0 and expect > 0:
            raise AdbError("分块传输未读取到数据（设备端 dd 失败），稍后自动重试")

    def _push_blocks(self, args: List[str], f, chunk_len: int,
                     serial: Optional[str],
                     cancel: Optional[ControlToken],
                     progress: Optional[Callable[[int], None]],
                     speed_limit: float, done_base: int) -> None:
        """执行一条 exec-in dd，把本地块写入子进程 stdin。"""
        cmd = self._base + (["-s", serial] if serial else []) + args
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdin is not None
        sent = 0
        throttler = Throttler(0.1)
        start = time.monotonic()
        try:
            while sent < chunk_len:
                if cancel is not None:
                    if cancel.cancelled:
                        proc.kill()
                        raise TaskCancelled(cancel.reason or "任务已取消")
                    cancel.wait_if_paused()
                buf = f.read(min(DD_BS, chunk_len - sent))
                if not buf:
                    break
                proc.stdin.write(buf)
                sent += len(buf)
                if progress and throttler.ready():
                    progress(done_base + sent)
                self._maybe_sleep(start, done_base + sent, speed_limit)
            proc.stdin.close()
            proc.stdin = None           # 避免 communicate 再次 flush 已关闭的管道
            err = proc.stderr.read() if proc.stderr else b""
            proc.wait(timeout=60)
            if proc.returncode != 0:
                raise self._translate_error(_decode(err), args)
        finally:
            if proc.poll() is None:
                proc.kill()

    @staticmethod
    def _maybe_sleep(start: float, sent: int, limit_bps: float) -> None:
        """限速：超过设定速率就睡到平均值回落（3.5 限速选项）。"""
        if limit_bps and limit_bps > 0:
            elapsed = time.monotonic() - start
            should = sent / limit_bps
            if should > elapsed:
                time.sleep(min(1.0, should - elapsed))

    # -------------------------------------------------- 错误翻译
    @staticmethod
    def _translate_error(msg: str, args: List[str]) -> TaskError:
        low = (msg or "").lower()
        if "device offline" in low or "device '" in low and "not found" in low:
            return DeviceGoneError("设备已断开连接，请重新插拔 USB 数据线后继续。")
        if "closed" in low and "device" in low:
            return DeviceGoneError("设备连接中断（可能息屏或拔线），请检查后重连。")
        if "unauthorized" in low:
            return TaskError(
                "设备未授权 USB 调试。\n"
                "请在手机弹窗中点击「允许 USB 调试」，并勾选「始终允许」。"
            )
        if "permission denied" in low or "read-only" in low:
            return TaskError("权限不足：该目录/文件无法访问（未 root 时系统目录不可读）。")
        if "no space left" in low or "not enough space" in low:
            return TaskError("目标设备/磁盘空间不足，请清理后重试。")
        if "protocol fault" in low or "broken pipe" in low:
            return TaskError("adb 通信中断（传输通道异常），将自动重试。")
        if "secure_mkdirs failed" in low or "failed to copy" in low:
            return TaskError(f"文件传输失败：{msg.strip() or '未知原因'}")
        return TaskError(f"adb 执行失败：{msg.strip() or ' '.join(args[:6])}")


# ---------------------------------------------------------------- 设备监控
class DeviceMonitor:
    """轮询检测设备在线状态（A4：拔线提示 / 重连恢复）。"""

    def __init__(self, adb: Adb, serial: str,
                 interval: float = 2.0) -> None:
        self.adb = adb
        self.serial = serial
        self.interval = interval
        self._online = True

    def check_once(self) -> bool:
        """返回设备当前是否在线。"""
        try:
            state = self.adb.device_state(self.serial)
        except AdbError:
            state = "unknown"
        return state == "device"

    def wait_reconnect(self, timeout: float = 300.0,
                       cancel: Optional[ControlToken] = None) -> bool:
        """等待设备重新上线，返回是否成功（掉线后自动续传的基础）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.cancelled:
                return False
            if self.check_once():
                return True
            time.sleep(self.interval)
        return False

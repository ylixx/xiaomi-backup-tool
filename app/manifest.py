# -*- coding: utf-8 -*-
"""
备份清单（manifest）模块。

manifest.json 结构：
{
  "version": 1,
  "created": "2025-01-01 12:00:00",
  "device": {...},
  "entries": [
    {
      "id": "photos/DCIM/Camera/a.jpg",
      "category": "photos",
      "source": "/sdcard/DCIM/Camera/a.jpg",   # 手机端源路径（还原时默认作为目标路径）
      "relpath": "DCIM/Camera/a.jpg",          # 相对类别目录的路径
      "target": "photos/DCIM/Camera/a.jpg",    # 备份目录内的相对路径
      "size": 12345,
      "mtime": 1700000000,
      "sha256": "....",                        # 本地备份文件哈希（还原/校验基准）
      "src_hash": "....",                      # 手机端哈希（能取到时用于双向校验）
      "status": "pending|success|failed|skipped",
      "error": null,
      "tries": 0
    }
  ]
}

要点：
  - 原子化保存（写 .tmp 后 replace），崩溃不会写坏 manifest
  - 每次保存前自动生成 manifest.json.bak
  - manifest 损坏/缺失时可从备份目录扫描重建（E2 降级处理）
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .utils import (TaskError, atomic_write_json, get_logger, hash_file,
                    read_json, sha256_file)

log = get_logger("manifest")

MANIFEST_NAME = "manifest.json"
MANIFEST_BAK = "manifest.json.bak"
INFO_NAME = "backup_info.json"

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
# 增量备份中手机端已删除的文件：数据保留在备份目录，但不再参与还原/续传
STATUS_REMOVED = "removed"


@dataclass
class Entry:
    """manifest 中的一条文件记录。"""

    id: str
    category: str
    source: str            # 手机端路径
    relpath: str           # 类别内相对路径
    target: str            # 备份目录内相对路径（category/relpath）
    size: int = 0
    mtime: int = 0
    sha256: str = ""
    src_hash: str = ""
    status: str = STATUS_PENDING
    error: Optional[str] = None
    tries: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Entry":
        known = {f for f in cls.__dataclass_fields__}  # 兼容旧字段
        return cls(**{k: v for k, v in d.items() if k in known})


class ManifestError(TaskError):
    pass


class Manifest:
    """线程安全的备份清单。"""

    def __init__(self, backup_dir: str | os.PathLike,
                 device: Optional[Dict[str, Any]] = None) -> None:
        self.backup_dir = Path(backup_dir)
        self.path = self.backup_dir / MANIFEST_NAME
        self.version = 1
        self.created = time.strftime("%Y-%m-%d %H:%M:%S")
        self.device = device or {}
        self.entries: List[Entry] = []
        self._index: Dict[str, Entry] = {}
        self._lock = threading.RLock()

    # -------------------------------------------------- 增删查改
    def add(self, entry: Entry) -> None:
        with self._lock:
            if entry.id in self._index:
                return
            self.entries.append(entry)
            self._index[entry.id] = entry

    def get(self, entry_id: str) -> Optional[Entry]:
        with self._lock:
            return self._index.get(entry_id)

    def update(self, entry_id: str, **fields: Any) -> None:
        with self._lock:
            e = self._index.get(entry_id)
            if e is None:
                return
            for k, v in fields.items():
                setattr(e, k, v)

    def all_entries(self) -> List[Entry]:
        with self._lock:
            return list(self.entries)

    def by_status(self, status: str) -> List[Entry]:
        with self._lock:
            return [e for e in self.entries if e.status == status]

    def stats(self) -> Dict[str, Any]:
        """按类别统计：文件数 / 总大小。"""
        with self._lock:
            out: Dict[str, Dict[str, int]] = {}
            for e in self.entries:
                s = out.setdefault(e.category, {"count": 0, "size": 0})
                s["count"] += 1
                s["size"] += e.size
            total_c = len(self.entries)
            total_s = sum(e.size for e in self.entries)
            return {"by_category": out, "count": total_c, "size": total_s}

    # -------------------------------------------------- 持久化
    def save(self) -> None:
        with self._lock:
            data = {
                "version": self.version,
                "created": self.created,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "device": self.device,
                "entries": [e.to_dict() for e in self.entries],
            }
            # 保留上一份作为备份，便于损坏时恢复
            if self.path.exists():
                try:
                    bak = self.backup_dir / MANIFEST_BAK
                    bak.write_bytes(self.path.read_bytes())
                except OSError:
                    pass
            atomic_write_json(self.path, data)

    def save_info(self, extra: Optional[Dict[str, Any]] = None) -> None:
        """写 backup_info.json（设备信息、选项、时间等元数据）。"""
        info = {
            "app": "XiaomiUsbBackup",
            "version": 1,
            "created": self.created,
            "device": self.device,
        }
        if extra:
            info.update(extra)
        atomic_write_json(self.backup_dir / INFO_NAME, info)

    # -------------------------------------------------- 加载 / 重建
    @classmethod
    def load(cls, backup_dir: str | os.PathLike) -> "Manifest":
        """读取 manifest；损坏时抛出中文提示（尝试 .bak）。"""
        backup_dir = Path(backup_dir)
        path = backup_dir / MANIFEST_NAME
        bak = backup_dir / MANIFEST_BAK
        for candidate, label in ((path, ""), (bak, "（使用备份副本）")):
            if not candidate.exists():
                continue
            try:
                data = read_json(candidate)
                m = cls(backup_dir)
                m.version = int(data.get("version", 1))
                m.created = data.get("created", m.created)
                m.device = data.get("device", {})
                m.entries = [Entry.from_dict(d) for d in data.get("entries", [])]
                m._index = {e.id: e for e in m.entries}
                if label:
                    log.warning("manifest 主文件损坏，已加载备份副本 %s", candidate)
                return m
            except Exception as e:  # noqa: BLE001
                log.error("读取 %s 失败：%s", candidate, e)
                if candidate == bak:
                    raise ManifestError(
                        f"备份清单损坏且备份副本也无法解析：{candidate}\n"
                        f"可点击「校验」页的「重建清单」尝试从文件目录恢复。"
                    ) from e
                # 主文件坏了继续试 .bak
        raise ManifestError(
            f"目录中没有找到备份清单（{MANIFEST_NAME}）：{backup_dir}\n"
            f"若确认是本工具生成的备份，可点击「重建清单」按目录扫描恢复（无哈希记录）。"
        )

    @classmethod
    def rebuild(cls, backup_dir: str | os.PathLike) -> "Manifest":
        """E2 降级处理：manifest 丢失/损坏时，从备份目录扫描重建。

        注意：重建的清单没有 sha256，校验阶段会现场计算并写回。
        """
        backup_dir = Path(backup_dir)
        if not backup_dir.is_dir():
            raise ManifestError(f"目录不存在：{backup_dir}")
        m = cls(backup_dir)
        for f in sorted(backup_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.name in (MANIFEST_NAME, MANIFEST_BAK, INFO_NAME):
                continue
            rel = f.relative_to(backup_dir).as_posix()
            category = rel.split("/", 1)[0]
            relpath = rel.split("/", 1)[1] if "/" in rel else f.name
            entry = Entry(
                id=rel,
                category=category,
                source="",       # 重建后未知手机端源路径
                relpath=relpath,
                target=rel,
                size=f.stat().st_size,
                mtime=int(f.stat().st_mtime),
                status=STATUS_SUCCESS,
            )
            m.add(entry)
        m.save()
        log.info("已从目录重建清单：%d 个文件 -> %s", len(m.entries), m.path)
        return m

    # -------------------------------------------------- 校验辅助
    def verify_entry(self, entry: Entry, backup_dir: Optional[Path] = None,
                     cancel=None) -> Dict[str, Any]:
        """校验单个文件：大小 + sha256（流式，内存安全）。"""
        base = backup_dir or self.backup_dir
        fpath = base / entry.target
        result = {"id": entry.id, "ok": False, "reason": ""}
        if not fpath.exists():
            result["reason"] = "文件缺失"
            return result
        actual_size = fpath.stat().st_size
        if entry.size and actual_size != entry.size:
            result["reason"] = f"大小不一致（清单 {entry.size}，实际 {actual_size}）"
            return result
        try:
            digest = sha256_file(fpath, cancel=cancel)
        except TaskError:
            raise
        except Exception as e:  # noqa: BLE001
            result["reason"] = f"读取失败：{e}"
            return result
        if entry.sha256 and digest != entry.sha256:
            result["reason"] = f"sha256 不一致（清单 {entry.sha256[:12]}…，实际 {digest[:12]}…）"
            return result
        if not entry.sha256:
            # 重建的清单没有哈希 → 现场补写，让后续校验有据可依
            entry.sha256 = digest
        result["ok"] = True
        result["sha256"] = digest
        return result

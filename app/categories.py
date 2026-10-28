# -*- coding: utf-8 -*-
"""
备份类别定义：类别 → 手机端目录 + 扩展名过滤规则。

说明：
  - DCIM 同时包含照片和视频，按扩展名分流，避免重复备份
  - 微信 / QQ 常见图片、文件目录一并覆盖（不同版本路径不同，不存在的自动跳过）
  - 扩展名过滤为空表示「目录下全部文件」
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

IMG_EXT = {
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "heif",
    "dng", "raw", "svg", "tif", "tiff", "cr2", "nef", "arw",
}
VIDEO_EXT = {
    "mp4", "3gp", "mkv", "mov", "avi", "webm", "m4v", "mpg", "mpeg",
    "ts", "flv", "rmvb", "wmv", "hevc",
}
AUDIO_EXT = {
    "mp3", "m4a", "aac", "flac", "wav", "ogg", "wma", "amr", "opus",
    "mid", "mka", "ape",
}

# 文档类扩展名白名单（深度扫描/文档类别按此过滤，避免把照片视频一并打包）
# 覆盖：Office / PDF / 电子发票(OFD) / 纯文本 / 数据配置 / WPS / 电子书 / 网页存档
DOC_EXT = {
    # Microsoft Office
    "doc", "docx", "docm", "dot", "dotx",
    "xls", "xlsx", "xlsm", "xlt", "xltx", "xlsb",
    "ppt", "pptx", "pptm", "pot", "potx", "pps", "ppsx",
    # PDF / 增值税电子发票(OFD)
    "pdf", "ofd",
    # 纯文本 / 标记 / 富文本
    "txt", "text", "md", "markdown", "rtf", "log",
    "csv", "tsv",
    # 数据 / 配置
    "json", "xml", "yml", "yaml", "ini", "conf", "cfg", "properties", "dat",
    # 金山 WPS
    "wps", "et", "dps",
    # 电子书
    "epub", "mobi", "azw", "azw3", "fb2",
    # 网页存档
    "html", "htm", "mht", "mhtml",
}


@dataclass
class Category:
    key: str                 # 目录名（photos/audio/video/docs/...）
    label: str               # UI 显示名
    dirs: List[str]          # 手机端扫描根目录
    exts: Optional[set] = None   # None=不过滤；set=仅保留这些扩展名
    desc: str = ""

    def match(self, filename: str) -> bool:
        if not self.exts:
            return True
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext in self.exts


CATEGORIES: Dict[str, Category] = {
    "photos": Category(
        key="photos",
        label="图片（DCIM/Pictures/微信QQ图片）",
        dirs=[
            "/sdcard/DCIM",
            "/sdcard/Pictures",
            "/sdcard/tencent/MicroMsg/WeiXin",
            "/sdcard/Pictures/WeiXin",
            "/sdcard/tencent/QQ_Images",
            "/sdcard/Pictures/QQ",
            "/sdcard/WeiXin",
        ],
        exts=IMG_EXT,
        desc="含相机照片、截图、微信/QQ 保存的图片，保留原始文件名与 EXIF",
    ),
    "audio": Category(
        key="audio",
        label="音频（音乐/录音/铃声）",
        dirs=[
            "/sdcard/Music",
            "/sdcard/MIUI/sound_recorder",
            "/sdcard/Recordings",
            "/sdcard/Ringtones",
            "/sdcard/Notifications",
            "/sdcard/Alarms",
        ],
        exts=AUDIO_EXT,
        desc="含本地音乐、MIUI 录音机录音、铃声",
    ),
    "video": Category(
        key="video",
        label="视频（Movies/DCIM视频/录屏）",
        dirs=[
            "/sdcard/Movies",
            "/sdcard/DCIM",
            "/sdcard/MIUI/screen_recorder",
            "/sdcard/ScreenRecorder",
            "/sdcard/tencent/MicroMsg/Video",
        ],
        exts=VIDEO_EXT,
        desc="含相册视频、录屏、下载视频",
    ),
    "docs": Category(
        key="docs",
        label="文档（Office/PDF/文本/发票/电子书，按类型过滤）",
        dirs=[
            "/sdcard/Download",
            "/sdcard/Downloads",
            "/sdcard/Documents",
            "/sdcard/tencent/QQfile_recv",
            "/sdcard/tencent/MicroMsg/Download",
            "/sdcard/tencent/MicroMsg/WeiXin",
            "/sdcard/WPS",
            "/sdcard/BaiduNetdisk",
            "/sdcard/AliYunPan",           # 阿里云盘
            "/sdcard/TencentDocuments",    # 腾讯文档
            "/sdcard/QuarkDownload",       # 夸克网盘
            "/sdcard/115",                 # 115 网盘
            "/sdcard/Xunlei",             # 迅雷下载
        ],
        exts=DOC_EXT,
        desc="仅备份文档类文件（Office/PDF/文本/OFD发票/电子书等），不打包照片视频；"
             "已内置常见网盘/云盘目录（阿里云盘、腾讯文档、夸克、115、迅雷等）。"
             "仍担心遗漏可勾选「深度扫描整机文档」遍历整个手机存储。",
    ),
    "contacts": Category(
        key="contacts",
        label="联系人/短信/通话记录（导出为文本）",
        dirs=[],
        exts=None,
        desc="通过 adb content 查询导出（部分 MIUI 版本可能限制读取，导出失败不影响其它类别）",
    ),
    "appdata": Category(
        key="appdata",
        label="应用数据（adb backup，需手机端确认）",
        dirs=[],
        exts=None,
        desc="未 root 无法完整导出应用私有数据；微信聊天记录请用「微信自带迁移」",
    ),
}

# 备份目录结构中的固定子目录（与验收 B4 对应）
MEDIA_KEYS = ("photos", "audio", "video", "docs")

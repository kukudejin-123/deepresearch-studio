"""
文件上传安全模块

第一期安全改造：原版上传接口直接把任意文件名、任意类型、任意大小的
文件落盘，存在路径穿越、恶意脚本、磁盘打满三类风险。本模块提供：

1. 扩展名白名单（可通过 UPLOAD_ALLOWED_EXTENSIONS 配置）；
2. 单文件大小上限（UPLOAD_MAX_MB，流式写入时实时校验，超限即中断）；
3. 文件名清洗：剥离目录成分、控制字符，限制长度，拒绝空名和隐藏文件。
"""

import os
import re
from pathlib import Path
from typing import Optional

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 默认允许的文件类型：与 read_file_content 工具支持的格式保持一致
_DEFAULT_ALLOWED_EXTENSIONS = ".pdf,.docx,.md,.txt,.xlsx,.xls,.csv"

# 文件名最大长度（不含目录）
_MAX_FILENAME_LENGTH = 100

# 控制字符（含 NUL）和 Windows 保留字符
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")


def get_allowed_extensions() -> set[str]:
    """从环境变量读取允许的扩展名集合"""
    raw = os.getenv("UPLOAD_ALLOWED_EXTENSIONS", _DEFAULT_ALLOWED_EXTENSIONS)
    return {ext.strip().lower() for ext in raw.split(",") if ext.strip()}


def get_max_upload_bytes() -> int:
    """从环境变量读取单文件大小上限（字节）"""
    try:
        megabytes = float(os.getenv("UPLOAD_MAX_MB", "20"))
    except ValueError:
        megabytes = 20.0
    return int(megabytes * 1024 * 1024)


def sanitize_filename(filename: str) -> Optional[str]:
    """
    清洗上传文件名

    - 只保留 basename，剥离一切目录成分（防止 `../../etc/passwd`）；
    - 移除控制字符和 Windows 保留字符；
    - 限制长度；空名、隐藏文件直接拒绝。

    :param filename: 原始文件名
    :return: 清洗后的文件名；不合法时返回 None
    """
    if not filename:
        return None

    # 客户端可能携带完整路径或 URL 路径，统一只取最后一段
    name = Path(filename.replace("\\", "/")).name
    name = _CONTROL_CHARS_PATTERN.sub("", name).strip()
    # 只剥离尾部点号（Windows 不允许以点结尾的文件名）；
    # 前导点代表隐藏文件，保留下来供下一步拒绝
    name = name.rstrip(".")

    if not name:
        return None
    if name.startswith("."):
        return None
    if len(name) > _MAX_FILENAME_LENGTH:
        suffix = Path(name).suffix[:20]
        stem = Path(name).stem[: _MAX_FILENAME_LENGTH - len(suffix) - 1]
        name = f"{stem}{suffix}"

    return name


def validate_upload(filename: str) -> Optional[str]:
    """
    校验单个上传文件（文件名 + 扩展名）

    :param filename: 原始文件名
    :return: 拒绝原因；通过时返回 None
    """
    safe_name = sanitize_filename(filename)
    if safe_name is None:
        return f"文件名不合法: {filename!r}"

    extension = Path(safe_name).suffix.lower()
    if not extension:
        return f"文件缺少扩展名，无法识别类型: {filename!r}"

    if extension not in get_allowed_extensions():
        allowed = "、".join(sorted(get_allowed_extensions()))
        return f"不支持的文件类型 {extension}，允许的类型: {allowed}"

    return None


class UploadTooLargeError(Exception):
    """上传文件超过大小上限时抛出"""

    def __init__(self, filename: str, max_bytes: int):
        self.filename = filename
        self.max_bytes = max_bytes
        super().__init__(f"文件 {filename} 超过大小上限 {max_bytes // (1024 * 1024)}MB")

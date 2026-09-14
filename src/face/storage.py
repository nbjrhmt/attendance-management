"""人脸照片存储：格式校验、魔数嗅探与落盘。

安全与可靠性要点：

- **扩展名不可信**：以文件头（魔数）判断真实格式，只接受 JPEG / PNG / BMP；
- **大小限制**：默认单张不超过 ``FACE_MAX_IMAGE_MB``（默认 2MB）；
- **存储路径**：``FACE_UPLOAD_DIR/{family_id}/{member_id}_{时间戳}_{md5前8位}.{ext}``，
  按家庭分目录便于运维排查，文件名含时间戳与内容摘要避免覆盖；
- **不对外暴露静态目录**：``uploads/`` 是身份证级的人脸数据，
  读取统一走带鉴权的 ``GET /api/face/photo/{member_id}``。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import BASE_DIR, settings

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "sniff_image_suffix",
    "validate_image",
    "save_image",
    "resolve_image_path",
    "content_type_of",
]

#: 支持的图片格式与 MIME 类型
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
}

#: 文件头（魔数）与对应扩展名
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"BM", ".bmp"),
)

#: 读取文件头所需的字节数
_HEAD_BYTES = 8


def sniff_image_suffix(content: bytes) -> str | None:
    """根据文件头判断图片格式，返回扩展名；无法识别时返回 ``None``。"""
    head = content[:_HEAD_BYTES]
    for signature, suffix in _MAGIC_SIGNATURES:
        if head.startswith(signature):
            return suffix
    return None


def content_type_of(suffix: str) -> str:
    """由扩展名取 MIME 类型。"""
    return ALLOWED_CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")


def validate_image(content: bytes, filename: str | None = None) -> str:
    """校验人脸照片并返回规范化后的扩展名。

    校验项：非空、大小上限、文件头是否为受支持的图片格式。

    :raises BusinessError: 校验失败（400）
    """
    if not content:
        raise BusinessError("上传的照片为空", code=ResponseCode.PARAM_ERROR)

    max_bytes = settings.face_max_image_bytes
    if len(content) > max_bytes:
        raise BusinessError(
            f"照片大小不能超过 {settings.FACE_MAX_IMAGE_MB}MB（当前 {len(content) / 1024 / 1024:.1f}MB）",
            code=ResponseCode.PARAM_ERROR,
        )

    suffix = sniff_image_suffix(content)
    if suffix is None:
        raise BusinessError(
            "照片格式不受支持，仅支持 JPG / PNG / BMP 格式",
            code=ResponseCode.PARAM_ERROR,
        )

    declared = Path(filename or "").suffix.lower()
    if declared and declared != suffix and ALLOWED_CONTENT_TYPES.get(declared) != content_type_of(suffix):
        logger.warning(
            "上传照片扩展名(%s)与实际格式(%s)不一致，已按实际格式处理", declared, suffix
        )
    return suffix


def _upload_root() -> Path:
    """人脸照片根目录（不存在时创建）。"""
    root = settings.face_upload_path
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_image(
    content: bytes, *, family_id: int, member_id: int, suffix: str
) -> tuple[str, str]:
    """保存人脸照片。

    :return: ``(相对项目根目录的路径, 照片 MD5)``
    """
    md5 = hashlib.md5(content).hexdigest()
    target_dir = _upload_root() / str(family_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = target_dir / f"{member_id}_{timestamp}_{md5[:8]}{suffix}"
    target.write_bytes(content)

    try:
        relative = target.relative_to(BASE_DIR).as_posix()
    except ValueError:
        # 目录被配置到项目外时，直接记录绝对路径
        relative = target.as_posix()
    logger.info("人脸照片已保存：%s（%d 字节）", relative, len(content))
    return relative, md5


def resolve_image_path(relative_path: str) -> Path:
    """把数据库中记录的相对路径还原为绝对路径。"""
    path = Path(relative_path)
    return path if path.is_absolute() else BASE_DIR / path

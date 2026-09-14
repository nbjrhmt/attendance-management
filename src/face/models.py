"""人脸模块数据模型：``face_record`` 表与相关枚举。

设计要点：

- **一成员一条记录**：``member_id`` 唯一并级联删除（成员被删除时记录随之删除）；
- 记录只保存"该成员当前的人脸"，重新录入会覆盖 ``face_token`` 与照片信息；
- 删除人脸采用**停用**（``status=inactive``）而非物理删除，保留照片便于追溯与重新录入；
- ``last_matched_at`` / ``last_match_score`` 用于记录最近一次识别情况，供后续签到与审计使用。

人脸照片不通过静态目录对外暴露：``uploads/`` 下有全村的身份证级人脸数据，
因此照片统一走带鉴权的 ``GET /api/face/photo/{member_id}`` 接口读取。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column
from src.member.models import FamilyMember  # noqa: F401  注册 family_member 模型（外键目标）

__all__ = ["FaceProviderName", "FaceRecord", "FaceRecordStatus"]


class FaceProviderName(str, Enum):
    """人脸识别提供方。"""

    BAIDU = "baidu"
    LOCAL = "local"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {"baidu": "百度AI人脸识别", "local": "本地模式（不比对）"}[self.value]


class FaceRecordStatus(str, Enum):
    """人脸记录状态。"""

    REGISTERED = "registered"
    INACTIVE = "inactive"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {"registered": "已录入", "inactive": "已删除"}[self.value]


class FaceRecord(BaseModel):
    """人脸录入记录表（与家庭成员一对一）。"""

    __tablename__ = "face_record"
    __table_args__ = (
        Index("ix_face_record_status_provider", "status", "provider"),
        {"comment": "人脸录入记录表（与家庭成员一对一）"},
    )

    member_id: Mapped[int] = mapped_column(
        ForeignKey("family_member.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="家庭成员ID（family_member.id，一对一）",
    )
    provider: Mapped[FaceProviderName] = mapped_column(
        enum_column(FaceProviderName, name="face_provider"),
        nullable=False,
        default=FaceProviderName.BAIDU,
        comment="人脸识别提供方：baidu 百度AI / local 本地模式",
    )
    group_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="人脸库（用户组）标识",
    )
    face_token: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="提供方返回的人脸标识（百度 face_token）",
    )
    image_path: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="人脸照片路径（相对项目根目录）",
    )
    image_md5: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="照片 MD5，用于查重与追溯",
    )
    image_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="照片字节数",
    )
    status: Mapped[FaceRecordStatus] = mapped_column(
        enum_column(FaceRecordStatus, name="face_record_status"),
        nullable=False,
        default=FaceRecordStatus.REGISTERED,
        comment="状态：registered 已录入 / inactive 已删除",
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="最近一次录入时间",
    )
    last_matched_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近一次识别成功时间",
    )
    last_match_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="最近一次识别得分（0-100）",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="备注",
    )

    @property
    def is_registered(self) -> bool:
        """人脸是否处于已录入（可参与识别）状态。"""
        return self.status == FaceRecordStatus.REGISTERED

    def __repr__(self) -> str:
        return (
            f"<FaceRecord id={self.id} member_id={self.member_id} "
            f"provider={self.provider.value} status={self.status.value}>"
        )

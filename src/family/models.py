"""家庭模型：家庭表 ``family`` 与家庭状态枚举。

设计要点：

- **一户主一家庭**：``owner_id`` 关联 ``sys_user.id``（role=family 的账号）并加唯一约束；
- **户号**：可由管理员指定真实户籍号；未指定时由系统生成 ``F`` + 6 位序号
  （形如 ``F000123``）。生成为"插入后按自增主键回填"，因此 ``household_no`` 在
  数据库层允许为空，但业务层保证提交时一定有值（见 :func:`src.family.crud.create_family`）；
- **成员关系**：``family_member`` 表见 :mod:`src.member.models`，仅按 ``family_id`` 关联，
  不在此处定义 ``members`` relationship，避免模块间双向依赖。

时间字段与其他表一致，使用服务器本地时间。
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.common.database import BaseModel
from src.common.models import enum_column
from src.user.models import User  # noqa: F401  注册 sys_user 模型并用于类型标注

__all__ = ["Family", "FamilyStatus"]


class FamilyStatus(str, Enum):
    """家庭状态。"""

    ACTIVE = "active"
    INACTIVE = "inactive"

    @property
    def label(self) -> str:
        """中文名称，用于日志与提示信息。"""
        return {"active": "正常", "inactive": "已停用"}[self.value]


class Family(BaseModel):
    """家庭表（以户主为单位，一户主一家庭）。"""

    __tablename__ = "family"
    __table_args__ = (
        Index("ix_family_status_village", "status", "village"),
        {"comment": "家庭表（以户主为单位）"},
    )

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("sys_user.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
        comment="户主用户ID（sys_user.id，role=family）",
    )
    household_no: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        unique=True,
        index=True,
        comment="户号（唯一）；未指定时自动生成 F+6位序号，插入后同一事务内回填",
    )
    address: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="家庭住址",
    )
    village: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="所属村/组",
    )
    contact_phone: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="家庭联系电话",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="备注",
    )
    status: Mapped[FamilyStatus] = mapped_column(
        enum_column(FamilyStatus, name="family_status"),
        nullable=False,
        default=FamilyStatus.ACTIVE,
        comment="状态：active 正常 / inactive 已停用",
    )

    #: 户主账号（``lazy="selectin"`` 保证查询家庭时自动加载，便于返回户主姓名/电话）
    owner: Mapped[User] = relationship("User", lazy="selectin")

    @property
    def is_active(self) -> bool:
        """家庭是否正常（未停用）。"""
        return self.status == FamilyStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"<Family id={self.id} household_no={self.household_no!r} "
            f"owner_id={self.owner_id} status={self.status.value}>"
        )

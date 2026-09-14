"""家庭成员模型：成员表 ``family_member`` 与性别、关系、状态枚举。

设计要点：

- 成员按 ``family_id`` 归属家庭；``user_id`` 仅在"户主本人"这一行有值，
  用于把登录账号与签到对象关联起来（户主注册时自动生成该行）；
- ``needs_checkin``：家庭可为每位成员单独设置是否需要签到（对应 AGENTS.md 的业务规则）；
- ``id_card`` 唯一可空：允许暂不录入身份证号，录入后不允许重复；
- 状态使用 ``active`` / ``inactive``（停用）而非物理删除，便于保留历史签到数据：
  阶段五引入 ``checkin_record`` / ``leave_request``（均以成员为外键，``ON DELETE RESTRICT``）后，
  成员的 ``DELETE`` 接口语义即为"停用"（见 :func:`src.member.service.delete_member`）；
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from sqlalchemy import Boolean, Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column
from src.family.models import Family  # noqa: F401  注册 family 模型（外键目标）

__all__ = ["FamilyMember", "Gender", "MemberRelation", "MemberStatus"]


class Gender(str, Enum):
    """性别。"""

    MALE = "male"
    FEMALE = "female"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {"male": "男", "female": "女"}[self.value]


class MemberRelation(str, Enum):
    """与户主的关系。"""

    HOUSEHOLDER = "householder"
    SPOUSE = "spouse"
    SON = "son"
    DAUGHTER = "daughter"
    FATHER = "father"
    MOTHER = "mother"
    OTHER = "other"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {
            "householder": "户主",
            "spouse": "配偶",
            "son": "儿子",
            "daughter": "女儿",
            "father": "父亲",
            "mother": "母亲",
            "other": "其他",
        }[self.value]


class MemberStatus(str, Enum):
    """成员状态。"""

    ACTIVE = "active"
    INACTIVE = "inactive"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {"active": "正常", "inactive": "已停用"}[self.value]


class FamilyMember(BaseModel):
    """家庭成员表。"""

    __tablename__ = "family_member"
    __table_args__ = (
        Index("ix_family_member_family_status", "family_id", "status"),
        {"comment": "家庭成员表"},
    )

    family_id: Mapped[int] = mapped_column(
        ForeignKey("family.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="所属家庭ID（family.id）",
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联登录账号（仅户主本人有值，其他成员为空）",
    )
    name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="姓名",
    )
    gender: Mapped[Gender | None] = mapped_column(
        enum_column(Gender, name="member_gender", length=10),
        nullable=True,
        comment="性别：male 男 / female 女",
    )
    relation: Mapped[MemberRelation] = mapped_column(
        enum_column(MemberRelation, name="member_relation"),
        nullable=False,
        default=MemberRelation.OTHER,
        comment="与户主关系：householder 户主 / spouse 配偶 / son 儿子 / daughter 女儿 / father 父亲 / mother 母亲 / other 其他",
    )
    id_card: Mapped[str | None] = mapped_column(
        String(18),
        nullable=True,
        unique=True,
        index=True,
        comment="身份证号（18 位，唯一）",
    )
    birth_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="出生日期",
    )
    phone: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="联系电话",
    )
    needs_checkin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否需要签到：1 需要 / 0 不需要",
    )
    status: Mapped[MemberStatus] = mapped_column(
        enum_column(MemberStatus, name="member_status"),
        nullable=False,
        default=MemberStatus.ACTIVE,
        comment="状态：active 正常 / inactive 已停用",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="备注",
    )

    @property
    def is_active(self) -> bool:
        """成员是否正常（未停用）。"""
        return self.status == MemberStatus.ACTIVE

    @property
    def is_householder(self) -> bool:
        """是否为户主本人的成员记录。"""
        return self.relation == MemberRelation.HOUSEHOLDER

    def __repr__(self) -> str:
        return (
            f"<FamilyMember id={self.id} family_id={self.family_id} "
            f"name={self.name!r} relation={self.relation.value} status={self.status.value}>"
        )

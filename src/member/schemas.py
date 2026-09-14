"""家庭成员模块的请求/响应数据结构。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from src.member.id_card import validate_id_card
from src.member.models import Gender, MemberRelation, MemberStatus
from src.user.schemas import PhoneStr, RealNameStr

__all__ = [
    "IdCardStr",
    "MemberCreate",
    "MemberOut",
    "MemberStatusUpdate",
    "MemberUpdate",
]

#: 身份证号字段：18 位，含校验位校验，自动规范化为大写 X
IdCardStr = Annotated[str, Field(min_length=18, max_length=18), AfterValidator(validate_id_card)]


class MemberCreate(BaseModel):
    """添加家庭成员请求。"""

    model_config = ConfigDict(extra="forbid")

    name: RealNameStr = Field(description="成员姓名")
    relation: MemberRelation = Field(
        default=MemberRelation.OTHER, description="与户主关系"
    )
    gender: Gender | None = Field(
        default=None, description="性别；填写身份证号时可留空自动识别"
    )
    id_card: IdCardStr | None = Field(
        default=None, description="身份证号（18 位，需唯一）"
    )
    birth_date: date | None = Field(
        default=None, description="出生日期；填写身份证号时可留空自动识别"
    )
    phone: PhoneStr | None = Field(default=None, description="联系电话")
    needs_checkin: bool = Field(default=True, description="是否需要签到，默认需要")
    remark: str | None = Field(default=None, max_length=255, description="备注")
    family_id: int | None = Field(
        default=None,
        ge=1,
        description="所属家庭ID；仅管理员可为指定家庭添加成员，家庭用户添加时系统自动使用本人家庭",
    )


class MemberUpdate(BaseModel):
    """修改家庭成员请求（未提交的字段保持原值）。"""

    model_config = ConfigDict(extra="forbid")

    name: RealNameStr | None = Field(default=None, description="成员姓名")
    relation: MemberRelation | None = Field(default=None, description="与户主关系")
    gender: Gender | None = Field(default=None, description="性别")
    id_card: IdCardStr | None = Field(
        default=None, description="身份证号，传 null 表示清空"
    )
    birth_date: date | None = Field(default=None, description="出生日期")
    phone: PhoneStr | None = Field(default=None, description="联系电话，传 null 表示清空")
    needs_checkin: bool | None = Field(default=None, description="是否需要签到")
    remark: str | None = Field(default=None, max_length=255, description="备注")


class MemberStatusUpdate(BaseModel):
    """启用/停用成员请求。"""

    model_config = ConfigDict(extra="forbid")

    status: MemberStatus = Field(description="目标状态：active 正常 / inactive 已停用")


class MemberOut(BaseModel):
    """成员信息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="成员ID")
    family_id: int = Field(description="所属家庭ID")
    user_id: int | None = Field(default=None, description="关联登录账号ID（户主本人）")
    name: str = Field(description="姓名")
    gender: Gender | None = Field(default=None, description="性别")
    relation: MemberRelation = Field(description="与户主关系")
    id_card: str | None = Field(default=None, description="身份证号（敏感信息，请勿外传）")
    birth_date: date | None = Field(default=None, description="出生日期")
    phone: str | None = Field(default=None, description="联系电话")
    needs_checkin: bool = Field(description="是否需要签到")
    status: MemberStatus = Field(description="状态")
    remark: str | None = Field(default=None, description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

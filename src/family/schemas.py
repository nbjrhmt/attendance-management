"""家庭模块的请求/响应数据结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.family.models import FamilyStatus
from src.member.schemas import MemberOut
from src.user.schemas import PhoneStr

__all__ = [
    "FamilyCreate",
    "FamilyDetailOut",
    "FamilyOut",
    "FamilyRegisterInfo",
    "FamilyUpdate",
]


class FamilyRegisterInfo(BaseModel):
    """户主注册时可选携带的初始家庭档案信息。

    全部字段可留空：留空时系统自动创建家庭档案并生成户号（``F`` + 6 位序号）。
    """

    model_config = ConfigDict(extra="forbid")

    household_no: str | None = Field(
        default=None, min_length=1, max_length=32, description="户号；留空自动生成"
    )
    address: str | None = Field(default=None, max_length=200, description="家庭住址")
    village: str | None = Field(default=None, max_length=100, description="所属村/组")
    contact_phone: PhoneStr | None = Field(default=None, description="家庭联系电话")
    remark: str | None = Field(default=None, max_length=255, description="备注")


class FamilyCreate(FamilyRegisterInfo):
    """管理员创建家庭档案请求。"""

    owner_id: int = Field(
        ge=1, description="户主用户ID（必须是 family 角色且尚无家庭档案的用户）"
    )


class FamilyUpdate(BaseModel):
    """修改家庭信息请求（未提交的字段保持原值）。"""

    model_config = ConfigDict(extra="forbid")

    household_no: str | None = Field(
        default=None, min_length=1, max_length=32, description="户号（不可为空）"
    )
    address: str | None = Field(
        default=None, max_length=200, description="家庭住址，传 null 表示清空"
    )
    village: str | None = Field(
        default=None, max_length=100, description="所属村/组，传 null 表示清空"
    )
    contact_phone: PhoneStr | None = Field(
        default=None, description="家庭联系电话，传 null 表示清空"
    )
    remark: str | None = Field(
        default=None, max_length=255, description="备注，传 null 表示清空"
    )
    status: FamilyStatus | None = Field(
        default=None, description="状态：active 正常 / inactive 已停用"
    )


class FamilyOut(BaseModel):
    """家庭信息响应（不含成员明细）。"""

    id: int = Field(description="家庭ID")
    household_no: str | None = Field(description="户号")
    owner_id: int = Field(description="户主用户ID")
    owner_name: str = Field(description="户主姓名")
    owner_username: str = Field(description="户主登录用户名")
    owner_phone: str | None = Field(default=None, description="户主手机号")
    address: str | None = Field(default=None, description="家庭住址")
    village: str | None = Field(default=None, description="所属村/组")
    contact_phone: str | None = Field(default=None, description="家庭联系电话")
    remark: str | None = Field(default=None, description="备注")
    status: FamilyStatus = Field(description="状态")
    member_count: int = Field(default=0, description="有效成员数（不含已停用成员）")
    checkin_required_count: int = Field(default=0, description="其中需要签到的成员数")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")


class FamilyDetailOut(FamilyOut):
    """家庭详情响应（含成员列表）。"""

    members: list[MemberOut] = Field(
        default_factory=list, description="家庭成员列表（含已停用成员）"
    )

"""用户模块的请求/响应数据结构（Pydantic 模型）。

约定：

- 输入模型（``*Create`` / ``*Update`` / ``*Change`` 等）统一使用
  ``extra="forbid"``，拒绝未声明的字段，避免越权字段被静默忽略；
- 输出模型（``UserOut``）永远不会包含 ``password_hash``，杜绝密码外泄；
- 响应模型开启 ``from_attributes``，可直接由 ORM 对象校验生成。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from src.user.models import UserRole, UserStatus

__all__ = [
    "PasswordChange",
    "PasswordReset",
    "UserAdminUpdate",
    "UserCreate",
    "UserOut",
    "UserRegister",
    "UserStatusUpdate",
]

#: 密码最小长度（字符数）
PASSWORD_MIN_LENGTH: int = 6

#: 密码最大长度（字符数）
PASSWORD_MAX_LENGTH: int = 64

#: bcrypt 算法只处理前 72 字节，超长会导致密码尾部被静默丢弃，因此显式拦截
BCRYPT_MAX_BYTES: int = 72


def _validate_password_bytes(value: str) -> str:
    """校验密码的 UTF-8 字节长度不超过 bcrypt 上限。"""
    byte_length = len(value.encode("utf-8"))
    if byte_length > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"密码过长：UTF-8 编码后不能超过 {BCRYPT_MAX_BYTES} 字节（当前 {byte_length} 字节）"
        )
    return value


#: 密码字段：6~64 个字符，且 UTF-8 编码不超过 72 字节
PasswordStr = Annotated[
    str,
    Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    AfterValidator(_validate_password_bytes),
]

#: 用户名字段：4~50 位字母、数字或下划线
UsernameStr = Annotated[str, Field(min_length=4, max_length=50, pattern=r"^[A-Za-z0-9_]+$")]

#: 真实姓名字段
RealNameStr = Annotated[str, Field(min_length=1, max_length=50)]

#: 手机号字段：中国大陆手机号
PhoneStr = Annotated[str, Field(pattern=r"^1[3-9]\d{9}$")]


class UserRegister(BaseModel):
    """家庭用户（户主）注册请求。"""

    model_config = ConfigDict(extra="forbid")

    username: UsernameStr = Field(description="登录用户名，4~50 位字母、数字或下划线")
    password: PasswordStr = Field(description="登录密码，6~64 位")
    real_name: RealNameStr = Field(description="户主真实姓名")
    phone: PhoneStr | None = Field(default=None, description="手机号，可选，需唯一")


class UserCreate(BaseModel):
    """管理员新增用户请求。"""

    model_config = ConfigDict(extra="forbid")

    username: UsernameStr = Field(description="登录用户名")
    password: PasswordStr = Field(description="初始密码，6~64 位")
    real_name: RealNameStr = Field(description="真实姓名")
    phone: PhoneStr | None = Field(default=None, description="手机号，可选，需唯一")
    role: UserRole = Field(default=UserRole.FAMILY, description="角色，默认家庭用户")
    status: UserStatus = Field(default=UserStatus.ACTIVE, description="状态，默认正常")


class UserAdminUpdate(BaseModel):
    """用户信息修改请求。

    管理员可修改全部字段；家庭用户/工作人员调用时只能修改 ``real_name`` 与 ``phone``，
    若提交 ``role`` 或 ``status`` 会被拒绝（403）。

    未提交的字段保持原值：``model_dump(exclude_unset=True)`` 用于区分
    "未提交" 与 "显式提交 null（清空手机号）"。
    """

    model_config = ConfigDict(extra="forbid")

    real_name: RealNameStr | None = Field(default=None, description="真实姓名")
    phone: PhoneStr | None = Field(default=None, description="手机号，传 null 表示清空")
    role: UserRole | None = Field(default=None, description="角色（仅管理员可改）")
    status: UserStatus | None = Field(default=None, description="状态（仅管理员可改）")


class UserStatusUpdate(BaseModel):
    """启用/禁用用户请求。"""

    model_config = ConfigDict(extra="forbid")

    status: UserStatus = Field(description="目标状态：active 启用 / disabled 禁用")


class PasswordChange(BaseModel):
    """本人修改密码请求。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, description="原密码")
    new_password: PasswordStr = Field(description="新密码，6~64 位")


class PasswordReset(BaseModel):
    """管理员重置密码请求（无需原密码）。"""

    model_config = ConfigDict(extra="forbid")

    new_password: PasswordStr = Field(description="新密码，6~64 位")


class UserOut(BaseModel):
    """用户信息响应（不含任何密码字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="用户ID")
    username: str = Field(description="登录用户名")
    real_name: str = Field(description="真实姓名")
    phone: str | None = Field(default=None, description="手机号")
    role: UserRole = Field(description="角色")
    status: UserStatus = Field(description="状态")
    last_login_at: datetime | None = Field(default=None, description="最后登录时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

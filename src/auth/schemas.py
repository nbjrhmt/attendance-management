"""认证模块的请求/响应数据结构（Pydantic 模型）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.family.schemas import FamilyRegisterInfo
from src.user.schemas import UserOut, UserRegister

__all__ = ["LoginRequest", "RefreshRequest", "RegisterRequest", "TokenResponse"]


class RegisterRequest(UserRegister):
    """家庭用户（户主）注册请求：账号信息 + 可选的初始家庭档案信息。

    注册成功后系统会在同一事务内自动创建**家庭档案**（户号形如 ``F000123``）
    与**户主成员记录**；家庭信息全部留空也可注册，之后再通过
    ``PUT /api/families/{family_id}`` 补充住址等信息即可。
    """

    family: FamilyRegisterInfo | None = Field(
        default=None, description="初始家庭档案信息（可选，留空自动生成户号）"
    )


class LoginRequest(BaseModel):
    """登录请求。

    这里故意不对密码做复杂度校验：登录只校验"是否正确"，
    密码策略属于注册/改密环节，避免登录接口泄露密码规则。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=50, description="用户名")
    password: str = Field(min_length=1, max_length=128, description="密码")


class RefreshRequest(BaseModel):
    """刷新访问令牌请求。"""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, description="登录接口返回的刷新令牌")


class TokenResponse(BaseModel):
    """登录/刷新成功返回的令牌信息。"""

    access_token: str = Field(description="访问令牌，用于业务接口鉴权")
    refresh_token: str = Field(description="刷新令牌，用于换取新的访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型，固定为 bearer")
    expires_in: int = Field(description="访问令牌有效期（秒）")
    user: UserOut = Field(description="登录用户信息")

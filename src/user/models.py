"""用户模型：用户表 ``sys_user`` 及角色、状态枚举。

角色划分（对应 AGENTS.md 的角色说明）：

- ``admin``：管理员，可管理所有数据、查看统计报表、审批请假
- ``staff``：工作人员，可协助管理员进行签到操作
- ``family``：家庭用户（户主），可注册、管理家庭成员、进行人脸签到

表命名约定：账号/权限类系统表统一使用 ``sys_`` 前缀（如 ``sys_user``），
业务表使用业务名（如 ``family``、``checkin_record``），避免与 MySQL 关键字冲突。

时间约定：数据库中的时间字段使用服务器本地时间（``func.now()``），不使用 UTC；
JWT 中的 ``iat`` / ``exp`` 遵循 JWT 规范使用 UTC 时间戳。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column

__all__ = ["User", "UserRole", "UserStatus"]


class UserRole(str, Enum):
    """用户角色。"""

    ADMIN = "admin"
    STAFF = "staff"
    FAMILY = "family"

    @property
    def label(self) -> str:
        """中文名称，用于日志与提示信息。"""
        return {"admin": "管理员", "staff": "工作人员", "family": "家庭用户"}[self.value]


class UserStatus(str, Enum):
    """用户状态。"""

    ACTIVE = "active"
    DISABLED = "disabled"

    @property
    def label(self) -> str:
        """中文名称，用于日志与提示信息。"""
        return {"active": "正常", "disabled": "已禁用"}[self.value]


class User(BaseModel):
    """用户表。

    - 用户名全局唯一，作为登录账号；
    - 密码只存储 bcrypt 哈希（``password_hash``），永不存储明文；
    - 手机号可选但唯一（MySQL 与 SQLite 的唯一索引均允许多个 NULL）；
    - ``family`` 角色用户即家庭户主，家庭详情在阶段三的家庭模块中维护。
    """

    __tablename__ = "sys_user"
    __table_args__ = (
        Index("ix_sys_user_role_status", "role", "status"),
        {"comment": "用户表（管理员 / 工作人员 / 家庭用户）"},
    )

    username: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="登录用户名，全局唯一",
    )
    password_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="密码哈希（bcrypt，60 字符）",
    )
    real_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="真实姓名",
    )
    phone: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        unique=True,
        index=True,
        comment="手机号，可为空且唯一",
    )
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, name="user_role"),
        nullable=False,
        default=UserRole.FAMILY,
        comment="角色：admin 管理员 / staff 工作人员 / family 家庭用户",
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, name="user_status"),
        nullable=False,
        default=UserStatus.ACTIVE,
        comment="状态：active 正常 / disabled 已禁用",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="最后登录时间",
    )

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        """是否为管理员。"""
        return self.role == UserRole.ADMIN

    @property
    def is_active(self) -> bool:
        """账号是否可正常使用。"""
        return self.status == UserStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} username={self.username!r} "
            f"role={self.role.value} status={self.status.value}>"
        )

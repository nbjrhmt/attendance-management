"""ORM 公共工具：可移植的枚举列类型。

项目约定（详见 ``docs/database.md``）：

- 枚举字段统一使用 ``VARCHAR(20)`` 存储枚举**值**（``native_enum=False``），
  不使用 MySQL 原生 ENUM，也不生成 CHECK 约束；
  取值合法性由 Pydantic（入参）与 SQLAlchemy（``validate_strings=True``）在应用层保证；
- ``values_callable`` 保证数据库中存的是 ``admin`` 而不是成员名 ``ADMIN``，
  与接口返回的取值保持一致；
- 同一套类型在 MySQL 与 SQLite（单元测试）下行为一致。

用法::

    from src.common.models import enum_column

    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, name="user_role"),
        nullable=False,
        default=UserRole.FAMILY,
        comment="角色：admin / staff / family",
    )
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import Enum as SAEnum

__all__ = ["enum_column"]


def enum_column(enum_cls: type[Enum], *, name: str, length: int = 20) -> SAEnum:
    """构造可移植的枚举列类型。

    :param enum_cls: 枚举类，需继承 ``str, Enum``
    :param name: 枚举类型名（用于 CHECK 约束命名与元数据标识）
    :param length: 底层 VARCHAR 长度，默认 20
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )

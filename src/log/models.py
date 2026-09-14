"""操作日志模型：``operation_log`` 表（审计用）。

设计要点：

- **用户名快照**：``username`` 冗余保存操作发生时的用户名，账号被删除后日志依然可读；
- ``user_id`` 可空并声明 ``ON DELETE SET NULL``：账号被物理删除后保留审计线索，
  只失去"关联到具体账号"的能力；
- ``module`` / ``action`` 为英文枚举式取值（如 ``event`` / ``create``），
  不做数据库级枚举约束，便于后续模块扩展埋点时无需改表；
- ``target_type`` + ``target_id`` 描述操作对象（如 ``event`` + ``12``，展示为 ``event/12``）；
- ``detail`` 为人类可读摘要（如「创建活动：村晚联欢」「状态：pending→active」）；
- ``ip`` 记录请求来源 IP（由 :mod:`src.log.middleware` 注入，长度 45 兼容 IPv6）。

索引：``INDEX(user_id)``、``INDEX(module, created_at)``、``INDEX(created_at)``——
分别支撑"按操作人查"、"按模块 + 时间范围查"与"按时间清理/翻页"三类审计查询。

日志**只增不改**：接口层不提供修改日志的入口，仅支持按时间批量清理
（:func:`src.log.service.clean_logs`）。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.user.models import User  # noqa: F401  注册 sys_user 模型（外键目标）

__all__ = ["OperationLog"]


class OperationLog(BaseModel):
    """操作日志表。"""

    __tablename__ = "operation_log"
    __table_args__ = (
        Index("ix_operation_log_user_id", "user_id"),
        Index("ix_operation_log_module_created", "module", "created_at"),
        Index("ix_operation_log_created_at", "created_at"),
        {"comment": "操作日志表（审计：记录管理员与工作人员的关键操作）"},
    )

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="操作人ID（sys_user.id）；账号被删除后置空，仅保留用户名快照",
    )
    username: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="操作人用户名（写入时快照，账号删除后仍可追溯）",
    )
    module: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="业务模块：event 活动 / checkin 签到 / leave 请假 等",
    )
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="操作类型：create / update / delete / change_status / face_checkin / "
        "manual_checkin / correct / approve / reject 等",
    )
    target_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="操作对象类型：event / checkin / member / leave 等",
    )
    target_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="操作对象ID",
    )
    detail: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="人类可读的操作摘要",
    )
    ip: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
        comment="请求来源IP（兼容 IPv6）",
    )

    @property
    def target(self) -> str | None:
        """操作对象的展示形式，如 ``event/12``；无对象时返回 ``None``。"""
        if self.target_type is None or self.target_id is None:
            return None
        return f"{self.target_type}/{self.target_id}"

    def __repr__(self) -> str:
        return (
            f"<OperationLog id={self.id} user={self.username!r} "
            f"module={self.module} action={self.action} target={self.target}>"
        )

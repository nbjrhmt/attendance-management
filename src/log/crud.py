"""操作日志数据访问层（CRUD）。

本层只负责数据库读写：不做权限判断（由接口层依赖保证），
也不主动提交事务（写操作按调用方要求 ``flush`` 或 ``commit``）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from src.common.utils import like_pattern
from src.log.models import OperationLog

__all__ = [
    "create_log",
    "delete_before",
    "get_log_by_id",
    "list_logs",
]


def create_log(
    db: Session,
    *,
    user_id: int | None,
    username: str,
    module: str,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
    ip: str | None = None,
    commit: bool = False,
) -> OperationLog:
    """写入一条操作日志。

    :param commit: 默认 ``False``——只 ``flush``，让日志随调用方的事务一起提交，
        从而与业务变更保持原子性（见 :func:`src.log.service.record_operation`）
    """
    log = OperationLog(
        user_id=user_id,
        username=username,
        module=module,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        ip=ip,
    )
    db.add(log)
    if commit:
        db.commit()
        db.refresh(log)
    else:
        db.flush()
    return log


def get_log_by_id(db: Session, log_id: int) -> OperationLog | None:
    """按主键查询日志。"""
    return db.get(OperationLog, log_id)


def list_logs(
    db: Session,
    *,
    user_id: int | None = None,
    module: str | None = None,
    action: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[OperationLog], int]:
    """分页查询操作日志（按时间倒序，最新的在前）。

    :param keyword: 模糊匹配操作人用户名 / 操作摘要（LIKE 通配符已转义）
    :return: ``(当前页日志列表, 总记录数)``
    """
    conditions = []
    if user_id is not None:
        conditions.append(OperationLog.user_id == user_id)
    if module:
        conditions.append(OperationLog.module == module)
    if action:
        conditions.append(OperationLog.action == action)
    if start_time is not None:
        conditions.append(OperationLog.created_at >= start_time)
    if end_time is not None:
        conditions.append(OperationLog.created_at <= end_time)
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                OperationLog.username.like(pattern, escape="\\"),
                OperationLog.detail.like(pattern, escape="\\"),
            )
        )

    total = (
        db.scalar(
            select(func.count()).select_from(OperationLog).where(*conditions)
        )
        or 0
    )
    items = list(
        db.scalars(
            select(OperationLog)
            .where(*conditions)
            # 审计查询按操作时间倒序；时间相同时按自增ID倒序，保证分页稳定
            .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, int(total)


def delete_before(db: Session, before: datetime) -> int:
    """物理删除指定时间之前（``created_at < before``）的日志。

    :return: 删除的日志条数
    """
    result = db.execute(
        delete(OperationLog).where(OperationLog.created_at < before)
    )
    db.commit()
    return int(result.rowcount or 0)

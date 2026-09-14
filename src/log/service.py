"""操作日志业务逻辑层。

职责：

- :func:`record_operation`：**埋点入口**，供 event / checkin / leave 的 service 调用。
  只向当前事务追加一行并 ``flush``，**不 commit**——日志与业务变更同一次提交完成，
  既保证"有业务变更就有日志"，也不会改变调用方的事务语义；
- :func:`list_operation_logs` / :func:`get_operation_log`：审计查询（分页、筛选、详情）；
- :func:`clean_logs`：按时间批量清理历史日志（物理删除，唯一会主动 commit 的写操作）。

埋点约定（模块与操作类型）：

| 模块 | action | 触发点 |
| --- | --- | --- |
| ``event`` | ``create`` / ``update`` / ``delete`` / ``change_status`` | :mod:`src.event.service` |
| ``checkin`` | ``face_checkin`` / ``manual_checkin`` / ``correct`` | :mod:`src.checkin.service` |
| ``leave`` | ``approve`` / ``reject`` | :mod:`src.leave.service` |

家庭用户的自助行为（提交请假、撤销请假、人脸录入等）不埋点，只审计管理侧操作。

错误码约定（与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 日志不存在 | 404 | 操作日志不存在：``{log_id}`` |
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.log import crud
from src.log.context import get_current_ip
from src.log.models import OperationLog
from src.log.schemas import OperationLogOut
from src.user.models import User

__all__ = [
    "clean_logs",
    "get_operation_log",
    "list_operation_logs",
    "record_operation",
]

#: 未登录/系统操作在日志中显示的用户名
SYSTEM_USERNAME = "system"


def _to_out(log: OperationLog) -> OperationLogOut:
    """ORM 日志对象 -> 响应 DTO（补上 ``target`` 展示字段）。"""
    return OperationLogOut(
        id=log.id,
        user_id=log.user_id,
        username=log.username,
        module=log.module,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        target=log.target,
        detail=log.detail,
        ip=log.ip,
        created_at=log.created_at,
    )


# ----------------------------------------------------------------------
# 埋点
# ----------------------------------------------------------------------
def record_operation(
    db: Session,
    user: User | None,
    *,
    module: str,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> OperationLog:
    """记录一次操作（埋点）。

    :param user: 操作人 ORM 对象；为 ``None`` 时记为系统操作
    :param module: 业务模块，如 ``event`` / ``checkin`` / ``leave``
    :param action: 操作类型，如 ``create`` / ``change_status`` / ``approve``
    :param detail: 人类可读摘要；超过 500 字符会被截断（列长度限制）
    :param ip: 来源 IP；不传时自动取当前请求的客户端 IP（见 :mod:`src.log.context`）
    :return: 已 ``flush`` 的日志 ORM 对象（未提交，随调用方事务提交）
    """
    return crud.create_log(
        db,
        user_id=user.id if user is not None else None,
        username=user.username if user is not None else SYSTEM_USERNAME,
        module=module,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail[:500] if detail else None,
        ip=ip or get_current_ip(),
    )


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_operation_logs(
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
) -> tuple[list[OperationLogOut], int]:
    """分页查询操作日志（按时间倒序）。"""
    logs, total = crud.list_logs(
        db,
        user_id=user_id,
        module=module,
        action=action,
        start_time=start_time,
        end_time=end_time,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return [_to_out(log) for log in logs], total


def get_operation_log(db: Session, log_id: int) -> OperationLogOut:
    """查询日志详情，不存在时抛出 404。"""
    log = crud.get_log_by_id(db, log_id)
    if log is None:
        raise BusinessError(
            f"操作日志不存在：{log_id}", code=ResponseCode.NOT_FOUND
        )
    return _to_out(log)


# ----------------------------------------------------------------------
# 清理
# ----------------------------------------------------------------------
def clean_logs(db: Session, before: datetime) -> int:
    """清理 ``before`` 之前的日志（物理删除）。

    :return: 删除的日志条数
    """
    return crud.delete_before(db, before)

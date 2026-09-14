"""操作日志接口路由（挂载前缀 ``/api/logs``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | ``/api/logs`` | 日志列表（分页、按操作人/模块/操作/时间/关键字筛选） | 管理员 |
| GET | ``/api/logs/{log_id}`` | 日志详情 | 管理员 |
| DELETE | ``/api/logs/clean`` | 清理指定日期之前的日志 | 管理员 |

说明：

- 操作日志属于**审计数据**，仅管理员可读（工作人员与家庭用户 403）；
- 日志由业务代码通过 :func:`src.log.service.record_operation` 自动写入
  （event / checkin / leave 关键操作），**没有"新增日志"接口**，避免审计数据被伪造；
- ``DELETE /clean`` 按 ``created_at < before`` 物理删除，``before`` 为必填日期，
  用于定期清理历史数据（如保留最近 180 天）。

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.log import service as log_service
from src.log.schemas import KEYWORD_MAX_LENGTH, OperationLogOut

router = APIRouter()


@router.get(
    "",
    response_model=ApiResponse[PageData[OperationLogOut]],
    summary="操作日志列表",
    description=(
        "分页查询操作日志（按时间倒序），支持按操作人、模块、操作类型、"
        "时间范围与关键字（用户名 / 操作摘要）筛选。仅管理员可访问。"
    ),
)
def list_logs(
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    user_id: Annotated[int | None, Query(ge=1, description="按操作人筛选")] = None,
    module: Annotated[
        str | None, Query(max_length=50, description="业务模块：event / checkin / leave")
    ] = None,
    action: Annotated[
        str | None, Query(max_length=50, description="操作类型：create / approve 等")
    ] = None,
    start_time: Annotated[
        datetime | None, Query(description="起始时间（含），格式 2026-03-01T00:00:00")
    ] = None,
    end_time: Annotated[
        datetime | None, Query(description="结束时间（含），格式 2026-03-31T23:59:59")
    ] = None,
    keyword: Annotated[
        str | None, Query(max_length=KEYWORD_MAX_LENGTH, description="用户名/操作摘要模糊搜索")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询操作日志。"""
    logs, total = log_service.list_operation_logs(
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
    return success_response(
        data=PageData[OperationLogOut](
            total=total, page=page, page_size=page_size, items=logs
        )
    )


@router.delete(
    "/clean",
    response_model=ApiResponse[dict],
    summary="清理历史日志",
    description=(
        "物理删除 ``created_at < before`` 的操作日志，``before`` 为必填日期"
        "（如 ``2026-01-01`` 表示清理该日期 00:00 之前的日志），返回删除条数。"
    ),
)
def clean_logs(
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    before: Annotated[date, Query(description="清理该日期之前的日志（不含该日）")],
) -> dict[str, Any]:
    """清理指定日期之前的历史日志。"""
    deleted = log_service.clean_logs(db, datetime.combine(before, time.min))
    return success_response(
        data={"deleted": deleted, "before": before.isoformat()},
        message=f"已清理 {deleted} 条历史日志",
    )


@router.get(
    "/{log_id}",
    response_model=ApiResponse[OperationLogOut],
    summary="操作日志详情",
    description="查询单条操作日志的完整信息（含操作对象与来源IP）。仅管理员可访问。",
)
def get_log(
    log_id: Annotated[int, Path(ge=1, description="日志ID")],
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询操作日志详情。"""
    return success_response(data=log_service.get_operation_log(db, log_id))

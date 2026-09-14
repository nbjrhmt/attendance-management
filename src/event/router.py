"""签到活动接口路由（挂载前缀 ``/api/events``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/events`` | 创建签到活动 | 管理员 |
| GET | ``/api/events`` | 活动列表（分页、状态筛选、关键字搜索） | 登录用户 |
| GET | ``/api/events/{event_id}`` | 活动详情 | 登录用户 |
| PUT | ``/api/events/{event_id}`` | 修改活动（未结束的活动可改） | 管理员 |
| DELETE | ``/api/events/{event_id}`` | 删除活动（仅未开始且无签到/请假记录） | 管理员 |
| PUT | ``/api/events/{event_id}/status`` | 状态迁移：开始 / 结束 / 取消 | 管理员 |

状态机与错误码约定见 :mod:`src.event.service`：
``pending → active → finished``、``pending → cancelled``，其余迁移返回 409；
活动结束时会在同一次提交内生成缺勤/请假记录（详见 :mod:`src.checkin.service`）。

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser, CurrentUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.event import service as event_service
from src.event.models import EventStatus
from src.event.schemas import EventCreate, EventOut, EventStatusUpdate, EventUpdate

router = APIRouter()

#: 状态迁移成功后的提示语
_STATUS_MESSAGES: dict[EventStatus, str] = {
    EventStatus.ACTIVE: "活动已开始",
    EventStatus.FINISHED: "活动已结束",
    EventStatus.CANCELLED: "活动已取消",
}


@router.post(
    "",
    response_model=ApiResponse[EventOut],
    summary="创建签到活动",
    description=(
        "管理员创建签到活动，创建后状态为「未开始」，需通过状态接口开始后才进入"
        "「进行中」。结束时间必须晚于开始时间，否则返回 400。"
    ),
)
def create_event(
    data: EventCreate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """创建签到活动。"""
    event = event_service.create_event(db, current_user, data)
    return success_response(data=event, message="活动创建成功")


@router.get(
    "",
    response_model=ApiResponse[PageData[EventOut]],
    summary="活动列表",
    description="分页查询签到活动，支持按状态筛选与关键字（活动名称/地点）搜索。",
)
def list_events(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[EventStatus | None, Query(description="状态筛选")] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="活动名称/地点模糊搜索")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询活动列表。"""
    events, total = event_service.list_events(
        db, status=status, keyword=keyword, page=page, page_size=page_size
    )
    return success_response(
        data=PageData[EventOut](
            total=total, page=page, page_size=page_size, items=events
        )
    )


@router.get(
    "/{event_id}",
    response_model=ApiResponse[EventOut],
    summary="活动详情",
    description="查询指定签到活动的详细信息（含状态与迟到阈值）。",
)
def get_event(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询活动详情。"""
    return success_response(data=event_service.get_event(db, event_id))


@router.put(
    "/{event_id}",
    response_model=ApiResponse[EventOut],
    summary="修改签到活动",
    description=(
        "管理员修改活动信息（未提交的字段保持原值）。"
        "已结束或已取消的活动不可修改（409）；修改后结束时间仍须晚于开始时间。"
    ),
)
def update_event(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    data: EventUpdate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修改签到活动。"""
    event = event_service.update_event(db, current_user, event_id, data)
    return success_response(data=event, message="更新成功")


@router.delete(
    "/{event_id}",
    response_model=ApiResponse[dict],
    summary="删除签到活动",
    description=(
        "仅「未开始」且没有任何签到/请假记录的活动可以删除；"
        "其余情况返回 409，避免历史签到数据悬空。"
    ),
)
def delete_event(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """删除签到活动。"""
    event_service.delete_event(db, current_user, event_id)
    return success_response(message="删除成功")


@router.put(
    "/{event_id}/status",
    response_model=ApiResponse[EventOut],
    summary="活动状态迁移",
    description=(
        "状态机：pending → active（开始）、active → finished（结束）、"
        "pending → cancelled（取消），非法迁移返回 409。"
        "活动结束时自动为「应签到但无记录」的成员生成缺勤/请假记录"
        "（已通过请假者记 leave，其余记 absent），返回消息中给出生成本数。"
    ),
)
def transition_event_status(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    data: EventStatusUpdate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """迁移活动状态。"""
    event, generated = event_service.transition_status(db, current_user, event_id, data)
    message = _STATUS_MESSAGES[event.status]
    if generated:
        message = f"{message}，自动生成 {generated} 条缺勤/请假记录"
    return success_response(data=event, message=message)

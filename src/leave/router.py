"""请假管理接口路由（挂载前缀 ``/api/leaves``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/leaves`` | 提交请假申请 | 户主（本户成员）/ 管理员（任意成员） |
| GET | ``/api/leaves`` | 请假列表（分页、状态/活动/家庭筛选） | 管理员 / 工作人员（全部）、户主（本户） |
| GET | ``/api/leaves/{leave_id}`` | 请假详情 | 户主（本户）/ 管理员 / 工作人员 |
| PUT | ``/api/leaves/{leave_id}/approve`` | 审批（通过 / 驳回） | 管理员 |
| PUT | ``/api/leaves/{leave_id}/cancel`` | 撤销申请 | 户主本人 / 管理员 |

说明：

- 同一成员同一活动同时只能有一条「待审批/已通过」的申请，重复提交返回 409；
- 已通过请假的成员若实际到场签到，仍以实际签到记录为准；
  活动结束时仍未签到的已通过请假成员记为 ``leave``（详见 :mod:`src.checkin.service`）；
- 已结束 / 已取消的活动不接受新的请假申请。

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
from src.leave import service as leave_service
from src.leave.models import LeaveStatus
from src.leave.schemas import LeaveCreate, LeaveOut, LeaveReviewRequest

router = APIRouter()


@router.post(
    "",
    response_model=ApiResponse[LeaveOut],
    summary="提交请假申请",
    description=(
        "户主为本家庭成员提交请假，或由管理员代任意成员提交。"
        "同一成员同一活动已存在「待审批/已通过」的申请时返回 409；"
        "已结束或已取消的活动不接受申请。"
    ),
)
def create_leave(
    data: LeaveCreate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """提交请假申请。"""
    leave = leave_service.create_leave(db, current_user, data)
    return success_response(data=leave, message="请假申请已提交，等待审批")


@router.get(
    "",
    response_model=ApiResponse[PageData[LeaveOut]],
    summary="请假列表",
    description=(
        "分页查询请假申请，支持按状态、活动与家庭筛选；"
        "管理员/工作人员可查看全部，家庭用户仅能查看本户（跨户返回 403）。"
    ),
)
def list_leaves(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[LeaveStatus | None, Query(description="状态筛选")] = None,
    event_id: Annotated[int | None, Query(ge=1, description="按活动筛选")] = None,
    family_id: Annotated[
        int | None, Query(ge=1, description="按家庭筛选（管理员/工作人员可用）")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询请假申请。"""
    leaves, total = leave_service.list_leaves(
        db,
        current_user,
        status=status,
        event_id=event_id,
        family_id=family_id,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data=PageData[LeaveOut](
            total=total, page=page, page_size=page_size, items=leaves
        )
    )


@router.get(
    "/{leave_id}",
    response_model=ApiResponse[LeaveOut],
    summary="请假详情",
    description="查询指定请假申请：本人所属家庭、管理员与工作人员可访问。",
)
def get_leave(
    leave_id: Annotated[int, Path(ge=1, description="请假申请ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询请假详情。"""
    return success_response(data=leave_service.get_leave(db, current_user, leave_id))


@router.put(
    "/{leave_id}/approve",
    response_model=ApiResponse[LeaveOut],
    summary="审批请假",
    description=(
        "管理员审批请假：``status`` 为 ``approved``（通过）或 ``rejected``（驳回），"
        "可选 ``remark`` 审批说明。仅「待审批」的申请可审批，重复审批返回 409。"
    ),
)
def review_leave(
    leave_id: Annotated[int, Path(ge=1, description="请假申请ID")],
    data: LeaveReviewRequest,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """审批请假申请。"""
    leave = leave_service.review_leave(db, current_user, leave_id, data)
    return success_response(data=leave, message=f"审批完成：{leave.status.label}")


@router.put(
    "/{leave_id}/cancel",
    response_model=ApiResponse[LeaveOut],
    summary="撤销请假",
    description=(
        "户主本人（本户成员）或管理员撤销请假申请，仅「待审批」的申请可撤销，"
        "其余状态返回 409。"
    ),
)
def cancel_leave(
    leave_id: Annotated[int, Path(ge=1, description="请假申请ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """撤销请假申请。"""
    leave = leave_service.cancel_leave(db, current_user, leave_id)
    return success_response(data=leave, message="请假申请已撤销")

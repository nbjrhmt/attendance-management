"""家庭成员管理接口路由（挂载前缀 ``/api/members``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/members`` | 添加家庭成员 | 户主本人 / 管理员（需指定 family_id） |
| GET | ``/api/members`` | 成员列表（分页、状态/是否需签到筛选、关键字搜索） | 户主本人 / 管理员 / 工作人员 |
| GET | ``/api/members/{member_id}`` | 成员详情 | 户主本人 / 管理员 / 工作人员 |
| PUT | ``/api/members/{member_id}`` | 修改成员信息 | 户主本人 / 管理员 |
| PUT | ``/api/members/{member_id}/status`` | 启用/停用成员 | 户主本人 / 管理员 |
| DELETE | ``/api/members/{member_id}`` | 删除成员（阶段五起语义为**停用**，保留签到历史） | 户主本人 / 管理员 |

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from src.auth.dependencies import CurrentUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.member import service as member_service
from src.member.models import MemberStatus
from src.member.schemas import (
    MemberCreate,
    MemberOut,
    MemberStatusUpdate,
    MemberUpdate,
)

router = APIRouter()


@router.post(
    "",
    response_model=ApiResponse[MemberOut],
    summary="添加家庭成员",
    description=(
        "户主为本人家庭添加成员（无需传 family_id）；管理员添加时必须在请求体中指定 family_id。"
        "填写身份证号时，性别与出生日期可留空，系统会自动识别并校验身份证号校验位。"
    ),
)
def create_member(
    data: MemberCreate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """添加家庭成员。"""
    member = member_service.create_member(db, current_user, data)
    return success_response(data=member, message="成员添加成功")


@router.get(
    "",
    response_model=ApiResponse[PageData[MemberOut]],
    summary="成员列表",
    description=(
        "户主查看本户成员；管理员与工作人员可查看全部，并用 family_id 筛选指定家庭。"
    ),
)
def list_members(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    family_id: Annotated[int | None, Query(ge=1, description="按家庭筛选（管理员/工作人员可用）")] = None,
    status: Annotated[MemberStatus | None, Query(description="状态筛选")] = None,
    needs_checkin: Annotated[bool | None, Query(description="是否只查需要签到的成员")] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="姓名/身份证号/电话模糊搜索")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询成员列表。"""
    members, total = member_service.list_members(
        db,
        current_user,
        family_id=family_id,
        status=status,
        needs_checkin=needs_checkin,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data=PageData[MemberOut](
            total=total, page=page, page_size=page_size, items=members
        )
    )


@router.get(
    "/{member_id}",
    response_model=ApiResponse[MemberOut],
    summary="成员详情",
    description="户主本人、管理员、工作人员可访问。",
)
def get_member(
    member_id: Annotated[int, Path(ge=1, description="成员ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询成员详情。"""
    return success_response(data=member_service.get_member(db, current_user, member_id))


@router.put(
    "/{member_id}",
    response_model=ApiResponse[MemberOut],
    summary="修改成员信息",
    description="户主本人或管理员可修改；未提交的字段保持原值，传 null 可清空身份证号/电话/备注。",
)
def update_member(
    member_id: Annotated[int, Path(ge=1, description="成员ID")],
    data: MemberUpdate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修改成员信息。"""
    member = member_service.update_member(db, current_user, member_id, data)
    return success_response(data=member, message="更新成功")


@router.put(
    "/{member_id}/status",
    response_model=ApiResponse[MemberOut],
    summary="启用/停用成员",
    description="停用后该成员不参与签到统计，但历史数据保留；户主本人或管理员可操作。",
)
def update_member_status(
    member_id: Annotated[int, Path(ge=1, description="成员ID")],
    data: MemberStatusUpdate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """启用/停用成员。"""
    member = member_service.update_status(db, current_user, member_id, data)
    message = "成员已启用" if data.status == MemberStatus.ACTIVE else "成员已停用"
    return success_response(data=member, message=message)


@router.delete(
    "/{member_id}",
    response_model=ApiResponse[dict],
    summary="删除成员（停用）",
    description=(
        "户主本人或管理员删除成员。阶段五引入签到记录后，删除语义改为**停用**："
        "成员状态置为 ``inactive`` 并保留其签到/请假历史与人脸记录，"
        "接口仍返回 200「删除成功」，成员仍可在列表/详情中查到。"
        "重复删除已停用的成员返回 409。"
    ),
)
def delete_member(
    member_id: Annotated[int, Path(ge=1, description="成员ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """删除成员。"""
    member_service.delete_member(db, current_user, member_id)
    return success_response(message="删除成功")

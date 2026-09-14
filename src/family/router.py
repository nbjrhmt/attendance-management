"""家庭管理接口路由（挂载前缀 ``/api/families``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/families`` | 为指定户主创建家庭档案 | 管理员 |
| GET | ``/api/families`` | 家庭列表（分页、状态/村组筛选、关键字搜索） | 管理员 / 工作人员 |
| GET | ``/api/families/me`` | 本人家庭详情（含成员列表） | 家庭用户（户主） |
| GET | ``/api/families/{family_id}`` | 家庭详情（含成员列表） | 户主本人 / 管理员 / 工作人员 |
| PUT | ``/api/families/{family_id}`` | 修改家庭信息 | 户主本人 / 管理员 |
| DELETE | ``/api/families/{family_id}`` | 停用家庭档案（级联停用成员） | 管理员 |

> 户主通过 ``POST /api/auth/register`` 注册时会**自动创建家庭档案与户主成员行**，
> 无需调用建档接口；建档接口用于管理员补录既有家庭（如迁移历史数据）。

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser, CurrentUser, StaffOrAdminUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.family import service as family_service
from src.family.models import FamilyStatus
from src.family.schemas import (
    FamilyCreate,
    FamilyDetailOut,
    FamilyOut,
    FamilyUpdate,
)

router = APIRouter()


@router.post(
    "",
    response_model=ApiResponse[FamilyOut],
    summary="创建家庭档案",
    description=(
        "管理员为指定户主（role=family 的用户）创建家庭档案，并自动生成户主成员记录。"
        "户号可留空，系统会生成 F+6 位序号。"
    ),
)
def create_family(
    data: FamilyCreate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """创建家庭档案。"""
    family = family_service.create_family(db, data)
    return success_response(
        data=family, message=f"家庭档案创建成功，户号 {family.household_no}"
    )


@router.get(
    "",
    response_model=ApiResponse[PageData[FamilyOut]],
    summary="家庭列表",
    description="分页查询家庭档案，支持按状态、村组筛选与关键字（户号/住址/户主姓名）搜索。",
)
def list_families(
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
    status: Annotated[FamilyStatus | None, Query(description="状态筛选")] = None,
    village: Annotated[str | None, Query(max_length=100, description="按村组精确筛选")] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="户号/住址/户主姓名模糊搜索")
    ] = None,
) -> dict[str, Any]:
    """分页查询家庭列表。"""
    families, total = family_service.list_families(
        db,
        page=page,
        page_size=page_size,
        status=status,
        village=village,
        keyword=keyword,
    )
    return success_response(
        data=PageData[FamilyOut](
            total=total, page=page, page_size=page_size, items=families
        )
    )


@router.get(
    "/me",
    response_model=ApiResponse[FamilyDetailOut],
    summary="我的家庭",
    description="户主查看本人家庭档案与全部成员（含已停用成员）。",
)
def get_my_family(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询本人家庭详情。"""
    return success_response(data=family_service.get_my_family(db, current_user))


@router.get(
    "/{family_id}",
    response_model=ApiResponse[FamilyDetailOut],
    summary="家庭详情",
    description="查询指定家庭档案与成员列表：户主本人、管理员、工作人员可访问。",
)
def get_family_detail(
    family_id: Annotated[int, Path(ge=1, description="家庭ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询家庭详情。"""
    return success_response(
        data=family_service.get_family_detail(db, current_user, family_id)
    )


@router.put(
    "/{family_id}",
    response_model=ApiResponse[FamilyOut],
    summary="修改家庭信息",
    description="户主本人或管理员可修改户号、住址、村组、联系电话与备注；工作人员无写权限。",
)
def update_family(
    family_id: Annotated[int, Path(ge=1, description="家庭ID")],
    data: FamilyUpdate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修改家庭信息。"""
    family = family_service.update_family(db, current_user, family_id, data)
    return success_response(data=family, message="更新成功")


@router.delete(
    "/{family_id}",
    response_model=ApiResponse[dict],
    summary="停用家庭档案",
    description=(
        "管理员停用家庭档案，并级联停用该家庭的全部正常成员。"
        "数据不会被物理删除，以便保留历史签到记录。"
    ),
)
def deactivate_family(
    family_id: Annotated[int, Path(ge=1, description="家庭ID")],
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """停用家庭档案。"""
    member_count = family_service.deactivate_family(db, current_user, family_id)
    return success_response(
        data={"deactivated_members": member_count},
        message=f"家庭档案已停用，同时停用 {member_count} 名成员",
    )

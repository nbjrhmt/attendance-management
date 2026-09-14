"""统计报表接口路由（挂载前缀 ``/api/statistics``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | ``/api/statistics/overview`` | 全村总览（家庭/成员/活动/签到/平均出勤率） | 管理员 / 工作人员 |
| GET | ``/api/statistics/events/{event_id}`` | 单活动统计（活动 + 汇总 + 家庭维度分页） | 管理员 / 工作人员 |
| GET | ``/api/statistics/families`` | 家庭参与度排行（分页、筛选、排序） | 管理员 / 工作人员 |
| GET | ``/api/statistics/trend`` | 按活动时间的签到趋势（分页） | 管理员 / 工作人员 |
| GET | ``/api/statistics/export`` | 导出 CSV（活动 / 家庭） | 管理员 / 工作人员 |

说明：

- 统计接口**纯读**：不建表、不写库，全部由聚合查询实时计算；
- 报表含全村数据，因此**仅管理员与工作人员**可访问，家庭用户返回 403；
- 出勤率口径与阶段五 ``summary`` 一致，详见 :mod:`src.statistics.service`；
- ``GET /api/statistics/export`` 返回 CSV 文件流（**不使用统一响应格式**，与图片类接口同属例外），
  UTF-8 BOM + ``Content-Disposition: attachment; filename*=UTF-8''<URL 编码中文名>``，
  Excel 直接打开不乱码。

所有非文件类响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.auth.dependencies import StaffOrAdminUser
from src.common.database import get_db
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode, success_response
from src.common.schemas import ApiResponse, PageData
from src.statistics import service as statistics_service
from src.statistics.schemas import (
    EventStatisticsOut,
    FamilyRankingOut,
    StatisticsOverviewOut,
    TrendPointOut,
)
from src.statistics.service import OrderByField, OrderDirection

router = APIRouter()

#: 报表文件名（中文名按 RFC 5987 编码后写入 Content-Disposition）
EVENTS_FILENAME = "活动签到统计.csv"
FAMILIES_FILENAME = "家庭参与度统计.csv"


@router.get(
    "/overview",
    response_model=ApiResponse[StatisticsOverviewOut],
    summary="总览统计",
    description=(
        "全村总览：正常家庭数、正常成员数、活动总数（含进行中/已结束）、"
        "实际签到次数（signed + late）与已结束活动的平均出勤率"
        "（算术平均，2 位小数；无已结束活动时为 null）。"
    ),
)
def get_overview(
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询总览统计。"""
    return success_response(data=statistics_service.build_overview(db))


@router.get(
    "/events/{event_id}",
    response_model=ApiResponse[EventStatisticsOut],
    summary="单活动统计",
    description=(
        "指定活动的统计：活动信息 + 全村口径汇总（应签到、各状态计数、出勤率）"
        "+ 家庭维度分页列表（该户应签到人数与各状态计数，按应签到人数降序）。"
        "活动不存在返回 404。"
    ),
)
def get_event_statistics(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询单活动统计。"""
    return success_response(
        data=statistics_service.get_event_statistics(
            db, event_id, page=page, page_size=page_size
        )
    )


@router.get(
    "/families",
    response_model=ApiResponse[PageData[FamilyRankingOut]],
    summary="家庭参与度排行",
    description=(
        "家庭参与度排行（**仅参与过已结束活动的家庭入榜**）：在册成员数、应签到人数、"
        "参与活动场次、签到次数与出勤率；支持村组精确筛选、户号/户主姓名模糊搜索，"
        "以及按出勤率/签到次数/在册成员数排序（默认出勤率降序）。"
    ),
)
def list_family_rankings(
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    village: Annotated[
        str | None, Query(max_length=100, description="按村组精确筛选")
    ] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="户主姓名/户号模糊搜索")
    ] = None,
    order_by: Annotated[
        OrderByField, Query(description="排序字段：rate 出勤率 / checkins 签到次数 / members 在册成员数")
    ] = "rate",
    order: Annotated[
        OrderDirection, Query(description="排序方向：desc 降序 / asc 升序")
    ] = "desc",
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询家庭参与度排行。"""
    families, total = statistics_service.list_family_rankings(
        db,
        village=village,
        keyword=keyword,
        order_by=order_by,
        order=order,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data=PageData[FamilyRankingOut](
            total=total, page=page, page_size=page_size, items=families
        )
    )


@router.get(
    "/trend",
    response_model=ApiResponse[PageData[TrendPointOut]],
    summary="签到趋势",
    description=(
        "按活动开始时间的签到趋势（升序，分页）：每行一个活动的应签到人数、"
        "各状态计数与出勤率。日期范围按 ``event.start_time`` 过滤，"
        "``start_date`` 缺省为最近 30 天，``end_date`` 缺省为今天。"
    ),
)
def list_trend(
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    start_date: Annotated[
        date | None, Query(description="起始日期（含），格式 2026-03-01；缺省为最近 30 天")
    ] = None,
    end_date: Annotated[
        date | None, Query(description="结束日期（含），格式 2026-03-31；缺省为今天")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询签到趋势。"""
    points, total = statistics_service.list_trend(
        db, start_date=start_date, end_date=end_date, page=page, page_size=page_size
    )
    return success_response(
        data=PageData[TrendPointOut](
            total=total, page=page, page_size=page_size, items=points
        )
    )


@router.get(
    "/export",
    summary="导出统计报表（CSV）",
    description=(
        "导出 CSV 报表：``type=events`` 导出活动签到统计（可选 ``event_id``、"
        "``start_date``/``end_date``）；``type=families`` 导出家庭参与度统计"
        "（支持 ``village``/``keyword``/``order_by``/``order``）。"
        "文件为 UTF-8 BOM 编码，Excel 直接打开不乱码；返回文件流，不使用统一响应格式。"
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/csv": {}},
            "description": "CSV 文件流（UTF-8 BOM）",
        }
    },
)
def export_statistics(
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    export_type: Annotated[
        str, Query(alias="type", description="导出类型：events 活动统计 / families 家庭统计")
    ],
    event_id: Annotated[
        int | None, Query(ge=1, description="活动ID（仅 type=events，缺省导出全部活动）")
    ] = None,
    start_date: Annotated[
        date | None, Query(description="起始日期（仅 type=events，按活动开始时间过滤）")
    ] = None,
    end_date: Annotated[
        date | None, Query(description="结束日期（仅 type=events，按活动开始时间过滤）")
    ] = None,
    village: Annotated[
        str | None, Query(max_length=100, description="村组精确筛选（仅 type=families）")
    ] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="户主姓名/户号模糊搜索（仅 type=families）")
    ] = None,
    order_by: Annotated[
        OrderByField, Query(description="排序字段（仅 type=families）")
    ] = "rate",
    order: Annotated[
        OrderDirection, Query(description="排序方向（仅 type=families）")
    ] = "desc",
) -> StreamingResponse:
    """导出统计报表 CSV。"""
    if export_type == "events":
        content = statistics_service.build_events_csv(
            db, event_id=event_id, start_date=start_date, end_date=end_date
        )
        filename = EVENTS_FILENAME
    elif export_type == "families":
        content = statistics_service.build_families_csv(
            db, village=village, keyword=keyword, order_by=order_by, order=order
        )
        filename = FAMILIES_FILENAME
    else:
        # type 只允许 events / families（其余取值由参数校验拦截，这里兜底）
        raise BusinessError(
            "导出类型只能是 events（活动统计）或 families（家庭统计）",
            code=ResponseCode.PARAM_ERROR,
        )

    # 中文文件名按 RFC 5987 编码，浏览器 / Excel 均能正确识别
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )

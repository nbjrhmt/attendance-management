"""统计报表模块的响应数据结构（Pydantic 模型）。

统计接口**纯读**：没有任何请求体模型（查询参数直接写在路由签名上），
只有分层的响应模型：

- :class:`StatisticsOverviewOut`：总览计数；
- :class:`EventStatisticsOut`：单活动统计（活动信息 + 总汇总 + 家庭维度列表）；
- :class:`FamilyRankingOut`：家庭参与度排行行；
- :class:`TrendPointOut`：按活动时间的签到趋势行。

出勤率口径统一为 ``(signed + late) / 应签到数``，四舍五入 4 位小数（总览为 2 位）；
分母为 0 时按字段说明返回 ``None`` 或 ``0.0``（见各字段描述）。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from src.checkin.schemas import CheckinSummaryOut
from src.common.schemas import PageData
from src.event.models import EventStatus
from src.event.schemas import EventOut

__all__ = [
    "EventFamilyStatOut",
    "EventStatisticsOut",
    "FamilyRankingOut",
    "StatisticsOverviewOut",
    "TrendPointOut",
]


class StatisticsOverviewOut(BaseModel):
    """全村总览统计。"""

    total_families: int = Field(description="正常状态的家庭数（status=active）")
    total_members: int = Field(description="正常状态的成员数（status=active）")
    total_events: int = Field(description="签到活动总数（含各种状态）")
    active_events: int = Field(description="进行中的活动数（status=active）")
    finished_events: int = Field(description="已结束的活动数（status=finished）")
    total_checkins: int = Field(description="全部活动中的实际签到次数（signed + late）")
    avg_attendance_rate: float | None = Field(
        default=None,
        description="已结束活动的平均出勤率（算术平均，2 位小数）；无已结束活动时为 null",
    )


class EventFamilyStatOut(BaseModel):
    """单个活动中某个家庭的签到情况。"""

    family_id: int = Field(description="家庭ID")
    household_no: str | None = Field(default=None, description="户号")
    owner_name: str = Field(description="户主姓名")
    village: str | None = Field(default=None, description="所属村/组")
    expected: int = Field(description="该户在本次活动的应签到人数（家庭正常 + 成员正常 + 需要签到）")
    signed: int = Field(default=0, description="已签到人数")
    late: int = Field(default=0, description="迟到人数")
    absent: int = Field(default=0, description="缺勤人数")
    leave: int = Field(default=0, description="请假人数")
    rate: float | None = Field(
        default=None,
        description="该户出勤率 (signed+late)/expected，4 位小数；expected=0 时为 null",
    )


class EventStatisticsOut(BaseModel):
    """单活动统计：活动信息 + 总汇总 + 家庭维度列表（分页）。"""

    event: EventOut = Field(description="活动信息")
    summary: CheckinSummaryOut = Field(description="全村口径的活动签到汇总（口径同阶段五）")
    families: PageData[EventFamilyStatOut] = Field(description="按家庭的分页统计（按应签到人数降序）")


class FamilyRankingOut(BaseModel):
    """家庭参与度排行行。"""

    family_id: int = Field(description="家庭ID")
    household_no: str | None = Field(default=None, description="户号")
    owner_name: str = Field(description="户主姓名")
    village: str | None = Field(default=None, description="所属村/组")
    active_member_count: int = Field(description="在册成员数（成员 status=active）")
    checkin_required_count: int = Field(description="其中需要签到的成员数")
    event_participated_count: int = Field(
        description="参与过的已结束活动场次数（产生过签到记录的场次）"
    )
    total_checkins: int = Field(description="全部活动中的实际签到次数（signed + late）")
    attendance_rate: float | None = Field(
        default=None,
        description="已结束活动的出勤率 (signed+late)/应签到总数，4 位小数；分母为 0 时为 null",
    )


class TrendPointOut(BaseModel):
    """按活动时间的签到趋势行（每行一个活动）。"""

    event_id: int = Field(description="活动ID")
    name: str = Field(description="活动名称")
    start_date: date = Field(description="活动开始日期")
    status: EventStatus = Field(description="活动状态：pending / active / finished / cancelled")
    total_expected: int = Field(description="应签到人数")
    signed: int = Field(default=0, description="已签到人数")
    late: int = Field(default=0, description="迟到人数")
    absent: int = Field(default=0, description="缺勤人数")
    leave: int = Field(default=0, description="请假人数")
    abnormal: int = Field(default=0, description="异常人数")
    attendance_rate: float = Field(
        description="出勤率 (signed+late)/total_expected，4 位小数；应签到人数为 0 时为 0.0"
    )

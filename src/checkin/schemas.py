"""签到业务模块的请求/响应数据结构（Pydantic 模型）。

包含签到记录、活动签到汇总（summary）与"活动签到明细"三个层次：

- :class:`CheckinRecordOut`：单条签到记录（含成员姓名、活动名称，便于前端直接展示）；
- :class:`CheckinSummaryOut`：某活动的签到汇总统计（应签到人数、各状态计数与出勤率）；
- :class:`EventCheckinDetailOut`：活动信息 + 汇总 + 分页明细。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.checkin.models import CheckinMethod, CheckinStatus
from src.common.schemas import PageData
from src.event.schemas import EventOut

__all__ = [
    "CheckinManualRequest",
    "CheckinRecordOut",
    "CheckinSummaryOut",
    "CheckinUpdate",
    "EventCheckinDetailOut",
]

#: 备注最大长度
REMARK_MAX_LENGTH = 255


class CheckinManualRequest(BaseModel):
    """手动签到请求（管理员 / 工作人员代成员签到）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, description="签到活动ID")
    member_id: int = Field(ge=1, description="家庭成员ID")
    remark: str | None = Field(default=None, max_length=REMARK_MAX_LENGTH, description="备注")


class CheckinUpdate(BaseModel):
    """修正签到记录请求（仅管理员）。

    除状态与备注外，服务层会同步维护审计字段 ``reviewed_by_id`` / ``reviewed_at``，
    并在状态与签到方式冲突时做一致性修补（详见 :mod:`src.checkin.service`）。
    """

    model_config = ConfigDict(extra="forbid")

    status: CheckinStatus = Field(
        description="修正后的状态：signed / late / absent / leave / abnormal"
    )
    remark: str | None = Field(default=None, max_length=REMARK_MAX_LENGTH, description="备注")
    review_remark: str | None = Field(
        default=None, max_length=REMARK_MAX_LENGTH, description="修正说明（审计用）"
    )


class CheckinRecordOut(BaseModel):
    """签到记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="签到记录ID")
    event_id: int = Field(description="签到活动ID")
    event_name: str = Field(description="活动名称")
    family_id: int = Field(description="家庭ID")
    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    method: CheckinMethod | None = Field(
        default=None, description="签到方式：face / manual；系统生成的缺勤、请假为 null"
    )
    status: CheckinStatus = Field(description="签到状态")
    checked_at: datetime | None = Field(
        default=None, description="签到时间；缺勤/请假为 null"
    )
    face_score: float | None = Field(default=None, description="人脸签到得分")
    reviewed_by_id: int | None = Field(default=None, description="最近一次修正人ID")
    reviewed_at: datetime | None = Field(default=None, description="最近一次修正时间")
    review_remark: str | None = Field(default=None, description="修正说明")
    remark: str | None = Field(default=None, description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")


class CheckinSummaryOut(BaseModel):
    """活动签到汇总统计。

    - ``total_expected``：应签到人数，即"家庭正常 + 成员正常 + 需要签到"的成员总数；
      活动结束时会为其中没有签到记录的成员生成缺勤/请假记录，因此活动结束后
      ``total_expected`` 与签到记录总数一致；
    - ``attendance_rate``：出勤率 ``(signed + late) / total_expected``，取值 0~1，
      保留 4 位小数；应签到人数为 0 时返回 ``0.0``。
    """

    total_expected: int = Field(description="应签到人数")
    signed: int = Field(default=0, description="已签到人数")
    late: int = Field(default=0, description="迟到人数")
    absent: int = Field(default=0, description="缺勤人数")
    leave: int = Field(default=0, description="请假人数")
    abnormal: int = Field(default=0, description="异常人数")
    attendance_rate: float = Field(description="出勤率 (signed+late)/total_expected，0~1")


class EventCheckinDetailOut(BaseModel):
    """活动签到明细响应：活动信息 + 汇总统计 + 分页签到记录。"""

    event: EventOut = Field(description="活动信息")
    summary: CheckinSummaryOut = Field(description="签到汇总统计")
    checkins: PageData[CheckinRecordOut] = Field(description="分页签到记录")

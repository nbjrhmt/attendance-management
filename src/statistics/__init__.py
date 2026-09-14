"""统计报表模块：活动签到率、家庭参与情况、迟到/缺勤/请假等多维统计。

本模块**不建表、不写库**，所有指标由 :mod:`src.statistics.crud` 的聚合查询实时计算：

- :mod:`src.statistics.schemas`：报表响应模型（总览 / 单活动 / 家庭排行 / 趋势）
- :mod:`src.statistics.crud`：聚合查询（计数、状态分布、应签到人数、报表行）
- :mod:`src.statistics.service`：口径与编排（出勤率计算、CSV 导出文本）
- :mod:`src.statistics.router`：接口层

已实现接口（统一以 ``/api/statistics`` 开头，**仅管理员与工作人员**可访问）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | ``/api/statistics/overview`` | 总览（家庭数、成员数、活动数、签到次数、平均出勤率） |
| GET | ``/api/statistics/events/{event_id}`` | 单个活动的签到统计（汇总 + 家庭维度分页） |
| GET | ``/api/statistics/families`` | 家庭参与度排行（分页、筛选、三种排序） |
| GET | ``/api/statistics/trend`` | 按活动时间的签到趋势（默认最近 30 天，分页） |
| GET | ``/api/statistics/export`` | 导出 CSV（``type=events|families``，UTF-8 BOM） |

口径要点：出勤率 = ``(signed + late) / 应签到人数``；
活动维度复用阶段五 :func:`src.checkin.service.build_summary` 的"当前应签到人数"口径，
家庭排行维度以"该户在已结束活动中的签到记录总数"作为历史分母；
分母为 0 时按字段说明返回 ``None``（趋势行返回 ``0.0``）。
"""

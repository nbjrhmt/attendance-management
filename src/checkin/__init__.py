"""签到业务模块：处理人脸识别签到与手动签到，维护签到记录与统计。

签到状态取值：``signed``（已签到）、``late``（迟到）、``absent``（缺勤）、
``leave``（请假）、``abnormal``（异常）。

分层结构：

- :mod:`src.checkin.models`：``checkin_record`` 表与签到方式/状态枚举
- :mod:`src.checkin.schemas`：签到记录、汇总统计与"活动签到明细"模型
- :mod:`src.checkin.crud`：数据访问层（含应签到成员查询、状态分组统计）
- :mod:`src.checkin.service`：签到窗口、迟到判定、幂等、缺勤生成与统计
- :mod:`src.checkin.router`：接口层

已实现接口（统一以 ``/api/checkins`` 开头）：

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/checkins/face`` | 人脸识别签到（multipart：event_id + file） | 管理员 / 工作人员 |
| POST | ``/api/checkins/manual`` | 手动签到（event_id + member_id） | 管理员 / 工作人员 |
| GET | ``/api/checkins`` | 签到记录列表（按活动/家庭/状态筛选） | 登录用户（家庭用户仅本户） |
| GET | ``/api/checkins/events/{event_id}`` | 某活动的签到明细 + 汇总统计 | 登录用户（家庭用户仅本户） |
| PUT | ``/api/checkins/{checkin_id}`` | 修正签到记录 | 管理员 |

关键规则：签到需在活动时间窗内；超过 ``late_threshold_minutes`` 记为迟到；
一成员一活动一条记录（重复签到 409）；活动结束时为应签到未签到成员批量生成
缺勤（``absent``）或请假（``leave``）记录。
"""

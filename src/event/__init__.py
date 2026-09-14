"""签到活动模块：管理员创建签到活动，指定签到时间范围与迟到阈值。

分层结构：

- :mod:`src.event.models`：``event`` 表与活动状态枚举（pending/active/finished/cancelled）
- :mod:`src.event.schemas`：请求/响应模型
- :mod:`src.event.crud`：数据访问层
- :mod:`src.event.service`：业务逻辑与状态机（结束时联动生成缺勤/请假记录）
- :mod:`src.event.router`：接口层

已实现接口（统一以 ``/api/events`` 开头）：

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/events`` | 创建签到活动 | 管理员 |
| GET | ``/api/events`` | 活动列表（分页、状态筛选、关键字搜索） | 登录用户 |
| GET | ``/api/events/{event_id}`` | 活动详情 | 登录用户 |
| PUT | ``/api/events/{event_id}`` | 修改活动（已结束/已取消返回 409） | 管理员 |
| DELETE | ``/api/events/{event_id}`` | 删除活动（仅未开始且无签到/请假记录） | 管理员 |
| PUT | ``/api/events/{event_id}/status`` | 状态迁移：开始 / 结束 / 取消 | 管理员 |

状态机：``pending`` → ``active`` → ``finished``，或 ``pending`` → ``cancelled``；
``active → finished`` 时会在同一事务内为应签到未签到成员生成缺勤/请假记录
（详见 :mod:`src.checkin.service`）。
"""

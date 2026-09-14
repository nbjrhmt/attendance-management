"""操作日志模块：记录管理员与工作人员的关键操作，用于审计与问题追踪。

分层结构：

- :mod:`src.log.models`：``operation_log`` 表（用户名快照 + 模块/操作/对象/摘要/IP）
- :mod:`src.log.schemas`：响应模型（日志为只读审计数据，没有请求体模型）
- :mod:`src.log.crud`：数据访问层（写入默认只 ``flush``，随调用方事务提交）
- :mod:`src.log.service`：埋点入口 :func:`~src.log.service.record_operation`
  与查询/清理
- :mod:`src.log.context`：请求来源 IP 的上下文（纯 ASGI 中间件注入）
- :mod:`src.log.router`：接口层

已实现接口（统一以 ``/api/logs`` 开头，**仅管理员**可访问）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | ``/api/logs`` | 日志列表（分页、按操作人/模块/操作/时间范围/关键字筛选） |
| GET | ``/api/logs/{log_id}`` | 日志详情 |
| DELETE | ``/api/logs/clean`` | 清理指定日期之前的日志（``before`` 必填，返回删除条数） |

埋点范围（详见 :mod:`src.log.service`）：``event`` 的创建/修改/删除/状态迁移、
``checkin`` 的人脸签到/手动签到/人工修正、``leave`` 的审批（通过/驳回）；
家庭用户的自助行为（提交/撤销请假等）不埋点，只审计管理侧操作。
"""

"""请假管理模块：家庭用户（户主）提前提交请假申请，管理员审批。

请假状态约定：``pending``（待审批）、``approved``（已通过）、``rejected``（已驳回）、
``cancelled``（已撤销）。

分层结构：

- :mod:`src.leave.models`：``leave_request`` 表与请假状态枚举
- :mod:`src.leave.schemas`：请求/响应模型
- :mod:`src.leave.crud`：数据访问层（含"待审批/已通过"判重与已通过成员查询）
- :mod:`src.leave.service`：提交、审批、撤销与权限规则
- :mod:`src.leave.router`：接口层

已实现接口（统一以 ``/api/leaves`` 开头）：

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/leaves`` | 提交请假申请 | 户主（本户成员）/ 管理员（任意成员） |
| GET | ``/api/leaves`` | 请假列表（按状态/活动/家庭筛选） | 管理员 / 工作人员（全部）、户主（本户） |
| GET | ``/api/leaves/{leave_id}`` | 请假详情 | 户主（本户）/ 管理员 / 工作人员 |
| PUT | ``/api/leaves/{leave_id}/approve`` | 审批请假 | 管理员 |
| PUT | ``/api/leaves/{leave_id}/cancel`` | 撤销请假申请 | 户主本人 / 管理员 |

关键规则：同一成员同一活动同时只能有一条待审批/已通过的请假（重复提交 409）；
已通过的请假只在活动结束时影响缺勤生成，若该成员实际到场签到则以实际签到为准。
"""

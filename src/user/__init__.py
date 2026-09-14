"""用户管理模块：维护 admin（管理员）、staff（工作人员）、family（家庭用户）三类账号。

已实现（阶段二）：

- ``GET    /api/users``：用户列表（分页、角色/状态筛选、关键字搜索）—— 管理员
- ``POST   /api/users``：新增用户（可指定角色与状态）—— 管理员
- ``GET    /api/users/{user_id}``：用户详情 —— 本人或管理员
- ``PUT    /api/users/{user_id}``：修改用户信息 —— 本人（姓名/手机号）或管理员（全部）
- ``PUT    /api/users/{user_id}/password``：修改本人密码（需原密码）
- ``PUT    /api/users/{user_id}/reset-password``：重置密码 —— 管理员
- ``PUT    /api/users/{user_id}/status``：启用/禁用账号 —— 管理员
- ``DELETE /api/users/{user_id}``：删除账号 —— 管理员

模块结构：

- ``models``：``sys_user`` 表与角色/状态枚举
- ``schemas``：请求/响应数据结构与密码校验规则
- ``crud``：数据访问层
- ``service``：业务逻辑与权限规则
- ``router``：接口层

说明：``family`` 角色用户即家庭户主，家庭档案（户号、地址等）在阶段三的家庭模块中维护。
"""

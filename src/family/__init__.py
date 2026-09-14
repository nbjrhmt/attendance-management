"""家庭管理模块：家庭以户主为单位创建，一户主一家庭。

已实现（阶段三）：

- ``POST   /api/families``：为指定户主创建家庭档案 —— 管理员
- ``GET    /api/families``：家庭列表（分页、状态/村组筛选、关键字搜索）—— 管理员/工作人员
- ``GET    /api/families/me``：本人家庭详情（含成员列表）—— 户主
- ``GET    /api/families/{family_id}``：家庭详情（含成员列表）—— 户主/管理员/工作人员
- ``PUT    /api/families/{family_id}``：修改家庭信息 —— 户主本人/管理员
- ``DELETE /api/families/{family_id}``：停用家庭档案（级联停用成员）—— 管理员

自动建档：户主通过 ``POST /api/auth/register`` 注册时，会在同一事务内自动创建
家庭档案（户号形如 ``F000123``）与户主成员记录，入口为
:func:`src.family.service.ensure_family_for_owner`。

模块结构：

- ``models``：``family`` 表与家庭状态枚举
- ``schemas``：请求/响应数据结构
- ``crud``：数据访问层（含成员数聚合子查询）
- ``service``：业务逻辑与权限规则
- ``router``：接口层
"""

"""认证模块：登录注册、JWT 令牌签发与校验、角色权限控制。

已实现（阶段二）：

- ``POST /api/auth/register``：家庭用户（户主）注册，角色固定为 family
- ``POST /api/auth/login``：账号密码登录，返回 access_token 与 refresh_token
- ``POST /api/auth/refresh``：使用 refresh_token 换取新的访问令牌
- ``GET  /api/auth/profile``：获取当前登录用户信息

模块结构：

- ``security``：密码哈希（bcrypt）与 JWT 签发/解析
- ``dependencies``：鉴权依赖（``CurrentUser`` / ``AdminUser`` / ``require_roles``）
- ``service``：注册、登录、刷新的业务逻辑
- ``router``：接口层

暂未实现（依赖 Redis，后续补充）：``POST /api/auth/logout`` 退出登录与令牌黑名单。
当前 JWT 为无状态令牌，退出登录由客户端删除本地令牌完成。
"""

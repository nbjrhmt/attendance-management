# 接口文档

本文档覆盖**已实现并经测试验证**的接口（阶段一认证基础、阶段二用户/认证、阶段三家庭/成员、
阶段四人脸识别、阶段五活动与签到、阶段六统计报表与操作日志、阶段七 AI 智能助手）。
服务启动后可在 `/docs`（Swagger UI，可直接在线调试）与 `/redoc` 查看自动生成的接口文档。

## 一、通用约定

| 项目 | 约定 |
| --- | --- |
| 基础地址 | `http://127.0.0.1:8001` |
| 业务前缀 | `/api`（由 `API_PREFIX` 配置） |
| 请求格式 | `application/json`（`Content-Type: application/json`） |
| 鉴权方式 | 请求头 `Authorization: Bearer <access_token>`，在 Swagger 中点击 Authorize 填入 access_token 即可 |
| 时间字段 | 格式为 `YYYY-MM-DDTHH:MM:SS`（服务器本地时间） |

### 统一响应格式

所有接口（含异常）均返回：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

### HTTP 状态码策略

| 场景 | HTTP 状态码 | 响应体 `code` |
| --- | --- | --- |
| 成功 | 200 | 0 |
| 参数校验失败（字段缺失/格式错误/密码过短等） | 200 | 400 |
| 业务异常（原密码错误、不能删除自己等） | 400 | 400 |
| 未登录 / 令牌无效或过期 | 401 | 401 |
| 权限不足 / 账号被禁用 | 403 | 403 |
| 资源不存在 | 404 | 404 |
| 资源冲突（用户名、手机号、户号重复） | 409 | 409 |
| 外部服务不可用 / 未配置（如未配置百度人脸密钥） | 503 | 503 |
| 未捕获异常 | 500 | 500 |

> 前端拦截器可统一按 HTTP 状态码处理登录失效（401 跳登录页），
> 业务提示统一读取响应体中的 `message`。

**接口规范例外**：图片/文件下载类接口（如 `GET /api/face/photo/{member_id}`）直接返回二进制流，
不使用统一响应格式；AI 助手的 `POST /api/assistant/chat/stream` 返回 SSE 事件流，同属例外。

### 分页参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | int | 否 | 1 | 页码，从 1 开始 |
| `page_size` | int | 否 | 10 | 每页条数，1~100 |

分页响应结构：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 25,
    "page": 1,
    "page_size": 10,
    "items": []
  }
}
```

### 角色与权限

| 角色 | 值 | 权限范围 |
| --- | --- | --- |
| 管理员 | `admin` | 全部用户管理、活动创建、统计报表、请假审批、生成活动简报 |
| 工作人员 | `staff` | 协助签到、查看统计报表、生成活动简报 |
| 家庭用户 | `family` | 管理本人及家庭成员、人脸签到、提交请假、向 AI 助手查询本户数据 |

> AI 助手（第十三章）的工具有自己的权限边界，且与上表一致：
> `get_statistics_overview` / `get_families_ranking` 仅 admin/staff 可用（家庭用户得到
> 「无权限查看统计报表」结果串）；`get_event_summary` / `get_leave_status` 对家庭用户
> 强制收敛到本户；活动简报的生成与读取仅 admin/staff。

---

## 二、系统接口

### GET `/health` 健康检查

```json
{"code": 0, "message": "success", "data": {"status": "ok"}}
```

### GET `/` 服务信息

返回服务名称、版本、运行环境、接口前缀与文档地址。

---

## 三、认证接口

### 1. POST `/api/auth/register` 家庭用户（户主）注册

家庭以户主为单位注册：注册成功后角色固定为 `family`，状态为 `active`，
并且**在同一事务内自动创建家庭档案与户主成员记录**（户主本人也是一名家庭成员，
`relation=householder`）。三步写入要么全部成功、要么全部回滚。

请求体：

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `username` | string | 是 | 4~50 位字母、数字或下划线 |
| `password` | string | 是 | 6~64 位，且 UTF-8 编码不超过 72 字节 |
| `real_name` | string | 是 | 1~50 字符 |
| `phone` | string | 否 | 中国大陆手机号（`1[3-9]` 开头共 11 位），需唯一 |
| `family` | object | 否 | 初始家庭档案信息，见下表；全部留空则自动生成户号 |

`family` 子字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `household_no` | string | 户号，1~32 字符，需唯一；留空自动生成 `F` + 6 位序号（如 `F000123`） |
| `address` | string | 家庭住址，最长 200 字符 |
| `village` | string | 所属村/组，最长 100 字符 |
| `contact_phone` | string | 家庭联系电话，手机号格式 |
| `remark` | string | 备注，最长 255 字符 |

```json
{
  "username": "zhangsan",
  "password": "zhangsan123456",
  "real_name": "张三",
  "phone": "13900000001",
  "family": {
    "address": "幸福村 12 号",
    "village": "幸福村",
    "contact_phone": "13900000001"
  }
}
```

成功响应（`message` 会提示已自动建档，返回的是账号信息）：

```json
{
  "code": 0,
  "message": "注册成功，已自动创建家庭档案",
  "data": {
    "id": 2,
    "username": "zhangsan",
    "real_name": "张三",
    "phone": "13900000001",
    "role": "family",
    "status": "active",
    "last_login_at": null,
    "created_at": "2025-01-01T10:00:00",
    "updated_at": "2025-01-01T10:00:00"
  }
}
```

注册完成后可调用 `GET /api/families/me` 查看自动创建的家庭档案与户主成员记录。

失败：`409` 用户名/手机号/户号已被占用（此时账号不会残留，整体已回滚）；
`400` 参数不符合约束（提交未声明字段也会被拒绝）。

### 2. POST `/api/auth/login` 登录

```json
{"username": "admin", "password": "你的密码"}
```

成功响应：

```json
{
  "code": 0,
  "message": "登录成功",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
    "token_type": "bearer",
    "expires_in": 7200,
    "user": {"id": 1, "username": "admin", "role": "admin", "status": "active"}
  }
}
```

- `expires_in`：access_token 有效期（秒），默认 7200（120 分钟）
- refresh_token 有效期默认 7 天（`REFRESH_TOKEN_EXPIRE_DAYS`）
- 失败：`401` 用户名或密码错误（账号不存在与密码错误返回相同提示）；`403` 账号已被禁用
- 登录成功会更新 `last_login_at`

### 3. POST `/api/auth/refresh` 刷新访问令牌

```json
{"refresh_token": "eyJhbGciOiJIUzI1NiIs..."}
```

成功返回与登录相同的结构（滑动续期，同时返回新的 refresh_token，客户端应覆盖保存）。

失败：`401` 令牌无效/过期，或误传了 access_token（提示"请使用刷新令牌"）。

### 4. GET `/api/auth/profile` 当前登录用户信息

请求头：`Authorization: Bearer <access_token>`

返回当前用户信息（结构同注册响应），失败：`401` 未提供令牌/令牌无效/用户已被删除。

---

## 四、用户管理接口

> 除"查看/修改本人信息"外，其余接口均要求管理员角色。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/users` | 用户列表（分页、角色/状态筛选、关键字搜索） | 管理员 |
| POST | `/api/users` | 新增用户（可指定角色与状态） | 管理员 |
| GET | `/api/users/{user_id}` | 用户详情 | 本人或管理员 |
| PUT | `/api/users/{user_id}` | 修改用户信息 | 本人（姓名/手机号）或管理员（全部） |
| PUT | `/api/users/{user_id}/password` | 修改本人密码（需原密码） | 本人 |
| PUT | `/api/users/{user_id}/reset-password` | 重置密码（无需原密码） | 管理员 |
| PUT | `/api/users/{user_id}/status` | 启用/禁用账号 | 管理员（不可操作自己） |
| DELETE | `/api/users/{user_id}` | 删除账号 | 管理员（不可删除自己） |

### 1. GET `/api/users` 用户列表

Query 参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `page` / `page_size` | int | 分页，见上文 |
| `role` | string | 角色筛选：`admin` / `staff` / `family` |
| `status` | string | 状态筛选：`active` / `disabled` |
| `keyword` | string | 模糊匹配用户名 / 真实姓名 / 手机号（最长 50 字符，`%`、`_` 等通配符按普通字符处理） |

示例：`GET /api/users?page=1&page_size=10&role=family&keyword=张`

### 2. POST `/api/users` 新增用户

```json
{
  "username": "worker_01",
  "password": "worker123456",
  "real_name": "工作人员甲",
  "phone": "13900000031",
  "role": "staff",
  "status": "active"
}
```

失败：`409` 用户名/手机号重复；`400` 参数不合法（如 `role` 传 `superman`）。

### 3. GET `/api/users/{user_id}` 用户详情

本人或管理员可访问：

- 本人查询他人 → `403`「权限不足：只能操作本人信息」
- 用户不存在 → `404`「用户不存在：999999」

### 4. PUT `/api/users/{user_id}` 修改用户信息

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `real_name` | string | 真实姓名，不能为空 |
| `phone` | string \| null | 手机号，传 `null` 表示清空，需唯一 |
| `role` | string | **仅管理员可提交**，非管理员提交返回 `403` |
| `status` | string | **仅管理员可提交**，非管理员提交返回 `403` |

未提交的字段保持原值；提交未声明字段（如 `nickname`）返回 `400`。

### 5. PUT `/api/users/{user_id}/password` 修改本人密码

```json
{"old_password": "旧密码", "new_password": "新密码至少6位"}
```

失败：`403` 修改他人密码；`400` 原密码不正确 / 新密码与原密码相同。

> 说明：当前 JWT 为无状态令牌，修改密码后旧令牌在有效期内仍可使用，
> 令牌黑名单（退出登录）将在引入 Redis 后实现。

### 6. PUT `/api/users/{user_id}/reset-password` 重置密码（管理员）

```json
{"new_password": "resetpass123456"}
```

### 7. PUT `/api/users/{user_id}/status` 启用/禁用

```json
{"status": "disabled"}
```

- 成功后该用户所有令牌立即失效（鉴权时校验账号状态）
- 不允许操作自己的账号：`400`「不能修改自己的账号状态」

### 8. DELETE `/api/users/{user_id}` 删除账号

```json
{"code": 0, "message": "删除成功", "data": {}}
```

- 不允许删除自己：`400`「不能删除自己的账号」
- 删除后该用户令牌立即失效：`401`「用户不存在或已被删除」

---

## 五、家庭管理接口

> 权限约定：**管理员**可管理全部家庭；**工作人员**只读（协助签到时需要名单）；
> **家庭用户**只能读写本人的家庭档案。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/families` | 为指定户主创建家庭档案（含户主成员行） | 管理员 |
| GET | `/api/families` | 家庭列表（分页、状态/村组筛选、关键字搜索） | 管理员 / 工作人员 |
| GET | `/api/families/me` | 本人家庭详情（含成员列表） | 家庭用户（户主） |
| GET | `/api/families/{family_id}` | 家庭详情（含成员列表） | 户主本人 / 管理员 / 工作人员 |
| PUT | `/api/families/{family_id}` | 修改家庭信息 | 户主本人 / 管理员 |
| DELETE | `/api/families/{family_id}` | 停用家庭档案（级联停用成员） | 管理员 |

### 1. POST `/api/families` 创建家庭档案

用于管理员补录既有家庭（如迁移历史数据）。户主通过注册接口注册时已自动建档，无需调用本接口。

```json
{
  "owner_id": 5,
  "address": "和平村 3 号",
  "village": "和平村",
  "household_no": "HJ2025001"
}
```

- `owner_id`：必填，必须是 `role=family` 且尚无家庭档案的用户
- 成功后自动生成户主成员记录，响应中的 `member_count` 为 1
- 失败：`409` 该户主已有家庭档案 / 户号被占用；`400` 用户不是家庭角色；`404` 用户不存在

### 2. GET `/api/families` 家庭列表

Query 参数：`page`、`page_size`、`status`（active/inactive）、`village`（村组精确匹配）、
`keyword`（模糊匹配户号 / 住址 / 村组 / 户主姓名 / 户主用户名）。

响应 `data.items[]` 字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `household_no` | 家庭ID / 户号 |
| `owner_id` / `owner_name` / `owner_username` / `owner_phone` | 户主信息 |
| `address` / `village` / `contact_phone` / `remark` | 家庭信息 |
| `status` | 家庭状态 |
| `member_count` | 有效成员数（不含已停用成员） |
| `checkin_required_count` | 其中需要签到的成员数 |

### 3. GET `/api/families/me` 我的家庭

户主查看本人家庭档案，`data` 中额外包含 `members[]`（含已停用成员）。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": 1,
    "household_no": "F000001",
    "owner_name": "张三",
    "member_count": 2,
    "checkin_required_count": 2,
    "status": "active",
    "members": [
      {"id": 1, "name": "张三", "relation": "householder", "needs_checkin": true, "status": "active"},
      {"id": 2, "name": "李四", "relation": "spouse", "needs_checkin": true, "status": "active"}
    ]
  }
}
```

失败：`401` 未登录；`404` 当前账号尚无家庭档案（如管理员/工作人员账号）。

### 4. GET `/api/families/{family_id}` 家庭详情

户主本人、管理员、工作人员可读，返回结构同"我的家庭"。
跨家庭访问返回 `403`，家庭不存在返回 `404`。

### 5. PUT `/api/families/{family_id}` 修改家庭信息

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `household_no` | string | 户号，不能为空，需唯一 |
| `address` / `village` / `remark` | string \| null | 传 `null` 表示清空 |
| `contact_phone` | string \| null | 手机号格式，传 `null` 表示清空 |
| `status` | string | 仅管理员可提交（户主提交会被拒绝：`403`） |

失败：`403` 工作人员/跨家庭；`409` 户号冲突；`400` 户号为空或提交未声明字段。

### 6. DELETE `/api/families/{family_id}` 停用家庭档案

管理员操作。**不物理删除数据**，而是把家庭标记为 `inactive` 并级联停用其全部正常成员，
以便保留历史签到记录。

```json
{"code": 0, "message": "家庭档案已停用，同时停用 2 名成员", "data": {"deactivated_members": 2}}
```

失败：`409` 该家庭已处于停用状态；`403` 非管理员。

---

## 六、家庭成员接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/members` | 添加家庭成员 | 户主本人 / 管理员（需指定 `family_id`） |
| GET | `/api/members` | 成员列表（分页、状态/是否需签到筛选、关键字搜索） | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/members/{member_id}` | 成员详情 | 户主本人 / 管理员 / 工作人员 |
| PUT | `/api/members/{member_id}` | 修改成员信息 | 户主本人 / 管理员 |
| PUT | `/api/members/{member_id}/status` | 启用/停用成员 | 户主本人 / 管理员 |
| DELETE | `/api/members/{member_id}` | 删除成员 | 户主本人 / 管理员 |

### 1. POST `/api/members` 添加成员

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 成员姓名，1~50 字符 |
| `relation` | string | 否 | 与户主关系，默认 `other`；可选 `spouse` / `son` / `daughter` / `father` / `mother` / `other` |
| `gender` | string | 否 | `male` / `female`；填写身份证号时可留空自动识别 |
| `id_card` | string | 否 | 18 位身份证号，需唯一；**含校验位与出生日期校验** |
| `birth_date` | date | 否 | 出生日期，格式 `YYYY-MM-DD`；填身份证号时可留空自动识别 |
| `phone` | string | 否 | 联系电话 |
| `needs_checkin` | bool | 否 | 是否需要签到，默认 `true` |
| `remark` | string | 否 | 备注 |
| `family_id` | int | 否 | **仅管理员**：为目标家庭添加成员时必须指定；家庭用户提交会被拒绝（`403`） |

```json
{
  "name": "李四",
  "relation": "spouse",
  "id_card": "110105199003151247"
}
```

成功响应中的 `gender` 与 `birth_date` 由身份证号自动识别：

```json
{
  "code": 0,
  "message": "成员添加成功",
  "data": {
    "id": 2,
    "family_id": 1,
    "name": "李四",
    "relation": "spouse",
    "gender": "female",
    "id_card": "110105199003151247",
    "birth_date": "1990-03-15",
    "needs_checkin": true,
    "status": "active"
  }
}
```

失败：`409` 身份证号已被其他成员使用；`400` 身份证号格式/校验位/出生日期错误、管理员未指定 `family_id`；
`403` 工作人员或跨家庭；`409` 家庭已停用。

### 2. GET `/api/members` 成员列表

Query 参数：`page`、`page_size`、`family_id`（管理员/工作人员可用）、`status`、`needs_checkin`、
`keyword`（模糊匹配姓名 / 身份证号 / 电话）。

家庭用户传他人 `family_id` 会被拒绝（`403`）；不传则默认查本户。

### 3. GET `/api/members/{member_id}` 成员详情

户主本人、管理员、工作人员可读；跨家庭 `403`；不存在 `404`。

### 4. PUT `/api/members/{member_id}` 修改成员信息

字段同新增（除 `family_id` 外），均为可选，未提交的字段保持原值；
`id_card` / `phone` / `remark` 传 `null` 表示清空；`name` 不能为空。

### 5. PUT `/api/members/{member_id}/status` 启用/停用

```json
{"status": "inactive"}
```

停用后该成员不参与"有效成员数 / 需签到人数"统计（家庭 `member_count` 随之减少），
但历史数据保留。

### 6. DELETE `/api/members/{member_id}` 删除成员（阶段五起语义为**停用**）

```json
{"code": 0, "message": "删除成功", "data": {}}
```

阶段五引入签到记录后，删除语义改为**停用**：成员状态置为 `inactive`，
签到/请假历史与人脸记录全部保留（`checkin_record`、`leave_request` 均以成员为外键），
因此接口仍返回 200「删除成功」，成员在列表/详情中仍可查到（`status=inactive`）。

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 正常删除（由正常状态变为停用） | 200 | 删除成功 |
| 重复删除已停用的成员 | 409 | 成员「X」已处于停用状态 |
| 跨家庭操作 | 403 | 权限不足：只有户主本人或管理员可以维护家庭信息 |

---

## 七、人脸识别接口

> 提供方由 ``FACE_PROVIDER`` 决定：``baidu``（百度 AI 人脸库，生产使用）或
> ``local``（本地模式，仅保存照片与录入状态、不做比对，供未申请到百度密钥时前端联调）。
> 未配置百度密钥且未显式切到 local 时，这些接口返回 ``503`` 并给出配置指引。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/face/register` | 录入人脸（multipart 上传照片） | 户主本人 / 管理员 |
| POST | `/api/face/update` | 重新录入（更新）人脸 | 户主本人 / 管理员 |
| POST | `/api/face/delete` | 删除人脸 | 户主本人 / 管理员 |
| POST | `/api/face/search` | 人脸搜索（1:N 识别） | 管理员 / 工作人员 |
| GET | `/api/face/status/{member_id}` | 成员人脸录入状态 | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/face/records` | 人脸录入记录列表 | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/face/photo/{member_id}` | 读取人脸照片（图片流） | 户主本人 / 管理员 / 工作人员 |

### 1. POST `/api/face/register` 录入人脸

请求为 ``multipart/form-data``：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `member_id` | int | 是 | 家庭成员ID |
| `file` | file | 是 | 人脸照片：JPG / PNG / BMP，默认不超过 2MB（`FACE_MAX_IMAGE_MB`） |

处理流程：照片校验（非空 / 大小 / **文件头魔数**，不信任扩展名）
→ 人脸检测（`FACE_DETECT_ON_REGISTER=true` 时，拒绝无人脸或多张人脸的照片）
→ 调用人脸库录入 → 照片落盘（`uploads/face/{family_id}/`）→ 写入 `face_record`。

成功响应：

```json
{
  "code": 0,
  "message": "成员「李四」人脸录入成功",
  "data": {
    "member_id": 2,
    "member_name": "李四",
    "family_id": 1,
    "provider": "baidu",
    "group_id": "attendance_family",
    "status": "registered",
    "image_size": 68432,
    "registered_at": "2025-01-01T10:00:00",
    "photo_url": "/api/face/photo/2",
    "detect": {"face_num": 1, "face_probability": 0.99, "blur": 0.12, "illumination": 120.0, "completeness": 0.98},
    "note": null
  }
}
```

失败：`409` 该成员已录入（提示改用更新接口）/ 成员或家庭已停用；`400` 照片为空 / 超大小 / 格式不支持 /
未检测到人脸 / 检测到多张人脸；`403` 跨家庭或工作人员；`404` 成员不存在；`503` 人脸服务不可用。

### 2. POST `/api/face/update` 更新人脸

请求与录入相同；仅当该成员**已录入**时可用，会覆盖人脸库中的旧照片并生成新的 `face_token`。
未录入时返回 `404`「该成员尚未录入人脸，请先调用录入接口」。

### 3. POST `/api/face/delete` 删除人脸

```json
{"member_id": 2}
```

删除人脸库中的人脸，本地记录置为 `inactive` 并保留照片以便追溯与重新录入。未录入时返回 `404`。

### 4. POST `/api/face/search` 人脸搜索

请求为 ``multipart/form-data``，字段 `file`（现场照片）。

```json
{
  "code": 0,
  "message": "识别成功：李四",
  "data": {
    "matched": true,
    "score": 95.5,
    "threshold": 80.0,
    "provider": "baidu",
    "member": {"id": 2, "name": "李四", "family_id": 1, "relation": "spouse", "needs_checkin": true},
    "family": {"family_id": 1, "household_no": "F000001", "village": "幸福村", "owner_name": "张三"},
    "candidates": [{"member_id": 2, "member_name": "李四", "family_id": 1, "household_no": "F000001", "score": 95.5}],
    "note": null
  }
}
```

- **未识别到人员是正常结果**：返回 `code=0` 且 `matched=false`（`message` 为"未识别到匹配的家庭成员"），
  由前端据此提示，而不是当成接口错误；
- 得分低于 `FACE_MATCH_THRESHOLD`（默认 80）不算匹配，但仍会在 `candidates` 中返回，便于人工选择；
- **已停用成员 / 已停用家庭的成员会被过滤**，不会识别为在场人员；
- 识别成功会更新该成员人脸记录的 `last_matched_at` 与 `last_match_score`；
- 仅管理员与工作人员可调用（全村人脸库 1:N 检索若对家庭用户开放，等于允许探测他人身份）。

### 5. GET `/api/face/status/{member_id}` 人脸录入状态

```json
{"code": 0, "message": "success", "data": {"member_id": 2, "member_name": "李四", "family_id": 1,
 "has_face": true, "status": "registered", "provider": "baidu", "group_id": "attendance_family",
 "registered_at": "2025-01-01T10:00:00", "last_matched_at": null, "last_match_score": null,
 "image_size": 68432, "photo_url": "/api/face/photo/2"}}
```

未录入时 `has_face=false`，其余字段为 `null`。

### 6. GET `/api/face/records` 人脸录入记录列表

Query 参数：`page`、`page_size`、`family_id`（管理员/工作人员可用）、`status`（`registered`/`inactive`）、
`keyword`（成员姓名）。家庭用户不传 `family_id` 时默认查本户，传他人 `family_id` 返回 `403`。

### 7. GET `/api/face/photo/{member_id}` 读取人脸照片

返回图片二进制流（`Content-Type: image/*`），需携带 `Authorization` 头。

> **接口规范例外**：图片/文件类接口不套用统一响应格式。
> 人脸照片属于敏感个人信息，因此 `uploads/` 不会被暴露为静态目录，
> 必须经本接口鉴权访问（户主本人 / 管理员 / 工作人员）。

---

## 八、签到活动接口

> 活动由管理员创建，面向全村"需要签到"的家庭成员；活动状态机为
> `pending`（未开始）→ `active`（进行中）→ `finished`（已结束），
> 或 `pending` → `cancelled`（已取消）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/events` | 创建签到活动 | 管理员 |
| GET | `/api/events` | 活动列表（分页、状态筛选、关键字搜索） | 登录用户 |
| GET | `/api/events/{event_id}` | 活动详情 | 登录用户 |
| PUT | `/api/events/{event_id}` | 修改活动 | 管理员（已结束/已取消 409） |
| DELETE | `/api/events/{event_id}` | 删除活动 | 管理员（仅未开始且无记录） |
| PUT | `/api/events/{event_id}/status` | 状态迁移（开始/结束/取消） | 管理员 |

### 1. POST `/api/events` 创建签到活动

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | string | 是 | - | 活动名称，1~100 字符 |
| `description` | string | 否 | null | 活动说明，≤500 字符 |
| `location` | string | 否 | null | 活动地点，≤100 字符 |
| `start_time` | datetime | 是 | - | 开始时间（服务器本地时间） |
| `end_time` | datetime | 是 | - | 结束时间，**必须晚于开始时间**，否则 400 |
| `late_threshold_minutes` | int | 否 | 15 | 迟到阈值（分钟），取值 0~1440 |

```json
{
  "code": 0,
  "message": "活动创建成功",
  "data": {
    "id": 1,
    "name": "2026 年春季村民大会",
    "description": "季度村民大会签到",
    "location": "村委会大院",
    "start_time": "2026-03-01T09:00:00",
    "end_time": "2026-03-01T11:00:00",
    "late_threshold_minutes": 15,
    "status": "pending",
    "created_at": "2026-02-25T10:00:00",
    "updated_at": "2026-02-25T10:00:00"
  }
}
```

创建后状态固定为 `pending`，需调用状态接口开始后才进入 `active`。

### 2. GET `/api/events` 活动列表

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `status` | string | 否 | - | 状态筛选：`pending` / `active` / `finished` / `cancelled` |
| `keyword` | string | 否 | - | 模糊匹配活动名称 / 活动地点 |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数 |

按开始时间倒序返回（同一时间按 ID 倒序）。

### 3. GET `/api/events/{event_id}` 活动详情

返回活动完整信息；活动不存在返回 404「签到活动不存在：{id}」。

### 4. PUT `/api/events/{event_id}` 修改活动

请求体字段与创建一致（均为可选，未提交字段保持原值）。
`finished` / `cancelled` 的活动返回 409「活动已结束，无法修改」/「活动已取消，无法修改」；
修改后仍需满足"结束时间晚于开始时间"，否则 400。

### 5. DELETE `/api/events/{event_id}` 删除活动

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 未开始且无签到/请假记录 | 200 | 删除成功 |
| 活动已开始/已结束/已取消 | 409 | 只有未开始的活动可以删除（当前状态：进行中） |
| 已有签到记录 | 409 | 该活动已有签到记录，无法删除 |
| 已有请假记录 | 409 | 该活动已有请假记录，无法删除 |

> 已有记录的活动不允许删除：签到记录与请假记录都以 `event.id` 为外键（`ON DELETE RESTRICT`），
> 物理删除会破坏历史数据与统计口径。

### 6. PUT `/api/events/{event_id}/status` 活动状态迁移

请求体：

```json
{"status": "active"}
```

| 当前状态 | 目标状态 | 结果 |
| --- | --- | --- |
| `pending` | `active` | 200「活动已开始」 |
| `active` | `finished` | 200「活动已结束，自动生成 N 条缺勤/请假记录」 |
| `pending` | `cancelled` | 200「活动已取消」 |
| 其他任意组合（含同状态重复迁移） | - | 409「不允许的状态变更：进行中 → 已取消」 |

`active → finished` 会在同一事务内为**应签到但无签到记录**的成员生成记录：
有已通过请假的记 `leave`，其余记 `absent`（详见第九节）。

---

## 九、签到业务接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/checkins/face` | 人脸签到（multipart：`event_id` + `file`） | 管理员 / 工作人员 |
| POST | `/api/checkins/manual` | 手动签到（`event_id` + `member_id`） | 管理员 / 工作人员 |
| GET | `/api/checkins` | 签到记录列表 | 登录用户（家庭用户仅本户） |
| GET | `/api/checkins/events/{event_id}` | 活动签到明细 + 汇总统计 | 登录用户（家庭用户仅本户） |
| PUT | `/api/checkins/{checkin_id}` | 人工修正签到记录 | 管理员 |

### 签到窗口与迟到判定

| 条件 | 状态码 | 提示 |
| --- | --- | --- |
| 活动已取消（`cancelled`） | 400 | 活动已取消 |
| 活动已结束（`finished`） | 400 | 活动已结束 |
| `now < start_time` | 400 | 活动尚未开始 |
| `now > end_time` | 400 | 活动已结束 |

迟到判定：`checked_at - start_time > late_threshold_minutes * 60` 秒 → `late`，否则 `signed`。

成员资格校验：成员/家庭不存在 404；家庭已停用 400「该家庭档案已停用，无法签到」；
成员已停用 400「该成员已停用，无法签到」；`needs_checkin=false` 400「该成员无需签到」。

幂等：同一成员同一活动已有记录时返回 409「该成员已在本次活动中签到」（人脸与手动签到一致）。

### 1. POST `/api/checkins/face` 人脸签到

请求为 `multipart/form-data`：`event_id`（表单）+ `file`（现场照片）。

流程：加载活动（不存在 404）→ 1:N 人脸识别（复用阶段四 `face_service.search_face`）
→ 未识别到成员 400「未识别到人脸库中的成员」→ 校验成员资格 / 签到窗口 / 是否重复 → 落库
（`method=face`、`face_score=识别得分`、`status` 按迟到规则判定）。

```json
{
  "code": 0,
  "message": "李小明 签到成功（已签到）",
  "data": {
    "id": 1,
    "event_id": 1,
    "event_name": "2026 年春季村民大会",
    "family_id": 1,
    "member_id": 2,
    "member_name": "李小明",
    "method": "face",
    "status": "signed",
    "checked_at": "2026-03-01T09:05:00",
    "face_score": 95.44,
    "reviewed_by_id": null,
    "reviewed_at": null,
    "review_remark": null,
    "remark": null,
    "created_at": "2026-03-01T09:05:00",
    "updated_at": "2026-03-01T09:05:00"
  }
}
```

### 2. POST `/api/checkins/manual` 手动签到

```json
{"event_id": 1, "member_id": 2, "remark": "人脸识别不可用，工作人员代签"}
```

规则与人脸签到完全一致，只是 `method=manual`、`face_score` 为 `null`。

### 3. GET `/api/checkins` 签到记录列表

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_id` | int | 否 | 按活动筛选 |
| `family_id` | int | 否 | 按家庭筛选（管理员/工作人员可用） |
| `status` | string | 否 | `signed` / `late` / `absent` / `leave` / `abnormal` |
| `keyword` | string | 否 | 成员姓名模糊搜索 |
| `page` / `page_size` | int | 否 | 分页参数 |

家庭用户只能查看本户记录，显式指定其他 `family_id` 返回 403「权限不足：只能查看本家庭的签到记录」。

### 4. GET `/api/checkins/events/{event_id}` 活动签到明细

返回活动信息 + 汇总统计 + 分页签到记录：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "event": { "id": 1, "name": "2026 年春季村民大会", "status": "finished", "...": "..." },
    "summary": {
      "total_expected": 4,
      "signed": 1,
      "late": 0,
      "absent": 2,
      "leave": 1,
      "abnormal": 0,
      "attendance_rate": 0.25
    },
    "checkins": { "total": 4, "page": 1, "page_size": 10, "items": [] }
  }
}
```

- `total_expected`：**应签到人数** = 家庭正常 + 成员正常 + `needs_checkin=true` 的成员总数；
  活动结束时会为其中无记录的成员生成缺勤/请假记录，因此结束后与记录总数一致；
- `attendance_rate`：`(signed + late) / total_expected`，取值 0~1（保留 4 位小数），
  应签到人数为 0 时返回 `0.0`；
- 家庭用户查看该接口时，`checkins` 与 `summary` 均收敛为**本户视角**，
  避免向任意家庭用户暴露全村成员出勤情况。

### 5. PUT `/api/checkins/{checkin_id}` 人工修正签到记录

```json
{"status": "signed", "remark": "现场补签", "review_remark": "已核实到场"}
```

- 仅管理员可操作（工作人员与家庭用户 403）；记录不存在 404「签到记录不存在：{id}」；
- 自动记录 `reviewed_by_id`（当前管理员）与 `reviewed_at`（当前时间）；
- 一致性修补：
  - 修正为 `signed` / `late`：`checked_at` 为空时补为修正时间，`method` 为空时补为 `manual`；
  - 修正为 `absent` / `leave` / `abnormal`：清空 `checked_at` 与 `method`。

---

## 十、请假管理接口

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/leaves` | 提交请假申请 | 户主（本户成员）/ 管理员（任意成员） |
| GET | `/api/leaves` | 请假列表 | 管理员 / 工作人员（全部）、户主（本户） |
| GET | `/api/leaves/{leave_id}` | 请假详情 | 户主（本户）/ 管理员 / 工作人员 |
| PUT | `/api/leaves/{leave_id}/approve` | 审批（通过 / 驳回） | 管理员 |
| PUT | `/api/leaves/{leave_id}/cancel` | 撤销申请 | 户主本人 / 管理员 |

### 1. POST `/api/leaves` 提交请假申请

```json
{"event_id": 1, "member_id": 2, "reason": "外出务工"}
```

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 提交成功（状态 `pending`） | 200 | 请假申请已提交，等待审批 |
| 活动 / 成员不存在 | 404 | 签到活动不存在：1 / 家庭成员不存在：2 |
| 非本户成员（户主）或工作人员 | 403 | 权限不足：只有户主本人或管理员可以维护家庭信息 |
| 活动已结束 / 已取消 | 400 | 活动已结束 / 活动已取消 |
| 成员已停用 / 家庭已停用 / 无需签到 | 400 | 该成员已停用，无法请假 / 该家庭档案已停用，无法请假 / 该成员无需签到 |
| 已有待审批或已通过的请假 | 409 | 该成员已有待审批或已通过的请假 |

被驳回或已撤销的申请可以重新提交（旧记录作为历史保留）。

### 2. GET `/api/leaves` 请假列表

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 否 | `pending` / `approved` / `rejected` / `cancelled` |
| `event_id` | int | 否 | 按活动筛选 |
| `family_id` | int | 否 | 按家庭筛选（管理员/工作人员可用） |
| `page` / `page_size` | int | 否 | 分页参数 |

家庭用户强制本户范围，指定其他家庭返回 403「权限不足：只能查看本家庭的请假申请」。

### 3. GET `/api/leaves/{leave_id}` 请假详情

返回请假申请及关联的成员姓名、活动名称、家庭户号；不存在的申请返回 404「请假申请不存在：{id}」；
其他家庭的户主访问返回 403。

### 4. PUT `/api/leaves/{leave_id}/approve` 审批请假

```json
{"status": "approved", "remark": "情况属实"}
```

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 审批成功（`approved` / `rejected`） | 200 | 审批完成：已通过 / 已驳回 |
| 审批结果不是 `approved` / `rejected` | 400 | 审批结果只能是 approved（通过）或 rejected（驳回） |
| 重复审批（已通过/已驳回） | 409 | 该请假申请已审批，无法重复审批 |
| 审批已撤销的申请 | 409 | 该请假申请已撤销，无法审批 |
| 非管理员 | 403 | 权限不足：该操作仅限管理员 |

审批成功后会记录 `reviewed_by_id`（当前管理员）、`reviewed_at` 与 `review_remark`。

### 5. PUT `/api/leaves/{leave_id}/cancel` 撤销请假

户主本人（本户成员）或管理员可撤销**待审批**的申请；其他家庭 403、
非待审批状态返回 409「只有待审批的请假可以撤销」；工作人员不可撤销（403）。

---

## 十一、统计报表接口

> 统计接口**纯读**：不建表、不写库，全部指标由聚合查询实时计算。
> 报表含全村数据，因此**仅管理员与工作人员**可访问，家庭用户返回 403。
> 出勤率口径统一为 `(signed + late) / 应签到人数`，四舍五入 4 位小数（总览为 2 位）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/statistics/overview` | 全村总览 | 管理员 / 工作人员 |
| GET | `/api/statistics/events/{event_id}` | 单活动统计（含家庭维度分页） | 管理员 / 工作人员 |
| GET | `/api/statistics/families` | 家庭参与度排行 | 管理员 / 工作人员 |
| GET | `/api/statistics/trend` | 按活动时间的签到趋势 | 管理员 / 工作人员 |
| GET | `/api/statistics/export` | 导出 CSV（活动 / 家庭） | 管理员 / 工作人员 |

### 1. GET `/api/statistics/overview` 总览

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total_families": 2,
    "total_members": 5,
    "total_events": 5,
    "active_events": 1,
    "finished_events": 2,
    "total_checkins": 5,
    "avg_attendance_rate": 0.4
  }
}
```

| 字段 | 口径 |
| --- | --- |
| `total_families` | 正常状态的家庭数（`family.status=active`） |
| `total_members` | 正常状态的成员数（`family_member.status=active`；家庭停用时会级联停用其成员） |
| `total_events` / `active_events` / `finished_events` | 活动总数 / 进行中 / 已结束 |
| `total_checkins` | 全部活动中 **signed + late** 的记录数（实际到场次数） |
| `avg_attendance_rate` | 各**已结束活动**出勤率的算术平均，2 位小数；无已结束活动时为 `null` |

### 2. GET `/api/statistics/events/{event_id}` 单活动统计

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` / `page_size` | int | 否 | 1 / 10 | **家庭维度列表**的分页参数 |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "event": { "id": 1, "name": "2026 年春季村民大会", "status": "finished", "...": "..." },
    "summary": {
      "total_expected": 5, "signed": 2, "late": 1,
      "absent": 2, "leave": 0, "abnormal": 0, "attendance_rate": 0.6
    },
    "families": {
      "total": 2, "page": 1, "page_size": 10,
      "items": [
        {
          "family_id": 1, "household_no": "F000001", "owner_name": "张三",
          "village": "测试村", "expected": 3, "signed": 1, "late": 1,
          "absent": 1, "leave": 0, "rate": 0.6667
        }
      ]
    }
  }
}
```

- `summary` 与阶段五 `GET /api/checkins/events/{id}` 的口径完全一致（可直接对照）；
- `families.items[].expected`：该户在本次活动的应签到人数（家庭正常 + 成员正常 + 需要签到）；
  `rate = (signed + late) / expected`，`expected=0` 时为 `null`；
- 入榜家庭 = "有应签到成员"或"在本次活动中留有签到记录"的家庭（后者保证活动结束后
  被停用家庭的历史统计不丢失），按 `expected` 降序；
- 活动不存在返回 404「签到活动不存在：{event_id}」。

### 3. GET `/api/statistics/families` 家庭参与度排行

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `village` | string | 否 | - | 按村组**精确**筛选 |
| `keyword` | string | 否 | - | 户主姓名 / 户号模糊搜索（LIKE 通配符已转义） |
| `order_by` | string | 否 | `rate` | `rate` 出勤率 / `checkins` 签到次数 / `members` 在册成员数 |
| `order` | string | 否 | `desc` | `desc` 降序 / `asc` 升序 |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数 |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 2, "page": 1, "page_size": 10,
    "items": [
      {
        "family_id": 1, "household_no": "F000001", "owner_name": "张三",
        "village": "测试村", "active_member_count": 3, "checkin_required_count": 3,
        "event_participated_count": 2, "total_checkins": 4, "attendance_rate": 0.75
      }
    ]
  }
}
```

- **入榜条件**：该户在**已结束活动**中产生过签到记录（至少参与过一场已结束活动）；
- `event_participated_count`：参与过的已结束活动场次数；
- `total_checkins`：该户在**全部活动**中的 signed + late 次数；
- `attendance_rate`：`该户在已结束活动中的 (signed+late) / 该户在已结束活动中的应签到总数`
  （历史分母，取活动结束时的记录总数，不因日后新增成员而回溯变化），分母为 0 时为 `null`。

### 4. GET `/api/statistics/trend` 签到趋势

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `start_date` | date | 否 | 今天 - 30 天 | 起始日期（含），按 `event.start_time` 过滤 |
| `end_date` | date | 否 | 今天 | 结束日期（含） |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数 |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 5, "page": 1, "page_size": 10,
    "items": [
      {
        "event_id": 1, "name": "已结束活动一", "start_date": "2026-03-01",
        "status": "finished", "total_expected": 5,
        "signed": 2, "late": 1, "absent": 2, "leave": 0, "abnormal": 0,
        "attendance_rate": 0.6
      }
    ]
  }
}
```

按 `event.start_time` **升序**返回；`start_date > end_date` 返回 400「开始日期不能晚于结束日期」；
`total_expected` 与 `attendance_rate` 采用与 `summary` 相同的"当前应签到人数"口径
（应签到人数为 0 时出勤率为 `0.0`）。

### 5. GET `/api/statistics/export` 导出 CSV

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | **是** | `events` 活动签到统计 / `families` 家庭参与度统计 |
| `event_id` | int | 否 | 仅 `type=events`：指定活动（缺省导出全部活动）；活动不存在返回 404 |
| `start_date` / `end_date` | date | 否 | 仅 `type=events`：按活动开始时间过滤 |
| `village` / `keyword` / `order_by` / `order` | - | 否 | 仅 `type=families`：与排行接口一致 |

- 响应为**文件流**（`Content-Type: text/csv; charset=utf-8`），**不使用统一响应格式**；
- `Content-Disposition: attachment; filename*=UTF-8''%E6%B4%BB%E5%8A%A8...`
  （中文文件名按 RFC 5987 做 URL 编码，如 `活动签到统计.csv` / `家庭参与度统计.csv`）；
- 文件首字符为 UTF-8 BOM（`\ufeff`），**Excel 直接打开中文不乱码**；
- 出勤率列输出 0~1 的比例并固定保留 4 位小数（如 `0.6000`），无数据时为 `0.0000`；
- `type` 缺失或取值非法返回 400「导出类型只能是 events（活动统计）或 families（家庭统计）」。

列定义：

| type | 列 |
| --- | --- |
| `events` | 活动ID, 活动名称, 开始时间, 结束时间, 状态, 应签到, 已签到, 迟到, 缺勤, 请假, 异常, 出勤率 |
| `families` | 户号, 户主, 村组, 在册成员, 应签到, 参与活动数, 签到次数, 出勤率 |

> 状态列输出中文标签（如「已结束」）；时间列格式为 `YYYY-MM-DD HH:MM:SS`。

---

## 十二、操作日志接口

> 操作日志属于**审计数据**：仅管理员可读（工作人员与家庭用户 403）。
> 日志由业务代码在关键操作时自动写入（见下方"埋点范围"），**没有"新增日志"接口**，
> 避免审计数据被伪造。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/logs` | 日志列表（分页 + 多维筛选） | 管理员 |
| GET | `/api/logs/{log_id}` | 日志详情 | 管理员 |
| DELETE | `/api/logs/clean` | 清理指定日期之前的日志 | 管理员 |

### 1. GET `/api/logs` 日志列表

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `user_id` | int | 否 | - | 按操作人ID筛选 |
| `module` | string | 否 | - | 业务模块：`event` / `checkin` / `leave` |
| `action` | string | 否 | - | 操作类型：`create` / `approve` / `manual_checkin` 等 |
| `start_time` / `end_time` | datetime | 否 | - | 操作时间范围（含边界），如 `2026-03-01T00:00:00` |
| `keyword` | string | 否 | - | 操作人用户名 / 操作摘要模糊搜索（LIKE 通配符已转义） |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数 |

按操作时间**倒序**返回（同一时间按ID倒序，分页稳定）：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 12, "page": 1, "page_size": 10,
    "items": [
      {
        "id": 12,
        "user_id": 1,
        "username": "admin",
        "module": "event",
        "action": "change_status",
        "target_type": "event",
        "target_id": 3,
        "target": "event/3",
        "detail": "状态：active→finished；自动生成 2 条缺勤/请假记录",
        "ip": "127.0.0.1",
        "created_at": "2026-03-01T11:05:00"
      }
    ]
  }
}
```

### 2. GET `/api/logs/{log_id}` 日志详情

返回与列表相同的字段；日志不存在返回 404「操作日志不存在：{log_id}」。

### 3. DELETE `/api/logs/clean` 清理历史日志

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `before` | date | **是** | 清理**该日期 00:00 之前**的日志（不含该日），如 `2026-01-01` |

```json
{"code": 0, "message": "已清理 128 条历史日志", "data": {"deleted": 128, "before": "2026-01-01"}}
```

### 埋点范围（哪些操作会产生日志）

| 模块 | action | 触发点 | 摘要示例 |
| --- | --- | --- | --- |
| `event` | `create` / `update` / `delete` / `change_status` | 活动创建、修改、删除、状态迁移 | 创建活动：村晚联欢 / 状态：pending→active |
| `checkin` | `face_checkin` / `manual_checkin` / `correct` | 人脸签到、手动签到、管理员修正记录 | 人脸签到：李小明（活动3，已签到） |
| `leave` | `approve` / `reject` | 管理员审批请假（通过 / 驳回） | 审批请假：李小明（活动3）→ 已通过 |

> 家庭用户的自助行为（提交请假、撤销请假、人脸录入等）**不埋点**，只审计管理侧操作；
> 日志与业务变更在同一次数据库事务中提交，响应结构与业务行为不受埋点影响。

---

## 十三、AI 智能助手接口

> **定位**：把大模型接到平台真实数据上——多轮对话 + 工具调用（Function Calling）+
> 会话记忆 + 角色权限 + SSE 流式 + 活动简报生成（AIGC）+ 平台指南检索（轻量 RAG）。
> 默认 `LLM_PROVIDER=mock`，**无需任何 API Key 即可跑通全流程**：mock 提供方会按内置意图表
> 自动请求工具（问"我家签到情况"→ 先 `get_events` 取活动ID、再 `get_event_summary` 取真实汇总），
> 因此无密钥也能看到真实的工具调用链与真实业务数据；
> 配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 后切换为真实模型（由模型自主决定调用哪些工具）。
>
> **权限**：所有工具都复用既有 service 的权限链，不绕过权限裸查 SQL：
> 家庭用户只能取到本户数据，统计类工具对家庭用户返回「无权限」结果串（不是异常）。
> 每次对话都会返回本轮的工具调用轨迹，便于核对"回答里的数字是否来自平台"。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/assistant/chat` | 多轮对话（含工具调用轨迹） | 登录用户 |
| POST | `/api/assistant/chat/stream` | 同上，SSE 流式 | 登录用户 |
| GET | `/api/assistant/conversations` | 本人会话分页列表 | 登录用户（仅本人） |
| GET | `/api/assistant/conversations/{id}/messages` | 本人会话消息分页（时间正序） | 登录用户（仅本人） |
| DELETE | `/api/assistant/conversations/{id}` | 删除本人会话（级联删除消息） | 登录用户（仅本人） |
| POST | `/api/assistant/events/{event_id}/summary` | 生成活动简报（AIGC） | 管理员 / 工作人员 |
| GET | `/api/assistant/events/{event_id}/summary` | 读取已保存简报（无则 404） | 管理员 / 工作人员 |

### 可用工具

| 工具 | 说明 | 数据来源 | 权限 |
| --- | --- | --- | --- |
| `get_events` | 活动列表（可按 `status` / `keyword` / 分页筛选） | `src.event.service.list_events` | 登录用户 |
| `get_event_detail` | 活动详情（时间/地点/状态/迟到阈值） | `src.event.service.get_event_detail` | 登录用户 |
| `get_event_summary` | 活动签到汇总与明细（与 `GET /api/checkins/events/{id}` 同款口径） | `src.checkin.service.get_event_checkins` | 家庭用户仅本户 |
| `get_leave_status` | 请假查询（可按 `event_id` / `status` 筛选） | `src.leave.service.list_leaves` | 家庭用户仅本户 |
| `get_statistics_overview` | 全村总览（家庭/成员/活动/签到/平均出勤率） | `src.statistics.service.build_overview` | 管理员 / 工作人员 |
| `get_families_ranking` | 家庭参与度排行 | `src.statistics.service.list_family_rankings` | 管理员 / 工作人员 |
| `search_knowledge` | 平台使用指南检索（关键词打分 + 二元组召回） | `ai_knowledge` 表 | 登录用户 |

> 工具结果会截断到 **500 字**后回填给模型（避免上下文被明细挤满），
> 工具内部的业务异常（活动不存在、无权限等）会转换为结果串由模型转述，**不会 500**。

### 1. POST `/api/assistant/chat` AI 对话

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message` | string | 是 | 用户消息，1~2000 字（纯空白按空消息处理，返回 400） |
| `conversation_id` | int | 否 | 会话ID；不传则新建会话（标题取本条消息前 20 字），传入则续接本人会话 |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "conversation_id": 1,
    "reply": "根据查询结果：活动「2026 年春季村民大会」（已结束）全村签到汇总：\n- 应签到 5 人｜已签到 2｜迟到 1｜缺勤 2｜请假 0｜异常 0｜出勤率 60.00%",
    "tool_calls": [
      {
        "name": "get_event_summary",
        "args": {"event_id": 1},
        "result": "活动「2026 年春季村民大会」（已结束）全村签到汇总：\n- 应签到 5 人｜已签到 2｜迟到 1｜缺勤 2｜请假 0｜异常 0｜出勤率 60.00%\n签到明细（共 5 条，最多展示 8 条）：\n- 张三：已签到（2026-03-01 09:05）\n- 李四：迟到（2026-03-01 09:40）"
      }
    ]
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `conversation_id` | 会话ID；续聊时原样回传即可（也用于查询消息列表与删除会话） |
| `reply` | 助手回复（mock 模式下为模板化文本：命中意图时引用工具结果前 100 字，否则提示配置真实 key） |
| `tool_calls` | 本轮实际执行的工具调用：名称、入参、结果摘要（≤500 字）；未调用工具时为空数组 |

**错误码**

| 场景 | HTTP / code | 提示 |
| --- | --- | --- |
| 未登录 / 令牌无效 | 401 | 未提供认证令牌，请先登录 |
| 消息为空 / 超过 2000 字 | 400 / 400 | 消息不能为空 / 消息长度不能超过 2000 字 |
| 会话不存在 | 404 | 会话不存在：`{id}` |
| 访问他人会话 | 403 | 权限不足：只能访问本人的会话 |
| 未配置 LLM（`openai_compat`）或服务商报错 | 503 | 未配置 LLM：请在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL |

### 2. POST `/api/assistant/chat/stream` AI 对话（SSE 流式）

入参、权限、错误码与 `/chat` 完全一致；响应为 `text/event-stream; charset=utf-8`，
**不使用统一响应格式**（与 CSV / 图片接口同属例外）。事件每行形如 `data: {json}`，
每条事件后跟一个空行：

```
data: {"type": "start", "conversation_id": 1}
data: {"type": "tool", "name": "get_event_summary", "args": {"event_id": 1}, "result": "活动「2026 年春季村民大会」…"}
data: {"type": "token", "content": "根据查"}
data: {"type": "token", "content": "询结果"}
data: {"type": "done", "conversation_id": 1, "reply": "根据查询结果：…"}
```

| 事件 | 时机 | 字段 |
| --- | --- | --- |
| `start` | 建立流时立即下发 | `conversation_id` |
| `tool` | 每执行一次工具调用 | `name`、`args`、`result`（结果摘要 ≤500 字） |
| `token` | 工具循环结束后逐段下发最终文本 | `content`（增量文本） |
| `done` | 结束前 | `conversation_id`、`reply`（完整回复，与 token 拼接一致） |

> 实现说明：工具循环走**非流式**（保证完整拿到 `tool_calls`），最终文本再以 token 逐段下发，
> 因此前端可以先渲染"工具调用卡片"，再呈现打字机效果。
> 校验（消息为空、跨用户会话）在开流之前完成，参数错误仍返回标准统一响应（400/403/404）。
> `done` 之前会把 user / assistant 消息与工具轨迹落库，因此**刷新后仍可续聊**。

### 3. GET `/api/assistant/conversations` 会话列表

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数（`page_size` ≤ 100） |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 2,
    "page": 1,
    "page_size": 10,
    "items": [
      {"id": 2, "title": "全村签到情况怎么样？", "message_count": 4,
       "created_at": "2026-03-02 10:00:00", "updated_at": "2026-03-02 10:05:00"}
    ]
  }
}
```

> 只返回**本人**会话，按最近活跃（`updated_at`）倒序；`message_count` 便于前端展示"共几轮对话"。

### 4. GET `/api/assistant/conversations/{conversation_id}/messages` 会话消息

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` / `page_size` | int | 否 | 1 / 10 | 分页参数（按 `id` 升序，即时间正序） |

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total": 2,
    "page": 1,
    "page_size": 10,
    "items": [
      {"id": 1, "conversation_id": 1, "role": "user", "content": "全村签到情况怎么样？",
       "tool_calls": [], "created_at": "2026-03-02 10:00:00"},
      {"id": 2, "conversation_id": 1, "role": "assistant", "content": "根据查询结果：…",
       "tool_calls": [{"name": "get_statistics_overview", "args": {}, "result": "全村统计总览：…"}],
       "created_at": "2026-03-02 10:00:03"}
    ]
  }
}
```

他人会话返回 403，会话不存在返回 404。

### 5. DELETE `/api/assistant/conversations/{conversation_id}` 删除会话

```json
{"code": 0, "message": "会话已删除", "data": {"conversation_id": 1, "deleted_messages": 4}}
```

> 同时删除该会话下的全部消息（`deleted_messages` 为删除条数）。
> 他人会话返回 403，会话不存在返回 404。

### 6. POST `/api/assistant/events/{event_id}/summary` 生成活动简报（AIGC）

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `event_id` | int（路径） | 是 | 活动ID |

流程：权限校验（管理员/工作人员）→ 组装真实统计快照（活动 + 签到各状态计数 + 出勤率 +
请假审批统计）→ 要求模型输出**严格 JSON**（`title` / `highlights`（3 条）/ `body` Markdown）
→ 解析失败时**降级**为模板拼接并在正文中标注「（降级）」→ `upsert` `activity_summary`
（**一活动一份**，重复生成覆盖更新）。

```json
{
  "code": 0,
  "message": "简报已生成",
  "data": {
    "id": 1,
    "event_id": 1,
    "title": "2026 年春季村民大会活动简报",
    "content": "# 2026 年春季村民大会活动简报\n\n## 活动亮点\n- 到场 3 人，出勤率 60.00%\n…",
    "meta": {
      "event": {"id": 1, "name": "2026 年春季村民大会", "status": "finished", "status_label": "已结束", "...": "..."},
      "checkin": {"total_expected": 5, "signed": 2, "late": 1, "absent": 2, "leave": 0, "abnormal": 0, "attendance_rate": 0.6},
      "leave": {"total": 1, "pending": 0, "approved": 1, "rejected": 0, "cancelled": 0, "members": []},
      "highlights": ["到场 3 人", "出勤率 60.00%", "无异常"],
      "degraded": false,
      "provider": "openai_compat",
      "model": "deepseek-chat",
      "generated_at": "2026-03-02 10:10:00"
    },
    "degraded": false,
    "created_by_id": 1,
    "created_at": "2026-03-02 10:10:00",
    "updated_at": "2026-03-02 10:10:00"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `content` | 简报全文（Markdown），可直接渲染 |
| `meta` | 本次生成使用的统计快照（JSON），便于核对简报数字与统计接口是否一致 |
| `degraded` | `true` 表示模型输出不规范或调用失败，已降级为模板拼接（正文含「（降级）」标记） |

**错误码**：家庭用户 403（权限不足：只有管理员或工作人员可以生成活动简报）；活动不存在 404。

### 7. GET `/api/assistant/events/{event_id}/summary` 读取活动简报

返回最近一次生成的简报（结构同上）；尚未生成返回 404「该活动尚未生成简报：`{id}`」；
家庭用户返回 403。

---

## 十四、快速联调示例（PowerShell）

```powershell
# 1) 健康检查
Invoke-RestMethod http://127.0.0.1:8001/health

# 2) 管理员登录
$login = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
    -ContentType 'application/json' `
    -Body '{"username":"admin","password":"你的密码"}'

# 3) 携带令牌访问受保护接口
$headers = @{ Authorization = "Bearer $($login.data.access_token)" }
Invoke-RestMethod http://127.0.0.1:8001/api/auth/profile -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/users?page=1&page_size=10" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/families?page=1&page_size=10" -Headers $headers

# 4) 家庭用户注册（无需登录，自动创建家庭档案）
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/register `
    -ContentType 'application/json' `
    -Body '{"username":"zhangsan","password":"zhangsan123456","real_name":"张三","family":{"village":"幸福村"}}'

# 5) 户主登录并管理家庭成员
$familyLogin = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
    -ContentType 'application/json' `
    -Body '{"username":"zhangsan","password":"zhangsan123456"}'
$familyHeaders = @{ Authorization = "Bearer $($familyLogin.data.access_token)" }

Invoke-RestMethod http://127.0.0.1:8001/api/families/me -Headers $familyHeaders

Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/members -Headers $familyHeaders `
    -ContentType 'application/json' `
    -Body '{"name":"李四","relation":"spouse","id_card":"110105199003151247"}'

Invoke-RestMethod "http://127.0.0.1:8001/api/members?page=1&page_size=10" -Headers $familyHeaders

# 6) 人脸录入（multipart 上传照片；PowerShell 需用 -Form 或 curl）
curl.exe -X POST http://127.0.0.1:8001/api/face/register `
    -H "Authorization: Bearer $($familyLogin.data.access_token)" `
    -F "member_id=2" -F "file=@C:\photos\lisi.jpg"

# 7) 人脸录入状态与照片
Invoke-RestMethod http://127.0.0.1:8001/api/face/status/2 -Headers $familyHeaders
Invoke-RestMethod http://127.0.0.1:8001/api/face/records -Headers $familyHeaders

# 8) 人脸识别（工作人员/管理员，用于现场签到）
curl.exe -X POST http://127.0.0.1:8001/api/face/search `
    -H "Authorization: Bearer $($login.data.access_token)" `
    -F "file=@C:\photos\onsite.jpg"

# 9) 阶段五：创建签到活动并开始（管理员）
$event = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/events -Headers $headers `
    -ContentType 'application/json' `
    -Body '{"name":"春季村民大会","location":"村委会大院","start_time":"2026-03-01T09:00:00","end_time":"2026-03-01T11:00:00","late_threshold_minutes":15}'
Invoke-RestMethod -Method Put "http://127.0.0.1:8001/api/events/$($event.data.id)/status" -Headers $headers `
    -ContentType 'application/json' -Body '{"status":"active"}'

# 10) 人脸签到（工作人员/管理员）
curl.exe -X POST http://127.0.0.1:8001/api/checkins/face `
    -H "Authorization: Bearer $($login.data.access_token)" `
    -F "event_id=$($event.data.id)" -F "file=@C:\photos\onsite.jpg"

# 11) 手动签到（人脸不可用时的备用方式）
$manualBody = @{ event_id = $event.data.id; member_id = 2 } | ConvertTo-Json
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/checkins/manual -Headers $headers `
    -ContentType 'application/json' -Body $manualBody

# 12) 户主提交请假 → 管理员审批
$leaveBody = @{ event_id = $event.data.id; member_id = 2; reason = "外出务工" } | ConvertTo-Json
$leave = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/leaves -Headers $familyHeaders `
    -ContentType 'application/json' -Body $leaveBody
Invoke-RestMethod -Method Put "http://127.0.0.1:8001/api/leaves/$($leave.data.id)/approve" -Headers $headers `
    -ContentType 'application/json' -Body '{"status":"approved","remark":"情况属实"}'

# 13) 结束活动（自动生成缺勤/请假记录）并查看签到明细与汇总
Invoke-RestMethod -Method Put "http://127.0.0.1:8001/api/events/$($event.data.id)/status" -Headers $headers `
    -ContentType 'application/json' -Body '{"status":"finished"}'
Invoke-RestMethod "http://127.0.0.1:8001/api/checkins/events/$($event.data.id)" -Headers $headers

# 14) 签到记录列表（按活动/状态/家庭筛选）
Invoke-RestMethod "http://127.0.0.1:8001/api/checkins?event_id=$($event.data.id)&status=absent" -Headers $headers

# 15) 阶段六：统计报表（管理员/工作人员）
Invoke-RestMethod http://127.0.0.1:8001/api/statistics/overview -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/statistics/events/$($event.data.id)" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/statistics/families?order_by=rate&order=desc" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/statistics/trend?start_date=2026-03-01&end_date=2026-03-31" -Headers $headers

# 16) 导出 CSV（保存到文件；Excel 可直接打开，中文不乱码）
curl.exe -s -H "Authorization: Bearer $($login.data.access_token)" `
    "http://127.0.0.1:8001/api/statistics/export?type=events" -o "活动签到统计.csv"
curl.exe -s -H "Authorization: Bearer $($login.data.access_token)" `
    "http://127.0.0.1:8001/api/statistics/export?type=families&village=测试村" -o "家庭参与度统计.csv"

# 17) 阶段六：操作日志审计（仅管理员）
Invoke-RestMethod "http://127.0.0.1:8001/api/logs?page=1&page_size=20" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/logs?module=checkin&action=manual_checkin" -Headers $headers
Invoke-RestMethod -Method Delete "http://127.0.0.1:8001/api/logs/clean?before=2026-01-01" -Headers $headers

# 18) 阶段七：AI 对话（助手会先调用工具查真实数据，tool_calls 给出调用轨迹）
$chatBody = @{ message = "这次活动的签到情况怎么样？" } | ConvertTo-Json
$chat = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/assistant/chat `
    -Headers $headers -ContentType 'application/json' -Body $chatBody
$chat.data.reply
$chat.data.tool_calls

# 19) 多轮对话：带上 conversation_id 续聊（助手记得上下文）
$chat2Body = @{ message = "那缺勤的是谁？"; conversation_id = $chat.data.conversation_id } | ConvertTo-Json
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/assistant/chat `
    -Headers $headers -ContentType 'application/json' -Body $chat2Body

# 20) SSE 流式（curl -N 观察 start → tool → token → done）
curl.exe -N -X POST http://127.0.0.1:8001/api/assistant/chat/stream `
    -H "Authorization: Bearer $($login.data.access_token)" `
    -H "Content-Type: application/json" `
    -d "{\"message\":\"最近有哪些活动？\"}"

# 21) 生成/读取活动简报（AIGC，一活动一份；家庭用户 403）
Invoke-RestMethod -Method Post "http://127.0.0.1:8001/api/assistant/events/$($event.data.id)/summary" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/assistant/events/$($event.data.id)/summary" -Headers $headers

# 22) 会话与消息管理（仅本人可见）
Invoke-RestMethod "http://127.0.0.1:8001/api/assistant/conversations?page=1&page_size=10" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8001/api/assistant/conversations/$($chat.data.conversation_id)/messages" -Headers $headers
Invoke-RestMethod -Method Delete "http://127.0.0.1:8001/api/assistant/conversations/$($chat.data.conversation_id)" -Headers $headers
```

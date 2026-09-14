# 乡村基层活动智能签到管理平台（后端）

面向乡村基层的活动签到管理系统：以**家庭为单位**进行人脸签到，管理员可创建签到活动、
审批请假并查看签到统计报表；内置 **AI 智能助手**（多轮对话 + 工具调用 + 流式输出 +
活动简报生成），所有回答都基于平台真实数据。

- 后端：Python 3.11+ / FastAPI / SQLAlchemy / MySQL / Redis
- 认证：JWT（python-jose + passlib）
- AI：LLM 工具调用（Function Calling）+ 轻量 RAG + SSE 流式（默认 mock 模式，无密钥可跑）
- 统一响应：所有接口返回 `{"code": 0, "message": "success", "data": {}}`

---

## 一、功能概览

| 角色 | 说明 | 主要能力 |
| --- | --- | --- |
| admin（管理员） | 村/社区管理人员 | 创建签到活动、管理全部数据、查看统计报表、审批请假、生成活动简报 |
| staff（工作人员） | 现场协助人员 | 协助管理员进行签到操作（含手动签到备用）、查看统计报表、生成活动简报 |
| family（家庭用户） | 以户主为单位注册 | 管理家庭成员、人脸录入、人脸签到、提交请假、向 AI 助手查询本户数据 |

核心业务规则：

- 家庭以户主为单位注册，户主可添加 / 编辑 / 删除家庭成员（阶段五起"删除"为**停用**，保留签到历史）
- 每个家庭成员可单独设置是否需要签到
- 签到活动由管理员创建，指定时间范围与迟到阈值
- 签到方式：人脸识别签到（主）、手动签到（备用）
- 请假需提前申请，由管理员审批
- 签到状态：`signed`（已签到）、`late`（迟到）、`absent`（缺勤）、`leave`（请假）、`abnormal`（异常）

AI 智能助手（阶段七）核心规则：

- **回答必须基于平台真实数据**：涉及活动、签到、请假、统计的问题一律先调用工具查询，
  系统提示词明确禁止编造数字；每次对话都返回工具调用轨迹（名称 / 入参 / 结果摘要）供审计
- **权限与接口完全一致**：工具内部复用既有 service（家庭用户只能取到本户数据，
  统计类工具对家庭用户返回「无权限」提示），不绕过权限裸查 SQL
- **无密钥可用**：`LLM_PROVIDER=mock`（默认）即可跑通"对话 → 工具调用 → 真实数据回答 → 简报生成"全流程
- **一活动一份简报**：`activity_summary` 按活动唯一，重复生成覆盖更新；模型输出不规范时降级为模板拼接并标注「降级」

---

## 二、技术栈

| 分类 | 技术 | 版本/说明 |
| --- | --- | --- |
| 语言 | Python | 3.11+（当前开发环境 3.13.9） |
| Web 框架 | FastAPI | 0.115+ |
| ASGI 服务器 | Uvicorn | 0.30+ |
| ORM | SQLAlchemy | 2.0（2.0 声明式风格） |
| 数据库 | MySQL | 8.0，驱动 PyMySQL |
| 缓存 | Redis | 配置与依赖已预留，当前未接入使用 |
| 配置 | pydantic-settings | 从 `.env` / 环境变量加载 |
| 认证 | python-jose + passlib | JWT 签发校验 + bcrypt 密码加密 |
| 人脸识别 | 百度 AI 人脸识别 API（V3） | 已接入，可用 `FACE_PROVIDER=local` 在无密钥时联调 |
| LLM（AI 助手） | OpenAI 兼容接口（httpx 直调） | DeepSeek / 豆包方舟 / 通义等均可，`LLM_PROVIDER=mock` 无密钥可跑 |
| 演示前端 | 原生 HTML/CSS/JS | 与后端同源托管（web/），零构建 |
| 前端（后期） | Vue 3 + Element Plus | 管理后台 |
| 家庭端（后期） | 微信小程序 | 家庭用户端 |

---

## 三、目录结构

```
Attendance Management/
├── src/
│   ├── auth/            # 登录注册、JWT 认证、权限
│   │   ├── security.py       # bcrypt 密码哈希、JWT 签发与解析
│   │   ├── dependencies.py   # 鉴权依赖（CurrentUser / AdminUser / require_roles）
│   │   ├── schemas.py        # 登录、刷新令牌的请求/响应模型
│   │   ├── service.py        # 注册 / 登录 / 刷新令牌业务逻辑
│   │   └── router.py         # 认证接口
│   ├── user/            # 用户管理
│   │   ├── models.py         # sys_user 表、角色与状态枚举
│   │   ├── schemas.py        # 用户请求/响应模型与密码校验规则
│   │   ├── crud.py           # 数据访问层
│   │   ├── service.py        # 业务逻辑与权限规则
│   │   └── router.py         # 用户管理接口
│   ├── family/          # 家庭管理
│   │   ├── models.py         # family 表（一户主一家庭）与家庭状态枚举
│   │   ├── schemas.py        # 家庭请求/响应模型
│   │   ├── crud.py           # 数据访问层（含成员数聚合子查询）
│   │   ├── service.py        # 业务逻辑、权限规则、注册自动建档入口
│   │   └── router.py         # 家庭管理接口
│   ├── member/          # 家庭成员管理
│   │   ├── models.py         # family_member 表与性别/关系/状态枚举
│   │   ├── id_card.py        # 身份证号校验与性别、出生日期解析
│   │   ├── schemas.py        # 成员请求/响应模型
│   │   ├── crud.py           # 数据访问层
│   │   ├── service.py        # 业务逻辑与权限规则
│   │   └── router.py         # 家庭成员接口
│   ├── face/            # 人脸识别
│   │   ├── models.py         # face_record 表（与成员一对一）
│   │   ├── schemas.py        # 人脸请求/响应模型
│   │   ├── storage.py        # 照片格式（魔数）校验与落盘
│   │   ├── baidu_client.py   # 百度 AI 人脸识别 V3 客户端
│   │   ├── provider.py       # 提供方抽象（百度 / 本地模式）与依赖工厂
│   │   ├── crud.py           # 数据访问层
│   │   ├── service.py        # 业务逻辑、权限规则与照片编排
│   │   └── router.py         # 人脸识别接口
│   ├── event/           # 签到活动管理（阶段五）
│   │   ├── models.py         # event 表与活动状态枚举（pending/active/finished/cancelled）
│   │   ├── schemas.py        # 活动请求/响应模型
│   │   ├── crud.py           # 数据访问层
│   │   ├── service.py        # 业务逻辑与状态机（结束时联动生成缺勤/请假记录）
│   │   └── router.py         # 签到活动接口
│   ├── checkin/         # 签到业务（阶段五）
│   │   ├── models.py         # checkin_record 表与签到方式/状态枚举
│   │   ├── schemas.py        # 签到记录、汇总统计、活动明细模型
│   │   ├── crud.py           # 数据访问层（含应签到成员查询与状态分组统计）
│   │   ├── service.py        # 签到窗口、迟到判定、幂等、缺勤生成、统计
│   │   └── router.py         # 人脸/手动签到、记录列表、明细、修正
│   ├── leave/           # 请假管理（阶段五）
│   │   ├── models.py         # leave_request 表与请假状态枚举
│   │   ├── schemas.py        # 请假请求/响应模型
│   │   ├── crud.py           # 数据访问层（含"待审批/已通过"判重）
│   │   ├── service.py        # 提交、审批、撤销与权限规则
│   │   └── router.py         # 请假管理接口
│   ├── statistics/      # 统计报表（阶段六，纯读、不建表）
│   │   ├── schemas.py        # 报表响应模型（总览/单活动/家庭排行/趋势）
│   │   ├── crud.py           # 聚合查询（计数、状态分布、应签到人数、报表行）
│   │   ├── service.py        # 口径与编排（出勤率计算、CSV 导出文本）
│   │   └── router.py         # 统计接口（总览/单活动/排行/趋势/导出）
│   ├── log/             # 操作日志（阶段六，审计）
│   │   ├── models.py         # operation_log 表（用户名快照 + 模块/操作/对象/IP）
│   │   ├── schemas.py        # 日志响应模型
│   │   ├── crud.py           # 数据访问层（写入默认只 flush，随调用方事务提交）
│   │   ├── context.py        # 请求来源 IP 上下文（纯 ASGI 中间件）
│   │   ├── service.py        # 埋点入口 record_operation + 查询/清理
│   │   └── router.py         # 日志接口（列表/详情/清理）
│   ├── assistant/       # AI 智能助手（阶段七：LLM / Agent / AIGC）
│   │   ├── models.py         # ai_conversation / ai_message / ai_knowledge / activity_summary
│   │   ├── schemas.py        # 对话、流式事件、会话消息、简报的请求/响应模型
│   │   ├── crud.py           # 数据访问层（会话分页、消息级联删除、知识库、简报 upsert）
│   │   ├── provider.py       # LLM 提供方抽象（OpenAI 兼容 / Mock）与依赖工厂
│   │   ├── service.py        # 系统提示词、Agent 工具循环、7 个工具、RAG 检索、简报 AIGC
│   │   └── router.py         # /api/assistant 接口（对话 / SSE 流式 / 会话 / 简报）
│   ├── common/          # 公共工具
│   │   ├── database.py       # 引擎 / Session / Base / BaseModel / init_db
│   │   ├── models.py         # 可移植枚举列类型（enum_column）
│   │   ├── response.py       # 统一响应工具与业务状态码
│   │   ├── exceptions.py     # BusinessError 业务异常
│   │   ├── schemas.py        # ApiResponse / PageData 响应模型
│   │   └── utils.py          # LIKE 通配符转义等小工具
│   └── config/
│       └── settings.py  # 全局配置（数据库、Redis、JWT、人脸识别、LLM 等）
├── tests/               # 单元测试（pytest，491 个用例，内存 SQLite 离线运行）
│   ├── conftest.py      # 内存 SQLite + TestClient + 用户/家庭/成员/人脸/活动/成员工厂夹具
│   ├── helpers.py       # 测试常量与统一响应断言
│   ├── baidu_mock.py    # 百度人脸识别服务的离线段测试替身（httpx.MockTransport）
│   ├── test_health.py   # 阶段一：健康检查、演示前端页面、404 统一格式
│   ├── test_database.py # 建表与模型注册回归测试
│   ├── test_auth.py     # 阶段二：注册、登录、令牌、鉴权
│   ├── test_user.py     # 阶段二：用户管理、权限、密码
│   ├── test_family.py   # 阶段三：自动建档、家庭管理、权限、停用
│   ├── test_member.py   # 阶段三：成员增删改查、身份证校验、统计
│   ├── test_face.py     # 阶段四：人脸录入/更新/删除/搜索、照片、权限
│   ├── test_event.py    # 阶段五：活动创建/修改/删除、状态机、结束联动
│   ├── test_checkin.py  # 阶段五：人脸/手动签到、窗口与迟到、幂等、汇总、修正
│   ├── test_leave.py    # 阶段五：请假提交/审批/撤销、权限、与签到联动
│   ├── test_statistics.py # 阶段六：总览/单活动/家庭排行/趋势/CSV 导出
│   ├── test_log.py      # 阶段六：埋点事务语义、日志查询/清理、权限矩阵
│   └── test_assistant.py # 阶段七：Mock/OpenAI 提供方、工具调用、隔离、流式、简报（离线）
├── docs/                # 项目文档（api.md / database.md）
├── scripts/
│   ├── create_admin.py  # 建表 + 创建/重置管理员账号
│   ├── seed_ai_knowledge.py # AI 知识库种子（平台使用指南 20 条 Q&A，幂等）
│   └── cleanup_test_data.py # 测试数据清理（--purge-test-generated --yes 清理 tst 前缀测试账号/家庭/成员/人脸/活动/签到/请假/AI 数据）
├── uploads/             # 运行时生成：人脸照片（已 gitignore，不对外暴露）
├── web/                 # 演示前端（原生 HTML/CSS/JS，与后端同源托管，零构建）
│   ├── index.html       # 单页演示：登录 / 注册 / 总览 / 活动签到 / 请假管理 / AI 智能助手
│   ├── app.js           # 前端逻辑（调用 /api/*，含人脸签到、CSV 导出与 SSE 流式对话）
│   └── styles.css       # 样式
├── main.py              # 应用入口（根路径提供演示页面，/static 提供前端资源）
├── test_dsh_face_full.py # 集成测试（15 个用例，真实 MySQL + 百度人脸，需先启动后端服务）
├── requirements.txt     # 依赖清单
├── .env.example         # 环境变量示例
├── .gitignore
└── README.md
```

---

## 四、环境要求

- Python 3.11 及以上（已在 Python 3.13.9 上验证）
- MySQL 8.0（阶段二起登录/注册/用户管理需要，启动服务本身不依赖）
- Redis 5.0+（当前未使用；令牌黑名单、验证码、热点数据缓存将在后续阶段接入）

---

## 五、安装步骤

### 1. 创建并激活虚拟环境

项目根目录已内置 `.venv`，可直接使用；若需重建：

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 安装依赖

```powershell
# 激活虚拟环境后
pip install -r requirements.txt

# 或直接使用虚拟环境中的解释器（无需激活）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 配置环境变量

```powershell
copy .env.example .env      # Windows
# cp .env.example .env      # Linux / macOS
```

然后按实际环境修改 `.env`，至少需要修改：

| 变量 | 说明 |
| --- | --- |
| `DB_USER` / `DB_PASSWORD` | MySQL 账号密码 |
| `DB_NAME` | 数据库名，默认 `attendance` |
| `JWT_SECRET_KEY` | 生产环境必须替换为随机长字符串（`openssl rand -hex 32`） |
| `CORS_ORIGINS` | 前端管理后台地址，多个用英文逗号分隔 |
| `BAIDU_FACE_API_KEY` / `BAIDU_FACE_SECRET_KEY` | 百度智能云人脸识别应用的 AK/SK（阶段四） |
| `FACE_PROVIDER` | 人脸识别提供方：`baidu`（默认）或 `local`（无密钥时联调，不做比对） |
| `LLM_PROVIDER` | AI 助手提供方：`mock`（默认，无密钥可跑）或 `openai_compat`（阶段七） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 使用真实大模型时的接口地址、密钥与模型名（如 `https://api.deepseek.com/v1` + `deepseek-chat`） |

> 环境变量优先级高于 `.env` 文件，`.env` 已被 `.gitignore` 忽略，不会提交到版本库。

### 4. 创建数据库并初始化

```sql
CREATE DATABASE IF NOT EXISTS attendance
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;
```

然后执行初始脚本：**建表 + 创建管理员账号**（管理员无法通过注册接口创建，必须用脚本）：

```powershell
# 先检查数据库连通性（凭据取自 .env）
.\.venv\Scripts\python.exe -c "from src.common.database import ping_database; print(ping_database())"

# 建表 + 交互式输入密码创建管理员（密码不会进入命令行历史）
.\.venv\Scripts\python.exe scripts\create_admin.py --username admin

# 常用参数：--password 直接指定密码；--force 重置已存在管理员的密码
.\.venv\Scripts\python.exe scripts\create_admin.py --help
```

> 家庭用户无需脚本，直接调用 `POST /api/auth/register` 自行注册。

Redis 目前未使用（阶段二无状态 JWT 不需要缓存），如需一并启动：`redis-server`
（Windows 建议使用 WSL 或 Memurai）。

---

## 六、启动方式

```powershell
# 方式一：直接运行（读取 .env 中的 HOST / PORT / DEBUG）
.\.venv\Scripts\python.exe main.py

# 方式二：使用 uvicorn（开发热重载）
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

启动后可访问：

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:8001/ | 演示前端页面（登录 / 活动签到 / 请假 / AI 智能助手） |
| http://127.0.0.1:8001/health | 健康检查 |
| http://127.0.0.1:8001/docs | Swagger 交互式接口文档 |
| http://127.0.0.1:8001/redoc | ReDoc 接口文档 |

健康检查返回：

```json
{
  "code": 0,
  "message": "success",
  "data": {"status": "ok"}
}
```

### 运行测试

```powershell
# 全部单元用例（491 个，无需 MySQL / Redis：使用内存 SQLite 并自动建表）
.\.venv\Scripts\python.exe -m pytest tests/ -v

# 只跑某个模块
.\.venv\Scripts\python.exe -m pytest tests/test_auth.py -v

# 集成测试（真实 MySQL + 真实百度人脸识别，15 个用例，**需先启动后端服务**）
.\.venv\Scripts\python.exe -m pytest test_dsh_face_full.py -v
```

预期结果：`491 passed`，可能附带若干第三方库 `DeprecationWarning`
（starlette / anyio / pytest 内部提示，非业务代码问题）。

> - 单元测试全部离线：AI 助手模块的测试**完全不联网**（LLM 一律使用 `MockLLMProvider`，
>   OpenAI 兼容实现用 `httpx.MockTransport` 做传输层替身），人脸模块用 `tests/baidu_mock.py` 替身。
> - 时间敏感用例（如签到趋势）显式传入活动覆盖的日期范围，**任何时刻运行结果一致**。
> - 集成测试 `test_dsh_face_full.py`（项目根目录）依赖真实环境：
>   MySQL 已启动、`.env` 中的百度密钥有效、桌面测试照片就位（可用 `TEST_IMG_*` 环境变量覆盖路径）、
>   后端已启动并将 `TEST_BASE_URL` 指向该服务（例如 `$env:TEST_BASE_URL="http://127.0.0.1:8001"`，
>   与本文档其余示例端口一致；不设置时脚本使用自带的默认地址）；用例自带清理，可重复运行。

---

## 七、接口规范

- 所有接口遵循 RESTful 风格，业务接口统一以 `/api/` 开头（前缀由 `API_PREFIX` 配置）
- 所有响应（含异常）均为统一格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {}
}
```

业务状态码定义（`src/common/response.py` 中的 `ResponseCode`）：

| code | 含义 | 说明 |
| --- | --- | --- |
| 0 | 成功 | 业务处理成功 |
| 400 | 参数错误 | 参数校验失败、原密码错误等 |
| 401 | 未认证 | 未登录、令牌无效/过期/类型错误 |
| 403 | 无权限 | 角色权限不足、账号被禁用、只能操作本人数据 |
| 404 | 资源不存在 | 路径或数据不存在 |
| 405 | 方法不允许 | HTTP 方法错误 |
| 409 | 资源冲突 | 用户名/手机号重复 |
| 429 | 请求过于频繁 | 触发限流 |
| 500 | 服务器错误 | 未捕获异常 |

异常响应策略：

| 场景 | HTTP 状态码 | 响应体 `code` |
| --- | --- | --- |
| 成功 | 200 | 0 |
| 参数校验失败（Pydantic，如字段缺失、密码过短） | 200 | 400 |
| 业务异常（`BusinessError`，如原密码错误、用户名重复） | 与 `code` 一致（400/401/403/404/409） | 同左 |
| 协议层错误（路由不存在 404、方法不允许 405） | 原始状态码 | 同左 |
| 未捕获异常 | 500 | 500（`DEBUG=true` 时返回异常信息，生产环境仅返回通用提示） |

> 这样前端既能用 HTTP 状态码做统一拦截（401 跳登录页），也能用 `code` 做业务分支判断。

### 在代码中使用统一响应

```python
from src.common.response import ResponseCode, error_response, success_response


def demo_ok() -> dict:
    """成功响应：{code: 0, message: success, data: {...}}。"""
    return success_response(data={"status": "ok"})


def demo_message(user: dict) -> dict:
    """成功响应 + 自定义提示消息。"""
    return success_response(data=user, message="登录成功")


def demo_error() -> dict:
    """业务错误响应：HTTP 状态码与业务码一致（此处 401）。"""
    return error_response(message="用户名或密码错误", code=ResponseCode.UNAUTHORIZED)
```

### 抛出业务异常

```python
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode


def demo_conflict() -> None:
    """用户名已占用：自动转换为统一响应，HTTP 状态码与业务码一致（409）。"""
    raise BusinessError("用户名已被占用", code=ResponseCode.CONFLICT)


def demo_forbidden() -> None:
    """账号被禁用：HTTP 状态码与业务码一致（403）。"""
    raise BusinessError("账号已被禁用，请联系管理员", code=ResponseCode.FORBIDDEN)
```

### 在接口中校验登录与角色

```python
from fastapi import APIRouter, Depends

from src.auth.dependencies import AdminUser, CurrentUser, require_roles
from src.common.response import success_response
from src.user.models import User, UserRole

router = APIRouter()


@router.get("/profile")
def profile(current_user: CurrentUser) -> dict:
    """任何已登录用户可访问，current_user 为 ORM 用户对象。"""
    return success_response(data={"username": current_user.username})


@router.get("/users")
def list_users(current_user: AdminUser) -> dict:
    """仅管理员可访问，其他角色自动返回 403。"""
    return {"message": f"管理员 {current_user.username} 已通过鉴权"}


# 自定义角色组合：require_roles(UserRole.ADMIN, UserRole.STAFF) 作为 Depends 使用
@router.get("/staff-only")
def staff_only(current_user: User = Depends(require_roles(UserRole.ADMIN, UserRole.STAFF))) -> dict:
    """管理员或工作人员可访问（自定义角色组合示例）。"""
    return success_response(data={"username": current_user.username})
```

### 使用数据库会话

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.common.database import get_db
from src.family.models import Family

router = APIRouter()


@router.get("/families")
def list_families(db: Session = Depends(get_db)) -> int:
    """示例：依赖注入数据库会话后执行查询。"""
    return db.query(Family).count()
```

### 定义 ORM 模型

```python
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel


class Family(BaseModel):
    """家庭表：继承 BaseModel 自动获得 id / created_at / updated_at 字段。"""

    __tablename__ = "family"

    household_name: Mapped[str] = mapped_column(String(50), nullable=False)
```

---

## 八、已实现接口

### 认证（`/api/auth`）

| 方法 | 路径 | 说明 | 需登录 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 家庭用户（户主）注册，角色固定为 family | 否 |
| POST | `/api/auth/login` | 登录，返回 access_token（120 分钟）与 refresh_token（7 天） | 否 |
| POST | `/api/auth/refresh` | 用 refresh_token 换取新令牌（滑动续期） | 否 |
| GET | `/api/auth/profile` | 当前登录用户信息 | 是 |

### 用户管理（`/api/users`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/users` | 用户列表（分页 + 角色/状态筛选 + 关键字搜索） | 管理员 |
| POST | `/api/users` | 新增用户（可指定角色与状态） | 管理员 |
| GET | `/api/users/{user_id}` | 用户详情 | 本人或管理员 |
| PUT | `/api/users/{user_id}` | 修改用户信息 | 本人（姓名/手机号）或管理员（全部） |
| PUT | `/api/users/{user_id}/password` | 修改本人密码（需原密码） | 本人 |
| PUT | `/api/users/{user_id}/reset-password` | 重置密码（无需原密码） | 管理员 |
| PUT | `/api/users/{user_id}/status` | 启用/禁用账号 | 管理员（不可操作自己） |
| DELETE | `/api/users/{user_id}` | 删除账号 | 管理员（不可删除自己，户主需先停用家庭） |

### 家庭管理（`/api/families`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/families` | 为指定户主创建家庭档案（含户主成员行） | 管理员 |
| GET | `/api/families` | 家庭列表（分页 + 状态/村组筛选 + 关键字搜索） | 管理员 / 工作人员 |
| GET | `/api/families/me` | 本人家庭详情（含成员列表） | 家庭用户（户主） |
| GET | `/api/families/{family_id}` | 家庭详情（含成员列表） | 户主本人 / 管理员 / 工作人员 |
| PUT | `/api/families/{family_id}` | 修改家庭信息（户号/住址/村组/电话/备注） | 户主本人 / 管理员 |
| DELETE | `/api/families/{family_id}` | 停用家庭档案（级联停用成员，不物理删除） | 管理员 |

> 户主通过 `POST /api/auth/register` 注册时**自动创建家庭档案与户主成员记录**（同一事务），
> 户号形如 `F000001`，也可在注册请求的 `family` 字段中指定真实户籍号。

### 家庭成员（`/api/members`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/members` | 添加家庭成员（填写身份证号可自动识别性别/出生日期） | 户主本人 / 管理员（需指定 family_id） |
| GET | `/api/members` | 成员列表（分页 + 状态/是否需签到筛选 + 关键字搜索） | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/members/{member_id}` | 成员详情 | 户主本人 / 管理员 / 工作人员 |
| PUT | `/api/members/{member_id}` | 修改成员信息 | 户主本人 / 管理员 |
| PUT | `/api/members/{member_id}/status` | 启用/停用成员 | 户主本人 / 管理员 |
| DELETE | `/api/members/{member_id}` | 删除成员（阶段五起语义为**停用**，保留签到历史与人脸记录） | 户主本人 / 管理员 |

### 人脸识别（`/api/face`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/face/register` | 录入人脸（multipart 上传照片，含人脸检测） | 户主本人 / 管理员 |
| POST | `/api/face/update` | 重新录入（更新）人脸 | 户主本人 / 管理员 |
| POST | `/api/face/delete` | 删除人脸（保留照片与记录以便追溯） | 户主本人 / 管理员 |
| POST | `/api/face/search` | 人脸搜索（1:N 识别，用于现场签到） | 管理员 / 工作人员 |
| GET | `/api/face/status/{member_id}` | 成员人脸录入状态 | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/face/records` | 人脸录入记录列表（分页/按家庭筛选） | 户主本人 / 管理员 / 工作人员 |
| GET | `/api/face/photo/{member_id}` | 读取人脸照片（图片流，需鉴权） | 户主本人 / 管理员 / 工作人员 |

> 人脸识别默认使用百度 AI 人脸库（`FACE_PROVIDER=baidu`），需在 `.env` 配置
> `BAIDU_FACE_API_KEY` / `BAIDU_FACE_SECRET_KEY`；未申请到密钥时可设 `FACE_PROVIDER=local` 联调
> （仅保存照片与录入状态，**不做人脸比对**，生产环境禁止）。

完整请求/响应示例与错误码见 [`docs/api.md`](docs/api.md)，表结构见 [`docs/database.md`](docs/database.md)。

### 签到活动（`/api/events`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/events` | 创建签到活动（名称/地点/起止时间/迟到阈值） | 管理员 |
| GET | `/api/events` | 活动列表（分页 + 状态筛选 + 关键字搜索） | 登录用户 |
| GET | `/api/events/{event_id}` | 活动详情 | 登录用户 |
| PUT | `/api/events/{event_id}` | 修改活动（已结束/已取消返回 409） | 管理员 |
| DELETE | `/api/events/{event_id}` | 删除活动（仅未开始且无签到/请假记录） | 管理员 |
| PUT | `/api/events/{event_id}/status` | 状态迁移：开始 / 结束 / 取消 | 管理员 |

> 状态机：`pending` →「开始」→ `active` →「结束」→ `finished`，或 `pending` →「取消」→ `cancelled`；
> 非法迁移返回 409。**活动结束时自动为"应签到但无签到记录"的成员生成缺勤/请假记录**
> （已通过请假记 `leave`，其余记 `absent`），与活动状态在同一次事务内提交。

### 签到业务（`/api/checkins`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/checkins/face` | 人脸签到（multipart：`event_id` + 现场照片） | 管理员 / 工作人员 |
| POST | `/api/checkins/manual` | 手动签到（`event_id` + `member_id`） | 管理员 / 工作人员 |
| GET | `/api/checkins` | 签到记录列表（分页 + 活动/家庭/状态/姓名筛选） | 登录用户（家庭用户仅本户） |
| GET | `/api/checkins/events/{event_id}` | 活动签到明细 + 汇总统计（出勤率） | 登录用户（家庭用户仅本户） |
| PUT | `/api/checkins/{checkin_id}` | 人工修正签到记录（记录修正人与时间） | 管理员 |

> 人脸签到复用阶段四的 1:N 人脸识别；签到需在活动时间窗内（否则 400「活动尚未开始 / 活动已结束」），
> 超过 `late_threshold_minutes` 判定为迟到；一成员一活动一条记录（重复签到 409）。

### 请假管理（`/api/leaves`）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/leaves` | 提交请假申请（`event_id` + `member_id` + `reason`） | 户主（本户成员）/ 管理员（任意成员） |
| GET | `/api/leaves` | 请假列表（分页 + 状态/活动/家庭筛选） | 管理员 / 工作人员（全部）、户主（本户） |
| GET | `/api/leaves/{leave_id}` | 请假详情 | 户主（本户）/ 管理员 / 工作人员 |
| PUT | `/api/leaves/{leave_id}/approve` | 审批（`approved` / `rejected`） | 管理员 |
| PUT | `/api/leaves/{leave_id}/cancel` | 撤销申请（仅待审批可撤销） | 户主本人 / 管理员 |

> 同一成员同一活动同时只能有一条「待审批/已通过」的请假（重复提交 409）；
> 已通过的请假若该成员实际到场签到，以实际签到记录为准。

### 统计报表（`/api/statistics`，纯读）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/statistics/overview` | 总览（家庭/成员/活动/签到次数/平均出勤率） | 管理员 / 工作人员 |
| GET | `/api/statistics/events/{event_id}` | 单活动统计（活动 + 汇总 + 家庭维度分页） | 管理员 / 工作人员 |
| GET | `/api/statistics/families` | 家庭参与度排行（分页、村组/关键字筛选、三种排序） | 管理员 / 工作人员 |
| GET | `/api/statistics/trend` | 按活动时间的签到趋势（默认最近 30 天） | 管理员 / 工作人员 |
| GET | `/api/statistics/export` | 导出 CSV（`type=events` / `type=families`） | 管理员 / 工作人员 |

> 统计接口不建表、不写库，全部由聚合查询实时计算；出勤率 = `(signed + late) / 应签到人数`。
> 家庭排行仅统计**参与过已结束活动**的家庭。CSV 导出为 UTF-8 BOM 编码，
> 中文文件名按 RFC 5987 编码，Excel 直接打开不乱码，**不引入任何新依赖**。

### 操作日志（`/api/logs`，审计）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/logs` | 日志列表（分页 + 操作人/模块/操作/时间范围/关键字筛选） | 管理员 |
| GET | `/api/logs/{log_id}` | 日志详情 | 管理员 |
| DELETE | `/api/logs/clean` | 清理 `before` 日期之前的日志（返回删除条数） | 管理员 |

> 日志在关键操作时**自动写入**（活动的创建/修改/删除/状态迁移、人脸与手动签到、
> 签到记录修正、请假审批），与业务变更同一次事务提交，**不改变任何接口的响应结构**；
> 家庭用户的自助行为不埋点。日志记录操作人用户名快照与来源 IP，账号删除后仍可追溯。

### AI 智能助手（`/api/assistant`，阶段七）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/assistant/chat` | 多轮对话（工具调用轨迹随响应返回） | 登录用户 |
| POST | `/api/assistant/chat/stream` | 同上，SSE 流式（`start` → `tool`* → `token`* → `done`） | 登录用户 |
| GET | `/api/assistant/conversations` | 本人会话列表（分页，最近活跃在前） | 登录用户（仅本人） |
| GET | `/api/assistant/conversations/{id}/messages` | 本人会话消息（分页，时间正序） | 登录用户（仅本人） |
| DELETE | `/api/assistant/conversations/{id}` | 删除本人会话（级联删除消息） | 登录用户（仅本人） |
| POST | `/api/assistant/events/{event_id}/summary` | 生成活动简报（AIGC，一活动一份） | 管理员 / 工作人员 |
| GET | `/api/assistant/events/{event_id}/summary` | 读取已保存的活动简报 | 管理员 / 工作人员 |

> **能力**：多轮对话与记忆（消息落库，可续聊）、工具调用（Function Calling）、
> 角色权限（与接口同一条权限链）、SSE 流式输出、活动简报生成（AIGC）、
> 平台指南检索（轻量 RAG，关键词打分 + 二元组召回）。
>
> **可用工具**：`get_events`、`get_event_detail`、`get_event_summary`（家庭用户仅本户汇总）、
> `get_leave_status`（家庭用户仅本户）、`get_statistics_overview` 与 `get_families_ranking`
> （仅管理员 / 工作人员，家庭用户得到「无权限」结果串）、`search_knowledge`。
> 全部复用既有 service，**不绕过权限裸查 SQL**。
>
> **默认无需密钥**：`LLM_PROVIDER=mock` 时，命中意图的问题（问活动、签到、请假、统计、
> 使用指南）会自动触发**真实的工具调用**并回复「根据查询结果：…」；
> 未命中意图的闲聊回复「（mock 模式）已收到你的问题，配置真实 LLM key 后可获得智能回答」。
> 也就是说，无密钥即可完整演示工具调用、真实数据查询、SSE 流式与简报生成；
> 接入真实模型见下方「AI 智能助手使用说明」。

完整请求/响应示例与错误码见 [`docs/api.md`](docs/api.md)，表结构见 [`docs/database.md`](docs/database.md)。

### 登录联调示例

```powershell
# 1) 管理员登录
$login = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
    -ContentType 'application/json' -Body '{"username":"admin","password":"你的密码"}'

# 2) 携带令牌访问受保护接口
$headers = @{ Authorization = "Bearer $($login.data.access_token)" }
Invoke-RestMethod http://127.0.0.1:8001/api/auth/profile -Headers $headers

# 3) 家庭用户注册（无需登录）
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/register `
    -ContentType 'application/json' `
    -Body '{"username":"zhangsan","password":"zhangsan123456","real_name":"张三"}'

# 4) 人脸录入（multipart，PowerShell 用 curl.exe）
curl.exe -X POST http://127.0.0.1:8001/api/face/register `
    -H "Authorization: Bearer $($login.data.access_token)" `
    -F "member_id=2" -F "file=@C:\photos\lisi.jpg"
```

---

## 九、AI 智能助手使用说明

### 1. 能力一览

| 能力 | 说明 |
| --- | --- |
| 多轮对话 + 会话记忆 | 会话与消息全部落库（`ai_conversation` / `ai_message`），服务重启后可续聊；上下文取最近 12 条消息 |
| 工具调用（Function Calling） | 模型自主决定调用哪个工具（活动 / 签到 / 请假 / 统计 / 知识库），结果回填后再作答，响应中带完整调用轨迹 |
| 角色权限 | 工具内部复用既有 service：家庭用户只能取本户数据，统计类工具对家庭用户返回「无权限」提示，**绝不绕过权限裸查 SQL** |
| SSE 流式输出 | `POST /api/assistant/chat/stream`：`start` → `tool`* → `token`* → `done`；工具循环走非流式（完整拿到 `tool_calls`），最终文本再逐段下发 |
| 活动简报生成（AIGC） | `POST /api/assistant/events/{id}/summary`：真实统计快照 → 要求模型返回严格 JSON（title / highlights / body）→ 解析失败降级为模板拼接并标注「降级」→ 一活动一份 upsert |
| 平台指南检索（轻量 RAG） | `ai_knowledge` 表 20 条 Q&A（`scripts/seed_ai_knowledge.py` 写入），按关键词命中数打分 + 二元组召回取 top-k，注入系统提示词并作为 `search_knowledge` 工具 |

### 2. 两种运行模式

| 模式 | 配置 | 效果 |
| --- | --- | --- |
| mock（默认） | 不改 `.env` | 无密钥全流程可用：**意图识别会自动触发真实的工具调用**（问「我家签到情况」→ 先 `get_events` 取活动ID、再 `get_event_summary` 取真实汇总），最终回复引用工具结果；没有命中意图的闲聊回复配置提示 |
| 真实模型 | `LLM_PROVIDER=openai_compat` + `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 由大模型自主决定调用哪些工具并组织语言，回答依然只依据工具返回的真实数据 |

> mock 模式的回答模板只有两句：未命中意图时是
> 「（mock 模式）已收到你的问题，配置真实 LLM key 后可获得智能回答」，
> 有工具结果时是「根据查询结果：{工具结果前 100 字}」——
> 这样即使**没有任何密钥**，也能在演示中看到真实的工具调用链与真实业务数据。

配置真实模型（**只需改 `.env`，不要改代码**；`.env` 已 gitignore）：

```ini
# DeepSeek（推荐，OpenAI 兼容）
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的密钥
LLM_MODEL=deepseek-chat

# 豆包（火山方舟）示例
# LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
# LLM_MODEL=doubao-pro-32k
# （模型名也可填方舟的接入点 ID，如 ep-2024xxxx）

# 可选调参
LLM_TIMEOUT_SECONDS=30    # 单次请求超时
AI_TOOL_MAX_ROUNDS=4      # 单轮对话最多工具调用轮数
AI_KNOWLEDGE_TOP_K=3      # 注入提示词的知识库条数
```

改完重启服务即可（启动日志会提示当前是否为 mock 模式）。`LLM_PROVIDER=openai_compat`
但未配置完整时，`/chat` 返回 503 并给出中文提示。

### 3. 初始化知识库（幂等）

```powershell
# 建表（若尚未建）+ 写入 20 条平台使用指南 Q&A；重复执行会跳过已存在条目
.\.venv\Scripts\python.exe scripts\seed_ai_knowledge.py

# 内容更新后重灌
.\.venv\Scripts\python.exe scripts\seed_ai_knowledge.py --force

# 只看会写入什么，不落库
.\.venv\Scripts\python.exe scripts\seed_ai_knowledge.py --dry-run
```

### 4. curl 演示示例

```powershell
# 0) 登录拿令牌（管理员可查全村，户主只能查本户）
$login = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
    -ContentType 'application/json' -Body '{"username":"admin","password":"你的密码"}'
$token = $login.data.access_token

# 1) 对话 + 工具调用（mock 模式下也会真实调用工具：先 get_events 拿活动ID，再 get_event_summary 取汇总）
curl.exe -s -X POST http://127.0.0.1:8001/api/assistant/chat `
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" `
    -d "{\"message\":\"我家这次活动的签到情况怎么样？\"}"
# 响应中的 tool_calls 会给出本轮调用的工具、入参与结果摘要（实测输出），例如：
# {"code":0,"message":"success","data":{"conversation_id":1,
#  "reply":"根据查询结果：活动「村民大会」（已结束）本户签到汇总：应签到 2 人｜已签到 1…",
#  "tool_calls":[{"name":"get_events","args":{"page_size":5},"result":"共 1 个活动（第 1 页，每页 5 条）：…"},
#                {"name":"get_event_summary","args":{"event_id":1},"result":"活动「村民大会」…"}]}}

# 1.1) 问统计类问题（仅管理员/工作人员；家庭用户会得到「无权限查看统计报表」的如实答复）
curl.exe -s -X POST http://127.0.0.1:8001/api/assistant/chat `
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" `
    -d "{\"message\":\"全村统计总览怎么样？\"}"

# 2) 续聊（带上 conversation_id 即为多轮对话，助手记得上下文）
curl.exe -s -X POST http://127.0.0.1:8001/api/assistant/chat `
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" `
    -d "{\"message\":\"那第一名是哪一户？\",\"conversation_id\":1}"

# 3) SSE 流式（-N 关闭缓冲，逐条打印事件；可观察 start → tool → token → done）
curl.exe -N -X POST http://127.0.0.1:8001/api/assistant/chat/stream `
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" `
    -d "{\"message\":\"最近有什么活动？\"}"

# 4) 生成活动简报（管理员/工作人员；event_id 换成真实活动ID）
curl.exe -s -X POST http://127.0.0.1:8001/api/assistant/events/1/summary `
    -H "Authorization: Bearer $token"

# 5) 读取已保存的简报 / 会话列表 / 会话消息
curl.exe -s http://127.0.0.1:8001/api/assistant/events/1/summary -H "Authorization: Bearer $token"
curl.exe -s "http://127.0.0.1:8001/api/assistant/conversations?page=1&page_size=10" -H "Authorization: Bearer $token"
curl.exe -s "http://127.0.0.1:8001/api/assistant/conversations/1/messages" -H "Authorization: Bearer $token"

# 6) 知识库类问题（轻量 RAG：命中「如何添加家庭成员」「人脸照片存哪里」等 Q&A）
curl.exe -s -X POST http://127.0.0.1:8001/api/assistant/chat `
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" `
    -d "{\"message\":\"人脸照片保存在哪里？\"}"
```

SSE 事件示例（`curl -N` 实际输出，每条事件后跟一个空行）：

```
data: {"type": "start", "conversation_id": 1}
data: {"type": "tool", "name": "get_event_summary", "args": {"event_id": 1}, "result": "活动「村民大会」（进行中）本户签到汇总：…"}
data: {"type": "token", "content": "根据查"}
data: {"type": "token", "content": "询结果"}
data: {"type": "done", "conversation_id": 1, "reply": "根据查询结果：…"}
```

> 前端建议：先按 `tool` 事件渲染"工具调用卡片"（让用户看到数据来源），
> 再按 `token` 事件做打字机输出，最后用 `done.reply` 落定最终文本。

### 5. 常见问题（AI 助手）

| 现象 | 原因与处理 |
| --- | --- |
| 回答是「（mock 模式）已收到你的问题…」 | 当前是 `LLM_PROVIDER=mock`，且这句话没有命中内置意图（如"签到情况/请假/统计/活动/怎么做"）。换一种问法即可看到工具调用，或配置真实 key 获得智能回答 |
| 回答是「根据查询结果：…」 | mock 模式下的标准行为：把工具结果前 100 字原样转述，用于验证"回答确实基于真实数据" |
| 家庭用户问「全村排行」得到「无权限查看统计报表」 | 预期行为：统计类工具仅管理员 / 工作人员可用，助手会如实说明只能查本户数据 |
| `/chat` 返回 503 | `LLM_PROVIDER=openai_compat` 但 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 未配全，或服务商返回错误（响应中带状态码） |
| 简报里出现「**（降级）**」 | 模型没有按要求返回 JSON（或调用失败），系统改用统计快照模板拼接——数据依然是真实的，`degraded=true` 便于前端标注 |
| 想改工具轮数上限 | 调整 `AI_TOOL_MAX_ROUNDS`（默认 4）；达到上限时助手会明确告知"已达到工具调用轮数上限"并给出已获取的信息 |

---

## 十、开发计划

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 阶段一 | 项目初始化：目录结构、应用入口、公共模块、配置模块、工程文件 | ✅ 已完成 |
| 阶段二 | 认证与用户：用户表、bcrypt 密码、JWT 令牌、鉴权依赖、用户管理、单元测试 | ✅ 已完成 |
| 阶段三 | 家庭与成员：family / family_member 表、注册自动建档、家庭与成员管理、身份证校验 | ✅ 已完成 |
| 阶段四 | 人脸识别：face_record 表、百度 AI 人脸库接入（录入/更新/删除/搜索）、照片管理、本地联调模式 | ✅ 已完成 |
| 阶段五 | 活动与签到：活动管理、人脸/手动签到、请假审批、缺勤生成与出勤统计 | ✅ 已完成 |
| 阶段六 | 统计报表与操作日志：总览/活动/家庭/趋势统计、CSV 导出、操作日志与审计埋点 | ✅ 已完成 |
| 阶段七 | AI 智能助手：多轮对话 + 工具调用 + 会话记忆 + 角色权限 + SSE 流式 + 活动简报 AIGC + 平台指南 RAG | ✅ 已完成 |
| 阶段八 | 演示前端（`web/`）：原生 HTML/CSS/JS 单页，与后端同源托管，覆盖登录注册、总览、活动签到、请假管理、AI 助手对话 | ✅ 已完成 |
| 阶段九 | Vue 3 管理后台 + 微信小程序家庭端 | ⏳ 待开发 |

各业务模块的路由注册位置已在 `main.py` 中预留，阶段二~阶段七的路由已放开注册；
后续阶段按同样方式取消对应注释即可。

---

## 十一、常见问题

**1. 启动时报 `pymysql` 相关错误**

未安装数据库驱动，执行 `pip install -r requirements.txt`。注意：启动服务**不会**主动连接数据库，
引擎为惰性连接，因此 MySQL 未启动也能正常访问 `/health`。

**2. 接口报 `(1045, "Access denied for user 'root'@'localhost'")`**

`.env` 中的 `DB_USER` / `DB_PASSWORD` 与本地 MySQL 不一致（服务能启动，但登录、注册等
涉及数据库的接口会失败）。请修正 `.env` 后验证：

```powershell
.\.venv\Scripts\python.exe -c "from src.common.database import ping_database; print(ping_database())"
```

**3. 接口报 `(1049, "Unknown database 'attendance'")`**

数据库未创建，执行：

```sql
CREATE DATABASE IF NOT EXISTS attendance DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
```

**4. 接口报 `(1146, "Table 'attendance.sys_user' doesn't exist")`**

数据表未创建，执行初始化脚本（同时创建管理员）：`.\.venv\Scripts\python.exe scripts\create_admin.py --username admin`

**5. 接口报「当前账号尚无家庭档案，请联系管理员创建」**

该账号不是家庭户主（管理员/工作人员没有家庭档案），或管理员创建的 `family` 角色用户尚未建档。
管理员可为指定户主补录：`POST /api/families`，请求体 `{"owner_id": 用户ID}`。
户主自行注册（`POST /api/auth/register`）会自动建档，无需此操作。

**6. 删除户主账号报 409「该账号是家庭户主」**

家庭档案通过外键保护着户主账号，需先停用家庭档案（`DELETE /api/families/{family_id}`），
再删除账号，避免出现"家庭无户主"的脏数据。

**7. 人脸接口报 503「未配置百度人脸识别密钥」**

在 `.env` 中填写 `BAIDU_FACE_API_KEY` / `BAIDU_FACE_SECRET_KEY`（百度智能云 → 人脸识别应用）；
若暂未申请密钥、只想联调前端流程，可设置 `FACE_PROVIDER=local`：此时照片与录入状态照常保存，
但**不做人脸比对**，搜索接口恒定返回"未识别"，生产环境（`ENV=production`）会拒绝启用该模式。

**8. 人脸录入报「照片中未检测到人脸」/「检测到多张人脸」**

录入前会先调用人脸检测（`FACE_DETECT_ON_REGISTER=true`），请使用单人正面、光照充足、
无遮挡的照片；如确认照片正常仍失败，可临时关闭该开关排查。

**9. 人脸照片存在哪里？可以直接用 URL 访问吗？**

照片保存在 `uploads/face/{家庭ID}/` 下（`FACE_UPLOAD_DIR` 可配置，已 gitignore）。
**`uploads/` 不会作为静态目录暴露**——那是全村的人脸数据，必须通过
`GET /api/face/photo/{member_id}` 携带令牌访问。修改 `FACE_UPLOAD_DIR` 后请重启服务。

**10. MySQL 8 报认证插件错误**

MySQL 8 默认 `caching_sha2_password`，需安装 `cryptography`（已在 `requirements.txt` 中）。
也可将用户改为 `mysql_native_password`。

**11. `passlib` 打印 bcrypt 版本告警**

`passlib 1.7.4` 与 `bcrypt >= 4.1` 存在兼容性告警，`requirements.txt` 已将 `bcrypt` 固定为 `4.0.1`。

**12. 生产环境必须替换 JWT 密钥**

`JWT_SECRET_KEY` 为默认值时，启动日志会打印告警。请用 `openssl rand -hex 32` 生成随机密钥写入 `.env`，
否则任何人可用默认密钥伪造令牌。

**13. PowerShell 无法激活虚拟环境**

执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 后重试，或直接使用
`.\.venv\Scripts\python.exe` 调用解释器。

**14. 中文日志/输出乱码**

Windows 控制台默认 GBK，执行 `chcp 65001` 切换为 UTF-8。

**15. 修改代码后接口没有变化**

服务启动时会打印实际加载的接口数量；请用 `http://127.0.0.1:8001/openapi.json` 确认接口列表。
特别注意：**若服务以 `DEBUG=true` 启动（热重载开启），保存文件即会自动重载；
但受限终端/沙箱环境下热重载子进程可能无法启动**（Windows 命名管道受限），此时请手动重启服务。

**16. 删除成员后为什么还能查到该成员？**

阶段五起"删除成员"的语义是**停用**（`status=inactive`）：签到记录与请假记录都以成员为外键，
物理删除会破坏历史数据。接口仍返回 200「删除成功」，但成员会保留在列表/详情中（状态为已停用），
不再参与签到与缺勤生成；重复删除返回 409。人脸记录同样保留，人脸识别会自动过滤已停用成员。

**17. 签到接口报「活动尚未开始」/「活动已结束」/「活动已取消」**

签到必须在活动时间窗内：`start_time ≤ 当前时间 ≤ end_time`，且活动状态为 `pending`/`active`。
活动被取消（400「活动已取消」）或已结束（400「活动已结束」）后不能再签到；
如时间确实不对，请让管理员用 `PUT /api/events/{event_id}` 调整起止时间。

**18. 活动结束后为什么多出很多「缺勤」记录？**

活动状态迁移到 `finished` 时，系统会为**所有应签到但无签到记录**的成员生成记录：
已通过请假的成员记为 `leave`，其余记为 `absent`（`method` 与 `checked_at` 为空）。
"应签到成员"= 家庭正常 + 成员正常 + `needs_checkin=true`；若某成员不需要签到，
请用 `PUT /api/members/{member_id}` 把 `needs_checkin` 设为 `false`。
出勤率口径为 `(signed + late) / 应签到人数`，见 `GET /api/checkins/events/{event_id}` 的 `summary`。

**19. 统计接口报 403**

`/api/statistics/*` 含全村数据，仅**管理员与工作人员**可访问；家庭用户请使用本户视角接口
（`GET /api/checkins/events/{event_id}`、`GET /api/families/me`）。

**20. 导出的 CSV 用 Excel 打开是乱码 / 文件名是乱码**

接口已写入 UTF-8 BOM 并用 `filename*=UTF-8''`（RFC 5987）声明中文文件名，
请用现代浏览器或 `curl -o` 保存；若用老版工具下载后手动改扩展名，可能丢失 BOM。
PowerShell 保存示例：`curl.exe -s -H "Authorization: Bearer <token>" "http://127.0.0.1:8001/api/statistics/export?type=events" -o 活动签到统计.csv`。

**21. 操作日志为什么没有记录我提交的请假？**

日志只审计**管理侧操作**：活动的创建/修改/删除/状态迁移、人脸与手动签到、签到记录修正、
请假审批；家庭用户的自助行为（提交请假、撤销请假、人脸录入）不埋点。
另外日志与业务变更同一次事务提交，若接口报错（4xx/5xx）则不会留下日志。

**22. AI 助手回答了「（mock 模式）已收到你的问题」，是坏了吗？**

没有坏。默认 `LLM_PROVIDER=mock`，刻意不依赖任何外部服务，便于离线演示与单测：
回答是模板化的，**但工具调用、真实数据查询、SSE 流式与简报生成都真实执行**。
按「九、AI 智能助手使用说明」配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 并设置
`LLM_PROVIDER=openai_compat` 后即可获得真实智能回答。

**23. AI 助手会编造数据吗？**

不会"主动编造"：系统提示词硬性要求"先调用工具查真实数据再回答、禁止编造数字"，
每个问题都通过工具（活动 / 签到汇总 / 请假 / 统计 / 知识库）取数，
响应的 `tool_calls` 字段会给出本轮调用的工具、入参与结果摘要，
可以逐条核对"回答里的数字是否来自平台"。工具失败时助手会如实说明，而不是编一个数字。

**24. 家庭用户问"全村出勤排行"时助手说无权限，是 bug 吗？**

不是。工具复用接口的权限链：`get_statistics_overview` / `get_families_ranking` 仅
管理员与工作人员可用，家庭用户会拿到「无权限查看统计报表」结果串（而不是报错），
助手据此说明"只能查看本户数据"，符合第 19 条的数据可见性设计。
家庭用户可直接问"我家这次活动签到情况"，助手会走 `get_event_summary` 取本户汇总。

**25. 活动简报里的数字和统计接口对不上？**

简报与 `GET /api/statistics/events/{id}` 使用同一口径（`build_summary`：应签到 = 家庭正常 +
成员正常 + `needs_checkin=true`）。若活动尚未结束，缺勤人数为 0（缺勤在活动结束时才生成），
因此简报中的数字会随活动状态变化；重新生成简报（再次调用 `POST .../summary`）即可刷新快照。

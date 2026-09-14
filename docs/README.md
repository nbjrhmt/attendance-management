# 项目文档目录

本目录用于存放项目的设计文档、接口文档与部署说明。

## 目录索引

| 文件 | 说明 | 状态 |
| --- | --- | --- |
| `README.md` | 文档索引与开发计划 | ✅ 已完成 |
| `api.md` | 接口文档（阶段一 ~ 阶段七全部接口，含 AI 智能助手） | ✅ 已完成 |
| `database.md` | 数据库设计说明（12 张表结构、约定、DDL、表关系） | ✅ 已完成 |
| `deploy.md` | 部署与运维说明 | ⏳ 待补充 |

## 分层约定

各业务模块统一采用如下分层（以 `src/user/` 为例）：

```
models.py     SQLAlchemy ORM 模型（表结构）
schemas.py    Pydantic 请求/响应模型（入参校验与出参裁剪）
crud.py       数据访问层（只做数据库读写，commit/refresh 在此完成）
service.py    业务逻辑层（业务规则、权限规则、返回 DTO）
router.py     接口层（依赖注入、调用 service、返回统一响应）
```

跨模块依赖方向：`router → service → crud → models`，公共能力放在 `src/common/`，
配置放在 `src/config/`。业务异常统一抛出 `BusinessError`（`src/common/exceptions.py`），
由 `main.py` 的全局处理器转换为统一响应。

**跨模块原子写入**：需要一次提交多张表的场景（如户主注册同时写 `sys_user` + `family` +
`family_member`），crud 层提供 `commit=False` 参数（仅 `flush` 取得主键），
由 service 层统一 `commit`，保证要么全部成功、要么全部回滚。
参考 `src/auth/service.py::register`。

**权限规则位置**：角色级限制（仅管理员等）由接口层的依赖（`AdminUser` / `StaffOrAdminUser` /
`require_roles`）保证；"本人或管理员"这类对象级规则由 service 层保证
（如 `src/family/service.py::ensure_family_manageable`）。

## 开发计划

### 阶段一：项目初始化（已完成）

- 项目目录结构（`src/` 下 12 个业务模块 + `tests/` + `docs/`；阶段七新增 `src/assistant/` 共 13 个）
- FastAPI 应用入口 `main.py`（CORS、统一异常处理、`/health`、`/` 服务信息）
- 公共模块 `src/common/database.py`、`src/common/response.py`
- 配置模块 `src/config/settings.py`
- 工程文件：`requirements.txt`、`.env.example`、`README.md`、`.gitignore`
- 单元测试 3 个（健康检查、服务信息、404 统一格式）

### 阶段二：认证与用户（已完成）

- 用户表 `sys_user` 设计（角色 admin/staff/family、状态 active/disabled、唯一约束与索引）
- 密码 bcrypt 哈希存储（passlib + bcrypt，72 字节上限校验）
- JWT 令牌：access_token（120 分钟）+ refresh_token（7 天），令牌类型校验
- 鉴权依赖：`CurrentUser`、`AdminUser`、`StaffOrAdminUser`、`require_roles(...)`
- 认证接口 4 个、用户管理接口 8 个
- 业务异常体系 `BusinessError` + 全局处理器；参数校验错误中文化
- 辅助脚本 `scripts/create_admin.py`（建表 + 创建/重置管理员）

### 阶段三：家庭与成员（已完成）

- 家庭表 `family`（一户主一家庭、户号自动生成 `F000123`、状态停用）
- 成员表 `family_member`（姓名、性别、与户主关系、身份证号唯一、是否需要签到、状态）
- **户主注册自动建档**：`POST /api/auth/register` 在同一事务内创建账号 + 家庭档案 + 户主成员行，
  入口为 `src/family/service.py::ensure_family_for_owner`（失败整体回滚）
- 身份证号校验：18 位格式 + GB 11643 校验位 + 出生日期合法性，并可自动解析性别与出生日期
- 家庭接口 6 个、成员接口 6 个；工作人员对家庭/成员名单只读
- 家庭列表附带 `member_count` / `checkin_required_count` 聚合统计
- 停用家庭时级联停用成员，保留历史数据
- 修复阶段二遗留缺陷：`init_db()` 仅导入包导致 `Base.metadata` 为空、建表失效
  （现改为登记并导入 `*.models` 模型模块，并补充回归测试）
- 单元测试 78 个（家庭 37 个、成员 37 个、数据库层 4 个）；累计 149 个全部通过

### 阶段四：人脸识别（已完成）

- 人脸记录表 `face_record`（与 `family_member` 一对一，`ON DELETE CASCADE`）
- **百度 AI 人脸识别 V3 客户端**（`src/face/baidu_client.py`）：access_token 缓存与提前刷新、
  token 失效自动重试、注册/更新/删除/搜索/检测五类能力、百度错误码到业务响应码的映射
- **提供方抽象**（`src/face/provider.py`）：`BaiduFaceProvider`（生产）与 `LocalFaceProvider`
  （本地模式，不做比对，供前端联调；生产环境禁止启用），经 FastAPI 依赖注入切换
- 人脸照片：文件头（魔数）与大小校验、按家庭分目录落盘、**不作为静态目录暴露**，
  读取走带鉴权的 `GET /api/face/photo/{member_id}`
- 人脸接口 7 个：录入、更新、删除、搜索（1:N）、状态、记录列表、照片读取
- 权限：录入/更新/删除限户主本人与管理员；搜索限管理员与工作人员；状态/照片/列表按家庭隔离
- 搜索会过滤**已停用成员与已停用家庭**，避免已迁出人员被识别为在场；识别成功记录
  `last_matched_at` / `last_match_score`
- 删除成员时级联清理人脸记录
- 单元测试 51 个（客户端/提供方 17 个、接口 34 个）：用 `httpx.MockTransport` 模拟百度服务，
  **真实客户端与提供方代码路径全部参与执行**，无需联网、无需百度密钥；累计 200 个用例全部通过
- 已在真实 MySQL 8.0.41 + 真实 HTTP 服务上完成端到端验证（`FACE_PROVIDER=local`）

未包含：人脸**活体检测**（判断"是否为真人本人"）属于签到场景需求，将在阶段五的签到流程中按需调用。

### 阶段五：活动与签到（已完成）

- 签到活动管理（时间范围、迟到阈值、状态机 `pending → active → finished` / `cancelled`）
- 人脸签到与手动签到、窗口校验与迟到判定、一成员一活动一条记录（幂等 409）
- 请假申请与管理员审批；成员删除改为"停用并保留历史"
- 活动结束联动生成缺勤（`absent`）/ 请假（`leave`）记录，出勤率 `(signed + late) / 应签到人数`
- 单元测试：活动、签到、请假三个模块（累计 368 个用例全部通过）

### 阶段六：统计报表与操作日志（已完成）

- 五个报表：总览、单活动统计、家庭参与度排行、签到趋势、CSV 导出（UTF-8 BOM，零新依赖）
- 报表**纯读**、不建表；仅管理员与工作人员可访问，家庭用户 403
- 操作日志审计：`operation_log` 表 + 请求 IP 中间件，与业务变更同一次事务提交

### 阶段七：AI 智能助手（已完成）

- 四张新表：`ai_conversation` / `ai_message` / `ai_knowledge` / `activity_summary`
- 提供方抽象 `src/assistant/provider.py`：`OpenAICompatProvider`（httpx 直调，
  含 SSE 流式解析）与 `MockLLMProvider`（无密钥可跑，含意图识别自动工具与简报模板）
- Agent 工具循环：7 个工具全部复用既有 service 的权限链（家庭用户仅本户，
  统计类工具对家庭用户返回「无权限」结果串）
- 接口 7 个：对话、SSE 流式、会话列表、会话消息、删除会话、生成简报、读取简报
- 知识库种子脚本 `scripts/seed_ai_knowledge.py`（20 条平台使用指南 Q&A，幂等）
- 单元测试 121 个（`tests/test_assistant.py`，完全离线：Mock 提供方 + httpx.MockTransport），
  累计 **491 个用例全部通过**；真实环境集成用例 `test_dsh_face_full.py` 15/15 可重复

### 阶段八：演示前端（已完成）

- `web/`：原生 HTML/CSS/JS 单页（`index.html` / `app.js` / `styles.css`），零构建、无新依赖
- 与后端同源托管：`GET /` 返回 `web/index.html`，`/static` 挂载前端资源（无需跨域配置）
- 覆盖登录/注册、总览看板、活动签到（含人脸签到）、请假管理、AI 智能助手对话（SSE 流式）
- 单元测试：`tests/test_health.py` 覆盖根路径演示页面与 `/static` 静态资源

### 阶段九：前端（待开发）

- Vue 3 + Element Plus 管理后台
- 微信小程序家庭端

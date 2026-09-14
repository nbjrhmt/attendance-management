# 数据库设计说明

- 数据库：MySQL 8.0，库名 `attendance`，字符集 `utf8mb4`
- ORM：SQLAlchemy 2.0（声明式风格），驱动 PyMySQL
- 建表方式：开发环境使用 `init_db()` 按模型创建；生产环境建议引入 Alembic 迁移
- 当前表：`sys_user`（阶段二）、`family` + `family_member`（阶段三）、`face_record`（阶段四）、
  `event` + `checkin_record` + `leave_request`（阶段五）、`operation_log`（阶段六）、
  `ai_conversation` + `ai_message` + `ai_knowledge` + `activity_summary`（阶段七）
- 统计报表（`/api/statistics`）**不建表**：全部指标由聚合查询实时计算

## 一、设计约定

| 约定 | 说明 |
| --- | --- |
| 表命名 | 账号/权限类系统表以 `sys_` 前缀（`sys_user`），业务表用业务名（`family`、`family_member`、`checkin_record`），避免与 MySQL 关键字冲突 |
| 主键 | 统一为 `id`，`INT` 自增，由 `BaseModel` 提供 |
| 时间字段 | 统一为 `created_at` / `updated_at`；`created_at` 由数据库默认值 `now()` 填充，`updated_at` 由 ORM 在 UPDATE 时自动刷新（见下方注意事项）；使用**服务器本地时间**，不使用 UTC |
| 枚举字段 | 使用 `VARCHAR(20)` 存储英文枚举值（`native_enum=False`），**不启用 MySQL 原生 ENUM 也不生成 CHECK 约束**，取值合法性由 Pydantic 与 SQLAlchemy（`validate_strings=True`）在应用层保证，便于后续扩展取值 |
| 外键 | 使用数据库外键约束并显式声明 `ON DELETE` 行为；跨模块引用（如 `family.owner_id → sys_user.id`）用 `RESTRICT` 保护历史数据，可空引用（如 `family_member.user_id`）用 `SET NULL` |
| 删除策略 | 账号为物理删除（但有家庭档案的户主会被拒绝删除）；**家庭与成员采用"停用"（`status=inactive`）而非物理删除**，以便保留历史签到数据。成员的 `DELETE` 接口在阶段三为物理删除，**阶段五起改为停用**（`checkin_record` / `leave_request` 以 `family_member.id` 为外键，物理删除会破坏历史统计）；活动的 `DELETE` 仅允许"未开始且无签到/请假记录"的活动 |
| 密码 | 只存储 bcrypt 哈希（`$2b$` 开头，60 字符），任何接口都不返回该字段 |

### 注意事项

1. **`role` 是 MySQL 保留字**：SQLAlchemy 会自动加反引号（`` `role` ``），手写 SQL 时也必须使用反引号。
2. **`updated_at` 由 ORM 维护**：`onupdate=func.now()` 是 SQLAlchemy 侧的默认值，只有通过 ORM 发起的 UPDATE 才会刷新它；直接执行原生 SQL 更新时需自行赋值。
3. **唯一且可空的列允许重复 NULL**（MySQL 与 SQLite 行为一致）：`sys_user.phone`、`family_member.id_card` 因此不会因多个空值冲突。
4. **`family.household_no` 数据库层可空**：户号默认由系统生成（`F` + 6 位序号，形如 `F000123`），生成方式为"插入取得自增主键后在同一事务内回填"，因此提交到数据库的家庭一定有户号（见 `src/family/crud.py::create_family`）。

## 二、`sys_user`（用户表）

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `username` | VARCHAR(50) | 否 | - | 登录用户名，唯一索引 |
| `password_hash` | VARCHAR(128) | 否 | - | bcrypt 密码哈希（实际 60 字符） |
| `real_name` | VARCHAR(50) | 否 | - | 真实姓名 |
| `phone` | VARCHAR(20) | 是 | NULL | 手机号，唯一索引（允许多个 NULL） |
| `role` | VARCHAR(20) | 否 | family | 角色：`admin` / `staff` / `family` |
| `status` | VARCHAR(20) | 否 | active | 状态：`active` 正常 / `disabled` 已禁用 |
| `last_login_at` | DATETIME | 是 | NULL | 最后登录时间 |
| `created_at` | DATETIME | 否 | now() | 创建时间 |
| `updated_at` | DATETIME | 否 | now() | 更新时间（ORM 更新时刷新） |

索引：`PRIMARY(id)`、`UNIQUE(username)`、`UNIQUE(phone)`、`INDEX(role, status)`

## 三、`family`（家庭表）

一户主一家庭：`owner_id` 唯一约束保证同一户主只对应一条家庭档案。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `owner_id` | INT | 否 | - | 户主用户ID，外键 → `sys_user.id`（`ON DELETE RESTRICT`），唯一索引 |
| `household_no` | VARCHAR(32) | 是* | 自动生成 | 户号，唯一索引；*业务层保证非空 |
| `address` | VARCHAR(200) | 是 | NULL | 家庭住址 |
| `village` | VARCHAR(100) | 是 | NULL | 所属村/组 |
| `contact_phone` | VARCHAR(20) | 是 | NULL | 家庭联系电话 |
| `remark` | VARCHAR(255) | 是 | NULL | 备注 |
| `status` | VARCHAR(20) | 否 | active | 状态：`active` 正常 / `inactive` 已停用 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`UNIQUE(owner_id)`、`UNIQUE(household_no)`、`INDEX(status, village)`

## 四、`family_member`（家庭成员表）

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `family_id` | INT | 否 | - | 所属家庭，外键 → `family.id`（`ON DELETE RESTRICT`） |
| `user_id` | INT | 是 | NULL | 关联登录账号，外键 → `sys_user.id`（`ON DELETE SET NULL`）；仅户主成员行有值 |
| `name` | VARCHAR(50) | 否 | - | 姓名 |
| `gender` | VARCHAR(10) | 是 | NULL | 性别：`male` / `female`（填身份证号时自动识别） |
| `relation` | VARCHAR(20) | 否 | other | 与户主关系：`householder` / `spouse` / `son` / `daughter` / `father` / `mother` / `other` |
| `id_card` | VARCHAR(18) | 是 | NULL | 身份证号，唯一索引（含校验位校验） |
| `birth_date` | DATE | 是 | NULL | 出生日期（填身份证号时自动识别） |
| `phone` | VARCHAR(20) | 是 | NULL | 联系电话 |
| `needs_checkin` | BOOL | 否 | 1 | 是否需要签到 |
| `status` | VARCHAR(20) | 否 | active | 状态：`active` 正常 / `inactive` 已停用 |
| `remark` | VARCHAR(255) | 是 | NULL | 备注 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(family_id, status)`、`UNIQUE(id_card)`、`INDEX(user_id)`

## 五、`event`（签到活动表）

活动由管理员创建，面向全村"需要签到"的家庭成员，**不建立活动-成员关联表**，
而是通过签到记录（`checkin_record`）反向关联。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `name` | VARCHAR(100) | 否 | - | 活动名称 |
| `description` | VARCHAR(500) | 是 | NULL | 活动说明 |
| `location` | VARCHAR(100) | 是 | NULL | 活动地点 |
| `start_time` | DATETIME | 否 | - | 开始时间（服务器本地时间） |
| `end_time` | DATETIME | 否 | - | 结束时间，必须晚于 `start_time`（服务层校验，否则 400） |
| `late_threshold_minutes` | INT | 否 | 15 | 迟到阈值（分钟），取自 `settings.DEFAULT_LATE_THRESHOLD_MINUTES`，校验 ≥ 0 |
| `status` | VARCHAR(20) | 否 | pending | 状态：`pending` 未开始 / `active` 进行中 / `finished` 已结束 / `cancelled` 已取消 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(status, start_time)`

状态机（见 `src/event/service.py::_ALLOWED_TRANSITIONS`）：

```
pending ──开始──▶ active ──结束──▶ finished
   │
   └──取消──▶ cancelled
```

- 非法迁移（如 `active → cancelled`、`finished → active`、同状态重复迁移）返回 409；
- `active → finished` 时会在**同一次事务**内先生成缺勤/请假记录，再把活动置为 `finished`。

## 六、`checkin_record`（签到记录表）

一成员一活动**最多一条**记录（`UNIQUE(event_id, member_id)`），
`family_id` 为冗余字段，便于按家庭聚合统计而无需再关联 `family_member`。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `event_id` | INT | 否 | - | 活动ID，外键 → `event.id`（`ON DELETE RESTRICT`） |
| `family_id` | INT | 否 | - | 家庭ID，外键 → `family.id`（`ON DELETE RESTRICT`），冗余存储便于统计 |
| `member_id` | INT | 否 | - | 成员ID，外键 → `family_member.id`（`ON DELETE RESTRICT`） |
| `method` | VARCHAR(10) | 是 | NULL | 签到方式：`face` 人脸 / `manual` 手动；系统生成的缺勤/请假为 NULL |
| `status` | VARCHAR(20) | 否 | - | 状态：`signed` 已签到 / `late` 迟到 / `absent` 缺勤 / `leave` 请假 / `abnormal` 异常 |
| `checked_at` | DATETIME | 是 | NULL | 签到时间；`absent` / `leave` 为 NULL |
| `face_score` | FLOAT | 是 | NULL | 人脸识别得分（0-100），手动签为空 |
| `reviewed_by_id` | INT | 是 | NULL | 最近一次人工修正人，外键 → `sys_user.id`（`ON DELETE SET NULL`） |
| `reviewed_at` | DATETIME | 是 | NULL | 最近一次人工修正时间 |
| `review_remark` | VARCHAR(255) | 是 | NULL | 修正说明 |
| `remark` | VARCHAR(255) | 是 | NULL | 备注 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`UNIQUE(event_id, member_id)`、`INDEX(event_id, status)`、
`INDEX(family_id)`、`INDEX(member_id)`

关键业务口径：

- 迟到判定：`checked_at - event.start_time > late_threshold_minutes * 60` 秒即为 `late`，否则 `signed`；
- 缺勤/请假由活动结束时批量生成：已通过请假的成员记 `leave`，其余应签到未签到成员记 `absent`，
  两者的 `method` 与 `checked_at` 均为 NULL；
- `abnormal` 由管理员通过 `PUT /api/checkins/{checkin_id}` 人工修正产生；
- 出勤率 `attendance_rate = (signed + late) / 应签到人数`（应签到人数 = 家庭正常 + 成员正常 + `needs_checkin=1`）。

## 七、`leave_request`（请假申请表）

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `event_id` | INT | 否 | - | 活动ID，外键 → `event.id`（`ON DELETE RESTRICT`） |
| `member_id` | INT | 否 | - | 成员ID，外键 → `family_member.id`（`ON DELETE RESTRICT`） |
| `reason` | VARCHAR(255) | 否 | - | 请假事由 |
| `status` | VARCHAR(20) | 否 | pending | 状态：`pending` 待审批 / `approved` 已通过 / `rejected` 已驳回 / `cancelled` 已撤销 |
| `reviewed_by_id` | INT | 是 | NULL | 审批人，外键 → `sys_user.id`（`ON DELETE SET NULL`） |
| `reviewed_at` | DATETIME | 是 | NULL | 审批时间 |
| `review_remark` | VARCHAR(255) | 是 | NULL | 审批说明 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(event_id, status)`、`INDEX(member_id)`、`INDEX(event_id)`

约束与联动：

- 同一成员同一活动**同时只能有一条** `pending` / `approved` 的申请（服务层校验，重复提交 409）；
- 被驳回或已撤销的申请可重新提交，历史记录保留；
- 已通过的请假只在活动结束时影响缺勤生成；若该成员实际到场签到，以实际签到记录为准。

## 八、`operation_log`（操作日志表）

审计表：记录管理员与工作人员的关键操作（活动创建/修改/删除/状态迁移、人脸与手动签到、
签到记录修正、请假审批）。**只增不改**，接口层只有只读查询与按时间清理。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `user_id` | INT | 是 | NULL | 操作人ID，外键 → `sys_user.id`（`ON DELETE SET NULL`） |
| `username` | VARCHAR(50) | 否 | - | 操作人用户名**快照**，账号删除后仍可追溯 |
| `module` | VARCHAR(50) | 否 | - | 业务模块：`event` / `checkin` / `leave` 等 |
| `action` | VARCHAR(50) | 否 | - | 操作类型：`create` / `update` / `delete` / `change_status` / `face_checkin` / `manual_checkin` / `correct` / `approve` / `reject` |
| `target_type` | VARCHAR(50) | 是 | NULL | 操作对象类型（`event` / `checkin` / `leave`），展示为 `target_type/target_id` |
| `target_id` | INT | 是 | NULL | 操作对象ID |
| `detail` | VARCHAR(500) | 是 | NULL | 人类可读摘要（如「创建活动：村晚联欢」「状态：pending→active」） |
| `ip` | VARCHAR(45) | 是 | NULL | 请求来源IP（兼容 IPv6，由纯 ASGI 中间件注入） |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(user_id)`、`INDEX(module, created_at)`、`INDEX(created_at)`——
分别支撑"按操作人查"、"按模块 + 时间范围查"、"按时间排序/清理"三类审计查询。

约定：

- 日志由业务代码通过 `src/log/service.py::record_operation` 写入，
  只 `flush` 不 `commit`，与业务变更**同一次事务**提交（保证"有业务变更就有日志"）；
- `user_id` 为空表示系统操作（`username` 记为 `system`）；
- 清理接口 `DELETE /api/logs/clean?before=YYYY-MM-DD` 按 `created_at < before` 物理删除。

## 九、`ai_conversation`（AI 会话表）

一个用户多条会话；标题自动取首条用户消息前 20 字。会话本身不存上下文，
上下文每次由 `ai_message` 实时还原（取最近 N 条），因此重启服务也能续聊。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `user_id` | INT | 否 | - | 会话所属用户，外键 → `sys_user.id`（`ON DELETE CASCADE`） |
| `title` | VARCHAR(100) | 是 | NULL | 会话标题，自动取首条用户消息前 20 字 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间（新消息写入时刷新 `updated_at`，用于"最近活跃在前"排序） |

索引：`PRIMARY(id)`、`INDEX(user_id)`

## 十、`ai_message`（AI 会话消息表）

多轮对话的记忆载体，角色与 OpenAI Chat Completions 对齐（`user` / `assistant` / `tool`）。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键（也是消息顺序：上下文按 `id` 升序还原，比秒级时间戳更精确） |
| `conversation_id` | INT | 否 | - | 所属会话，外键 → `ai_conversation.id`（`ON DELETE CASCADE`） |
| `role` | VARCHAR(20) | 否 | - | 角色：`user` 用户 / `assistant` 助手 / `tool` 工具结果 |
| `content` | TEXT | 是 | NULL | 消息正文；`tool` 消息为工具结果摘要（≤500 字） |
| `tool_calls` | TEXT | 是 | NULL | 工具调用 JSON 数组：`[{"name","args","result"}]`，供前端渲染与审计 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(conversation_id, id)`

约定：

- 接口层「删除会话」会**显式删除**该会话的消息（`DELETE /api/assistant/conversations/{id}` 返回
  `deleted_messages` 条数）；数据库层的 `ON DELETE CASCADE` 是双保险
  （SQLite 默认不启用外键约束，单元测试依赖显式删除保证行为一致）；
- 历史消息还原时 `tool` 消息不再单独发送（缺少 `tool_call_id` 会破坏协议），
  其内容已作为工具调用摘要写入上一条 assistant 消息。

## 十一、`ai_knowledge`（平台使用指南知识库表）

轻量 RAG 的语料表：`scripts/seed_ai_knowledge.py` 写入 20 条 Q&A（提炼自 README 的 FAQ 与
`docs/api.md` 的业务规则），幂等可重跑（`--force` 重灌）。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `question` | VARCHAR(200) | 否 | - | 标准问题（如「如何添加家庭成员？」） |
| `keywords` | VARCHAR(500) | 否 | - | 逗号分隔的检索关键词（兼容中英文逗号与顿号） |
| `answer` | TEXT | 否 | - | 标准答案（含接口路径与业务规则） |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 创建/更新时间 |

索引：`PRIMARY(id)`、`INDEX(question)`

检索方式（`src/assistant/service.py::search_knowledge`）：
按"`keywords` 中出现在用户问题里的个数"降序打分，得分相同则按"问题的 2 字滑窗在
`question + keywords` 中的覆盖率"排序，取 top-k（`AI_KNOWLEDGE_TOP_K`，默认 3）。
**不引入向量库或全文索引**——村级知识库规模（几十条）下，这套打分的召回与稳定性足够，
且结果完全可复现、可断言。命中内容会注入系统提示词，同时作为 `search_knowledge` 工具
供模型主动检索。

## 十二、`activity_summary`（活动简报表）

一活动一份（`event_id` 唯一），重复生成覆盖更新（upsert）；简报由 AI 依据真实统计快照生成，
生成失败/输出不规范时降级为模板拼接并在正文中标注「降级」。

| 字段 | 类型 | 允许空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INT | 否 | 自增 | 主键 |
| `event_id` | INT | 否 | - | 签到活动ID，外键 → `event.id`（`ON DELETE CASCADE`），**唯一** |
| `title` | VARCHAR(200) | 否 | - | 简报标题 |
| `content` | TEXT | 否 | - | 简报全文（Markdown） |
| `meta` | TEXT | 是 | NULL | 生成时使用的统计快照（JSON）：活动信息、签到各状态计数与出勤率、请假审批统计、`highlights`、`degraded`、`provider`、`model`、`generated_at` |
| `created_by_id` | INT | 是* | NULL | 生成人，外键 → `sys_user.id`（`ON DELETE SET NULL`）；*服务层写入时保证有值 |
| `created_at` / `updated_at` | DATETIME | 否 | now() | 首次/最近生成时间 |

索引：`PRIMARY(id)`、`UNIQUE(event_id)`

> **关于 `created_by_id` 的可空性**：需求中写作"NOT NULL + ON DELETE SET NULL"，
> 但 MySQL 8 明确拒绝该组合（错误码 1830：*Column 'created_by_id' cannot be NOT NULL:
> needed in a foreign key constraint ... SET NULL*，已在本机 MySQL 8.0.41 实测复现）。
> 为保留"管理员注销后简报不消失"的语义，这里按项目既有约定
> （`checkin_record.reviewed_by_id`、`operation_log.user_id`）定义为**可空 + SET NULL**。

## 十三、实际建表语句（MySQL 方言）

由 SQLAlchemy 生成，用于人工核对（实际执行由 `init_db()` 完成）：

```sql
CREATE TABLE sys_user (
    username      VARCHAR(50)  NOT NULL COMMENT '登录用户名，全局唯一',
    password_hash VARCHAR(128) NOT NULL COMMENT '密码哈希（bcrypt，60 字符）',
    real_name     VARCHAR(50)  NOT NULL COMMENT '真实姓名',
    phone         VARCHAR(20)      NULL COMMENT '手机号，可为空且唯一',
    `role`        VARCHAR(20)  NOT NULL COMMENT '角色：admin 管理员 / staff 工作人员 / family 家庭用户',
    status        VARCHAR(20)  NOT NULL COMMENT '状态：active 正常 / disabled 已禁用',
    last_login_at DATETIME         NULL COMMENT '最后登录时间',
    id            INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at    DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表（管理员 / 工作人员 / 家庭用户）';
CREATE UNIQUE INDEX ix_sys_user_username ON sys_user (username);
CREATE UNIQUE INDEX ix_sys_user_phone    ON sys_user (phone);
CREATE INDEX ix_sys_user_role_status     ON sys_user (`role`, status);

CREATE TABLE family (
    owner_id      INT          NOT NULL COMMENT '户主用户ID（sys_user.id，role=family）',
    household_no  VARCHAR(32)      NULL COMMENT '户号（唯一）；未指定时自动生成 F+6位序号，插入后同一事务内回填',
    address       VARCHAR(200)     NULL COMMENT '家庭住址',
    village       VARCHAR(100)     NULL COMMENT '所属村/组',
    contact_phone VARCHAR(20)      NULL COMMENT '家庭联系电话',
    remark        VARCHAR(255)     NULL COMMENT '备注',
    status        VARCHAR(20)  NOT NULL COMMENT '状态：active 正常 / inactive 已停用',
    id            INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at    DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (owner_id) REFERENCES sys_user (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='家庭表（以户主为单位）';
CREATE UNIQUE INDEX ix_family_owner_id     ON family (owner_id);
CREATE UNIQUE INDEX ix_family_household_no ON family (household_no);
CREATE INDEX ix_family_status_village      ON family (status, village);

CREATE TABLE family_member (
    family_id     INT          NOT NULL COMMENT '所属家庭ID（family.id）',
    user_id       INT              NULL COMMENT '关联登录账号（仅户主本人有值，其他成员为空）',
    name          VARCHAR(50)  NOT NULL COMMENT '姓名',
    gender        VARCHAR(10)      NULL COMMENT '性别：male 男 / female 女',
    relation      VARCHAR(20)  NOT NULL COMMENT '与户主关系：householder 户主 / spouse 配偶 / son 儿子 / daughter 女儿 / father 父亲 / mother 母亲 / other 其他',
    id_card       VARCHAR(18)      NULL COMMENT '身份证号（18 位，唯一）',
    birth_date    DATE             NULL COMMENT '出生日期',
    phone         VARCHAR(20)      NULL COMMENT '联系电话',
    needs_checkin BOOL         NOT NULL COMMENT '是否需要签到：1 需要 / 0 不需要',
    status        VARCHAR(20)  NOT NULL COMMENT '状态：active 正常 / inactive 已停用',
    remark        VARCHAR(255)     NULL COMMENT '备注',
    id            INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at    DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (family_id) REFERENCES family (id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id) REFERENCES sys_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='家庭成员表';
CREATE UNIQUE INDEX ix_family_member_id_card       ON family_member (id_card);
CREATE INDEX ix_family_member_family_id            ON family_member (family_id);
CREATE INDEX ix_family_member_family_status        ON family_member (family_id, status);
CREATE INDEX ix_family_member_user_id              ON family_member (user_id);

CREATE TABLE face_record (
    member_id       INT          NOT NULL COMMENT '家庭成员ID（family_member.id，一对一）',
    provider        VARCHAR(20)  NOT NULL COMMENT '人脸识别提供方：baidu 百度AI / local 本地模式',
    group_id        VARCHAR(64)  NOT NULL COMMENT '人脸库（用户组）标识',
    face_token      VARCHAR(128)     NULL COMMENT '提供方返回的人脸标识（百度 face_token）',
    image_path      VARCHAR(255) NOT NULL COMMENT '人脸照片路径（相对项目根目录）',
    image_md5       VARCHAR(32)  NOT NULL COMMENT '照片 MD5，用于查重与追溯',
    image_size      INT          NOT NULL COMMENT '照片字节数',
    status          VARCHAR(20)  NOT NULL COMMENT '状态：registered 已录入 / inactive 已删除',
    registered_at   DATETIME     NOT NULL DEFAULT now() COMMENT '最近一次录入时间',
    last_matched_at DATETIME         NULL COMMENT '最近一次识别成功时间',
    last_match_score FLOAT           NULL COMMENT '最近一次识别得分（0-100）',
    remark          VARCHAR(255)     NULL COMMENT '备注',
    id              INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at      DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at      DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (member_id) REFERENCES family_member (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人脸录入记录表（与家庭成员一对一）';
CREATE UNIQUE INDEX ix_face_record_member_id       ON face_record (member_id);
CREATE INDEX ix_face_record_status_provider        ON face_record (status, provider);

CREATE TABLE event (
    name                   VARCHAR(100) NOT NULL COMMENT '活动名称',
    description            VARCHAR(500)     NULL COMMENT '活动说明',
    location               VARCHAR(100)     NULL COMMENT '活动地点',
    start_time             DATETIME     NOT NULL COMMENT '开始时间（服务器本地时间）',
    end_time               DATETIME     NOT NULL COMMENT '结束时间（必须晚于开始时间）',
    late_threshold_minutes INT          NOT NULL COMMENT '迟到阈值（分钟），默认 15',
    status                 VARCHAR(20)  NOT NULL COMMENT '状态：pending 未开始 / active 进行中 / finished 已结束 / cancelled 已取消',
    id                     INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at             DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at             DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='签到活动表';
CREATE INDEX ix_event_status_start_time ON event (status, start_time);

CREATE TABLE checkin_record (
    event_id       INT          NOT NULL COMMENT '签到活动ID（event.id）',
    family_id      INT          NOT NULL COMMENT '家庭ID（family.id，冗余存储便于按家庭统计）',
    member_id      INT          NOT NULL COMMENT '家庭成员ID（family_member.id）',
    method         VARCHAR(10)      NULL COMMENT '签到方式：face 人脸 / manual 手动；系统生成的缺勤与请假记录为 NULL',
    status         VARCHAR(20)  NOT NULL COMMENT '签到状态：signed 已签到 / late 迟到 / absent 缺勤 / leave 请假 / abnormal 异常',
    checked_at     DATETIME         NULL COMMENT '签到时间（服务器本地时间）；缺勤/请假为 NULL',
    face_score     FLOAT            NULL COMMENT '人脸签到得分（0-100），手动签为空',
    reviewed_by_id INT              NULL COMMENT '最近一次人工修正的操作人（sys_user.id）',
    reviewed_at    DATETIME         NULL COMMENT '最近一次人工修正时间',
    review_remark  VARCHAR(255)     NULL COMMENT '人工修正说明',
    remark         VARCHAR(255)     NULL COMMENT '备注',
    id             INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at     DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at     DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    CONSTRAINT uq_checkin_record_event_member UNIQUE (event_id, member_id),
    FOREIGN KEY (event_id)        REFERENCES event (id)          ON DELETE RESTRICT,
    FOREIGN KEY (family_id)       REFERENCES family (id)         ON DELETE RESTRICT,
    FOREIGN KEY (member_id)       REFERENCES family_member (id)  ON DELETE RESTRICT,
    FOREIGN KEY (reviewed_by_id)  REFERENCES sys_user (id)       ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='签到记录表';
CREATE INDEX ix_checkin_record_event_status ON checkin_record (event_id, status);
CREATE INDEX ix_checkin_record_family       ON checkin_record (family_id);
CREATE INDEX ix_checkin_record_member_id    ON checkin_record (member_id);
CREATE INDEX ix_checkin_record_event_id     ON checkin_record (event_id);

CREATE TABLE leave_request (
    event_id       INT          NOT NULL COMMENT '签到活动ID（event.id）',
    member_id      INT          NOT NULL COMMENT '请假的家庭成员ID（family_member.id）',
    reason         VARCHAR(255) NOT NULL COMMENT '请假事由',
    status         VARCHAR(20)  NOT NULL COMMENT '状态：pending 待审批 / approved 已通过 / rejected 已驳回 / cancelled 已撤销',
    reviewed_by_id INT              NULL COMMENT '审批人（sys_user.id）',
    reviewed_at    DATETIME         NULL COMMENT '审批时间',
    review_remark  VARCHAR(255)     NULL COMMENT '审批说明',
    id             INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at     DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at     DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (event_id)       REFERENCES event (id)          ON DELETE RESTRICT,
    FOREIGN KEY (member_id)      REFERENCES family_member (id)  ON DELETE RESTRICT,
    FOREIGN KEY (reviewed_by_id) REFERENCES sys_user (id)       ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='请假申请表';
CREATE INDEX ix_leave_request_event_status ON leave_request (event_id, status);
CREATE INDEX ix_leave_request_member       ON leave_request (member_id);
CREATE INDEX ix_leave_request_event_id     ON leave_request (event_id);

CREATE TABLE operation_log (
    user_id       INT              NULL COMMENT '操作人ID（sys_user.id）；账号被删除后置空，仅保留用户名快照',
    username      VARCHAR(50)  NOT NULL COMMENT '操作人用户名（写入时快照，账号删除后仍可追溯）',
    module        VARCHAR(50)  NOT NULL COMMENT '业务模块：event 活动 / checkin 签到 / leave 请假 等',
    action        VARCHAR(50)  NOT NULL COMMENT '操作类型：create / update / delete / change_status / face_checkin / manual_checkin / correct / approve / reject 等',
    target_type   VARCHAR(50)      NULL COMMENT '操作对象类型：event / checkin / member / leave 等',
    target_id     INT              NULL COMMENT '操作对象ID',
    detail        VARCHAR(500)     NULL COMMENT '人类可读的操作摘要',
    ip            VARCHAR(45)      NULL COMMENT '请求来源IP（兼容 IPv6）',
    id            INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at    DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (user_id) REFERENCES sys_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志表（审计：记录管理员与工作人员的关键操作）';
CREATE INDEX ix_operation_log_user_id         ON operation_log (user_id);
CREATE INDEX ix_operation_log_module_created  ON operation_log (module, created_at);
CREATE INDEX ix_operation_log_created_at      ON operation_log (created_at);

CREATE TABLE ai_conversation (
    user_id    INT          NOT NULL COMMENT '会话所属用户ID（sys_user.id），账号删除时级联删除会话',
    title      VARCHAR(100)     NULL COMMENT '会话标题，自动取首条用户消息前 20 字',
    id         INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (user_id) REFERENCES sys_user (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 会话表（一个用户多条会话，标题取首条用户消息前 20 字）';
CREATE INDEX ix_ai_conversation_user_id ON ai_conversation (user_id);

CREATE TABLE ai_message (
    conversation_id INT          NOT NULL COMMENT '所属会话ID（ai_conversation.id），会话删除时级联删除消息',
    `role`          VARCHAR(20)  NOT NULL COMMENT '消息角色：user 用户 / assistant 助手 / tool 工具结果',
    content         TEXT             NULL COMMENT '消息正文；工具消息为工具结果摘要',
    tool_calls      TEXT             NULL COMMENT '工具调用 JSON 数组（name / arguments / result 摘要），仅 assistant 消息有值',
    id              INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at      DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at      DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (conversation_id) REFERENCES ai_conversation (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 会话消息表（user / assistant / tool，按 id 升序还原上下文）';
CREATE INDEX ix_ai_message_conversation_id_id ON ai_message (conversation_id, id);

CREATE TABLE ai_knowledge (
    question   VARCHAR(200) NOT NULL COMMENT '标准问题（如「如何添加家庭成员」）',
    keywords   VARCHAR(500) NOT NULL COMMENT '逗号分隔的检索关键词，命中数越多排序越靠前',
    answer     TEXT         NOT NULL COMMENT '标准答案（含接口路径与业务规则）',
    id         INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 知识库表（平台使用指南 Q&A，关键词命中打分检索）';
CREATE INDEX ix_ai_knowledge_question ON ai_knowledge (question);

CREATE TABLE activity_summary (
    event_id      INT          NOT NULL COMMENT '签到活动ID（event.id），唯一；活动删除时级联删除简报',
    title         VARCHAR(200) NOT NULL COMMENT '简报标题',
    content       TEXT         NOT NULL COMMENT '简报全文（Markdown）',
    meta          TEXT             NULL COMMENT '生成时使用的统计快照（JSON：应签到/各状态计数/出勤率/生成时间）',
    created_by_id INT              NULL COMMENT '生成人ID（sys_user.id）；账号删除后置空，简报仍保留',
    id            INT          NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    created_at    DATETIME     NOT NULL DEFAULT now() COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT now() COMMENT '更新时间',
    PRIMARY KEY (id),
    FOREIGN KEY (event_id)      REFERENCES event (id)    ON DELETE CASCADE,
    FOREIGN KEY (created_by_id) REFERENCES sys_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='活动简报表（由 AI 依据活动真实统计数据生成，一活动一份）';
CREATE UNIQUE INDEX ix_activity_summary_event_id ON activity_summary (event_id);
```

> 上述 DDL 与实际环境（MySQL 8.0.41）中 `SHOW CREATE TABLE` 的结果一致。

> 建表脚本的登记入口在 `src/common/database.py::_MODEL_MODULES`：
> `init_db()` 会先显式导入这些**模型模块**再 `create_all()`。
> 注意只导入包（`src.user`）不会加载 `models.py`，新增模型时必须在此登记。

## 十四、人脸照片的存储

人脸照片以文件形式保存在 ``FACE_UPLOAD_DIR``（默认 ``uploads/face/``），数据库只记录路径与摘要：

```
uploads/face/{family_id}/{member_id}_{yyyyMMddHHmmss}_{md5前8位}.{jpg|png|bmp}
```

- 按家庭分目录，便于运维排查与整户清理；
- 文件名含时间戳与内容摘要，重新录入不会覆盖旧文件（保留历史便于追溯）；
- ``uploads/`` 已加入 ``.gitignore``，**不作为静态目录暴露**，
  读取统一走带鉴权的 ``GET /api/face/photo/{member_id}``；
- 删除人脸只把数据库记录置为 ``inactive``（保留照片）；
  阶段五起"删除成员"改为**停用成员**（``status=inactive``），``face_record`` 行同样保留，
  人脸识别时会过滤已停用成员，不会造成误识别；照片文件保留在磁盘上，可由运维按需清理。

## 十五、表关系

```
sys_user (family 角色)  1 ──── 1  family              家庭档案          ✅ 阶段三
family                  1 ──── n  family_member       家庭成员          ✅ 阶段三
family_member           1 ──── 1  face_record         人脸记录          ✅ 阶段四
event                   1 ──── n  checkin_record      签到记录          ✅ 阶段五
family                  1 ──── n  checkin_record      签到记录（冗余）  ✅ 阶段五
family_member           1 ──── n  checkin_record      签到记录          ✅ 阶段五
event                   1 ──── n  leave_request       请假记录          ✅ 阶段五
family_member           1 ──── n  leave_request       请假记录          ✅ 阶段五
sys_user                1 ──── n  operation_log       操作日志（审计）  ✅ 阶段六
sys_user                1 ──── n  ai_conversation     AI 会话           ✅ 阶段七
ai_conversation         1 ──── n  ai_message          AI 会话消息       ✅ 阶段七
event                   1 ──── 1  activity_summary    活动简报（AIGC）  ✅ 阶段七
sys_user                1 ──── n  activity_summary    简报生成人        ✅ 阶段七
ai_knowledge            （独立语料表，无外键）        平台使用指南      ✅ 阶段七
```

> 统计报表（`/api/statistics/*`）不产生新表，完全由上述表实时聚合：
> 家庭/成员/活动计数取自 `family` / `family_member` / `event`，
> 签到与出勤口径取自 `checkin_record`（`signed` + `late` 视为到场）。
>
> AI 助手的工具同样**不新增任何查询表**：活动/签到/请假/统计一律复用既有表与既有 service，
> 只有会话记忆（`ai_conversation` / `ai_message`）、知识库（`ai_knowledge`）与
> 简报（`activity_summary`）是新表。

## 十六、初始化

```powershell
# 1) 检查数据库连通性（配置见 .env）
.\.venv\Scripts\python.exe -c "from src.common.database import ping_database; print(ping_database())"

# 2) 建表 + 创建管理员（管理员无法通过注册接口创建，必须用脚本）
.\.venv\Scripts\python.exe scripts\create_admin.py --username admin

# 3) 户主账号无需脚本：POST /api/auth/register 会自动创建家庭档案与户主成员行

# 4) AI 知识库（阶段七）：建表 + 写入 20 条平台使用指南 Q&A（幂等，--force 重灌）
.\.venv\Scripts\python.exe scripts\seed_ai_knowledge.py

# 5) 单元测试使用内存 SQLite，自动建表，无需 MySQL
.\.venv\Scripts\python.exe -m pytest -v
```

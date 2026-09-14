"""AI 智能助手模块（阶段七：LLM / Agent / AIGC）。

面向"所有回答都必须基于本平台真实数据"的目标，把大模型与既有的签到业务打通：
**多轮对话 + 工具调用（Function Calling）+ 会话记忆 + 角色权限 + SSE 流式
+ 活动简报生成（AIGC）+ 平台指南检索（轻量 RAG）**。
默认 ``LLM_PROVIDER=mock``，**无需任何 API Key 即可跑通全流程**，单元测试完全离线。

模块文件：

| 文件 | 职责 |
| --- | --- |
| :mod:`provider` | LLM 提供方抽象（``OpenAICompatProvider`` / ``MockLLMProvider``）+ 依赖工厂 |
| :mod:`models` | 四张表：``ai_conversation`` / ``ai_message`` / ``ai_knowledge`` / ``activity_summary`` |
| :mod:`schemas` | 对话、会话、消息、简报的请求/响应模型 |
| :mod:`crud` | 会话、消息、知识库、简报的数据访问（分页、级联删除） |
| :mod:`service` | 系统提示词、Agent 工具循环、7 个工具、知识库检索、AIGC 简报 |
| :mod:`router` | ``/api/assistant`` 接口（对话 / 流式 / 会话 / 简报） |

接口清单（全部需登录，挂载前缀 ``/api/assistant``）：

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/chat`` | 多轮对话，返回 ``{conversation_id, reply, tool_calls}`` | 登录用户 |
| POST | ``/chat/stream`` | SSE 流式对话（``start`` → ``tool``* → ``token``* → ``done``） | 登录用户 |
| GET | ``/conversations`` | 本人会话分页列表（最近活跃在前） | 登录用户（仅本人） |
| GET | ``/conversations/{id}/messages`` | 本人会话消息分页（时间正序） | 登录用户（仅本人） |
| DELETE | ``/conversations/{id}`` | 删除本人会话（级联删除消息） | 登录用户（仅本人） |
| POST | ``/events/{event_id}/summary`` | 生成活动简报（AIGC，一活动一份，可覆盖重生成） | 管理员 / 工作人员 |
| GET | ``/events/{event_id}/summary`` | 读取已保存的活动简报（无则 404） | 管理员 / 工作人员 |

可用工具（全部复用既有 service，权限链与对应接口完全一致，禁止绕过权限裸查 SQL）：

| 工具 | 数据来源 | 权限 |
| --- | --- | --- |
| ``get_events`` | ``src.event.service.list_events`` | 登录用户 |
| ``get_event_detail`` | ``src.event.service.get_event_detail`` | 登录用户 |
| ``get_event_summary`` | ``src.checkin.service.get_event_checkins``（与 ``GET /api/checkins/events/{id}`` 同款） | 家庭用户仅本户 |
| ``get_leave_status`` | ``src.leave.service.list_leaves`` | 家庭用户仅本户 |
| ``get_statistics_overview`` | ``src.statistics.service.build_overview`` | 管理员 / 工作人员 |
| ``get_families_ranking`` | ``src.statistics.service.list_family_rankings`` | 管理员 / 工作人员 |
| ``search_knowledge`` | ``ai_knowledge`` 关键词打分检索（轻量 RAG） | 登录用户 |

配置（``.env`` / 环境变量，默认 mock 即可运行）::

    LLM_PROVIDER=mock              # mock / openai_compat
    LLM_BASE_URL=                  # 如 https://api.deepseek.com/v1
    LLM_API_KEY=                   # 服务商 Key（不要提交到版本库）
    LLM_MODEL=                     # 如 deepseek-chat
    LLM_TIMEOUT_SECONDS=30
    AI_TOOL_MAX_ROUNDS=4           # 单轮对话最多工具调用轮数
    AI_KNOWLEDGE_TOP_K=3           # 注入提示词的知识库条数

知识库初始化（幂等）::

    .\\.venv\\Scripts\\python.exe scripts\\seed_ai_knowledge.py
    .\\.venv\\Scripts\\python.exe scripts\\seed_ai_knowledge.py --force   # 重灌
"""

from __future__ import annotations

__all__: list[str] = []

"""AI 智能助手业务逻辑层（Agent 编排 + 工具调用 + 轻量 RAG + AIGC 简报）。

能力概览：

| 能力 | 实现要点 |
| --- | --- |
| 多轮对话 | 消息落库 ``ai_message``，每次把最近 N 条历史拼进上下文，进程重启可续聊 |
| 会话记忆 | 同一 ``conversation_id`` 下按 ``id`` 升序还原；会话列表按最近活跃排序 |
| 工具调用 | ``_run_agent_loop``：LLM(tools) → 执行工具 → 回填 ``role=tool`` → 再问 LLM，最多 ``AI_TOOL_MAX_ROUNDS`` 轮 |
| 角色权限 | 所有工具都复用既有 service（``checkin`` / ``leave`` / ``statistics``），**禁止绕过权限裸查 SQL**；统计类工具仅管理员/工作人员可用，家庭用户得到"无权限"结果串而非异常 |
| SSE 流式 | ``chat_stream``：工具循环走非流式（完整拿到 ``tool_calls``），最终文本再以 token 事件逐段下发 |
| AIGC 简报 | ``generate_event_summary``：组装活动+统计+请假真实数据 → 要求 LLM 输出严格 JSON → 解析失败降级为模板拼接并标注「降级」→ upsert ``activity_summary`` |
| 轻量 RAG | ``search_knowledge``：按 ``ai_knowledge.keywords`` 命中数降序打分取 top-k，注入系统提示词并作为工具供模型主动检索 |

工具集（全部基于平台真实数据，复用既有 service 的权限链）：

============================  ==========================================  ====================
工具                          数据来源                                    权限
============================  ==========================================  ====================
``get_events``                ``src.event.service.list_events``           登录用户
``get_event_detail``          ``src.event.service.get_event_detail``      登录用户
``get_event_summary``         ``src.checkin.service.get_event_checkins``  家庭用户仅本户汇总
``get_leave_status``          ``src.leave.service.list_leaves``           家庭用户仅本户
``get_statistics_overview``   ``src.statistics.service.build_overview``   管理员 / 工作人员
``get_families_ranking``      ``src.statistics.service.list_family_rankings``  管理员 / 工作人员
``search_knowledge``          ``ai_knowledge``（关键词打分检索）            登录用户
============================  ==========================================  ====================

错误码约定（与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 会话不存在 | 404 | 会话不存在：``{id}`` |
| 访问他人会话 / 他人会话的消息 | 403 | 权限不足：只能访问本人的会话 |
| 消息为空 / 超过 2000 字 | 400 | 消息不能为空 / 消息长度不能超过 2000 字 |
| 生成 / 读取活动简报的非管理角色 | 403 | 权限不足：只有管理员或工作人员可以生成活动简报 |
| 活动不存在 | 404 | 签到活动不存在：``{id}`` |
| 简报尚未生成 | 404 | 该活动尚未生成简报 |
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from src.assistant import crud
from src.assistant.models import AIConversation, AIKnowledge, AIMessage, AIMessageRole
from src.assistant.provider import (
    LLMChatResult,
    LLMProvider,
    ToolCall,
    build_llm_provider,
)
from src.assistant.schemas import (
    MESSAGE_MAX_LENGTH,
    TOOL_RESULT_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ChatResponse,
    ConversationOut,
    MessageOut,
    SummaryOut,
    ToolCallOut,
)
from src.checkin import service as checkin_service
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import settings
from src.event import service as event_service
from src.event.models import EventStatus
from src.family import crud as family_crud
from src.leave import service as leave_service
from src.leave.models import LeaveStatus
from src.statistics import service as statistics_service
from src.user.models import User, UserRole

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_DEFINITIONS",
    "build_system_prompt",
    "chat",
    "chat_stream",
    "delete_conversation",
    "generate_event_summary",
    "get_event_summary_saved",
    "list_conversations",
    "list_messages",
    "search_knowledge",
]

#: 可查看统计类工具的返回角色（与 ``src.statistics.router`` 的依赖保持一致）
_STATISTICS_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})

#: 可使用活动简报能力的角色
_SUMMARY_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})

#: 会话上下文携带的历史消息条数上限（约 5~6 轮）
HISTORY_MESSAGE_LIMIT = 12

#: 注入系统提示词的单条知识库答案截断长度
KNOWLEDGE_ANSWER_PROMPT_LENGTH = 300

#: 工具结果摘要中最多展示的明细行数
TOOL_DETAIL_LIMIT = 8

#: 工具调用默认分页大小（工具结果要保持精简，避免污染上下文）
TOOL_PAGE_SIZE = 5

#: 模型未产出任何内容时的兜底回复
EMPTY_ANSWER_FALLBACK = "（抱歉，本轮没有得到有效内容，请换个说法再试一次）"

#: 家庭用户调用统计类工具时的结果串（不抛异常，交由模型转述）
STATISTICS_FORBIDDEN_RESULT = (
    "无权限查看统计报表：该能力仅管理员与工作人员可用；"
    "家庭用户可查询「本户签到情况」（get_event_summary）"
)

#: 活动状态取值 -> 中文（用于工具结果与简报）
_STATUS_LABELS: dict[str, str] = {
    EventStatus.PENDING.value: EventStatus.PENDING.label,
    EventStatus.ACTIVE.value: EventStatus.ACTIVE.label,
    EventStatus.FINISHED.value: EventStatus.FINISHED.label,
    EventStatus.CANCELLED.value: EventStatus.CANCELLED.label,
}

#: 请假状态取值 -> 中文
_LEAVE_LABELS: dict[str, str] = {
    LeaveStatus.PENDING.value: LeaveStatus.PENDING.label,
    LeaveStatus.APPROVED.value: LeaveStatus.APPROVED.label,
    LeaveStatus.REJECTED.value: LeaveStatus.REJECTED.label,
    LeaveStatus.CANCELLED.value: LeaveStatus.CANCELLED.label,
}

#: 签到状态取值 -> 中文
_CHECKIN_LABELS: dict[str, str] = {
    "signed": "已签到",
    "late": "迟到",
    "absent": "缺勤",
    "leave": "请假",
    "abnormal": "异常",
}

# ----------------------------------------------------------------------
# 工具声明（OpenAI tools 格式：type=function + JSON Schema）
# ----------------------------------------------------------------------
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "查询签到活动列表（可按状态与关键字筛选）。用于回答「最近有什么活动」「进行中的活动有哪些」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "active", "finished", "cancelled"],
                        "description": "活动状态：pending 未开始 / active 进行中 / finished 已结束 / cancelled 已取消",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "活动名称或地点关键字",
                    },
                    "page": {"type": "integer", "description": "页码，从 1 开始"},
                    "page_size": {"type": "integer", "description": "每页条数，1~20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_detail",
            "description": "查询单个签到活动的详情（时间、地点、状态、迟到阈值）。需要活动ID；不知道ID时先用 get_events 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer", "description": "签到活动ID"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_summary",
            "description": (
                "查询某活动的签到汇总与明细（应签到人数、已签到/迟到/缺勤/请假/异常计数、出勤率）。"
                "家庭用户只能看到本户汇总与明细，管理员与工作人员看到全村数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer", "description": "签到活动ID"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_leave_status",
            "description": "查询请假申请（可按活动与审批状态筛选）。家庭用户只能看到本户的请假记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer", "description": "签到活动ID（可选）"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "rejected", "cancelled"],
                        "description": "审批状态：pending 待审批 / approved 已通过 / rejected 已驳回 / cancelled 已撤销",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistics_overview",
            "description": "查询全村统计总览（家庭数、成员数、活动数、实际签到次数、平均出勤率）。仅管理员与工作人员可用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_families_ranking",
            "description": "查询家庭参与度排行（仅参与过已结束活动的家庭入榜）。仅管理员与工作人员可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "village": {"type": "string", "description": "按村组精确筛选"},
                    "keyword": {"type": "string", "description": "户号或户主姓名关键字"},
                    "order_by": {
                        "type": "string",
                        "enum": ["rate", "checkins", "members"],
                        "description": "排序字段：rate 出勤率 / checkins 签到次数 / members 在册成员数",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["desc", "asc"],
                        "description": "排序方向：desc 降序 / asc 升序",
                    },
                    "page": {"type": "integer", "description": "页码，从 1 开始"},
                    "page_size": {"type": "integer", "description": "每页条数，1~20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索平台使用指南知识库，回答「怎么做」类问题"
                "（如何注册、如何添加成员、如何录入人脸、如何请假、迟到怎么判定、照片存在哪里等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户问题或其中关键词"},
                },
                "required": ["query"],
            },
        },
    },
]

#: 工具名 -> 参数说明（用于系统提示词中的工具清单）
_TOOL_SUMMARY: dict[str, str] = {
    item["function"]["name"]: item["function"]["description"]  # type: ignore[index]
    for item in TOOL_DEFINITIONS
}


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def _truncate(text: str, limit: int) -> str:
    """截断文本并追加省略标记。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（已截断）"


def _arg_int(args: dict[str, Any], key: str, *, default: int | None = None) -> int | None:
    """安全地读取整数入参（模型可能给出字符串或非法值）。"""
    value = args.get(key, default)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BusinessError(f"参数 {key} 必须是整数：{value!r}") from exc


def _arg_str(args: dict[str, Any], key: str, *, default: str | None = None) -> str | None:
    """安全地读取字符串入参。"""
    value = args.get(key, default)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _arg_page(args: dict[str, Any]) -> tuple[int, int]:
    """读取分页参数并收敛到安全范围（页码 ≥1，每页 1~20）。"""
    page = _arg_int(args, "page", default=1) or 1
    page_size = _arg_int(args, "page_size", default=TOOL_PAGE_SIZE) or TOOL_PAGE_SIZE
    return max(1, page), min(max(1, page_size), 20)


def _format_time(value: datetime | None) -> str:
    """时间格式化（工具结果给人看，精确到分钟即可）。"""
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _format_rate(rate: float | None) -> str:
    """出勤率格式化（0.8571 -> 85.71%）。"""
    return "暂无" if rate is None else f"{rate * 100:.2f}%"


def _enum_value(raw: str | None, allowed: dict[str, str], *, name: str) -> str | None:
    """校验枚举入参，非法取值直接报错（模型会据此重新组织答案）。"""
    if raw is None:
        return None
    if raw not in allowed:
        raise BusinessError(
            f"参数 {name} 取值非法：{raw}（可选值：{'/'.join(allowed)}）"
        )
    return raw


# ----------------------------------------------------------------------
# 轻量 RAG：知识库检索
# ----------------------------------------------------------------------
def search_knowledge(
    db: Session, query: str, *, top_k: int | None = None
) -> list[tuple[AIKnowledge, int]]:
    """按关键词命中数检索知识库（轻量 RAG）。

    打分规则（无需向量库，村级知识库规模下足够有效且结果稳定）：

    1. **主序：关键词命中数**——把 ``ai_knowledge.keywords`` 按逗号切分，
       统计其中作为子串出现在用户问题里的个数，命中越多排得越靠前；
    2. **次序：二元组覆盖率**——用问题的 2 字滑窗去匹配 ``question + keywords``，
       用于覆盖关键词表未预料到的问法（例如问「如何添加家庭成员」而关键词是
       「添加成员」时，靠"添加/家庭/成员"等二元组仍能召回该条）；
    3. 完全相同得分时按条目 ``id`` 升序，保证结果可断言、可复现。

    :return: ``[(知识条目, 关键词命中数)]``，按上述规则降序，最多 ``top_k``
        （默认 ``settings.AI_KNOWLEDGE_TOP_K``）条
    """
    limit = top_k if top_k is not None else settings.AI_KNOWLEDGE_TOP_K
    text = (query or "").strip().lower()
    if not text or limit <= 0:
        return []

    grams = {text[index : index + 2] for index in range(len(text) - 1)} or {text}
    scored: list[tuple[AIKnowledge, int, int]] = []
    for row in crud.list_knowledge(db):
        keyword_hits = sum(1 for keyword in row.keyword_list if keyword.lower() in text)
        haystack = f"{row.question}{row.keywords}".lower()
        gram_hits = sum(1 for gram in grams if gram in haystack)
        if keyword_hits or gram_hits:
            scored.append((row, keyword_hits, gram_hits))

    scored.sort(key=lambda item: (-item[1], -item[2], item[0].id))
    return [(row, keyword_hits) for row, keyword_hits, _gram_hits in scored[:limit]]


def _knowledge_prompt_block(db: Session, query: str) -> str:
    """把检索命中的 Q&A 渲染成系统提示词片段（无命中时返回空串）。"""
    hits = search_knowledge(db, query)
    if not hits:
        return ""
    lines = ["【平台使用指南（知识库检索命中，回答「怎么做」类问题请优先依据它）】"]
    for row, _score in hits:
        lines.append(f"Q：{row.question}")
        lines.append(f"A：{_truncate(row.answer, KNOWLEDGE_ANSWER_PROMPT_LENGTH)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 系统提示词
# ----------------------------------------------------------------------
def _user_profile_text(db: Session, user: User) -> str:
    """当前用户画像：角色、姓名、所属家庭户号（家庭用户）。"""
    parts = [f"角色：{user.role.label}（{user.role.value}）", f"姓名：{user.real_name}"]
    if user.role == UserRole.FAMILY:
        family = family_crud.get_family_by_owner(db, user.id)
        if family is None:
            parts.append("所属家庭：暂无家庭档案")
        else:
            parts.append(
                f"所属家庭：户号 {family.household_no or '未编号'}"
                f"｜村组 {family.village or '未填写'}（只能看到本户数据）"
            )
    else:
        parts.append("数据范围：全村（管理员 / 工作人员）")
    return "；".join(parts)


def build_system_prompt(db: Session, user: User, query: str | None = None) -> str:
    """构造系统提示词（含时间、用户画像、平台能力、工具规范与知识库命中）。"""
    tools_text = "\n".join(
        f"- {name}：{description}" for name, description in _TOOL_SUMMARY.items()
    )
    sections = [
        "你是「乡村基层活动智能签到管理平台」的 AI 助手，服务于村委管理员、"
        "现场工作人员与家庭户主。",
        f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（服务器本地时间）",
        f"当前用户：{_user_profile_text(db, user)}",
        "平台能力简介：家庭与成员管理、人脸录入与识别、签到活动管理、人脸/手动签到、"
        "请假申请与审批、签到统计报表（总览/单活动/家庭排行/趋势）。",
        "工具使用规范（必须遵守）：\n"
        "1. 涉及活动、签到、请假、统计等事实性问题时，**必须先调用工具查询真实数据再回答**，"
        "严禁凭记忆或想象编造数字、姓名与时间；\n"
        "2. 工具返回的内容就是唯一事实来源，回答时需要给出具体数字，并可说明数据来源"
        "（如「活动签到汇总」）；\n"
        "3. 统计报表类工具（get_statistics_overview / get_families_ranking）仅管理员与"
        "工作人员可用；家庭用户调用会得到「无权限」结果，请如实告知其只能查看本户数据；\n"
        "4. 「怎么做」类问题（如何注册、如何添加成员、人脸照片在哪、迟到怎么判定等）"
        "优先依据下方知识库内容，并给出具体接口或操作路径；\n"
        "5. 工具无法回答时如实说明，不要虚构数据；回答使用简体中文，条理清晰、简明扼要。",
        f"可用工具：\n{tools_text}",
    ]
    knowledge = _knowledge_prompt_block(db, query or "")
    if knowledge:
        sections.append(knowledge)
    return "\n\n".join(sections)


# ----------------------------------------------------------------------
# 工具实现（全部复用既有 service，权限链与接口一致）
# ----------------------------------------------------------------------
def _tool_get_events(db: Session, user: User, args: dict[str, Any]) -> str:
    """活动列表（``src.event.service.list_events``）。"""
    status = _arg_str(args, "status")
    _enum_value(status, _STATUS_LABELS, name="status")
    keyword = _arg_str(args, "keyword")
    page, page_size = _arg_page(args)

    events, total = event_service.list_events(
        db,
        status=EventStatus(status) if status else None,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    if not events:
        return "未查询到符合条件的签到活动（可放宽状态或关键字条件后重试）"

    lines = [f"共 {total} 个活动（第 {page} 页，每页 {page_size} 条）："]
    for event in events:
        lines.append(
            f"- ID={event.id}「{event.name}」状态={event.status.label}"
            f"｜时间={_format_time(event.start_time)}~{_format_time(event.end_time)}"
            f"｜地点={event.location or '未填写'}"
            f"｜迟到阈值={event.late_threshold_minutes} 分钟"
        )
    return "\n".join(lines)


def _tool_get_event_detail(db: Session, user: User, args: dict[str, Any]) -> str:
    """活动详情（``src.event.service.get_event_detail``）。"""
    event_id = _arg_int(args, "event_id")
    if event_id is None:
        raise BusinessError("缺少参数 event_id（活动ID）")

    event = event_service.get_event(db, event_id)
    return (
        f"活动 ID={event.id}「{event.name}」\n"
        f"- 状态：{event.status.label}（{event.status.value}）\n"
        f"- 时间：{_format_time(event.start_time)} ~ {_format_time(event.end_time)}\n"
        f"- 地点：{event.location or '未填写'}\n"
        f"- 说明：{event.description or '无'}\n"
        f"- 迟到阈值：{event.late_threshold_minutes} 分钟"
    )


def _tool_get_event_summary(db: Session, user: User, args: dict[str, Any]) -> str:
    """活动签到汇总（复用 ``GET /api/checkins/events/{id}`` 的角色感知逻辑）。"""
    event_id = _arg_int(args, "event_id")
    if event_id is None:
        raise BusinessError("缺少参数 event_id（活动ID）；可先用 get_events 查询活动ID")

    detail = checkin_service.get_event_checkins(
        db, user, event_id, page=1, page_size=TOOL_DETAIL_LIMIT
    )
    summary = detail.summary
    scope = "全村" if user.role in _STATISTICS_ROLES else "本户"
    lines = [
        f"活动「{detail.event.name}」（{detail.event.status.label}）{scope}签到汇总：",
        f"- 应签到 {summary.total_expected} 人｜已签到 {summary.signed}｜迟到 {summary.late}"
        f"｜缺勤 {summary.absent}｜请假 {summary.leave}｜异常 {summary.abnormal}"
        f"｜出勤率 {_format_rate(summary.attendance_rate)}",
    ]
    if detail.checkins.items:
        lines.append(f"签到明细（共 {detail.checkins.total} 条，最多展示 {TOOL_DETAIL_LIMIT} 条）：")
        for record in detail.checkins.items:
            label = _CHECKIN_LABELS.get(record.status.value, record.status.value)
            lines.append(
                f"- {record.member_name}：{label}"
                f"（{_format_time(record.checked_at) if record.checked_at else '无签到时间'}）"
            )
    else:
        lines.append("签到明细：暂无记录")
    return "\n".join(lines)


def _tool_get_leave_status(db: Session, user: User, args: dict[str, Any]) -> str:
    """请假查询（``src.leave.service.list_leaves``，家庭用户被强制收敛到本户）。"""
    event_id = _arg_int(args, "event_id")
    status = _arg_str(args, "status")
    _enum_value(status, _LEAVE_LABELS, name="status")

    leaves, total = leave_service.list_leaves(
        db,
        user,
        event_id=event_id,
        status=LeaveStatus(status) if status else None,
        page=1,
        page_size=TOOL_DETAIL_LIMIT,
    )
    scope = "全部" if user.role in _STATISTICS_ROLES else "本户"
    if not leaves:
        return f"未查询到{scope}请假记录（可按活动或状态筛选）"

    lines = [f"{scope}请假记录共 {total} 条（最多展示 {TOOL_DETAIL_LIMIT} 条）："]
    for leave in leaves:
        lines.append(
            f"- {leave.member_name}｜活动「{leave.event_name}」"
            f"｜状态={leave.status.label}｜事由={leave.reason}"
        )
    return "\n".join(lines)


def _tool_get_statistics_overview(db: Session, user: User, args: dict[str, Any]) -> str:
    """全村总览（``src.statistics.service.build_overview``，仅管理员/工作人员）。"""
    if user.role not in _STATISTICS_ROLES:
        return STATISTICS_FORBIDDEN_RESULT

    overview = statistics_service.build_overview(db)
    return (
        "全村统计总览：\n"
        f"- 正常家庭 {overview.total_families} 户｜正常成员 {overview.total_members} 人\n"
        f"- 活动 {overview.total_events} 场（进行中 {overview.active_events}，"
        f"已结束 {overview.finished_events}）\n"
        f"- 实际签到 {overview.total_checkins} 次｜平均出勤率 "
        f"{_format_rate(overview.avg_attendance_rate)}"
    )


def _tool_get_families_ranking(db: Session, user: User, args: dict[str, Any]) -> str:
    """家庭参与度排行（``src.statistics.service.list_family_rankings``，仅管理员/工作人员）。"""
    if user.role not in _STATISTICS_ROLES:
        return STATISTICS_FORBIDDEN_RESULT

    order_by = _arg_str(args, "order_by", default="rate") or "rate"
    if order_by not in {"rate", "checkins", "members"}:
        raise BusinessError(
            f"参数 order_by 取值非法：{order_by}（可选值：rate/checkins/members）"
        )
    order = _arg_str(args, "order", default="desc") or "desc"
    if order not in {"desc", "asc"}:
        raise BusinessError(f"参数 order 取值非法：{order}（可选值：desc/asc）")

    page, page_size = _arg_page(args)
    families, total = statistics_service.list_family_rankings(
        db,
        village=_arg_str(args, "village"),
        keyword=_arg_str(args, "keyword"),
        order_by=order_by,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
        page=page,
        page_size=page_size,
    )
    if not families:
        return "暂无入榜家庭（家庭排行只统计参与过已结束活动的家庭）"

    lines = [f"家庭参与度排行（第 {page} 页，共 {total} 户入榜，按 {order_by} {order}）："]
    for index, item in enumerate(families, start=(page - 1) * page_size + 1):
        lines.append(
            f"{index}. {item.household_no or '无户号'} {item.owner_name}"
            f"｜在册 {item.active_member_count} 人｜参与 {item.event_participated_count} 场"
            f"｜签到 {item.total_checkins} 次｜出勤率 {_format_rate(item.attendance_rate)}"
        )
    return "\n".join(lines)


def _tool_search_knowledge(db: Session, user: User, args: dict[str, Any]) -> str:
    """知识库检索（轻量 RAG）。"""
    query = _arg_str(args, "query")
    if not query:
        raise BusinessError("缺少参数 query（要检索的问题）")

    hits = search_knowledge(db, query)
    if not hits:
        return f"知识库中没有与「{query}」匹配的使用指南，请结合平台功能如实回答或建议联系管理员"

    lines = [f"知识库命中 {len(hits)} 条："]
    for row, _score in hits:
        lines.append(f"Q：{row.question}\nA：{row.answer}")
    return "\n".join(lines)


#: 工具名 -> 处理函数
_TOOL_HANDLERS: dict[str, Callable[[Session, User, dict[str, Any]], str]] = {
    "get_events": _tool_get_events,
    "get_event_detail": _tool_get_event_detail,
    "get_event_summary": _tool_get_event_summary,
    "get_leave_status": _tool_get_leave_status,
    "get_statistics_overview": _tool_get_statistics_overview,
    "get_families_ranking": _tool_get_families_ranking,
    "search_knowledge": _tool_search_knowledge,
}


def _execute_tool(db: Session, user: User, name: str, args: dict[str, Any]) -> str:
    """执行一次工具调用：集中分发 + 权限校验 + 结果摘要（≤500 字）。

    工具内的业务异常（如活动不存在、统计无权限）会被转换为结果串交给模型转述，
    而不会让整轮对话失败——这样用户得到的是一句解释，而不是 500 错误。
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _truncate(
            f"未知工具：{name}。可用工具：{'、'.join(_TOOL_HANDLERS)}",
            TOOL_RESULT_MAX_LENGTH,
        )

    logger.info("AI 工具调用：%s(%s) user=%s", name, args, user.username)
    try:
        result = handler(db, user, args or {})
    except BusinessError as exc:
        logger.info("AI 工具业务异常：%s -> %s", name, exc.message)
        result = f"工具执行失败：{exc.message}"
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.exception("AI 工具执行异常：%s", name)
        result = f"工具执行异常：{exc.__class__.__name__}"

    return _truncate(str(result), TOOL_RESULT_MAX_LENGTH)


# ----------------------------------------------------------------------
# 会话与上下文
# ----------------------------------------------------------------------
def _validate_message(message: str) -> str:
    """校验用户消息（非空、≤2000 字）。"""
    text = (message or "").strip()
    if not text:
        raise BusinessError("消息不能为空", code=ResponseCode.PARAM_ERROR)
    if len(text) > MESSAGE_MAX_LENGTH:
        raise BusinessError(
            f"消息长度不能超过 {MESSAGE_MAX_LENGTH} 字（当前 {len(text)} 字）",
            code=ResponseCode.PARAM_ERROR,
        )
    return text


def _load_or_create_conversation(
    db: Session, user: User, message: str, conversation_id: int | None
) -> AIConversation:
    """加载或新建会话（跨用户访问返回 403，不存在返回 404）。"""
    if conversation_id is None:
        return crud.create_conversation(
            db, user_id=user.id, title=message[:TITLE_MAX_LENGTH]
        )

    conversation = crud.get_conversation_by_id(db, conversation_id)
    if conversation is None:
        raise BusinessError(
            f"会话不存在：{conversation_id}", code=ResponseCode.NOT_FOUND
        )
    if conversation.user_id != user.id:
        raise BusinessError(
            "权限不足：只能访问本人的会话", code=ResponseCode.FORBIDDEN
        )
    if not conversation.title:
        crud.update_conversation(
            db, conversation, {"title": message[:TITLE_MAX_LENGTH]}
        )
    return conversation


def _history_message(row: AIMessage) -> dict[str, Any] | None:
    """把历史消息还原成 OpenAI 消息格式。

    - ``tool`` 消息不再单独发送（缺少 ``tool_call_id`` 会破坏协议），
      其内容已经作为工具调用摘要写进上一条 assistant 消息；
    - 带工具调用的 assistant 消息渲染为"回复 + 上轮工具调用摘要"文本，
      既保留事实来源，又避免跨服务商的协议差异。
    """
    if row.role == AIMessageRole.TOOL:
        return None

    content = row.content or ""
    calls = _loads_tool_calls(row.tool_calls)
    if row.role == AIMessageRole.ASSISTANT and calls:
        detail = "；".join(
            f"{call.get('name')}({json.dumps(call.get('args') or {}, ensure_ascii=False)}) "
            f"→ {_truncate(str(call.get('result') or ''), 120)}"
            for call in calls
        )
        content = f"{content}\n（上一轮工具调用：{detail}）".strip()
    if not content:
        return None
    return {"role": row.role.value, "content": content}


def _build_messages(
    db: Session, user: User, conversation: AIConversation, query: str
) -> list[dict[str, Any]]:
    """构造本轮请求上下文：system + 最近 N 条历史（已含刚写入的当前用户消息）。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(db, user, query)}
    ]
    rows = crud.list_recent_messages(
        db, conversation_id=conversation.id, limit=HISTORY_MESSAGE_LIMIT
    )
    for row in rows:
        message = _history_message(row)
        if message is not None:
            messages.append(message)
    return messages


def _loads_tool_calls(raw: str | None) -> list[dict[str, Any]]:
    """解析 ``ai_message.tool_calls``（非法 JSON 视为空列表）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tool_calls 不是合法 JSON：%s", raw[:200])
        return []
    return parsed if isinstance(parsed, list) else []


def _dumps_tool_calls(records: list[dict[str, Any]]) -> str | None:
    """序列化工具调用轨迹（仅保留 name / args / result，控制列体积）。"""
    if not records:
        return None
    payload = [
        {
            "name": record["name"],
            "args": record["args"],
            "result": _truncate(str(record["result"]), TOOL_RESULT_MAX_LENGTH),
        }
        for record in records
    ]
    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------------
# Agent 循环
# ----------------------------------------------------------------------
def _truncated_answer(rounds: int, records: list[dict[str, Any]]) -> str:
    """工具轮数达到上限时的强制停止提示（含最后一次工具结果摘要）。"""
    note = (
        f"已达到工具调用轮数上限（{rounds} 轮，可通过 AI_TOOL_MAX_ROUNDS 调整），"
        "已停止继续查询。"
    )
    if records:
        note += f"\n已获取到的最后一条信息：{records[-1]['result']}"
    return note


def _run_agent_loop(
    db: Session,
    user: User,
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    *,
    max_rounds: int | None = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """执行"LLM → 工具 → LLM"循环。

    :return: ``(最终文本, 工具调用轨迹, 是否因轮数上限被截断)``
    """
    limit = max_rounds if max_rounds is not None else settings.AI_TOOL_MAX_ROUNDS
    records: list[dict[str, Any]] = []
    rounds = 0

    while True:
        if rounds >= limit:
            return _truncated_answer(rounds, records), records, True

        result = provider.chat(messages, tools=TOOL_DEFINITIONS)
        if isinstance(result, Iterator):  # pragma: no cover - 防御性兜底
            raise BusinessError("内部错误：工具循环不应使用流式响应")
        if not isinstance(result, LLMChatResult):  # pragma: no cover - 防御性兜底
            raise BusinessError("内部错误：LLM 返回类型不符合预期")

        if not result.wants_tools:
            return result.content or "", records, False

        # 回填 assistant 的工具调用消息（OpenAI 协议要求 tool 消息紧跟其后）
        messages.append(
            {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [call.to_openai() for call in result.tool_calls],
            }
        )
        for call in result.tool_calls:
            summary = _execute_tool(db, user, call.name, call.arguments)
            records.append({"name": call.name, "args": call.arguments, "result": summary})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id or f"call_{call.name}",
                    "name": call.name,
                    "content": summary,
                }
            )
        rounds += 1


def _stream_tokens(
    provider: LLMProvider, messages: list[dict[str, Any]], fallback: str
) -> Iterator[str]:
    """产出最终回答的文本增量。

    工具循环（非流式）结束后，最终文本以流式方式下发：真实服务商这里是**真实的
    SSE 流**（打字机效果、可随时中断），Mock 提供方按 4 字切块。
    若流式调用没有产出任何内容（例如脚本化 Mock 队列已耗尽），
    回退为按块切分已确定的最终文本，保证 SSE 一定有 ``token`` 事件。
    """
    produced = False
    buffer: list[str] = []
    try:
        stream = provider.chat(messages, tools=None, stream=True)
        if not isinstance(stream, Iterator):  # pragma: no cover - 防御性兜底
            raise BusinessError("内部错误：流式调用未返回生成器")
        for chunk in stream:
            if not chunk:
                continue
            produced = True
            buffer.append(str(chunk))
            yield str(chunk)
    except BusinessError as exc:
        # 已经开流，无法再改状态码，以 error 事件告知（由调用方渲染）
        logger.error("流式下发失败：%s", exc.message)
        yield from _fallback_chunks(fallback)
        return

    if not produced:
        yield from _fallback_chunks(fallback)


def _fallback_chunks(text: str, size: int = 4) -> Iterator[str]:
    """把已确定的文本按块切分（流式兜底）。"""
    for index in range(0, len(text), size):
        yield text[index : index + size]


# ----------------------------------------------------------------------
# 对话接口
# ----------------------------------------------------------------------
def chat(
    db: Session,
    current_user: User,
    message: str,
    conversation_id: int | None = None,
    *,
    provider: LLMProvider | None = None,
) -> ChatResponse:
    """非流式对话：跑完工具循环后一次性返回完整回答。

    :raises BusinessError: 消息为空/超长（400）、会话不存在（404）、跨用户会话（403）
    """
    text = _validate_message(message)
    llm = provider or build_llm_provider()
    conversation = _load_or_create_conversation(db, current_user, text, conversation_id)

    crud.create_message(
        db, conversation_id=conversation.id, role=AIMessageRole.USER, content=text
    )
    messages = _build_messages(db, current_user, conversation, text)

    answer, records, _truncated = _run_agent_loop(db, current_user, messages, llm)
    if not answer:
        answer = EMPTY_ANSWER_FALLBACK

    crud.create_message(
        db,
        conversation_id=conversation.id,
        role=AIMessageRole.ASSISTANT,
        content=answer,
        tool_calls=_dumps_tool_calls(records),
    )
    crud.touch_conversation(db, conversation)
    db.commit()

    return ChatResponse(
        conversation_id=conversation.id,
        reply=answer,
        tool_calls=[ToolCallOut(**record) for record in records],
    )


def chat_stream(
    db: Session,
    current_user: User,
    message: str,
    conversation_id: int | None = None,
    *,
    provider: LLMProvider | None = None,
) -> Iterator[str]:
    """流式对话：返回 SSE 事件文本生成器。

    事件序列：``start`` → 若干 ``tool`` → 若干 ``token`` → ``done``，
    每行形如 ``data: {json}\\n\\n``。

    校验与用户消息落库在**进入生成器之前**完成，因此参数错误仍以标准
    统一响应格式返回（400/403/404），不会先返回 200 再在流里报错。
    """
    text = _validate_message(message)
    llm = provider or build_llm_provider()
    conversation = _load_or_create_conversation(db, current_user, text, conversation_id)

    crud.create_message(
        db, conversation_id=conversation.id, role=AIMessageRole.USER, content=text
    )
    db.commit()

    conversation_id_value = conversation.id

    def event_stream() -> Iterator[str]:
        yield _sse({"type": "start", "conversation_id": conversation_id_value})

        messages = _build_messages(db, current_user, conversation, text)
        try:
            answer, records, truncated = _run_agent_loop(
                db, current_user, messages, llm
            )
        except BusinessError as exc:
            logger.error("AI 对话失败：%s", exc.message)
            yield _sse({"type": "error", "message": exc.message})
            return

        for record in records:
            yield _sse(
                {
                    "type": "tool",
                    "name": record["name"],
                    "args": record["args"],
                    "result": record["result"],
                }
            )

        chunks: list[str] = []
        if truncated or not answer:
            # 轮数被截断（或在流式前就已知无内容）时不再请求模型，直接下发已有文本
            source = answer or EMPTY_ANSWER_FALLBACK
            for chunk in _fallback_chunks(source):
                chunks.append(chunk)
                yield _sse({"type": "token", "content": chunk})
        else:
            for chunk in _stream_tokens(llm, messages, answer):
                chunks.append(chunk)
                yield _sse({"type": "token", "content": chunk})

        reply = "".join(chunks).strip() or EMPTY_ANSWER_FALLBACK
        crud.create_message(
            db,
            conversation_id=conversation_id_value,
            role=AIMessageRole.ASSISTANT,
            content=reply,
            tool_calls=_dumps_tool_calls(records),
        )
        crud.touch_conversation(db, conversation)
        db.commit()

        yield _sse(
            {
                "type": "done",
                "conversation_id": conversation_id_value,
                "reply": reply,
            }
        )

    return event_stream()


def _sse(payload: dict[str, Any]) -> str:
    """把事件体序列化为 SSE 文本行（每条事件以空行结束）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------------
# 会话查询与删除
# ----------------------------------------------------------------------
def list_conversations(
    db: Session, current_user: User, *, page: int = 1, page_size: int = 10
) -> tuple[list[ConversationOut], int]:
    """分页查询本人会话（最近活跃在前）。"""
    rows, total = crud.list_conversations(
        db, user_id=current_user.id, page=page, page_size=page_size
    )
    items = [
        ConversationOut(
            id=conversation.id,
            title=conversation.title,
            message_count=count,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )
        for conversation, count in rows
    ]
    return items, total


def _load_own_conversation(db: Session, current_user: User, conversation_id: int) -> AIConversation:
    """加载本人会话（不存在 404，非本人 403）。"""
    conversation = crud.get_conversation_by_id(db, conversation_id)
    if conversation is None:
        raise BusinessError(
            f"会话不存在：{conversation_id}", code=ResponseCode.NOT_FOUND
        )
    if conversation.user_id != current_user.id:
        raise BusinessError(
            "权限不足：只能访问本人的会话", code=ResponseCode.FORBIDDEN
        )
    return conversation


def list_messages(
    db: Session,
    current_user: User,
    conversation_id: int,
    *,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[MessageOut], int]:
    """分页查询会话消息（按时间正序）。"""
    _load_own_conversation(db, current_user, conversation_id)
    rows, total = crud.list_messages(
        db, conversation_id=conversation_id, page=page, page_size=page_size
    )
    items = [
        MessageOut(
            id=row.id,
            conversation_id=row.conversation_id,
            role=row.role,
            content=row.content,
            tool_calls=[ToolCallOut(**call) for call in _loads_tool_calls(row.tool_calls)],
            created_at=row.created_at,
        )
        for row in rows
    ]
    return items, total


def delete_conversation(db: Session, current_user: User, conversation_id: int) -> int:
    """删除本人会话（同时删除其消息）。

    :return: 级联删除的消息条数
    """
    conversation = _load_own_conversation(db, current_user, conversation_id)
    removed = crud.delete_messages_by_conversation(db, conversation.id)
    crud.delete_conversation(db, conversation, commit=False)
    db.commit()
    return removed


# ----------------------------------------------------------------------
# AIGC：活动简报
# ----------------------------------------------------------------------
#: 简报生成使用的系统提示词（强约束 JSON 输出，禁止编造数字）
SUMMARY_SYSTEM_PROMPT = (
    "你是乡村基层活动简报撰写助手。请**只依据用户提供的统计数据**撰写一份简明、"
    "面向村民的中文活动简报。\n"
    "硬性要求：\n"
    "1. 严禁编造任何数字、姓名、时间或事件，所有数字必须来自给定的统计数据；\n"
    "2. 必须只返回一个 JSON 对象，不要输出解释文字、不要用 Markdown 代码块包裹；\n"
    '3. JSON 结构：{"title": "简报标题（不超过 40 字）", '
    '"highlights": ["亮点1", "亮点2", "亮点3"], '
    '"body": "Markdown 正文（包含活动概况、签到情况分析、请假情况、后续建议四个部分）"}；\n'
    "4. 语言积极、务实，避免空话；人数较少的村庄要如实呈现，不夸大。"
)

#: 简报模板（模型输出无法解析时的降级方案）
SUMMARY_TEMPLATE = """# {title}

> {degraded_note}本简报由系统依据活动真实统计数据生成。

## 一、活动概况

- 活动名称：{event_name}
- 活动地点：{location}
- 活动时间：{start_time} ~ {end_time}
- 活动状态：{status_label}
- 迟到阈值：{late_threshold} 分钟

## 二、签到数据

| 指标 | 数值 |
| --- | --- |
| 应签到人数 | {total_expected} |
| 已签到 | {signed} |
| 迟到 | {late} |
| 缺勤 | {absent} |
| 请假 | {leave} |
| 异常 | {abnormal} |
| 出勤率 | {attendance_rate} |

## 三、出勤情况分析

- 到场 {present} 人（已签到 {signed} + 迟到 {late}），出勤率 {attendance_rate}；
- 缺勤 {absent} 人，{absent_comment}
- 请假 {leave_total} 人次（已通过 {approved}、待审批 {pending}），{leave_comment}

## 四、后续建议

{advice}
"""


def _summary_facts(db: Session, current_user: User, event_id: int) -> dict[str, Any]:
    """组装简报所需的真实统计快照（活动 + 签到汇总 + 请假统计）。"""
    event = event_service.load_event(db, event_id)
    summary = checkin_service.build_summary(db, event)

    leave_counts: dict[str, int] = {status.value: 0 for status in LeaveStatus}
    leaves, leave_total = leave_service.list_leaves(
        db, current_user, event_id=event_id, page=1, page_size=100
    )
    for leave in leaves:
        leave_counts[leave.status.value] = leave_counts.get(leave.status.value, 0) + 1

    facts: dict[str, Any] = {
        "event": {
            "id": event.id,
            "name": event.name,
            "location": event.location,
            "description": event.description,
            "start_time": _format_time(event.start_time),
            "end_time": _format_time(event.end_time),
            "status": event.status.value,
            "status_label": event.status.label,
            "late_threshold_minutes": event.late_threshold_minutes,
        },
        "checkin": {
            "total_expected": summary.total_expected,
            "signed": summary.signed,
            "late": summary.late,
            "absent": summary.absent,
            "leave": summary.leave,
            "abnormal": summary.abnormal,
            "attendance_rate": summary.attendance_rate,
        },
        "leave": {
            "total": leave_total,
            "pending": leave_counts.get(LeaveStatus.PENDING.value, 0),
            "approved": leave_counts.get(LeaveStatus.APPROVED.value, 0),
            "rejected": leave_counts.get(LeaveStatus.REJECTED.value, 0),
            "cancelled": leave_counts.get(LeaveStatus.CANCELLED.value, 0),
            "members": [
                {"member_name": leave.member_name, "status": leave.status.value}
                for leave in leaves[:TOOL_DETAIL_LIMIT]
            ],
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return facts


def _build_summary_messages(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """构造简报生成的请求消息（系统约束 + 真实统计快照）。"""
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请依据以下统计数据撰写活动简报，并严格按 JSON 结构返回：\n"
                f"{json.dumps(facts, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def _strip_code_fence(raw: str) -> str:
    """去掉模型可能附加的 ```json 代码块围栏。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _parse_summary_output(
    raw: str, facts: dict[str, Any]
) -> tuple[str, str, list[str], bool]:
    """解析模型输出。

    :return: ``(标题, Markdown 正文, 亮点列表, 是否降级)``；
        模型未按 JSON 约束输出时返回模板拼接结果并标记降级
    """
    payload: dict[str, Any] | None = None
    text = _strip_code_fence(raw or "")
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            logger.warning("简报输出不是合法 JSON，降级为模板拼接：%s", text[:200])

    event = facts["event"]
    checkin = facts["checkin"]
    default_title = f"{event['name']}活动简报"

    if not payload:
        return (*_template_summary(facts), True)

    title = str(payload.get("title") or "").strip() or default_title
    body = str(payload.get("body") or "").strip()
    raw_highlights = payload.get("highlights")
    highlights = [
        str(item).strip()
        for item in (raw_highlights if isinstance(raw_highlights, list) else [])
        if str(item).strip()
    ]
    if not body:
        logger.warning("简报输出缺少 body 字段，降级为模板拼接")
        return (*_template_summary(facts), True)

    lines = [f"# {title}", ""]
    if highlights:
        lines.append("## 活动亮点")
        lines.extend(f"- {item}" for item in highlights[:3])
        lines.append("")
    lines.append(body)
    lines.append("")
    lines.append(
        f"> 数据来源：本平台签到记录（应签到 {checkin['total_expected']} 人，"
        f"出勤率 {_format_rate(checkin['attendance_rate'])}），"
        f"生成时间 {facts['generated_at']}。"
    )
    return title, "\n".join(lines), highlights[:3], False


def _template_summary(facts: dict[str, Any]) -> tuple[str, str, list[str]]:
    """降级模板：直接用统计快照拼出简报（数据依然真实）。"""
    event = facts["event"]
    checkin = facts["checkin"]
    leave = facts["leave"]
    present = checkin["signed"] + checkin["late"]
    title = f"{event['name']}活动简报"

    absent_comment = (
        "已按平台规则计入缺勤，建议下次提前一天在群里提醒。"
        if checkin["absent"]
        else "无缺勤记录，全员到场。"
    )
    leave_comment = (
        "均已按请假流程处理。" if leave["total"] else "本次活动没有请假申请。"
    )
    advice = "\n".join(
        [
            f"1. 本次活动出勤率 {_format_rate(checkin['attendance_rate'])}，"
            f"到场 {present} 人（已签到 {checkin['signed']}、迟到 {checkin['late']}）。",
            f"2. 迟到 {checkin['late']} 人，"
            f"可考虑把签到开始时间提前 {event['late_threshold_minutes']} 分钟提醒村民。"
            if checkin["late"]
            else "2. 无迟到记录，现场组织有序。",
            "3. 建议在下次活动结束前完成人脸录入核对，减少现场手动签到。",
        ]
    )
    content = SUMMARY_TEMPLATE.format(
        title=title,
        degraded_note="**（降级）** 模型未按 JSON 约束返回，",
        event_name=event["name"],
        location=event["location"] or "未填写",
        start_time=event["start_time"],
        end_time=event["end_time"],
        status_label=event["status_label"],
        late_threshold=event["late_threshold_minutes"],
        total_expected=checkin["total_expected"],
        signed=checkin["signed"],
        late=checkin["late"],
        absent=checkin["absent"],
        leave=checkin["leave"],
        abnormal=checkin["abnormal"],
        attendance_rate=_format_rate(checkin["attendance_rate"]),
        present=present,
        absent_comment=absent_comment,
        leave_total=leave["total"],
        approved=leave["approved"],
        pending=leave["pending"],
        leave_comment=leave_comment,
        advice=advice,
    )
    highlights = [
        f"应签到 {checkin['total_expected']} 人，实际到场 {present} 人，"
        f"出勤率 {_format_rate(checkin['attendance_rate'])}",
        f"迟到 {checkin['late']} 人、缺勤 {checkin['absent']} 人、异常 {checkin['abnormal']} 人",
        f"请假 {leave['total']} 人次（已通过 {leave['approved']}、待审批 {leave['pending']}）",
    ]
    return title, content, highlights


def _to_summary_out(row: ActivitySummary, *, degraded: bool | None = None) -> SummaryOut:
    """ORM 简报表 -> 响应 DTO（meta 中的 JSON 解析为字典）。"""
    meta: dict[str, Any] = {}
    if row.meta:
        try:
            parsed = json.loads(row.meta)
            meta = parsed if isinstance(parsed, dict) else {"raw": row.meta}
        except json.JSONDecodeError:
            meta = {"raw": row.meta}

    return SummaryOut(
        id=row.id,
        event_id=row.event_id,
        title=row.title,
        content=row.content,
        meta=meta,
        degraded=bool(meta.get("degraded")) if degraded is None else degraded,
        created_by_id=row.created_by_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def generate_event_summary(
    db: Session,
    current_user: User,
    event_id: int,
    *,
    provider: LLMProvider | None = None,
) -> SummaryOut:
    """生成（或重新生成）活动简报并落库。

    流程：权限校验（admin/staff）→ 组装真实统计快照 → LLM 结构化输出
    （要求 JSON：title / highlights / body）→ 解析失败降级为模板拼接并标注「降级」
    → ``upsert`` ``activity_summary``（一活动一份）。

    :raises BusinessError: 非管理员/工作人员（403）、活动不存在（404）
    """
    if current_user.role not in _SUMMARY_ROLES:
        raise BusinessError(
            "权限不足：只有管理员或工作人员可以生成活动简报",
            code=ResponseCode.FORBIDDEN,
        )

    facts = _summary_facts(db, current_user, event_id)
    llm = provider or build_llm_provider()

    raw = ""
    try:
        result = llm.chat(_build_summary_messages(facts))
        if isinstance(result, LLMChatResult):
            raw = result.content
    except BusinessError as exc:
        # LLM 不可用时同样降级：简报仍然基于真实数据生成
        logger.warning("简报生成调用 LLM 失败，降级为模板：%s", exc.message)

    title, content, highlights, degraded = _parse_summary_output(raw, facts)
    meta = {
        **facts,
        "highlights": highlights,
        "degraded": degraded,
        "provider": getattr(llm, "name", "unknown"),
        "model": settings.LLM_MODEL or None,
    }
    meta_text = json.dumps(meta, ensure_ascii=False)

    existing = crud.get_summary_by_event(db, event_id)
    if existing is None:
        row = crud.create_summary(
            db,
            event_id=event_id,
            title=_truncate(title, 200),
            content=content,
            meta=meta_text,
            created_by_id=current_user.id,
        )
    else:
        row = crud.update_summary(
            db,
            existing,
            {
                "title": _truncate(title, 200),
                "content": content,
                "meta": meta_text,
                "created_by_id": current_user.id,
            },
        )

    logger.info(
        "活动简报已%s：event_id=%s user=%s degraded=%s",
        "更新" if existing is not None else "生成",
        event_id,
        current_user.username,
        degraded,
    )
    return _to_summary_out(row, degraded=degraded)


def get_event_summary_saved(
    db: Session, current_user: User, event_id: int
) -> SummaryOut:
    """读取已保存的活动简报（无则 404）。"""
    if current_user.role not in _SUMMARY_ROLES:
        raise BusinessError(
            "权限不足：只有管理员或工作人员可以查看活动简报",
            code=ResponseCode.FORBIDDEN,
        )

    row = crud.get_summary_by_event(db, event_id)
    if row is None:
        raise BusinessError(
            f"该活动尚未生成简报：{event_id}", code=ResponseCode.NOT_FOUND
        )
    return _to_summary_out(row)

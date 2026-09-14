"""AI 智能助手接口路由（挂载前缀 ``/api/assistant``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/assistant/chat`` | 多轮对话（工具调用结果随响应返回） | 登录用户 |
| POST | ``/api/assistant/chat/stream`` | 同上，SSE 流式（``text/event-stream``） | 登录用户 |
| GET | ``/api/assistant/conversations`` | 本人会话列表（分页，最近活跃在前） | 登录用户（仅本人） |
| GET | ``/api/assistant/conversations/{id}/messages`` | 本人会话消息（分页升序） | 登录用户（仅本人） |
| DELETE | ``/api/assistant/conversations/{id}`` | 删除本人会话（级联删除消息） | 登录用户（仅本人） |
| POST | ``/api/assistant/events/{event_id}/summary`` | 生成活动简报（AIGC） | 管理员 / 工作人员 |
| GET | ``/api/assistant/events/{event_id}/summary`` | 读取已保存的活动简报 | 管理员 / 工作人员 |

SSE 事件序列（``POST /api/assistant/chat/stream``）::

    data: {"type": "start", "conversation_id": 1}
    data: {"type": "tool", "name": "get_event_summary", "args": {...}, "result": "..."}
    data: {"type": "token", "content": "根据"}
    data: {"type": "token", "content": "查询"}
    data: {"type": "done", "conversation_id": 1, "reply": "根据查询结果：..."}

说明：

- 事件行以 ``data: {json}`` 形式下发、每条事件后跟一个空行，与
  ``EventSource``/``fetch`` 的通用解析方式兼容；工具循环全部完成后才开始下发
  ``token``，前端可先渲染"工具调用卡片"再呈现打字机效果；
- 对话与简报接口均为**统一响应格式**；只有 ``chat/stream`` 例外（SSE 文本流）；
- 家庭用户访问简报接口由 :data:`~src.auth.dependencies.StaffOrAdminUser` 返回 403，
  服务层同时保留独立校验（便于脚本/单测直接调用服务层）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.assistant import service as assistant_service
from src.assistant.provider import LLMProvider, get_llm_provider
from src.assistant.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationOut,
    MessageOut,
    StreamEvent,
    SummaryOut,
)
from src.auth.dependencies import CurrentUser, StaffOrAdminUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData

router = APIRouter()

#: LLM 提供方依赖（按 LLM_PROVIDER 配置返回 openai_compat 或 mock 实现）
LLMProviderDep = Annotated[LLMProvider, Depends(get_llm_provider)]

#: SSE 响应头（禁用中间层缓冲，保证增量实时到达浏览器）
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post(
    "/chat",
    response_model=ApiResponse[ChatResponse],
    summary="AI 对话",
    description=(
        "多轮对话：助手会先调用平台工具查询真实数据（活动/签到/请假/统计/知识库），"
        "再结合工具结果作答，`tool_calls` 字段返回本轮的工具调用轨迹"
        "（名称、入参、结果摘要），便于前端渲染与审计。"
        "不传 `conversation_id` 则新建会话（标题取消息前 20 字），"
        "传入则续接本人会话（他人会话返回 403）。"
        "回答完全基于本平台真实数据，默认 `LLM_PROVIDER=mock` 无需密钥即可跑通全流程。"
    ),
)
def chat(
    data: ChatRequest,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    provider: LLMProviderDep,
) -> dict[str, Any]:
    """多轮对话（非流式）。"""
    result = assistant_service.chat(
        db, current_user, data.message, data.conversation_id, provider=provider
    )
    return success_response(data=result)


@router.post(
    "/chat/stream",
    summary="AI 对话（SSE 流式）",
    description=(
        "与 `/chat` 入参、权限完全一致，但以 SSE 流式返回："
        "`start` → 若干 `tool`（工具调用轨迹）→ 若干 `token`（增量文本）→ `done`（完整回复与会话ID）。"
        "实现上工具循环走非流式（保证完整拿到 tool_calls），最终文本再逐段下发，"
        "因此前端可以「先展示工具调用、再打字机输出」。"
        "响应为 `text/event-stream`，不使用统一响应格式（与 CSV/图片接口同属例外）。"
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "text/event-stream": {"schema": StreamEvent.model_json_schema()}
            },
            "description": (
                "SSE 事件流：每条事件形如 `data: {json}`，事件后跟一个空行；"
                "事件体结构见 StreamEvent（type: start / tool / token / done）"
            ),
        }
    },
)
def chat_stream(
    data: ChatRequest,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    provider: LLMProviderDep,
) -> StreamingResponse:
    """多轮对话（SSE 流式）。"""
    events = assistant_service.chat_stream(
        db, current_user, data.message, data.conversation_id, provider=provider
    )
    return StreamingResponse(
        events, media_type="text/event-stream; charset=utf-8", headers=SSE_HEADERS
    )


@router.get(
    "/conversations",
    response_model=ApiResponse[PageData[ConversationOut]],
    summary="我的会话列表",
    description="分页查询本人会话（最近活跃在前），附带每个会话的消息条数。",
)
def list_conversations(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询本人会话列表。"""
    conversations, total = assistant_service.list_conversations(
        db, current_user, page=page, page_size=page_size
    )
    return success_response(
        data=PageData[ConversationOut](
            total=total, page=page, page_size=page_size, items=conversations
        )
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ApiResponse[PageData[MessageOut]],
    summary="会话消息列表",
    description=(
        "分页查询本人在指定会话中的消息（按时间正序，便于前端顺序渲染）；"
        "assistant 消息会带出该轮的工具调用轨迹。他人会话返回 403，不存在返回 404。"
    ),
)
def list_messages(
    conversation_id: Annotated[int, Path(ge=1, description="会话ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询会话消息。"""
    messages, total = assistant_service.list_messages(
        db, current_user, conversation_id, page=page, page_size=page_size
    )
    return success_response(
        data=PageData[MessageOut](
            total=total, page=page, page_size=page_size, items=messages
        )
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=ApiResponse[dict],
    summary="删除会话",
    description="删除本人会话（同时删除该会话下的全部消息），返回级联删除的消息条数。",
)
def delete_conversation(
    conversation_id: Annotated[int, Path(ge=1, description="会话ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """删除本人会话。"""
    removed = assistant_service.delete_conversation(db, current_user, conversation_id)
    return success_response(
        data={"conversation_id": conversation_id, "deleted_messages": removed},
        message="会话已删除",
    )


@router.post(
    "/events/{event_id}/summary",
    response_model=ApiResponse[SummaryOut],
    summary="生成活动简报（AIGC）",
    description=(
        "依据活动真实统计数据（应签到/各状态计数/出勤率/请假审批情况）生成 Markdown 活动简报："
        "要求模型返回严格 JSON（title / highlights / body），解析失败时**降级**为模板拼接"
        "并在正文中标注「降级」（此时 `degraded=true`）。一活动一份，重复生成覆盖更新。"
        "`meta` 字段返回本次生成使用的统计快照。仅管理员与工作人员可用（家庭用户 403）。"
    ),
)
def generate_event_summary(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    provider: LLMProviderDep,
) -> dict[str, Any]:
    """生成（或重新生成）活动简报。"""
    summary = assistant_service.generate_event_summary(
        db, current_user, event_id, provider=provider
    )
    message = "简报已生成" if not summary.degraded else "简报已生成（模型输出不规范，已降级为模板）"
    return success_response(data=summary, message=message)


@router.get(
    "/events/{event_id}/summary",
    response_model=ApiResponse[SummaryOut],
    summary="读取活动简报",
    description="读取已保存的活动简报（最近一次生成的结果）；尚未生成返回 404。仅管理员与工作人员可用。",
)
def get_event_summary(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """读取已保存的活动简报。"""
    return success_response(
        data=assistant_service.get_event_summary_saved(db, current_user, event_id)
    )

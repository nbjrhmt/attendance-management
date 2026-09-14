"""AI 智能助手模块的请求/响应数据结构（Pydantic 模型）。

约定与其他模块一致：输入模型 ``extra="forbid"`` 拒绝未声明字段；
输出模型开启 ``from_attributes`` 以便由 ORM 对象直接生成。

模型一览：

- :class:`ChatRequest` / :class:`ChatResponse`：对话接口的入参与出参（含工具调用轨迹）；
- :class:`StreamEvent`：SSE 事件体（``start`` / ``tool`` / ``token`` / ``done``），
  仅用于文档展示，实际下发的是 ``data: {json}`` 文本行；
- :class:`ConversationOut` / :class:`MessageOut`：会话与消息列表；
- :class:`SummaryOut`：活动简报（AIGC）读写响应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.assistant.models import AIMessageRole

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ConversationOut",
    "MESSAGE_MAX_LENGTH",
    "MessageOut",
    "StreamEvent",
    "SummaryOut",
    "ToolCallOut",
]

#: 单条用户消息最大长度（超出返回 400）
MESSAGE_MAX_LENGTH = 2000

#: 工具结果摘要最大长度（写入 role=tool 消息与 tool_calls 记录）
TOOL_RESULT_MAX_LENGTH = 500

#: 会话标题最大长度（取自首条用户消息前 20 字）
TITLE_MAX_LENGTH = 20

#: SSE 事件类型
StreamEventType = Literal["start", "tool", "token", "done", "error"]


class ChatRequest(BaseModel):
    """对话请求。

    ``conversation_id`` 为空时新建会话（标题自动取本条消息前 20 字），
    传入时续接该会话（必须属于当前登录用户，否则 403）。
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=MESSAGE_MAX_LENGTH,
        description=f"用户消息，1~{MESSAGE_MAX_LENGTH} 字",
    )
    conversation_id: int | None = Field(
        default=None, ge=1, description="会话ID；不传则新建会话"
    )

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        """去除首尾空白（全空白消息在服务层按「消息不能为空」拒绝）。"""
        return value.strip()


class ToolCallOut(BaseModel):
    """一次工具调用的轨迹（名称 / 入参 / 结果摘要）。"""

    name: str = Field(description="工具名称")
    args: dict[str, Any] = Field(default_factory=dict, description="工具入参")
    result: str = Field(description="工具结果摘要（最长 500 字）")


class ChatResponse(BaseModel):
    """对话响应。"""

    conversation_id: int = Field(description="会话ID（新建时返回新ID）")
    reply: str = Field(description="助手回复文本")
    tool_calls: list[ToolCallOut] = Field(
        default_factory=list, description="本轮实际执行的工具调用轨迹（无工具时为空数组）"
    )


class StreamEvent(BaseModel):
    """SSE 事件体（文档用）。

    事件顺序：``start`` → 若干 ``tool`` → 若干 ``token`` → ``done``；
    工具循环全部完成后才开始下发 ``token``，因此客户端可先渲染工具调用卡片，
    再呈现打字机效果。``error`` 仅用于请求通过校验后、流式过程中发生的异常。
    """

    type: StreamEventType = Field(description="事件类型：start / tool / token / done")
    conversation_id: int | None = Field(default=None, description="会话ID")
    name: str | None = Field(default=None, description="工具名称（type=tool）")
    args: dict[str, Any] | None = Field(default=None, description="工具入参（type=tool）")
    result: str | None = Field(default=None, description="工具结果摘要（type=tool）")
    content: str | None = Field(default=None, description="增量文本（type=token）")
    reply: str | None = Field(default=None, description="完整回复（type=done）")


class ConversationOut(BaseModel):
    """会话列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="会话ID")
    title: str | None = Field(default=None, description="会话标题")
    message_count: int = Field(default=0, description="会话消息条数")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="最近更新时间")


class MessageOut(BaseModel):
    """会话消息。"""

    id: int = Field(description="消息ID")
    conversation_id: int = Field(description="会话ID")
    role: AIMessageRole = Field(description="角色：user / assistant / tool")
    content: str | None = Field(default=None, description="消息正文")
    tool_calls: list[ToolCallOut] = Field(
        default_factory=list, description="工具调用轨迹（仅 assistant 消息可能有值）"
    )
    created_at: datetime = Field(description="创建时间")


class SummaryOut(BaseModel):
    """活动简报（AIGC）响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="简报ID")
    event_id: int = Field(description="签到活动ID")
    title: str = Field(description="简报标题")
    content: str = Field(description="简报全文（Markdown）")
    meta: dict[str, Any] = Field(
        default_factory=dict, description="生成时使用的统计快照（活动/签到/请假口径）"
    )
    degraded: bool = Field(
        default=False,
        description="是否为「降级」结果：模型未按 JSON 约束输出时由模板拼接生成",
    )
    created_by_id: int | None = Field(default=None, description="生成人ID")
    created_at: datetime = Field(description="首次生成时间")
    updated_at: datetime = Field(description="最近生成时间")

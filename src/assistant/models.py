"""AI 智能助手模块的数据模型（四张表）。

| 表 | 说明 | 关键约束 |
| --- | --- | --- |
| ``ai_conversation`` | 会话（一个用户多条会话） | ``INDEX(user_id)``，随账号删除级联清理 |
| ``ai_message`` | 会话消息（user / assistant / tool） | ``INDEX(conversation_id, id)``，随会话删除级联清理 |
| ``ai_knowledge`` | 平台使用指南知识库（轻量 RAG 语料） | ``INDEX(question)`` |
| ``activity_summary`` | 活动简报（AIGC 生成，一活动一份） | ``UNIQUE(event_id)``，随活动删除级联清理 |

设计要点：

- **会话记忆落库**：多轮对话的上下文来自 ``ai_message``（按 ``id`` 升序取最近 N 条），
  服务重启后依然可以续聊，不依赖任何进程内缓存；
- **工具调用可审计**：``ai_message.tool_calls`` 以 JSON 文本保存本轮的工具名、
  入参摘要与结果摘要，既供前端渲染"工具调用卡片"，也便于事后核对
  "回答是否真的基于平台数据"；
- **级联策略**：会话消息随会话、会话随账号、简报随活动删除（``ON DELETE CASCADE``），
  而 ``activity_summary.created_by_id`` 用 ``SET NULL`` 保留简报本身
  （管理员账号删除后简报仍然可读）。

.. note::
   ``activity_summary.created_by_id`` 在需求中写作"NOT NULL + ON DELETE SET NULL"，
   但 MySQL 8 明确拒绝该组合（错误码 1830：**Column 'x' cannot be NOT NULL:
   needed in a foreign key constraint 'y' SET NULL**，已在本机 MySQL 8.0.41 实测复现）。
   为保留"管理员注销后简报不消失"的语义，此处按项目既有约定
   （``checkin_record.reviewed_by_id`` / ``operation_log.user_id``）把该列定义为
   **可空 + ON DELETE SET NULL**，写入时由服务层保证一定有值。
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column

__all__ = ["AIConversation", "AIKnowledge", "AIMessage", "AIMessageRole", "ActivitySummary"]


class AIMessageRole(str, Enum):
    """会话消息角色（与 OpenAI Chat Completions 的角色保持一致）。"""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    @property
    def label(self) -> str:
        """中文名称，用于提示信息与日志。"""
        return {"user": "用户", "assistant": "助手", "tool": "工具"}[self.value]


class AIConversation(BaseModel):
    """AI 会话表。"""

    __tablename__ = "ai_conversation"
    __table_args__ = (
        Index("ix_ai_conversation_user_id", "user_id"),
        {"comment": "AI 会话表（一个用户多条会话，标题取首条用户消息前 20 字）"},
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sys_user.id", ondelete="CASCADE"),
        nullable=False,
        comment="会话所属用户ID（sys_user.id），账号删除时级联删除会话",
    )
    title: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="会话标题，自动取首条用户消息前 20 字",
    )

    def __repr__(self) -> str:
        return f"<AIConversation id={self.id} user_id={self.user_id} title={self.title!r}>"


class AIMessage(BaseModel):
    """AI 会话消息表（多轮对话的记忆载体）。"""

    __tablename__ = "ai_message"
    __table_args__ = (
        Index("ix_ai_message_conversation_id_id", "conversation_id", "id"),
        {"comment": "AI 会话消息表（user / assistant / tool，按 id 升序还原上下文）"},
    )

    conversation_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ai_conversation.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话ID（ai_conversation.id），会话删除时级联删除消息",
    )
    role: Mapped[AIMessageRole] = mapped_column(
        enum_column(AIMessageRole, name="ai_message_role"),
        nullable=False,
        comment="消息角色：user 用户 / assistant 助手 / tool 工具结果",
    )
    content: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="消息正文；工具消息为工具结果摘要",
    )
    tool_calls: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="工具调用 JSON 数组（name / arguments / result 摘要），仅 assistant 消息有值",
    )

    def __repr__(self) -> str:
        return (
            f"<AIMessage id={self.id} conversation_id={self.conversation_id} "
            f"role={self.role.value}>"
        )


class AIKnowledge(BaseModel):
    """平台使用指南知识库表（轻量 RAG 语料）。"""

    __tablename__ = "ai_knowledge"
    __table_args__ = (
        Index("ix_ai_knowledge_question", "question"),
        {"comment": "AI 知识库表（平台使用指南 Q&A，关键词命中打分检索）"},
    )

    question: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="标准问题（如「如何添加家庭成员」）",
    )
    keywords: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="逗号分隔的检索关键词，命中数越多排序越靠前",
    )
    answer: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="标准答案（含接口路径与业务规则）",
    )

    @property
    def keyword_list(self) -> list[str]:
        """切分关键词（兼容中英文逗号与顿号，去空去重）。"""
        raw = self.keywords.replace("，", ",").replace("、", ",")
        items = [part.strip() for part in raw.split(",")]
        seen: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.append(item)
        return seen

    def __repr__(self) -> str:
        return f"<AIKnowledge id={self.id} question={self.question!r}>"


class ActivitySummary(BaseModel):
    """活动简报表（AIGC 生成，一活动一份，重复生成则覆盖更新）。"""

    __tablename__ = "activity_summary"
    __table_args__ = (
        {"comment": "活动简报表（由 AI 依据活动真实统计数据生成，一活动一份）"},
    )

    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("event.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="签到活动ID（event.id），唯一；活动删除时级联删除简报",
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="简报标题",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="简报全文（Markdown）",
    )
    meta: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="生成时使用的统计快照（JSON：应签到/各状态计数/出勤率/生成时间）",
    )
    created_by_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sys_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="生成人ID（sys_user.id）；账号删除后置空，简报仍保留（写入时由服务层保证非空）",
    )

    def __repr__(self) -> str:
        return f"<ActivitySummary id={self.id} event_id={self.event_id} title={self.title!r}>"

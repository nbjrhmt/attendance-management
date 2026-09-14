"""AI 智能助手数据访问层（CRUD）。

本层只负责数据库读写：不做权限判断（会话归属校验在 :mod:`src.assistant.service`），
写操作按调用方要求 ``flush`` 或 ``commit``（默认 ``commit=True``，
因为会话与消息都是"说完即落库"的独立事务）。

设计说明：

- **消息按 ``id`` 升序**：``ai_message`` 是自增主键表，``id`` 顺序即写入顺序，
  比 ``created_at`` 更精确（同一秒内写入多条消息时时间戳可能相同），
  因此上下文还原与分页都按 ``id`` 排序；
- **级联删除在服务层显式执行**（:func:`delete_messages_by_conversation`）：
  MySQL 有 ``ON DELETE CASCADE`` 兜底，但 SQLite（单元测试）默认不启用外键约束，
  显式删除才能保证两种环境下行为一致；
- **知识库为小表**：打分检索在应用层完成，直接整表取出（见
  :func:`src.assistant.service.search_knowledge`），不引入全文索引或向量库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from src.assistant.models import (
    AIConversation,
    AIKnowledge,
    AIMessage,
    AIMessageRole,
    ActivitySummary,
)

__all__ = [
    "count_conversations",
    "count_knowledge",
    "count_messages",
    "create_conversation",
    "create_knowledge",
    "create_message",
    "create_summary",
    "delete_all_knowledge",
    "delete_conversation",
    "delete_messages_by_conversation",
    "get_conversation_by_id",
    "get_knowledge_by_question",
    "get_summary_by_event",
    "list_conversations",
    "list_knowledge",
    "list_messages",
    "list_recent_messages",
    "list_summaries",
    "touch_conversation",
    "update_conversation",
    "update_summary",
]


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------
def create_conversation(
    db: Session,
    *,
    user_id: int,
    title: str | None = None,
    commit: bool = True,
) -> AIConversation:
    """新建会话。"""
    conversation = AIConversation(user_id=user_id, title=title)
    db.add(conversation)
    if commit:
        db.commit()
        db.refresh(conversation)
    else:
        db.flush()
    return conversation


def get_conversation_by_id(db: Session, conversation_id: int) -> AIConversation | None:
    """按主键查询会话。"""
    return db.get(AIConversation, conversation_id)


def count_conversations(db: Session, user_id: int) -> int:
    """统计某用户的会话总数。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(AIConversation)
            .where(AIConversation.user_id == user_id)
        )
        or 0
    )


def list_conversations(
    db: Session, *, user_id: int, page: int = 1, page_size: int = 10
) -> tuple[list[tuple[AIConversation, int]], int]:
    """分页查询某用户的会话（最近更新的在前），并带出每个会话的消息条数。

    :return: ``([(会话, 消息条数)], 总数)``
    """
    total = count_conversations(db, user_id)
    rows = db.execute(
        select(AIConversation, func.count(AIMessage.id))
        .outerjoin(AIMessage, AIMessage.conversation_id == AIConversation.id)
        .where(AIConversation.user_id == user_id)
        .group_by(AIConversation.id)
        # 最近活跃的会话排在最前；同一时间戳时按ID倒序，保证分页稳定
        .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [(conversation, int(count)) for conversation, count in rows], total


def update_conversation(
    db: Session, conversation: AIConversation, values: dict[str, Any]
) -> AIConversation:
    """更新会话字段（如标题）。"""
    for field, value in values.items():
        setattr(conversation, field, value)
    db.commit()
    db.refresh(conversation)
    return conversation


def touch_conversation(db: Session, conversation: AIConversation) -> None:
    """刷新会话的 ``updated_at``（新消息写入后调用，保证列表排序反映最近活跃）。

    显式写入 :func:`datetime.now` 而不依赖列的 ``onupdate``：
    MySQL 与 SQLite 的时间函数差异较大，显式赋值在两个环境下行为完全一致。
    """
    db.execute(
        update(AIConversation)
        .where(AIConversation.id == conversation.id)
        .values(updated_at=datetime.now())
    )
    db.flush()


def delete_messages_by_conversation(db: Session, conversation_id: int) -> int:
    """删除会话下的全部消息（不提交，随调用方事务）。

    :return: 删除的消息条数
    """
    result = db.execute(
        delete(AIMessage).where(AIMessage.conversation_id == conversation_id)
    )
    return int(result.rowcount or 0)


def delete_conversation(
    db: Session, conversation: AIConversation, *, commit: bool = True
) -> None:
    """删除会话（同时显式删除其消息，见模块文档说明）。"""
    delete_messages_by_conversation(db, conversation.id)
    db.delete(conversation)
    if commit:
        db.commit()
    else:
        db.flush()


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------
def create_message(
    db: Session,
    *,
    conversation_id: int,
    role: AIMessageRole,
    content: str | None = None,
    tool_calls: str | None = None,
    commit: bool = True,
) -> AIMessage:
    """写入一条会话消息。"""
    message = AIMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
    )
    db.add(message)
    if commit:
        db.commit()
        db.refresh(message)
    else:
        db.flush()
    return message


def count_messages(db: Session, conversation_id: int) -> int:
    """统计会话消息条数。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
        )
        or 0
    )


def list_messages(
    db: Session, *, conversation_id: int, page: int = 1, page_size: int = 10
) -> tuple[list[AIMessage], int]:
    """分页查询会话消息（按 ``id`` 升序，即时间正序）。"""
    total = count_messages(db, conversation_id)
    items = list(
        db.scalars(
            select(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, total


def list_recent_messages(
    db: Session, *, conversation_id: int, limit: int
) -> list[AIMessage]:
    """取会话最近 ``limit`` 条消息（返回时恢复为时间正序）。"""
    if limit <= 0:
        return []
    rows = list(
        db.scalars(
            select(AIMessage)
            .where(AIMessage.conversation_id == conversation_id)
            .order_by(AIMessage.id.desc())
            .limit(limit)
        ).all()
    )
    return list(reversed(rows))


# ----------------------------------------------------------------------
# 知识库
# ----------------------------------------------------------------------
def create_knowledge(
    db: Session,
    *,
    question: str,
    keywords: str,
    answer: str,
    commit: bool = True,
) -> AIKnowledge:
    """新增一条知识库问答。"""
    item = AIKnowledge(question=question, keywords=keywords, answer=answer)
    db.add(item)
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()
    return item


def get_knowledge_by_question(db: Session, question: str) -> AIKnowledge | None:
    """按标准问题查询（种子脚本幂等判重使用）。"""
    return db.scalar(select(AIKnowledge).where(AIKnowledge.question == question))


def list_knowledge(db: Session, *, limit: int | None = None) -> list[AIKnowledge]:
    """取出知识库全部条目（按 ``id`` 升序，便于打分时稳定排序）。"""
    statement = select(AIKnowledge).order_by(AIKnowledge.id.asc())
    if limit is not None:
        statement = statement.limit(limit)
    return list(db.scalars(statement).all())


def count_knowledge(db: Session) -> int:
    """统计知识库条目数。"""
    return int(db.scalar(select(func.count()).select_from(AIKnowledge)) or 0)


def delete_all_knowledge(db: Session) -> int:
    """清空知识库（种子脚本 ``--force`` 重灌使用）。

    :return: 删除条数
    """
    result = db.execute(delete(AIKnowledge))
    db.commit()
    return int(result.rowcount or 0)


# ----------------------------------------------------------------------
# 活动简报
# ----------------------------------------------------------------------
def get_summary_by_event(db: Session, event_id: int) -> ActivitySummary | None:
    """查询某活动的简报（一活动一份）。"""
    return db.scalar(
        select(ActivitySummary).where(ActivitySummary.event_id == event_id)
    )


def create_summary(
    db: Session,
    *,
    event_id: int,
    title: str,
    content: str,
    meta: str | None,
    created_by_id: int | None,
    commit: bool = True,
) -> ActivitySummary:
    """新增简报。"""
    summary = ActivitySummary(
        event_id=event_id,
        title=title,
        content=content,
        meta=meta,
        created_by_id=created_by_id,
    )
    db.add(summary)
    if commit:
        db.commit()
        db.refresh(summary)
    else:
        db.flush()
    return summary


def update_summary(
    db: Session, summary: ActivitySummary, values: dict[str, Any]
) -> ActivitySummary:
    """更新简报（重复生成时的覆盖路径）。"""
    for field, value in values.items():
        setattr(summary, field, value)
    db.commit()
    db.refresh(summary)
    return summary


def list_summaries(
    db: Session, *, page: int = 1, page_size: int = 10
) -> tuple[list[ActivitySummary], int]:
    """分页查询全部简报（最近生成的在最前）。"""
    total = int(db.scalar(select(func.count()).select_from(ActivitySummary)) or 0)
    items = list(
        db.scalars(
            select(ActivitySummary)
            .order_by(ActivitySummary.updated_at.desc(), ActivitySummary.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, total

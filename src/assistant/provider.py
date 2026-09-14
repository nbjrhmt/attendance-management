"""LLM 提供方抽象（对齐 :mod:`src.face.provider` 的组织方式）。

统一封装"对话补全"能力，使 :mod:`src.assistant.service` 与具体服务商解耦：

- :class:`OpenAICompatProvider`：用 ``httpx`` 直调 ``{LLM_BASE_URL}/chat/completions``
  （OpenAI 兼容协议，DeepSeek / 豆包方舟 / 通义 / 本地 vLLM 均可），
  请求头 ``Authorization: Bearer {LLM_API_KEY}``，支持 OpenAI ``tools`` 格式与 SSE 流式解析；
- :class:`MockLLMProvider`：**无密钥全流程可用**，默认回答为模板化提示，
  并支持"脚本化响应队列"让单元测试可以确定性断言工具调用链路；
- :func:`get_llm_provider`：FastAPI 依赖入口，按 ``LLM_PROVIDER`` 返回实现并做进程内缓存
  （复用 httpx 连接池；单测通过 ``app.dependency_overrides`` 替换）。

.. note::
   **流式与工具调用的取舍**（与需求一致）：``stream=True`` 时不做流式工具调用，
   而是"先非流式跑完工具循环，最终文本再逐段下发"——
   即 agent 循环每轮都用非流式请求（可完整拿到 ``tool_calls``），
   确定不再需要工具后，再以流式方式重新生成最终回答并逐段 ``yield``。
   这样实现简单、演示真实（前端能看到真实的 SSE 增量），
   也避免了流式场景下按 ``index`` 聚合 ``tool_call`` 增量的复杂度。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "LLMChatResult",
    "LLMProvider",
    "MockLLMProvider",
    "OpenAICompatProvider",
    "ToolCall",
    "build_llm_provider",
    "get_llm_provider",
]

#: 标准的 OpenAI 工具调用终止符
_STREAM_DONE = "[DONE]"

#: 工具结果摘要最大长度（与 :mod:`src.assistant.schemas` 保持一致）
TOOL_RESULT_MAX_LENGTH = 500


@dataclass(slots=True)
class ToolCall:
    """模型请求执行的一次工具调用。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: 模型给出的调用 ID（OpenAI 协议要求回传，便于多轮对齐）
    id: str = ""

    def to_openai(self) -> dict[str, Any]:
        """转换为 OpenAI ``message.tool_calls`` 格式（用于回填上下文）。"""
        return {
            "id": self.id or f"call_{self.name}",
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class LLMChatResult:
    """一次非流式对话的结果。

    :param content: 模型回复文本（调用工具时通常为空串）
    :param tool_calls: 模型请求执行的工具调用列表
    :param raw_message: 原始 ``choices[0].message``（回填上下文时保持协议一致）
    """

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] | None = None

    @property
    def wants_tools(self) -> bool:
        """模型是否请求调用工具。"""
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """LLM 提供方协议。"""

    name: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> LLMChatResult | Iterator[str]:
        """对话补全。

        :param messages: OpenAI Chat Completions 格式的消息列表
        :param tools: OpenAI ``tools`` 格式的工具声明；``None`` 表示本轮不允许调用工具
        :param stream: ``False`` 返回 :class:`LLMChatResult`；
            ``True`` 返回**文本增量生成器**（仅文本，不含工具调用）
        """


# ----------------------------------------------------------------------
# OpenAI 兼容实现
# ----------------------------------------------------------------------
class OpenAICompatProvider:
    """OpenAI 兼容协议实现（httpx 直调，无官方 SDK 依赖）。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self.model = model if model is not None else settings.LLM_MODEL
        self.timeout = timeout if timeout is not None else settings.LLM_TIMEOUT_SECONDS
        self._client = http_client

    # -- 内部工具 ------------------------------------------------------
    @property
    def chat_completions_url(self) -> str:
        """对话补全地址（``{base_url}/chat/completions``）。"""
        return f"{self.base_url}/chat/completions"

    def _ensure_configured(self) -> None:
        """校验配置完整性，缺失时给出可操作的中文提示（503）。"""
        if not (self.base_url and self.api_key and self.model):
            raise BusinessError(
                "未配置 LLM：请在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，"
                "并设置 LLM_PROVIDER=openai_compat；"
                "若仅需演示流程可保持 LLM_PROVIDER=mock",
                code=ResponseCode.SERVICE_UNAVAILABLE,
            )

    def _headers(self) -> dict[str, str]:
        """请求头（Bearer 认证 + JSON）。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """构造请求体；未声明工具时不下发 ``tools`` 字段。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
        """解析 ``message.tool_calls``（参数为 JSON 字符串，解析失败按空参处理）。"""
        calls: list[ToolCall] = []
        for item in raw_tool_calls or []:
            function = item.get("function") or {}
            raw_arguments = function.get("arguments")
            arguments: dict[str, Any] = {}
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif isinstance(raw_arguments, str) and raw_arguments.strip():
                try:
                    parsed = json.loads(raw_arguments)
                    arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
                except json.JSONDecodeError:
                    logger.warning("工具入参不是合法 JSON，按空参处理：%s", raw_arguments)
                    arguments = {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            calls.append(
                ToolCall(name=name, arguments=arguments, id=str(item.get("id") or ""))
            )
        return calls

    def _request(self, payload: dict[str, Any]) -> tuple[httpx.Response, httpx.Client]:
        """发起 HTTP 请求，返回 ``(响应, 客户端)``（统一异常映射为 503）。

        流式请求使用 ``client.send(request, stream=True)``，调用方负责 ``close()``。
        """
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            if payload.get("stream"):
                request = client.build_request(
                    "POST",
                    self.chat_completions_url,
                    headers=self._headers(),
                    json=payload,
                )
                return client.send(request, stream=True), client
            response = client.post(
                self.chat_completions_url, headers=self._headers(), json=payload
            )
        except httpx.HTTPError as exc:
            logger.error("调用 LLM 失败：%s", exc)
            if self._client is None:
                client.close()
            raise BusinessError(
                f"LLM 服务调用失败：{exc}", code=ResponseCode.SERVICE_UNAVAILABLE
            ) from exc

        if response.status_code >= 400:
            logger.error(
                "LLM 返回错误：status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            raise BusinessError(
                f"LLM 服务返回错误（HTTP {response.status_code}）：{response.text[:200]}",
                code=ResponseCode.SERVICE_UNAVAILABLE,
            )
        return response, client

    # -- 能力实现 ------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> LLMChatResult | Iterator[str]:
        """对话补全（``stream=True`` 时返回文本增量生成器）。"""
        self._ensure_configured()
        payload = self._build_payload(messages, tools, stream=stream)
        if stream:
            return self._stream_tokens(payload)
        return self._chat_once(payload)

    def _chat_once(self, payload: dict[str, Any]) -> LLMChatResult:
        """非流式：解析 ``choices[0].message`` 的 ``content`` 与 ``tool_calls``。"""
        response, client = self._request(payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise BusinessError(
                "LLM 返回内容不是合法 JSON", code=ResponseCode.SERVICE_UNAVAILABLE
            ) from exc
        finally:
            if self._client is None:
                client.close()

        choices = data.get("choices") or []
        if not choices:
            raise BusinessError(
                "LLM 返回结果为空（choices 为空）", code=ResponseCode.SERVICE_UNAVAILABLE
            )

        message = choices[0].get("message") or {}
        return LLMChatResult(
            content=str(message.get("content") or ""),
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            raw_message=message,
        )

    def _stream_tokens(self, payload: dict[str, Any]) -> Iterator[str]:
        """流式：逐行解析 ``data: {...}`` 增量，遇 ``[DONE]`` 结束。"""
        stream, client = self._request(payload)
        try:
            for raw_line in stream.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == _STREAM_DONE:
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("忽略无法解析的流式分片：%s", data[:200])
                    continue
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)
        except httpx.HTTPError as exc:
            logger.error("读取 LLM 流式响应失败：%s", exc)
            raise BusinessError(
                f"LLM 流式响应读取失败：{exc}", code=ResponseCode.SERVICE_UNAVAILABLE
            ) from exc
        finally:
            stream.close()
            if self._client is None:
                client.close()


# ----------------------------------------------------------------------
# Mock 实现
# ----------------------------------------------------------------------
#: 无工具可调用时的默认回复
MOCK_DEFAULT_REPLY = "（mock 模式）已收到你的问题，配置真实 LLM key 后可获得智能回答"

#: 默认回复前缀（含工具结果时）
MOCK_TOOL_REPLY_PREFIX = "根据查询结果："

#: 工具结果摘要截取长度
MOCK_TOOL_SUMMARY_LENGTH = 100

#: 流式分块长度（字）
MOCK_STREAM_CHUNK_SIZE = 4

#: 工具结果中活动ID的提取模式（用于"自动工具"两步调用）
_EVENT_ID_PATTERN = re.compile(r"ID=(\d+)")

#: 无脚本时的"自动工具"意图表：命中任一关键词即调用对应工具（按顺序优先匹配）。
#: 目的：**没有 LLM 密钥也能演示真实的工具调用链**（服务端仍会真实查询平台数据）。
_INTENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "签到情况",
            "签到汇总",
            "签到统计",
            "签到明细",
            "签到",
            "出勤",
            "缺勤",
            "迟到",
            "谁没来",
        ),
        "get_event_summary",
    ),
    (("请假",), "get_leave_status"),
    (("统计", "总览", "排行", "排名", "参与度"), "get_statistics_overview"),
    (("活动", "大会", "安排", "日程"), "get_events"),
    (("怎么", "如何", "在哪", "哪里", "流程", "规则", "为什么", "多久", "限制"), "search_knowledge"),
)

#: 需要先拿到活动ID才能调用的工具（自动工具模式下先调 ``get_events`` 取ID）
_EVENT_SCOPED_TOOLS: frozenset[str] = frozenset({"get_event_summary"})

#: 判定"活动汇总/请假结果已拿到"的标记（避免自动工具重复调用）
_EVENT_RESULT_MARKERS: tuple[str, ...] = ("签到汇总", "请假记录")

#: 活动简报提示词标记：Mock 提供方据此识别"这是结构化简报请求"，
#: 并按模板返回合法 JSON（见 :meth:`MockLLMProvider._plan_summary_result`）。
#: 该字符串是 :data:`src.assistant.service.SUMMARY_SYSTEM_PROMPT` 的一部分，两者必须保持同步。
SUMMARY_PROMPT_MARKER = "活动简报撰写助手"


class MockLLMProvider:
    """无密钥可用的 Mock 提供方。

    行为（可确定性预测，便于演示与单测）：

    1. **脚本化响应队列**：构造时传入 ``script``（:class:`LLMChatResult` 或等价 dict 列表），
       每次 :meth:`chat` 依次弹出；队列耗尽后回落到默认行为。
       例如首轮返回 ``get_event_summary`` 工具调用、次轮返回总结文本，
       即可在完全离线的情况下断言"工具调用 → 结果回填 → 最终回答"整条链路；
    2. **自动工具（``auto_tools=True``，默认）**：没有脚本时按 :data:`_INTENT_RULES`
       识别用户意图并主动请求调用对应工具——问「我家签到情况」会先调 ``get_events``
       拿到活动ID、再调 ``get_event_summary``，最终回答引用真实汇总数据。
       这是"**无密钥也能演示真实工具调用链**"的关键：服务端照样执行工具、照样查库；
       传入 ``auto_tools=False`` 或提供 ``script`` 即可关闭；
    3. **默认行为**：若上一条消息是 ``role=tool``，回复
       ``根据查询结果：{首个工具结果前 100 字}``（因此"回答确实基于平台数据"）；
       否则回复 :data:`MOCK_DEFAULT_REPLY`；
    4. **流式**：把最终文本按 ``MOCK_STREAM_CHUNK_SIZE`` 字切块 ``yield``，
       拼接后与 :meth:`chat` 的返回完全一致（SSE 演示与断言都依赖这一点）；
       流式请求**不触发自动工具**（工具调用统一走非流式，见模块文档）。
    """

    name = "mock"

    def __init__(
        self,
        script: list[LLMChatResult | dict[str, Any]] | None = None,
        *,
        chunk_size: int = MOCK_STREAM_CHUNK_SIZE,
        auto_tools: bool = True,
    ) -> None:
        self._script: list[LLMChatResult] = [
            self._normalize(item) for item in (script or [])
        ]
        self.chunk_size = chunk_size
        self.auto_tools = auto_tools
        #: 每次调用的入参快照（单测据此断言工具结果已进入上下文）
        self.calls: list[dict[str, Any]] = []

    # -- 内部工具 ------------------------------------------------------
    @staticmethod
    def _normalize(item: LLMChatResult | dict[str, Any]) -> LLMChatResult:
        """把 dict 形式的脚本项规范化为 :class:`LLMChatResult`。"""
        if isinstance(item, LLMChatResult):
            return item
        calls = [
            ToolCall(
                name=str(call.get("name") or ""),
                arguments=dict(call.get("arguments") or {}),
                id=str(call.get("id") or ""),
            )
            for call in (item.get("tool_calls") or [])
        ]
        return LLMChatResult(content=str(item.get("content") or ""), tool_calls=calls)

    def push(self, item: LLMChatResult | dict[str, Any]) -> None:
        """追加一条脚本化响应（供测试在运行中补充脚本）。"""
        self._script.append(self._normalize(item))

    @staticmethod
    def _last_tool_content(messages: list[dict[str, Any]]) -> str | None:
        """取最后一条 ``role=tool`` 消息的内容（默认回复据此生成）。"""
        for message in reversed(messages):
            if message.get("role") == "tool":
                content = message.get("content")
                return str(content) if content else None
        return None

    def _default_reply(self, messages: list[dict[str, Any]]) -> str:
        """默认回复：有工具结果则引用工具结果，否则给出配置提示。"""
        tool_content = self._last_tool_content(messages)
        if tool_content:
            return f"{MOCK_TOOL_REPLY_PREFIX}{tool_content[:MOCK_TOOL_SUMMARY_LENGTH]}"
        return MOCK_DEFAULT_REPLY

    # -- 自动工具（无脚本时的意图识别） --------------------------------
    @staticmethod
    def _last_message_by_role(
        messages: list[dict[str, Any]], role: str
    ) -> dict[str, Any] | None:
        """取最后一条指定角色的消息。"""
        for message in reversed(messages):
            if message.get("role") == role:
                return message
        return None

    @classmethod
    def _last_user_content(cls, messages: list[dict[str, Any]]) -> str:
        """取最后一条用户消息的文本。"""
        message = cls._last_message_by_role(messages, "user")
        return str(message.get("content") or "") if message else ""

    @staticmethod
    def _detect_tool(question: str) -> str | None:
        """按 :data:`_INTENT_RULES` 识别意图对应的工具名（无命中返回 ``None``）。"""
        for keywords, tool_name in _INTENT_RULES:
            if any(keyword in question for keyword in keywords):
                return tool_name
        return None

    def _plan_tool_call(self, messages: list[dict[str, Any]]) -> ToolCall | None:
        """根据当前上下文规划下一次工具调用（自动工具模式）。

        两轮规划：

        1. 需要活动ID的工具（如 ``get_event_summary``）先调 ``get_events`` 取ID；
        2. 拿到活动列表后解析首个 ``ID=数字`` 再调目标工具，之后不再规划（交给默认回复引用结果）。
        """
        question = self._last_user_content(messages)
        if not question:
            return None
        target = self._detect_tool(question)
        if target is None:
            return None

        tool_content = self._last_tool_content(messages) or ""
        if any(marker in tool_content for marker in _EVENT_RESULT_MARKERS):
            return None  # 目标结果已拿到，交给默认回复

        if target in _EVENT_SCOPED_TOOLS:
            if tool_content:
                match = _EVENT_ID_PATTERN.search(tool_content)
                if match is None:
                    return None  # 活动列表为空或没有可解析的ID
                return ToolCall(
                    name=target, arguments={"event_id": int(match.group(1))}
                )
            return ToolCall(name="get_events", arguments={"page_size": 5})

        if tool_content:
            return None  # 该轮已经调用过工具，避免重复调用
        if target == "search_knowledge":
            return ToolCall(name=target, arguments={"query": question[:100]})
        return ToolCall(name=target, arguments={})

    def _next_result(
        self, messages: list[dict[str, Any]], *, allow_auto_tools: bool
    ) -> LLMChatResult:
        """取出下一条响应：脚本队列 → 结构化简报 → 自动工具 → 默认回复。"""
        if self._script:
            return self._script.pop(0)
        if allow_auto_tools and self.auto_tools:
            summary = self._plan_summary_result(messages)
            if summary is not None:
                return summary
            planned = self._plan_tool_call(messages)
            if planned is not None:
                return LLMChatResult(content="", tool_calls=[planned])
        return LLMChatResult(content=self._default_reply(messages))

    # -- 结构化输出（活动简报） ----------------------------------------
    @classmethod
    def _extract_facts(cls, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从简报请求中提取统计快照（用户消息里的第一个 JSON 对象）。"""
        content = cls._last_user_content(messages)
        start = content.find("{")
        if start < 0:
            return None
        try:
            parsed = json.loads(content[start:])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _plan_summary_result(
        self, messages: list[dict[str, Any]]
    ) -> LLMChatResult | None:
        """识别"活动简报"提示词并按固定模板返回合法 JSON。

        需求要求"mock 返回固定模板"：这样**没有密钥时也能看到一次完整、非降级的
        简报生成**（数字仍来自传入的真实统计快照）。识别依据是系统提示词中的
        :data:`SUMMARY_PROMPT_MARKER`，因此不会影响普通对话。
        """
        system_message = self._last_message_by_role(messages, "system")
        system_content = str(system_message.get("content") or "") if system_message else ""
        if SUMMARY_PROMPT_MARKER not in system_content:
            return None

        facts = self._extract_facts(messages)
        if not facts:
            return None
        event = facts.get("event") or {}
        checkin = facts.get("checkin") or {}
        leave = facts.get("leave") or {}

        name = str(event.get("name") or "本次活动")
        expected = int(checkin.get("total_expected") or 0)
        signed = int(checkin.get("signed") or 0)
        late = int(checkin.get("late") or 0)
        absent = int(checkin.get("absent") or 0)
        rate = float(checkin.get("attendance_rate") or 0.0)
        present = signed + late

        payload = {
            "title": f"{name}活动简报",
            "highlights": [
                f"应签到 {expected} 人，实际到场 {present} 人，出勤率 {rate * 100:.2f}%",
                f"已签到 {signed} 人、迟到 {late} 人、缺勤 {absent} 人",
                f"请假 {int(leave.get('total') or 0)} 人次"
                f"（已通过 {int(leave.get('approved') or 0)}、"
                f"待审批 {int(leave.get('pending') or 0)}）",
            ],
            "body": (
                f"## 活动概况\n{name}在{event.get('location') or '村内场地'}"
                f"（{event.get('start_time')} ~ {event.get('end_time')}）举行，"
                f"当前状态为{event.get('status_label')}。\n\n"
                f"## 签到情况\n应签到 {expected} 人，已签到 {signed} 人、迟到 {late} 人、"
                f"缺勤 {absent} 人，出勤率 {rate * 100:.2f}%。\n\n"
                f"## 请假情况\n共 {int(leave.get('total') or 0)} 人次请假"
                f"（已通过 {int(leave.get('approved') or 0)}、待审批 {int(leave.get('pending') or 0)}），"
                f"均按平台请假流程处理。\n\n"
                f"## 后续建议\n1. 出勤率 {rate * 100:.2f}%，"
                f"建议继续在活动前一天通过村群提醒；\n"
                f"2. 迟到 {late} 人，可提前 {int(event.get('late_threshold_minutes') or 0)} 分钟"
                f"开始签到；\n3. 建议在活动结束前完成人脸录入核对。"
            ),
        }
        logger.debug("Mock 提供方按模板返回活动简报 JSON：%s", payload["title"])
        return LLMChatResult(content=json.dumps(payload, ensure_ascii=False))

    # -- 能力实现 ------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> LLMChatResult | Iterator[str]:
        """对话补全（确定性、无网络请求）。"""
        self.calls.append({"messages": messages, "tools": tools, "stream": stream})
        # 流式请求只产出文本，不规划工具调用（工具调用统一走非流式）
        result = self._next_result(messages, allow_auto_tools=not stream)
        if stream:
            return self._chunk(result.content)
        return result

    def _chunk(self, text: str) -> Iterator[str]:
        """按 ``chunk_size`` 字切块（空文本产出零个分片）。"""
        for index in range(0, len(text), max(1, self.chunk_size)):
            yield text[index : index + max(1, self.chunk_size)]


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
#: 进程内提供方缓存（复用 httpx 连接池）
_provider_cache: dict[str, LLMProvider] = {}


def build_llm_provider() -> LLMProvider:
    """按配置构造 LLM 提供方（不做缓存）。"""
    provider_name = settings.LLM_PROVIDER

    if provider_name == MockLLMProvider.name:
        logger.warning(
            "AI 助手运行在 mock 模式（LLM_PROVIDER=mock）：回答为模板化提示，"
            "配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后可获得真实智能回答"
        )
        return MockLLMProvider()

    if not settings.llm_configured:
        raise BusinessError(
            "未配置 LLM：请在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，"
            "或将 LLM_PROVIDER 设为 mock 使用离线模板回答",
            code=ResponseCode.SERVICE_UNAVAILABLE,
        )
    return OpenAICompatProvider()


def get_llm_provider() -> LLMProvider:
    """FastAPI 依赖：获取 LLM 提供方（进程内缓存）。"""
    provider_name = settings.LLM_PROVIDER
    provider = _provider_cache.get(provider_name)
    if provider is None:
        provider = build_llm_provider()
        _provider_cache[provider_name] = provider
    return provider

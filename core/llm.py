"""LLM 封装：读取 .env 配置，基于 Anthropic SDK 实现流式对话。

本模块不打印任何东西，只把流式过程发成事件（见 core/events.py），
由订阅者消费（server/app.py 的 EventStream 负责推给界面）。
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from core.events import publish

# 项目根目录（core 的上一级）下的 .env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 强制模型用中文思考：放在系统提示的【最前面】与【最后面】各一次。
# 实测：只在结尾写一句「思考过程用中文」时，涉及英文工具名/报错时思考会漂移到英文；
# 把禁止英文思考提到最高优先级并首尾各强调一次后，思考能稳定为中文。
CHINESE_THINKING_RULE_HEAD = """【最高优先级】你所有的内部思考（thinking / reasoning）都必须使用简体中文，
禁止用英文思考、禁止出现英文句子。即使工具名、字段名、命令、报错信息本身是英文，
你对它们所做的推理与叙述也必须用中文。"""

CHINESE_THINKING_RULE_TAIL = (
    "再次强调：你的 thinking 必须全程使用简体中文，不得出现英文句子。"
)

# JSON 字符串里不允许出现的裸控制字符（含换行、制表符）
_BARE_CONTROL_RE = re.compile(r"[\x00-\x1f]")


def _escape_control_chars(raw: str) -> str:
    """把 JSON 文本里裸露的控制字符转义成 \\uXXXX。"""
    return _BARE_CONTROL_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", raw)


def _loads_lenient(raw: str):
    """逐级放宽地解析工具入参 JSON，全部失败时返回 None。

    依次尝试：
    1. 标准解析（strict=False，容忍字符串里的裸换行/制表符）；
    2. 先把裸控制字符转义掉再解析；
    3. 只取开头第一个完整 JSON 对象，丢弃后面多出来的内容。
    """
    attempts = (
        lambda text: json.loads(text, strict=False),
        lambda text: json.loads(_escape_control_chars(text), strict=False),
        lambda text: json.JSONDecoder(strict=False).raw_decode(text.lstrip())[0],
    )
    for attempt in attempts:
        try:
            return attempt(raw)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _is_tool_json_error(exc: Exception) -> bool:
    """是否是「SDK 组装工具入参时遇到非法 JSON」这一类错误。

    Anthropic SDK 会在每个 input_json_delta 上用 jiter 解析累积的参数 JSON，
    模型给出的 JSON 非法时直接抛 ValueError；这类错误重试一次通常就好了。
    """
    return isinstance(exc, ValueError) and "Unable to parse tool parameter JSON" in str(exc)


@dataclass
class ThinkResult:
    """think() 的结构化结果，调用方直接读取字段即可。"""

    thinking: str = ""  # 完整思考内容（模型默认输出思考时才有值）
    text: str = ""      # 完整回答文本（本轮只调用工具时可能为空串）
    content: list[dict] = field(default_factory=list)    # 追加进 history 的 assistant content blocks
    tool_uses: list[dict] = field(default_factory=list)  # 需要执行的工具调用


@dataclass
class _StreamState:
    """流式事件收集过程中的中间状态。"""

    # 按 block index 暂存内容块（index 由 SDK 事件提供，用来还原输出顺序）
    content_blocks: dict[int, dict] = field(default_factory=dict)  # 文本 / 工具内容块
    tool_blocks: dict[int, dict] = field(default_factory=dict)     # 工具调用元信息块
    tool_json: dict[int, list[str]] = field(default_factory=dict)  # 工具参数 JSON 分片
    thinking_blocks: dict[int, dict] = field(default_factory=dict)  # 思考 / 推理内容块
    text_blocks: set[int] = field(default_factory=set)             # 文本块的 index

    # 纯文本增量累积，最终拼成 ThinkResult 的字符串字段
    thinking_parts: list[str] = field(default_factory=list)  # 思考文本增量列表
    answer_parts: list[str] = field(default_factory=list)    # 回答文本增量列表

    # 收尾信息：停止原因可用于判断是否因 max_tokens 被截断
    stop_reason: str = ""


# 工具参数 JSON 非法时最多尝试几次（SDK 组装工具入参失败后自动重试）
MAX_TOOL_JSON_ATTEMPTS = 2


class LLM:
    """大语言模型客户端。"""

    def __init__(
        self,
        system: str | None = None,
        tools: list | None = None,
        max_tokens: int | None = None,
        label: str = "LLM",
        silent_tools: set[str] | None = None,
        chinese_thinking: bool = True,
        **kwargs,
    ) -> None:
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "")
        self.model = os.getenv("LLM_MODEL_ID", "deepseek-v4-pro")
        self.max_tokens = max_tokens or int(os.getenv("LLM_MAX_TOKENS", "8000"))
        self.system = self._with_chinese_thinking_rule(system, chinese_thinking)
        self.tools = tools
        self.label = label  # 打印时用于标识当前是哪个 Agent 在输出
        self.silent_tools = silent_tools or set()  # 这些工具的调用不打印「执行...工具」
        self.kwargs = kwargs
        self._call_count = 0  # 本实例已发起的模型调用次数，作为事件里的 call_id

        if not self.api_key:
            raise ValueError("未找到 LLM_API_KEY，请检查项目根目录下的 .env 文件")

        self.client = Anthropic(api_key=self.api_key, base_url=self.base_url)

    @staticmethod
    def _with_chinese_thinking_rule(
        system: str | None, enabled: bool = True
    ) -> str | None:
        """把「用中文思考」的要求包在系统提示的最前与最后。"""
        if not enabled:
            return system

        body = system.strip() if system else ""
        if body:
            return f"{CHINESE_THINKING_RULE_HEAD}\n\n{body}\n\n{CHINESE_THINKING_RULE_TAIL}"
        return f"{CHINESE_THINKING_RULE_HEAD}\n\n{CHINESE_THINKING_RULE_TAIL}"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def think(self, messages: list[dict]) -> ThinkResult:
        """流式调用模型，把过程发成事件，并返回结构化的 ThinkResult。

        主流程只保留清晰的三步：构建请求 -> 消费事件流 -> 组装结果。
        模型默认输出思考内容，这里直接消费并发出思考增量事件。
        """
        request = self._build_request(messages)

        last_error: Exception | None = None
        for attempt in range(MAX_TOOL_JSON_ATTEMPTS):
            state = _StreamState()
            call_id = self._next_call_id()
            publish(
                "stream_start",
                call_id=call_id,
                label=self.label,
                model=self.model,
                max_tokens=self.max_tokens,
            )

            try:
                with self.client.messages.stream(**request) as stream:
                    for event in stream:
                        self._consume_event(event, state, call_id)
            except Exception as exc:
                # SDK 组装工具入参失败（模型 JSON 非法）：重试一次通常就好
                last_error = exc
                publish("stream_end", call_id=call_id, stop_reason="error", truncated=False)
                if attempt + 1 < MAX_TOOL_JSON_ATTEMPTS and _is_tool_json_error(exc):
                    publish(
                        "workflow_step",
                        agent=self.label,
                        step=f"{self.label} 工具参数 JSON 非法，自动重试",
                    )
                    continue
                raise

            publish(
                "stream_end",
                call_id=call_id,
                stop_reason=state.stop_reason,
                truncated=state.stop_reason == "max_tokens",
            )
            return self._assemble_result(state, call_id)

        raise last_error or RuntimeError("模型调用失败")

    def _next_call_id(self) -> int:
        """本次 LLM 实例内的模型调用序号，从 1 开始。"""
        self._call_count += 1
        return self._call_count

    # ------------------------------------------------------------------
    # 请求组装
    # ------------------------------------------------------------------

    def _build_request(self, messages: list[dict]) -> dict:
        """组装发送给模型的请求体。"""
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if self.system:
            request["system"] = self.system
        if self.tools:
            request["tools"] = self.tools
        # 允许通过 **kwargs 覆盖 / 补充任意请求字段
        request.update(self.kwargs)
        return request

    # ------------------------------------------------------------------
    # 流式事件消费
    # ------------------------------------------------------------------

    def _consume_event(self, event, state: _StreamState, call_id: int) -> None:
        """按事件类型分发：block 开始登记元信息，delta 累积内容。"""
        event_type = getattr(event, "type", None)

        if event_type == "content_block_start":
            self._on_block_start(event, state)
        elif event_type == "content_block_delta":
            self._on_block_delta(event, state, call_id)
        elif event_type == "message_delta":
            # 收尾事件里带 stop_reason，可判断是否因 max_tokens 被截断
            delta = getattr(event, "delta", None)
            state.stop_reason = getattr(delta, "stop_reason", "") or state.stop_reason

    def _on_block_start(self, event, state: _StreamState) -> None:
        """content_block_start：登记 tool_use / thinking / text 块的元信息。"""
        block = getattr(event, "content_block", None)
        index = getattr(event, "index", 0)
        block_type = getattr(block, "type", None)

        if block_type == "tool_use":
            state.tool_blocks[index] = {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
            }
            state.tool_json[index] = []
        elif block_type in {"thinking", "reasoning"}:
            state.thinking_blocks[index] = {
                "type": "thinking",
                "thinking": getattr(block, "thinking", ""),
                "signature": getattr(block, "signature", ""),
            }
        elif block_type == "text":
            state.text_blocks.add(index)

    def _on_block_delta(self, event, state: _StreamState, call_id: int) -> None:
        """content_block_delta：按 delta 类型分发到对应处理器。"""
        index = getattr(event, "index", 0)
        delta = getattr(event, "delta", None)
        delta_type = getattr(delta, "type", None)

        if delta_type == "text_delta":
            self._on_text_delta(index, getattr(delta, "text", ""), state, call_id)
        elif delta_type in {"thinking_delta", "reasoning_delta", "reasoning_content_delta"}:
            self._on_thinking_delta(index, delta, state, call_id)
        elif delta_type == "signature_delta" and index in state.thinking_blocks:
            state.thinking_blocks[index]["signature"] += getattr(delta, "signature", "") or ""
        elif delta_type == "input_json_delta" and index in state.tool_blocks:
            state.tool_json[index].append(getattr(delta, "partial_json", ""))

    def _on_text_delta(
        self, index: int, text: str, state: _StreamState, call_id: int
    ) -> None:
        """累积回答文本，并发出回答增量事件。"""
        if not text:
            return
        state.answer_parts.append(text)
        block = state.content_blocks.setdefault(index, {"type": "text", "text": ""})
        block["text"] += text
        publish("answer_delta", call_id=call_id, index=index, text=text)

    def _on_thinking_delta(
        self, index: int, delta, state: _StreamState, call_id: int
    ) -> None:
        """累积思考文本，并发出思考增量事件。"""
        text = (
            getattr(delta, "thinking", None)
            or getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
            or getattr(delta, "text", None)
            or ""
        )
        if not text:
            return
        state.thinking_parts.append(text)
        block = state.thinking_blocks.setdefault(
            index, {"type": "thinking", "thinking": "", "signature": ""}
        )
        block["thinking"] += text
        publish("thinking_delta", call_id=call_id, index=index, text=text)

    # ------------------------------------------------------------------
    # 结果组装
    # ------------------------------------------------------------------

    def _assemble_result(self, state: _StreamState, call_id: int) -> ThinkResult:
        """合并各内容块、解析工具参数，返回 ThinkResult。"""
        # thinking block（含 signature）需要保留在 assistant content 中，
        # 后续把历史发回模型时思考内容才能被正确回传。
        for index, block in state.thinking_blocks.items():
            state.content_blocks[index] = block

        tool_uses: list[dict] = []
        for index in sorted(state.tool_blocks):
            tool_use = self._parse_tool_use(index, state)
            tool_uses.append(tool_use)
            state.content_blocks[index] = tool_use
            # 控制类工具（如 submit_preview_result）只是决策标记，不发出「执行...工具」事件
            if tool_use["name"] not in self.silent_tools:
                publish(
                    "tool_use",
                    call_id=call_id,
                    index=index,
                    tool=tool_use["name"],
                    input=tool_use["input"],
                    tool_use_id=tool_use["id"],
                )

        # 按 SDK 提供的 index 排序，还原模型输出的原始顺序
        content = [state.content_blocks[i] for i in sorted(state.content_blocks)]

        return ThinkResult(
            thinking="".join(state.thinking_parts),
            text="".join(state.answer_parts),
            content=content,
            tool_uses=tool_uses,
        )

    def _parse_tool_use(self, index: int, state: _StreamState) -> dict:
        """把分片到达的 JSON 参数拼接解析成完整 tool_use 块。

        模型生成的工具入参经常不是严格 JSON，实测遇到过两种：
        1. 字符串里直接写了真实换行/制表符（多行线索说明、脚本内容），
           而标准 JSON 不允许字符串里有裸控制字符；
        2. 一个完整 JSON 对象之后又多了些内容（重复输出、补一句说明）。
        所以逐级放宽解析，尽量把它救回来，而不是让整轮任务失败。
        """
        raw_input = "".join(state.tool_json[index])
        if not raw_input:
            return {**state.tool_blocks[index], "input": {}}

        name = state.tool_blocks[index]["name"]
        tool_input = _loads_lenient(raw_input)

        if tool_input is None:
            raise ValueError(
                f"工具 {name} 的参数不是有效 JSON：{raw_input[:400]}"
            )
        if not isinstance(tool_input, dict):
            raise ValueError(
                f"工具 {name} 的参数应当是 JSON 对象，实际是 {type(tool_input).__name__}："
                f"{raw_input[:400]}"
            )
        return {**state.tool_blocks[index], "input": tool_input}

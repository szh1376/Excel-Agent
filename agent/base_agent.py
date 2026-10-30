"""Agent 基类：定义 Agent 的通用接口与工具循环。

每个 Agent 只保留「当前任务」的短期记忆：一次 run() 就是一次任务，用户消息由
ContextManager 现场组装成局部变量（不留在 Agent 上）；任务一结束，Master 立刻
清空它的运行记录（reset_memory），因此任务之间不会互相串味。
"""

import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from core.context_manager import CONTEXT
from core.events import (
    BUS,
    publish,
    reset_current_agent,
    set_current_agent,
)
from core.llm import ThinkResult
from core.message import Message
from tools import ToolManager


class BaseAgent(ABC):
    """Agent 基类：定义 Agent 的通用接口与工具循环。"""

    def __init__(
        self,
        name: str,
        system_prompt: Optional[str] = None,
        tool_manager: ToolManager | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tool_manager = tool_manager or ToolManager()
        # 本次运行的记录，供 Master 汇总落盘
        self._model_calls: list[dict] = []  # 每次调用模型的思考 / 文本 / 工具名
        self._tool_calls: list[dict] = []   # 每次工具调用的 name / input / output
        # 本次运行生成的统计图与过程说明（由 Master 在调度时填充，见 MasterAgent._run_agent）
        self.charts: list[dict] = []
        self.steps: list[str] = []

    def reset_memory(self) -> None:
        """清空本 Agent 的短期记忆（上一次任务的模型调用、工具调用、图表与过程说明）。"""
        self._model_calls = []
        self._tool_calls = []
        self.charts = []
        self.steps = []

    @abstractmethod
    def run(self, input_text: str, **kwargs) -> Any:
        """运行 Agent，返回结果（字符串或结构化对象）。"""

    # ------------------------------------------------------------------
    # 运行记录（Master 收集后交给 ContextManager 落盘）
    # ------------------------------------------------------------------

    def get_run_record(self) -> dict:
        """返回本次运行的记录。"""
        return {
            "agent": self.name,
            "model_calls": self._model_calls,
            "tool_calls": self._tool_calls,
        }

    # ------------------------------------------------------------------
    # 工具循环
    # ------------------------------------------------------------------

    def _log_workflow_step(self, step: str) -> None:
        """发出工作流过程事件，便于追踪当前是哪个 Agent 在处理。

        agent 用本 Agent 的名字、task_id 取当前子任务上下文 —— 前端据此把这条
        说明挂到对应子任务的那张 Agent 卡片下面（而不是全堆在 Master 那里）。
        """
        publish("workflow_step", agent=self.name, step=step)

    def _run_tool_loop(
        self,
        stop_tools: set[str] | None = None,
        **context,
    ) -> ThinkResult:
        """跑一轮：按本 Agent 的上下文组装消息，反复调用 LLM 并执行工具。

        每次调用都是独立的一次任务：messages 是局部变量，任务跑完由 Master 统一
        清空短期记忆（见 MasterAgent._record_agent），所以上一个任务的上下文不会
        带进来 —— Agent 只有当前任务的短期记忆。
        """
        messages = CONTEXT.build_context(self.name, **context)

        stop_tools = stop_tools or set()

        token = set_current_agent(self.name)
        try:
            while True:
                result = self.llm.think(messages)
                messages.append(Message("assistant", result.content).to_dict())
                self._model_calls.append(
                    {
                        "thinking": result.thinking,
                        "text": result.text,
                        "tools": [block["name"] for block in result.tool_uses],
                    }
                )

                if not result.tool_uses:
                    return result

                # 执行本轮所有工具并回填 tool_result
                # （终止工具也要执行，这样消息序列始终合法）
                results = []
                for block in result.tool_uses:
                    output = self._execute_tool(block)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": output,
                        }
                    )
                messages.append(Message("user", results).to_dict())

                # 命中终止工具：结果已回填，结束循环，交给调用方解析决策
                if any(block["name"] in stop_tools for block in result.tool_uses):
                    return result
        finally:
            # 临时脚本统一由 Master 在本轮结束时清理（把 temp/ 整个删掉）：
            # 并行时多个 Agent 共用 temp/，各自 rmtree 会误删别人正在用的脚本。
            reset_current_agent(token)

    def _execute_tool(self, block: dict) -> str:
        """执行单个工具，记录并发出执行前后的事件，返回工具输出。"""
        name = block["name"]
        tool_input = block["input"]

        publish("tool_call", tool=name, input=tool_input, tool_use_id=block["id"])
        started = time.time()
        output = self.tool_manager.execute(name, **tool_input)
        elapsed = time.time() - started

        self._tool_calls.append({"name": name, "input": tool_input, "output": output})
        publish(
            "tool_result",
            tool=name,
            input=tool_input,
            output=output,
            elapsed=round(elapsed, 3),
            tool_use_id=block["id"],
        )
        return output

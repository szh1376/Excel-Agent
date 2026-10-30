"""事件层：把 Agent 的运行过程抽象成可订阅的事件流。

设计要点：
1. Agent / LLM / 工具层只负责「发出事件」，不关心谁来消费。
2. 消费方是订阅者：Web 层把事件推给界面（server/app.py 的 EventStream）。
3. 并行工作流下事件可能来自多个 worker 线程，因此 publish 内部加锁、seq 严格
   自增；订阅者回调放在锁外，避免 subscriber 里再 publish 造成死锁。

事件类型一览（字段见 EVENT_TYPES）：
- 会话级：session_loaded / turn_start / turn_end
- 计划级：plan / task_start / task_end
- 工作流：workflow_step / agent_start / agent_end / clue / agent_message
- 模型流：stream_start / thinking_delta / answer_delta / stream_end
- 工具级：tool_use / tool_call / tool_result
- 图表：chart_saved / error
"""

import contextvars
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 事件类型 -> 说明。前端 TypeScript 侧按这份定义写类型。
EVENT_TYPES: dict[str, str] = {
    # 会话
    "session_loaded": "会话历史已从本地读回",
    "turn_start": "一轮问答开始",
    "turn_end": "一轮问答结束（含最终答案）",
    # 计划 / 子任务
    "plan": "Master 对用户问题的拆解结果",
    "task_start": "一个子任务开始执行",
    "task_end": "一个子任务结束（含该子任务的答案）",
    # 工作流
    "workflow_step": "工作流推进说明（Master 路由 / 分支说明）",
    "agent_start": "某个 Agent 开始运行",
    "agent_end": "某个 Agent 运行结束（含耗时）",
    "clue": "Preview 交给 Loader 的结构化线索",
    "agent_message": "Agent 之间投递的一条消息",
    # 模型流
    "stream_start": "一次模型调用开始",
    "thinking_delta": "思考内容增量",
    "thinking_done": "思考阶段结束",
    "answer_delta": "回答内容增量",
    "stream_end": "一次模型调用结束",
    # 工具
    "tool_use": "模型决定调用某个工具",
    "tool_call": "工具开始执行（含入参）",
    "tool_result": "工具执行完成（含输出）",
    # 图表 / 错误
    "chart_saved": "生成了一张统计图，界面据此显示（图片走 /api/charts 静态接口）",
    "error": "运行出错",
}

# 会话记录文件所在目录，落盘事件时用来判断是否属于工作区
SESSION_DIR = Path("sessions")


@dataclass
class Event:
    """一条事件。字段保持扁平，方便直接序列化成 JSON 发给前端。"""

    type: str
    agent: str = ""            # 触发事件的 Agent 名字（Master / Preview / Loader / Analysis）
    task_id: str = ""          # 所属子任务（并行时区分同类 Agent 的多次运行）
    data: dict = field(default_factory=dict)
    time: float = field(default_factory=time.time)
    seq: int = 0               # 全局自增序号，前端用它保证顺序

    def to_dict(self) -> dict:
        """转成可直接 JSON 序列化的字典。"""
        return {
            "type": self.type,
            "agent": self.agent,
            "task_id": self.task_id,
            "data": self.data,
            "time": self.time,
            "seq": self.seq,
        }


Subscriber = Callable[[Event], None]


class EventBus:
    """事件总线：收集事件并同步分发给所有订阅者。

    publish 内部加锁保证 seq 与环形缓冲在多 worker 下安全；订阅者回调放在锁外，
    这样某个 subscriber 里再 publish 也不会死锁。
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.recent: list[Event] = []  # 环形缓冲，供前端刷新后重放
        self.buffer_size = 2000

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def subscribe(self, subscriber: Subscriber) -> Subscriber:
        """注册订阅者，返回它本身，方便当装饰器 / 取消订阅。"""
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """取消订阅（不存在时静默忽略）。"""
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def publish(self, type: str, agent: str = "", task_id: str = "", **data: Any) -> Event:
        """发布一条事件，同步分发给所有订阅者。"""
        with self._lock:
            self._seq += 1
            event = Event(type=type, agent=agent, task_id=task_id, data=data, seq=self._seq)
            self.recent.append(event)
            if len(self.recent) > self.buffer_size:
                del self.recent[: len(self.recent) - self.buffer_size]
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # 单个订阅者出错不能影响 Agent 运行
                pass
        return event

    def history(self, since_seq: int = 0) -> list[dict]:
        """取出序号大于 since_seq 的历史事件，用于前端断线重连后补齐。"""
        with self._lock:
            return [e.to_dict() for e in self.recent if e.seq > since_seq]


# 全局总线：整个进程共用一条事件流
BUS = EventBus()


# 当前 Agent / 当前子任务：让 LLM / 工具层发事件时不必层层传参。
# 注意：每个 worker 线程有独立的 context，互不干扰。
_current_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_agent", default=""
)
_current_task: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task", default=""
)


def set_current_agent(name: str) -> contextvars.Token:
    """标记当前正在运行的 Agent，返回 token 供 reset。"""
    return _current_agent.set(name)


def reset_current_agent(token: contextvars.Token) -> None:
    """恢复上一个 Agent 标记。"""
    _current_agent.reset(token)


def current_agent() -> str:
    """当前正在运行的 Agent 名字（无则空串）。"""
    return _current_agent.get()


def set_current_task(task_id: str) -> contextvars.Token:
    """标记当前子任务，返回 token 供 reset。"""
    return _current_task.set(task_id)


def reset_current_task(token: contextvars.Token) -> None:
    """恢复上一个子任务标记。"""
    _current_task.reset(token)


def current_task() -> str:
    """当前子任务 id（无则空串）。"""
    return _current_task.get()


def publish(
    type: str, agent: str | None = None, task_id: str | None = None, **data: Any
) -> Event:
    """便捷发布：agent / task_id 默认取当前上下文，也可以显式指定。"""
    return BUS.publish(
        type,
        agent=current_agent() if agent is None else agent,
        task_id=current_task() if task_id is None else task_id,
        **data,
    )

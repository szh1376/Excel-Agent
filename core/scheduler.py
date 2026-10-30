"""Agent 执行队列：每个 Agent 一个常驻 worker 线程，消息经队列直连投递。  # 模块文档字符串，说明该模块的用途

设计要点：  # 文档字符串中列出设计要点
1. 每个 Agent 一个 queue.Queue + 一条常驻线程，同类任务串行执行 ——  # 要点一：每个 Agent 独占一个队列和线程，保证串行
   这同时天然保证了单个 Agent 实例上的运行状态（_model_calls / _tool_calls /  # 解释串行执行的好处：避免状态被并发写坏
   charts）不会被并发写坏；不同 Agent 之间并行。  # 不同 Agent 之间可以并行执行
2. Agent 之间通过 send(AgentMessage) 直连投递（Preview → Loader → Analysis），  # 要点二：Agent 之间直接投递消息
   Master 只负责下发子任务与收集最终结果。  # Master 的职责仅是下发任务和收集结果
3. worker 里的异常转成 error 事件，不会打死 worker 线程。  # 要点三：异常处理机制，保证 worker 线程存活
"""

import queue
import threading
from typing import Callable
from core.events import publish
from core.message import AgentMessage


class Scheduler:
    """按 Agent 名字维护执行队列与 worker 线程。"""  # 类的文档字符串

    def __init__(self) -> None:  # 构造函数，初始化 Scheduler 实例
        self._queues: dict[str, queue.Queue] = {}  # 字典：Agent 名字 -> 对应的消息队列
        self._handlers: dict[str, Callable[[AgentMessage], None]] = {}  # 字典：Agent 名字 -> 处理函数
        self._threads: dict[str, threading.Thread] = {}  # 字典：Agent 名字 -> worker 线程
        self._lock = threading.Lock()  # 创建线程锁，保护共享状态的并发访问
        self._running = False  # 标记调度器是否正在运行

    def register(self, name: str, handler: Callable[[AgentMessage], None]) -> None:
        """登记一个 Agent 的处理函数（必须在 start 之前）。"""
        with self._lock:
            self._queues[name] = queue.Queue()
            self._handlers[name] = handler  #

    def start(self) -> None:
        """启动所有 worker 线程（常驻，直到 stop）。重复调用无副作用。"""
        with self._lock:
            if self._running:
                return
            for name in self._handlers:
                thread = threading.Thread(
                    # 线程执行的函数，传递的参数，线程的名称，设置为守护线程
                    target=self._loop, args=(name,), name=f"agent-{name}", daemon=True
                )
                self._threads[name] = thread
                thread.start()
            self._running = True

    def stop(self) -> None:
        """停止所有 worker（投递哨兵 None）。"""
        with self._lock:
            if not self._running:
                return
            queues = list(self._queues.values())
            self._running = False  # 标记调度器为停止状态
        # 向每个队列投递 None 哨兵，通知 worker 退出
        for q in queues:
            q.put(None)

    def send(self, message: AgentMessage) -> None:
        """把消息投给 receiver 的队列（Agent 之间直连）。"""
        with self._lock:
            # 获取接收的agent
            target = self._queues.get(message.receiver)
        if target is None:
            raise ValueError(f"未注册的 Agent：{message.receiver}")
        target.put(message)

    def _loop(self, name: str) -> None:
        """某个 Agent 的常驻循环：串行消费自己的队列。"""
        target = self._queues[name]  # 获取该 Agent 对应的队列
        handler = self._handlers[name]  # 获取该 Agent 对应的处理函数
        while True:
            message = target.get()  # 阻塞等待消息
            try:
                if message is None:      # 如果消息是 None（停止哨兵）
                    return
                handler(message)  # 调用处理函数处理消息
            except Exception as exc:
                publish("error", agent=name, message=f"{type(exc).__name__}: {exc}")
            finally:
                target.task_done()  # 通知队列该任务已完成
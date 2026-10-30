"""消息系统：对话消息 + Agent 之间的消息 / 子任务模型。

- Message：一条对话消息（role + content），可转成 Anthropic Messages API 格式。
- PlanTask / AgentMessage：并行工作流里 Master 拆解出的子任务，
  以及 Agent 之间经调度队列直连投递的消息。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


# Master 的三种意图（子任务级别）
INTENT_ANSWER = "answer"                    # 日常问题：Master 直接回答
INTENT_EXCEL_DIRECT = "excel_direct"        # 表格任务：不需要写代码 → Preview 读表直答
INTENT_EXCEL_WORKFLOW = "excel_workflow"    # 表格任务：需要写代码 → Loader 取数 → Analysis

VALID_INTENTS = (INTENT_ANSWER, INTENT_EXCEL_DIRECT, INTENT_EXCEL_WORKFLOW)

# AgentMessage 的消息类型
MSG_TASK = "task"        # Master → Preview/Loader：下发一个子任务
MSG_CLUE = "clue"        # Preview → Loader：待清洗表的线索
MSG_DATA = "data"        # Loader → Analysis：抽取好的数据文件
MSG_RESULT = "result"    # 任意 Agent → Master：该子任务的最终结果


class Message(BaseModel):
    """消息类：统一封装对话中的一条消息，通过 to_dict() 转为 API 格式。"""

    role: str
    content: str | list[dict]
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    def __init__(self, role: str, content: str | list[dict], **kwargs):
        super().__init__(
            role=role,
            content=content,
            timestamp=kwargs.get("timestamp", datetime.now()),
            metadata=kwargs.get("metadata", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为消息 API 格式（Anthropic Messages API）。"""
        return {
            "role": self.role,
            "content": self.content,
        }

    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"


@dataclass
class PlanTask:
    """Master 拆解出的一个子任务。"""

    task_id: str
    query: str                                          # 这个子任务要回答的问题
    intent: str                                         # 见上面的 INTENT_* 常量
    tables: list[str] = field(default_factory=list)     # 该子任务用到的全部原始表路径
    answer: str = ""                                    # intent == answer 时由模型直接给出的答案
    missing_tables: list[str] = field(default_factory=list)  # 模型声明还缺哪些表
    cleaned: dict[str, str] = field(default_factory=dict)  # 已经清洗过表的路径（服务器侧查）
    status: str = "pending"                             # pending / running / done / blocked / failed

    def missing_required_tables(self) -> list[str]:
        """判断是否缺表，缺少表则向用户询问"""
        if self.missing_tables:
            return list(self.missing_tables)
        if self.intent in (INTENT_EXCEL_DIRECT, INTENT_EXCEL_WORKFLOW) and not self.tables:
            return ["（未提供表格路径）"]
        return []

    def all_cleaned(self) -> bool:
        """这个子任务用到的表是否全部已清洗。"""
        return bool(self.tables) and all(self.cleaned.get(t) for t in self.tables)

    def uncleaned_tables(self) -> list[str]:
        """还没清洗过的表。"""
        return [t for t in self.tables if not self.cleaned.get(t)]

    def to_dict(self) -> dict:
        """给事件 / 前端的精简形态。"""
        return {
            "task_id": self.task_id,
            "query": self.query,
            "intent": self.intent,
            "tables": list(self.tables),
            "missing_tables": list(self.missing_tables),
            "status": self.status,
        }


@dataclass
class AgentMessage:
    """Agent 之间传递的一条消息（也是执行队列里的元素）。"""

    sender: str
    receiver: str
    kind: str
    task_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

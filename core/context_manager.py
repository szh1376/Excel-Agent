"""对话上下文管理：为各 Agent 组装上下文、记录对话、落盘与读取。

职责：
1. build_context()：按 Agent 名字组装它本轮的消息列表（messages）
   - Master   → 之前的对话 + 当前问题（不含工作目录文件清单）
   - Preview  → Master 的意图 + 涉及的表 + 当前问题（不含历史对话）
   - Loader   → 用户问题 + 各表清洗状态（已清洗路径 / 未清洗线索）
   - Analysis → 用户问题 + Loader 产出的数据文件路径
2. get_chat_history()：聊天记录（只有 query 与 answer，不含工具调用）
3. save()：把全部对话落盘成 JSON（含各 Agent 的思考与工具调用）
4. load()：读回本地对话，用于展示历史记录

多会话：每个会话一个 sessions/<id>.json，清单在 sessions/_index.json
（见 core/sessions.py）。本类只关心「当前会话」的内容，会话清单的增删改查
交给 SessionStore。
"""

import json
import os
import threading
from dataclasses import dataclass, field

from core.message import Message
from core.sessions import SessionStore

# 临时产物的路径约定（各 Agent 的上下文与落盘都用这里的定义）：
# 所有临时文件都在 temp/ 下、名字里带子任务号（taskXX），所以并行时不会互相覆盖；
# 整轮结束后由 Master 把 temp/ 整个删掉，不需要逐个文件追踪。
TEMP_ROOT = "temp"                        # 临时产物根目录（整轮结束整体删除）
TEMP_SCRIPT_DIR = f"{TEMP_ROOT}/scripts"  # 各 Agent 写的临时脚本
TEMP_DATA_DIR = f"{TEMP_ROOT}/data"       # 清洗临时表、抽取结果、临时图片


def temp_cleaned_file(task_id: str) -> str:
    """Loader 清洗结果的临时落点：每个子任务一份。"""
    return f"{TEMP_DATA_DIR}/_cleaned_{task_id or 'task'}.csv"


def temp_extracted_file(task_id: str) -> str:
    """Loader 抽取结果的临时落点（交给 Analysis 读）：每个子任务一份。"""
    return f"{TEMP_DATA_DIR}/_extracted_{task_id or 'task'}.csv"


def temp_script_example(task_id: str) -> str:
    """脚本路径示例：脚本一律放 temp/scripts/ 下，且以子任务号开头。"""
    return f"{TEMP_SCRIPT_DIR}/{task_id or 'task'}_xxx.py"

# 会话记录默认存放目录（不在 Master 的临时产物清理范围内）
SESSION_DIR = "sessions"

# 落盘时单个工具输出保留的最大字符数，避免 JSON 过大。
# 注意：事件流里的 tool_result 带的是完整输出，界面以实时事件为准。
MAX_SAVED_TOOL_OUTPUT = 800


@dataclass
class Turn:
    """一轮对话的完整记录。"""

    query: str
    answer: str = ""
    agents: list[dict] = field(default_factory=list)  # 各 Agent 的参与记录
    tasks: list[dict] = field(default_factory=list)   # 拆解计划（还原界面用）
    steps: list[str] = field(default_factory=list)    # Master 过程说明（还原界面用）
    elapsed: float | None = None                      # 本轮耗时（前端算好后回报）


class ContextManager:
    """对话上下文与记录的管理者（只管「当前会话」）。"""

    def __init__(
        self,
        session_id: str = "default",
        directory: str = SESSION_DIR,
        store: SessionStore | None = None,
    ):
        self.session_id = session_id
        self.directory = directory
        # 会话清单：标签、时间、绑定文件等列表信息
        self.store = store if store is not None else SessionStore(directory)
        self._turns: list[Turn] = []
        self._current: Turn | None = None
        # 并行工作流下多个 worker 会同时写记录，用锁保护 _current
        self._lock = threading.Lock()
        # 加载会话
        self.load()

    # ------------------------------------------------------------------
    # 会话切换（Web 层用；命令行只有一个会话，不需要调用）
    # ------------------------------------------------------------------

    def switch_session(self, session_id: str) -> str:
        """切换到指定会话：落盘当前会话后读入目标会话，返回目标会话 id。"""
        if self._current is not None:
            # 正在进行的这轮先落盘，避免切走时丢内容
            self.finish_turn(self._current.answer)

        self.session_id = session_id
        self._turns = []
        self._current = None
        self.store.ensure(session_id)
        self.load()
        return self.session_id

    def new_session(self, title: str = "", excel_file: str = "") -> str:
        """新建一个会话并切换过去，返回新会话 id。"""
        entry = self.store.create(title=title, excel_file=excel_file)
        return self.switch_session(entry.id)

    # ------------------------------------------------------------------
    # 记录一轮对话（由 Master 驱动）
    # ------------------------------------------------------------------

    def start_turn(self, query: str) -> None:
        """Master 收到新问题时调用，开启本轮记录。"""
        self._current = Turn(query=query)

    def add_agent_record(self, record: dict) -> None:
        """Master 收集到某个 Agent 的参与记录后调用（worker 并发出，需加锁）。"""
        trimmed = self._trim_record(record)
        with self._lock:
            if self._current is None:
                self._current = Turn(query=record.get("query", ""))
            self._current.agents.append(trimmed)

    def set_plan(self, tasks: list[dict]) -> None:
        """记录本轮的拆解计划（落盘后用于还原界面上的「任务拆分与分配」）。"""
        with self._lock:
            if self._current is None:
                return
            self._current.tasks = list(tasks)

    def add_step(self, step: str) -> None:
        """记录一条 Master 级过程说明（落盘后用于还原界面）。"""
        with self._lock:
            if self._current is None:
                return
            self._current.steps.append(step)

    def set_last_turn_elapsed(self, elapsed: float) -> None:
        """前端算好的本轮耗时回报过来后写进刚结束的那轮，并重新落盘。"""
        with self._lock:
            if not self._turns:
                return
            self._turns[-1].elapsed = round(float(elapsed), 3)
        self.save()

    def finish_turn(self, answer: str) -> None:
        """本轮结束：写入记录、落盘，并更新会话清单。"""
        if self._current is None:
            return
        self._current.answer = answer
        self._turns.append(self._current)
        self._current = None
        self.save()

        # 更新会话清单：轮数、最近时间，首次提问时顺便定标题
        first_query = self._turns[0].query if self._turns else ""
        self.store.touch(
            self.session_id,
            turns=len(self._turns),
            first_query=first_query,
        )

    def _trim_record(self, record: dict) -> dict:
        """落盘前裁剪：工具输出只留前若干字符。"""
        trimmed = dict(record)
        trimmed["tool_calls"] = [
            {
                "name": call.get("name", ""),
                "input": call.get("input", {}),
                "output": str(call.get("output", ""))[:MAX_SAVED_TOOL_OUTPUT],
            }
            for call in record.get("tool_calls", [])
        ]
        return trimmed

    # ------------------------------------------------------------------
    # 功能 2：聊天记录（只有 query 与 answer）
    # ------------------------------------------------------------------

    def get_chat_history(self) -> list[dict]:
        """聊天记录，不含任何工具调用，可直接当消息列表用。"""
        messages: list[dict] = []
        for turn in self._turns:
            messages.append({"role": "user", "content": turn.query})
            if turn.answer:
                messages.append({"role": "assistant", "content": turn.answer})
        return messages

    # ------------------------------------------------------------------
    # 功能 1：按 Agent 名字组装上下文
    # ------------------------------------------------------------------

    def build_context(self, agent_name: str, **payload) -> list[dict]:
        """按 Agent 名字分发到各自的组装函数，返回本轮消息列表（messages）。"""
        builders = {
            "Master": self._master_context,
            "Preview": self._preview_context,
            "Loader": self._loader_context,
            "Analysis": self._analysis_context,
        }
        builder = builders.get(agent_name)
        return builder(**payload) if builder else []

    def _master_context(self, question: str = "", **_) -> list[dict]:
        """Master：之前的对话（用户问题 + 最终答案）+ 当前问题"""
        messages = self.get_chat_history()
        messages.append(Message("user", question).to_dict())
        return messages

    def _preview_context(
        self,
        question: str = "",
        signal: str = "",
        tables: list[str] | None = None,
        **_,
    ) -> list[dict]:
        """Preview：Master 的意图 + 涉及的表 + 当前问题（不带之前的对话）。"""
        table_lines = [f"- {t}" for t in (tables or [])] or ["- （Master 未给出表格路径）"]
        text = "\n".join(
            [
                "【表格信息】",
                f"- Master 的意图（必须严格照做）：{signal or '（无）'}",
                "- 涉及的表：",
                *table_lines,
                "",
                f"【当前问题】{question}",
            ]
        )
        return [Message("user", text).to_dict()]

    def _loader_context(
        self,
        question: str = "",
        tables: list[str] | None = None,
        cleaned: dict[str, str] | None = None,
        clue: dict | None = None,
        task_id: str = "",
        **_,
    ) -> list[dict]:
        """Loader：用户问题 + 多表清洗状态（已清洗给路径；未清洗给线索）+ 本子任务的临时文件。"""
        cleaned = cleaned or {}
        cleaned_lines = [
            f"- {t} → 已清洗：{cleaned[t]}" for t in (tables or []) if cleaned.get(t)
        ] or ["- （没有已清洗的表）"]
        clue_text = json.dumps(clue, ensure_ascii=False, indent=2) if clue else "（无）"
        text = "\n".join(
            [
                f"用户问题：{question}",
                "",
                "【已清洗的表】",
                *cleaned_lines,
                "",
                "【待清洗表的线索】",
                clue_text,
                "",
                "【本子任务的临时文件】（都在 temp/ 下、名字里带本子任务编号，整轮结束会一起删掉）",
                f"- 清洗临时文件：{temp_cleaned_file(task_id)}",
                f"- 抽取结果文件：{temp_extracted_file(task_id)}",
                f"- 脚本示例：{temp_script_example(task_id)}",
                "",
                "请按上面的清洗状态取数：未清洗的表先按线索清洗并调用 save_cleaned_table 归档；"
                "再把所有相关表的数据整理成上面的抽取结果文件"
                "（encoding=\"utf-8-sig\"），"
                "最后调用 submit_loader_result 登记。不要做任何统计与计算。",
            ]
        )
        return [Message("user", text).to_dict()]

    def _analysis_context(
        self, question: str = "", data_file: str = "", task_id: str = "", **_
    ) -> list[dict]:
        """Analysis：用户问题 + Loader 产出的数据文件路径 + 本子任务的临时文件约定。"""
        text = (
            f"用户问题：{question}\n\n"
            f"Loader 提供的数据文件：{data_file or '（未收到数据文件）'}\n\n"
            "请用代码读取这个文件后完成分析与出图；数据不足时明确指出缺了什么。\n\n"
            "【本子任务的临时文件】（都在 temp/ 下、名字里带本子任务编号，整轮结束会一起删掉）\n"
            f"- 脚本示例：{temp_script_example(task_id)}\n"
            f"- 临时图片示例：{TEMP_DATA_DIR}/{task_id or 'task'}_chart1.png"
        )
        return [Message("user", text).to_dict()]

    # ------------------------------------------------------------------
    # 功能 3：保存到本地 JSON（含各 Agent 的思考与工具调用）
    # ------------------------------------------------------------------

    def file_path(self) -> str:
        """当前会话的本地 JSON 路径。"""
        return os.path.join(self.directory, f"{self.session_id}.json")

    def save(self) -> str:
        """把全部对话落盘，返回文件路径。"""
        os.makedirs(self.directory, exist_ok=True)
        path = self.file_path()
        data = {"session_id": self.session_id, "turns": self.to_dicts()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    # ------------------------------------------------------------------
    # 功能 4：读取本地对话（用于展示历史记录）
    # ------------------------------------------------------------------

    def load(self) -> list[dict]:
        """读回本地对话内容并返回；文件不存在或损坏时返回空列表。"""
        path = self.file_path()
        if not os.path.exists(path):
            return []

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

        raw_turns = data.get("turns", [])
        self._turns = [
            Turn(
                query=turn.get("query", ""),
                answer=turn.get("answer", ""),
                agents=turn.get("agents", []),
                tasks=turn.get("tasks", []),
                steps=turn.get("steps", []),
                elapsed=turn.get("elapsed"),
            )
            for turn in raw_turns
        ]
        return self.to_dicts()

    # ------------------------------------------------------------------
    # 查看
    # ------------------------------------------------------------------

    def to_dicts(self) -> list[dict]:
        """当前内存里的全部对话（含各 Agent 的思考与工具调用）。"""
        return [
            {
                "query": turn.query,
                "answer": turn.answer,
                "agents": turn.agents,
                "tasks": turn.tasks,
                "steps": turn.steps,
                "elapsed": turn.elapsed,
            }
            for turn in self._turns
        ]

    def __len__(self) -> int:
        return len(self._turns)


# 全局默认会话（当前只支持一个会话）
CONTEXT = ContextManager()

import os
import queue
import shutil
import time
from contextlib import contextmanager
from typing import List

from core.context_manager import CONTEXT, TEMP_ROOT, temp_extracted_file
from core.events import (
    BUS,
    publish,
    reset_current_agent,
    reset_current_task,
    set_current_agent,
    set_current_task,
)
from core.llm import LLM
from core.message import (
    INTENT_ANSWER,
    INTENT_EXCEL_DIRECT,
    INTENT_EXCEL_WORKFLOW,
    MSG_CLUE,
    MSG_DATA,
    MSG_TASK,
    VALID_INTENTS,
    AgentMessage,
    Message,
    PlanTask,
)
from core.scheduler import Scheduler
from tools import BaseTool, ToolManager, ToolParameter
from tools.builtin import GlobTool
from utils.excel_store import ExcelStore
from .base_agent import BaseAgent
from .analysis_agent import AnalysisAgent
from .loader_agent import LoaderAgent
from .preview_agent import PreviewAgent


MASTER_SYSTEM_PROMPT = f"""你是 ExcelAgent 的主控 Agent（Master）。

先判断用户问题可以拆成几个**子任务**(尽可能的拆分，尽可能的合理)，再为每个子任务定意图，最后一次性提交计划。

意图取值：
- `{INTENT_ANSWER}`：日常问题（不涉及表格）。你在 answer 字段里直接给出答案。
- `{INTENT_EXCEL_DIRECT}`：表格任务，但**不需要写代码**就能答
  （数个数、找最大/最小、数某列有几种取值、查某一行等）。
- `{INTENT_EXCEL_WORKFLOW}`：表格任务且**需要写代码**（统计汇总、分组、
  画图、清洗脏数据、跨表计算等）。

拆解规则：
- **必须拿全表格路径**：表格类子任务的 tables 里要放它用到的**全部**原始表格路径，
  一张都不能少（跨表计算、关联多张表时尤其重要）。
- 用户没给全时，先从【之前的对话】里推断补齐（例如上一轮提到过 "a.xlsx"）。
- 推断后仍然拿不到的，填进该子任务的 missing_tables，并在 answer 里向用户询问还缺哪张表；
  **不要猜文件名，也不要带着不全的表硬做。**
- **没拿全表之前不开启任何任务**：只要有任何子任务缺表（missing_tables 非空，或表格类
  子任务没给 tables），本轮就只向用户要表，直答 / 工作流等全部先不做。
- **先做表少的**：提交计划时按「涉及表数量从少到多」排序（只涉及一张表的排最前），
  服务器也按这个顺序派发 —— 表少的先跑完，才能尽早把表清洗好给后面的任务复用。

思考过程用中文。
"""

SUMMARY_SYSTEM_PROMPT = """你是 ExcelAgent 的主控 Agent（Master）。下面给你用户问题与各子问题的结果，
请汇总成一段给用户的最终回答：

- 覆盖每一个子问题，不要遗漏；
- 直接给结论，不要复述执行过程；
- 某个子问题失败或缺失时如实说明，不要编造。

思考过程用中文。
"""

# 等待所有子任务返回结果的最长时间（秒）
GATHER_TIMEOUT = 3600.0

# 计划 / 汇总阶段的事件用这两个伪 task_id 标记：前端据此把「Master 思考（拆解）」
TASK_PLAN = "plan"
TASK_SUMMARY = "summary"


class _SubmitPlanTool(BaseTool):
    """终止工具：承载 Master 对用户问题的拆解计划。"""

    def __init__(self):
        super().__init__(
            name="submit_plan",
            description=(
                "提交对用户问题的拆解计划（终止工具，必须调用一次）。"
                "plan.tasks 是子任务数组，每项含："
                "query（子问题）、intent（answer / excel_direct / excel_workflow）、"
                "tables（该子任务用到的全部原始表路径，必须齐全）、"
                "missing_tables（还缺哪些表，缺表时才填）、"
                "answer（intent=answer 时填写的答案）。"
                "只要有任何子任务缺表（missing_tables 非空，或表格类子任务没给 tables），"
                "本轮就不会执行任何任务，只把询问转给用户。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="plan",
                type="object",
                description=(
                    "拆解计划："
                    '{"tasks": [{"query": 子问题, "intent": 意图, '
                    '"tables": [表路径...], "missing_tables": [还缺的表...], '
                    '"answer": 答案}]}'
                ),
                required=True,
            ),
        ]

    def run(self, plan: dict) -> str:
        return "已提交拆解计划"


class MasterAgent(BaseAgent):
    """MasterAgent：拆解、并行分发、收集、汇总。"""

    def __init__(
        self,
        name: str = "Master",
        system_prompt: str | None = None,
        max_tokens: int = 8000,
    ):
        system = system_prompt or MASTER_SYSTEM_PROMPT
        tm = ToolManager(register_defaults=False)
        tm.register_tool(_SubmitPlanTool())
        tm.register_tool(GlobTool())
        super().__init__(name=name, system_prompt=system, tool_manager=tm)

        self.llm = LLM(
            system=self.system_prompt,
            tools=self.tool_manager.get_schemas(),
            max_tokens=max_tokens,
            label=self.name,
            silent_tools={"submit_plan"},
        )
        # 汇总模型
        self.summary_llm = LLM(
            system=SUMMARY_SYSTEM_PROMPT,
            tools=None,
            max_tokens=max_tokens,
            label=self.name,
        )

        self.preview = PreviewAgent()
        self.loader = LoaderAgent()
        self.analysis = AnalysisAgent()

        self.excel_store = ExcelStore()

        # 子任务结果队列，用于接收子线程传送的任务结果
        self._result_queue: queue.Queue = queue.Queue()
        self._task_seq = 0

        # 每个 Agent 一个常驻 worker 队列；Agent 之间通过 AgentMessage 直连投递
        self.scheduler = Scheduler()
        # 每个 handler 都包一层错误兜底：抛错也要给这个子任务一个结果，
        # 否则 Master 扇入会一直等到超时。
        self.scheduler.register(self.preview.name, self._guard(self._handle_preview))
        self.scheduler.register(self.loader.name, self._guard(self._handle_loader))
        self.scheduler.register(self.analysis.name, self._guard(self._handle_analysis))

        # 启动时提示已恢复的历史（ContextManager 会自动从本地读回该会话）
        if len(CONTEXT):
            BUS.publish(
                "session_loaded",
                agent=self.name,
                session_id=CONTEXT.session_id,
                turns=len(CONTEXT),
            )

    def run(self, input_text: str, **kwargs) -> str:
        """跑一轮问答：记录本轮对话，结束后删除临时产物。"""
        CONTEXT.start_turn(input_text)
        BUS.publish("turn_start", agent=self.name, query=input_text)
        answer = ""
        try:
            answer = self._run_workflow(input_text)
            return answer
        finally:
            CONTEXT.finish_turn(answer)
            self._delete_temp_dirs()
            # 耗时由前端计算并回报（见 server/app.py 的 turn_elapsed），后端不再自己算
            BUS.publish("turn_end", agent=self.name, query=input_text, answer=answer)

    # ------------------------------------------------------------------
    # Master 自己的阶段（过程说明 / 拆解 / 汇总）
    # ------------------------------------------------------------------

    def _log_workflow_step(self, step: str) -> None:
        """Master 的过程说明：既发事件（实时界面），也落盘（还原历史界面）。"""
        publish("workflow_step", agent=self.name, step=step)
        CONTEXT.add_step(step)

    @contextmanager
    def _stage(self, task_id: str):
        """Master 自己的一段处理：发 agent_start / agent_end，并标记 task_id。

        拆解与汇总不是子任务，但界面需要它们各自的卡片能正常收尾，
        所以这里手工补上开始 / 结束事件。
        """
        BUS.publish("agent_start", agent=self.name, task_id=task_id)
        token = set_current_task(task_id)
        try:
            yield
        finally:
            reset_current_task(token)
            BUS.publish("agent_end", agent=self.name, task_id=task_id)

    def _plan(self, input_text: str) -> list[PlanTask]:
        """用模型把用户问题拆成若干子任务（事件归到 TASK_PLAN，前端单独成块）。"""
        try:
            with self._stage(TASK_PLAN):
                self._run_tool_loop(stop_tools={"submit_plan"}, question=input_text)

            # 落盘这段「Master 思考」，重新打开界面时这张卡片才能还原
            CONTEXT.add_agent_record(
                {
                    **self.get_run_record(),
                    "task_id": TASK_PLAN,
                    "query": input_text,
                    "charts": [],
                    "steps": [],
                }
            )

            for call in self._tool_calls:
                if call["name"] != "submit_plan":
                    continue
                args = call.get("input") or {}
                raw_plan = args.get("plan") or {}
                raw_tasks = raw_plan.get("tasks") if isinstance(raw_plan, dict) else None
                tasks = self._parse_tasks(raw_tasks, input_text)
                if tasks:
                    return tasks
        finally:
            # 拆解这一段已经结束：清空 Master 的短期记忆，下一段 / 下一轮重新开始
            self.reset_memory()

        # 兜底：没拆解成功，退化成让 Master 直接回答（避免整轮丢失）
        return [self._new_task(query=input_text, intent=INTENT_ANSWER)]

    def _parse_tasks(self, raw_tasks, input_text: str) -> list[PlanTask]:
        """把模型给的 tasks 规整成 PlanTask 列表。"""
        if not isinstance(raw_tasks, list):
            return []

        tasks: list[PlanTask] = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue
            intent = (raw.get("intent") or "").strip()
            if intent not in VALID_INTENTS:
                intent = INTENT_ANSWER

            tasks.append(
                self._new_task(
                    query=(raw.get("query") or "").strip() or input_text,
                    intent=intent,
                    tables=self._as_list(raw.get("tables")),
                    missing_tables=self._as_list(raw.get("missing_tables")),
                    answer=(raw.get("answer") or "") or "",
                )
            )
        return tasks

    @staticmethod
    def _as_list(value) -> list[str]:
        """把模型给的字符串 / 数组规整成去空白后的字符串列表。"""
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item).strip() for item in value if str(item).strip()]

    def _new_task(
        self,
        query: str,
        intent: str,
        tables: list[str] | None = None,
        missing_tables: list[str] | None = None,
        answer: str = "",
    ) -> PlanTask:
        """建一个带全局唯一 id 的子任务。"""
        self._task_seq += 1
        return PlanTask(
            task_id=f"task{self._task_seq}",
            query=query,
            intent=intent,
            tables=tables or [],
            missing_tables=missing_tables or [],
            answer=answer,
        )

    def _summarize(
        self, input_text: str, plan: list[PlanTask], results: dict[str, str]
    ) -> str:
        """把所有子任务的结果汇总成给用户的最终回答。"""
        lines = [f"用户问题：{input_text}", "", "各子问题的结果："]
        for task in plan:
            lines.append(f"- [{task.intent}] {task.query}")
            lines.append(f"  结果：{results.get(task.task_id, '（无）')}")
        lines.append("")
        lines.append("请把上面所有结果汇总成给用户的最终回答。")

        messages = [Message("user", "\n".join(lines)).to_dict()]
        with self._stage(TASK_SUMMARY):
            token = set_current_agent(self.name)
            try:
                result = self.summary_llm.think(messages)
            finally:
                reset_current_agent(token)

        # 落盘这段「Master 整理结果」，重新打开界面时这张卡片才能还原
        CONTEXT.add_agent_record(
            {
                "agent": self.name,
                "model_calls": [
                    {"thinking": result.thinking, "text": result.text, "tools": []}
                ],
                "tool_calls": [],
                "task_id": TASK_SUMMARY,
                "query": input_text,
                "charts": [],
                "steps": [],
            }
        )
        return result.text

    # ------------------------------------------------------------------
    # 工作流编排（扇出 → 扇入）
    # ------------------------------------------------------------------

    def _run_workflow(self, input_text: str) -> str:
        # 新一轮：清空 Preview 的线索表，并丢掉上一轮遗留的结果队列
        self.preview.reset_clues()
        self._clear_results()

        # 获取Master的拆分计划任务
        plan = self._plan(input_text)

        # 优先派发「涉及表少」的子任务：它们完成得快，也能尽早把表清洗好，
        # 让后面涉及表多的子任务直接复用清洗结果。
        plan.sort(key=lambda task: len(task.tables))
        self._log_workflow_step(f"{self.name} 拆解出 {len(plan)} 个子任务")

        # 落盘一份拆分情况，以便重新打开界面时才能还原「任务拆分与分配」
        detail = [t.to_dict() for t in plan]
        CONTEXT.set_plan(detail)
        publish("plan", agent=self.name, tasks=detail)

        # 只要有子任务存在缺失表，则不执行所有任务，向用户询问缺失表路径
        blocked = [t for t in plan if t.missing_required_tables()]
        if blocked:
            return self._ask_for_tables(plan, blocked)

        # 服务器侧查每张表的清洗状态,为任务添加已清洗过的表
        for task in plan:
            if task.tables:
                task.cleaned = {
                    table: (self.excel_store.find_cleaned_file(table) or "")
                    for table in task.tables
                }

        results: dict[str, str] = {}
        pending: list[PlanTask] = []

        for task in plan:
            # master自己回答的任务则直接将答案记录
            if task.intent == INTENT_ANSWER:
                self._finish_inline(task, task.answer or "（没有生成回答）", results)
            else:
                pending.append(task)

        # 启动Worker线程
        self.scheduler.start()

        # 分配任务
        for task in pending:
            self._dispatch(task)

        # 等待子任务完成，收集结果
        if pending:
            # update为追加，替换key相同键值
            results.update(self._gather_results([t.task_id for t in pending]))

        # 只有一个子任务时不必再汇总，直接返回它的结果
        if len(plan) == 1:
            return results.get(plan[0].task_id, "") or "（没有生成回答）"
        return self._summarize(input_text, plan, results)

    def _ask_for_tables(self, plan: list[PlanTask], blocked: list[PlanTask]) -> str:
        """有子任务缺表：本轮不执行任何任务，只向用户要表。"""
        lines = [
            f"- 「{task.query}」还缺：{'、'.join(task.missing_required_tables())}"
            for task in blocked
        ]
        answer = (
            "开始处理前需要先把表格确认齐，本轮先不做任何处理。请补充：\n"
            + "\n".join(lines)
        )
        self._log_workflow_step(
            f"{self.name} 缺少表格，本轮不执行任何任务（待补 {len(blocked)} 项）"
        )
        # 整轮搁置：所有子任务都标为 blocked，前端据此显示「等待补表」
        for task in plan:
            task.status = "blocked"
            publish(
                "task_end",
                agent=self.name,
                task_id=task.task_id,
                answer=answer,
                status="blocked",
            )
        return answer

    def _finish_inline(self, task: PlanTask, answer: str, results: dict[str, str]) -> None:
        """Master 自己就能定稿的子任务（answer 意图）。"""
        # 也让「直答」任务在过程说明里有一条，和派发出去的任务一一对应
        self._log_workflow_step(f"{self.name} 直接回答：{task.query}")
        task.status = "done"
        publish("task_end", agent=self.name, task_id=task.task_id, answer=answer)
        results[task.task_id] = answer

    def _dispatch(self, task: PlanTask) -> None:
        """把一个子任务投给对应 Agent 的执行队列。"""
        task.status = "running"
        publish(
            "task_start",
            agent=self.name,
            task_id=task.task_id,
            query=task.query,
            intent=task.intent,
            tables=task.tables,
        )

        if task.intent == INTENT_EXCEL_DIRECT:
            receiver = self.preview.name            # 读表直答
        elif task.all_cleaned():
            receiver = self.loader.name             # 表都已清洗：直接取数
        else:
            receiver = self.preview.name            # 有未清洗的表：先出线索

        self._log_workflow_step(f"{self.name} → {receiver}：{task.query}")
        self.scheduler.send(
            AgentMessage(
                sender=self.name,
                receiver=receiver,
                kind=MSG_TASK,
                task_id=task.task_id,
                payload={"task": task},
            )
        )

    def _clear_results(self) -> None:
        """清空结果队列：本轮只收集本轮派发出去的结果。"""
        while True:
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                return

    def _gather_results(self, task_ids: list[str]) -> dict[str, str]:
        """等所有子任务的最终结果（从结果队列里收）。"""
        results: dict[str, str] = {}  # 存放已收到的结果：task_id -> answer
        remaining = set(task_ids)  # 还没收到结果的 task_id 集合
        deadline = time.time() + GATHER_TIMEOUT  # 计算总的截止时间

        while remaining:
            # 超时不再等待
            timeout = deadline - time.time()
            if timeout <= 0:
                break
            try:
                # 阻塞等待结果队列
                task_id, answer = self._result_queue.get(timeout=timeout)
            except queue.Empty:
                break  # 如果在 timeout 内没取到任何结果
            results[task_id] = answer  # 记录该任务的结果
            remaining.discard(task_id)  # 从待收集集合中移除，表示这个任务已收到
        # 处理未收到结果的任务
        for task_id in remaining:
            results[task_id] = "（该子任务超时未返回结果）"
        return results

    def _emit_result(self, task: PlanTask, answer: str) -> None:
        """某个子任务跑完：发事件并投进结果队列，供 Master 扇入。"""
        task.status = "done"
        publish("task_end", agent=self.name, task_id=task.task_id, answer=answer)
        self._result_queue.put((task.task_id, answer))

    # ------------------------------------------------------------------
    # Agent 结果处理（worker 线程里执行）
    # ------------------------------------------------------------------

    def _guard(self, handler):
        """把一个 worker handler 包成「出错也必须给结果」的版本。

        handler 抛异常时，Scheduler 只会发一条 error 事件、不会产出子任务结果，
        Master 扇入就会一直等到超时。这里兜底：出错也把这个子任务标成失败结果。
        """

        def wrapped(message: AgentMessage) -> None:
            try:
                handler(message)
            except Exception as exc:
                task: PlanTask | None = message.payload.get("task")
                label = f"{type(exc).__name__}: {exc}"
                publish("error", agent=message.receiver, message=label)
                if task is not None and task.status != "done":
                    self._emit_result(task, f"[执行失败] {label}")

        return wrapped

    def _handle_preview(self, message: AgentMessage) -> None:
        """Preview：直答，或给出线索后直连投给 Loader。"""
        task: PlanTask = message.payload["task"]
        # 直答
        if task.intent == INTENT_EXCEL_DIRECT:
            outcome = self._run_agent(
                self.preview, task, signal=task.intent, tables=task.tables
            )
            # 返回结果
            self._emit_result(task, outcome.answer or "（Preview 没有生成回答）")
            return
        # 走工作流（传给Preview子任务需要清洗的表格）
        outcome = self._run_agent(
            self.preview, task, signal=task.intent, tables=task.uncleaned_tables()
        )
        # 生成线索发送给Loader
        clue = outcome.clue or {}
        publish("clue", agent=self.preview.name, task_id=task.task_id, clue=clue)
        self.scheduler.send(
            AgentMessage(
                sender=self.preview.name,
                receiver=self.loader.name,
                kind=MSG_CLUE,
                task_id=task.task_id,
                payload={"task": task, "clue": clue},
            )
        )

    def _handle_loader(self, message: AgentMessage) -> None:
        """Loader：清洗/取数后，把数据文件直连投给 Analysis。"""
        task: PlanTask = message.payload["task"]
        # 获取清洗表
        cleaned = {
            table: (self.excel_store.find_cleaned_file(table) or "")
            for table in task.tables
        }
        # 处理任务
        loaded = self._run_agent(
            self.loader,
            task,
            tables=task.tables,
            cleaned=cleaned,
            clue=message.payload.get("clue"),
            task_id=task.task_id,
        )

        data_file = loaded.extracted_file_path
        if not data_file or not os.path.isfile(data_file):
            self._emit_result(
                task,
                f"[工作流中断] {self.loader.name} 未能产出可供分析的数据。\n{loaded.summary}",
            )
            return

        # 抽取结果统一落到本子任务专属的临时路径（模型写偏了也拉回来），
        # 避免与其它子任务、以及正在读它的 Analysis 撞车。
        unique = temp_extracted_file(task.task_id)
        if os.path.abspath(data_file) != os.path.abspath(unique):
            try:
                os.replace(data_file, unique)
            except OSError:
                unique = data_file  # 挪不过去就沿用模型给的路径
        data_file = unique

        self.scheduler.send(
            AgentMessage(
                sender=self.loader.name,
                receiver=self.analysis.name,
                kind=MSG_DATA,
                task_id=task.task_id,
                payload={"task": task, "data_file": data_file},
            )
        )

    def _handle_analysis(self, message: AgentMessage) -> None:
        """Analysis：出答案后回投 Master。"""
        task: PlanTask = message.payload["task"]
        answer = self._run_agent(
            self.analysis,
            task,
            data_file=message.payload["data_file"],
            task_id=task.task_id,
        )
        self._emit_result(task, answer)

    def _run_agent(self, agent, task: PlanTask, **kwargs):
        """在 worker 线程里跑一个子 Agent，发出开始 / 结束事件。"""
        charts: list[dict] = []
        steps: list[str] = []

        def collect(event) -> None:
            # 并行时总线上有所有 Agent 的事件，只收「本 Agent + 本子任务」这一份
            if event.agent != agent.name or event.task_id != task.task_id:
                return
            if event.type == "chart_saved":
                charts.append(
                    {
                        "filename": event.data.get("filename", ""),
                        "title": event.data.get("title", ""),
                        "chart_type": event.data.get("chart_type", ""),
                    }
                )
            elif event.type == "workflow_step":
                steps.append(event.data.get("step", ""))

        BUS.publish("agent_start", agent=agent.name, task_id=task.task_id)
        token = set_current_task(task.task_id)
        BUS.subscribe(collect)
        try:
            return agent.run(task.query, **kwargs)
        finally:
            BUS.unsubscribe(collect)
            reset_current_task(token)
            # 挂在 Agent 上，_record_agent 会把它并进记录一起落盘
            agent.charts = charts
            agent.steps = steps
            BUS.publish("agent_end", agent=agent.name, task_id=task.task_id)
            # 子任务结束：落盘记录并清空该 Agent 的短期记忆（跑失败也要清）
            self._record_agent(agent, task)
            agent.reset_memory()

    def _record_agent(self, agent, task: PlanTask) -> None:
        """一个子任务结束：把该 Agent 的运行记录交给 ContextManager"""
        record = agent.get_run_record()
        record["task_id"] = task.task_id
        record["query"] = task.query
        # 本 Agent 生成的统计图与过程说明：跟着记录一起落盘，刷新界面后才不会丢
        record["charts"] = getattr(agent, "charts", [])
        record["steps"] = getattr(agent, "steps", [])
        CONTEXT.add_agent_record(record)

    # ------------------------------------------------------------------
    # 删除临时产物
    # ------------------------------------------------------------------

    def _delete_temp_dirs(self) -> None:
        """删掉整轮的临时产物目录 temp/（脚本 + 中间数据），只留 cleaned/ 与 charts/。

        临时文件都在 temp/ 下、名字里带子任务号，所以这里不需要逐个文件追踪，
        整个目录一次删掉即可。静默清理：不发 workflow_step。
        """
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

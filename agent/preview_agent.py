"""Preview Agent：按 Master 的意图读表格，并交回答案或线索。"""  # 模块文档字符串，说明该模块的用途

import json
import os
from dataclasses import dataclass
from typing import List

from core.llm import LLM
from core.message import INTENT_EXCEL_DIRECT
from tools import BaseTool, ToolManager, ToolParameter
from tools.builtin import ReadExcelTool
from .base_agent import BaseAgent


PREVIEW_SYSTEM_PROMPT = """你是 Preview Agent。你只做推理，不执行代码。  

Master 已经判定好意图（见【表格信息】里的「Master 的意图」），你必须**严格照做**，不得自行更改。 

【意图 = excel_direct：直接回答】  # 分支一：直答
1. 用 read_excel 逐张读「涉及的表」，rows 传足够大（例如 500），不要只看默认前 20 行；  
2. 自己数、自己算，调用 submit_preview_result，把最终答案填进 answer。  

【意图 = excel_workflow：启动工作流】  # 分支二：走工作流
1. 用 read_excel 逐张读**还没清洗的表**，看清结构（sheet、表头行、数据区、噪声行）；  
2. 调用 submit_preview_result，把线索填进 clue，形状为： 
   {"tables": [ {"file": 表路径, "sheet": Sheet 名, 
                 "header": {"rows": ..., "final_names_row": ..., "merged_columns": ...}, 
                 "data_region": {"rows": ..., "cols": ...},  
                 "noise": {"skip_top_rows": ..., "footer_rows": ..., "notes": ...}, 
                 "recommendation": "加载建议"}, ... ]}  
   每张待清洗的表一项；没有待清洗的表时 tables 留空数组。 

注意： 
- clue 必须是**合法 JSON**：字符串里不要出现真实换行（用空格或分号分隔）， 
  不要出现未转义的双引号；每个字段尽量一句话说清（notes / recommendation 各不超过两句）。
- 一律使用相对路径；思考过程用中文  
"""


@dataclass
class PreviewResult:
    """Preview 的结果：直答给 answer，工作流给 clue。"""

    answer: str = ""                # 直答时的答案
    clue: dict | None = None        # 走工作流时的结构化线索（{"tables": [...]}）


class _SubmitPreviewTool(BaseTool):
    """终止工具：把 Preview 的结果交回 Master / 下游。"""

    def __init__(self):
        super().__init__(
            name="submit_preview_result",
            description=(
                "提交本次结果（终止工具，必须调用一次）。"  
                "Master 意图为 excel_direct 时填 answer；"  
                "Master 意图为 excel_workflow 时填 clue（{\"tables\": [...]}）。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [  # 参数列表
            ToolParameter(
                name="answer",
                type="string",
                description="意图为 excel_direct 时：给用户的最终回答",
                required=False,
            ),
            ToolParameter(
                name="clue",
                type="object",
                description=(
                    "意图为 excel_workflow 时：结构化线索 JSON，" 
                    "形如 {\"tables\": [ {file, sheet, header, data_region, noise, recommendation}, ... ]}"
                ),
                required=False,
            ),
        ]

    def run(self, answer: str = "", clue: dict | None = None) -> str:
        return "已提交 Preview 结果"


class PreviewAgent(BaseAgent):
    """Preview Agent：按 Master 意图用 read_excel 读表，再提交答案或线索。"""

    def __init__(
        self,
        name: str = "Preview",
        system_prompt: str | None = None,
        max_tokens: int = 8000,
    ):
        # Preview 只有两个工具：读表格 + 提交结果。
        tm = ToolManager(register_defaults=False)  #
        tm.register_tool(ReadExcelTool())
        tm.register_tool(_SubmitPreviewTool())

        super().__init__(
            name=name,
            system_prompt=system_prompt or PREVIEW_SYSTEM_PROMPT,
            tool_manager=tm,
        )

        self.llm = LLM(
            system=self.system_prompt,
            tools=self.tool_manager.get_schemas(),
            max_tokens=max_tokens,
            label=self.name,
            silent_tools={"submit_preview_result"},
        )

        # 本轮已经生成过线索的表（归一化键）：同一张表只感知一次，不重复生成线索
        self._clue_cache_tables: list[str] = []


    def run(  # 主入口
        self,
        input_text: str,
        signal: str = "",
        tables: list[str] | None = None,
        **kwargs,
    ) -> PreviewResult:
        """按 Master 的意图执行：直答交 answer，工作流交 clue。

        - signal：Master 的意图（excel_direct / excel_workflow），必须严格执行；
        - tables：这个子任务涉及的未清洗的表格路径。
        """
        tables = tables or []
        # 直答
        if signal == INTENT_EXCEL_DIRECT:
            result = self._run_tool_loop(
                stop_tools={"submit_preview_result"},
                question=input_text,
                signal=signal,
                tables=tables,
            )
            return self._parse_result(result)
        # 本轮还没生成过线索的表
        missing = [t for t in tables if not self.has_clue(t)]

        # 所有表都已有线索：说明 Loader 已经在处理它们了，不再重复感知。
        if not missing:
            self._log_workflow_step("命中已有线索，跳过感知过程，已将问题转交给Loader")
            return PreviewResult()

        # 只感知还没线索的表
        result = self._run_tool_loop(
            stop_tools={"submit_preview_result"},
            question=input_text,
            signal=signal,
            tables=missing,
        )
        # 登记本轮已经生成过线索的表
        for table in missing:
            self.mark_clue(table)
        # 只把本次新生成的线索交给 Loader；旧线索对应的表 Loader 早已在处理
        return self._parse_result(result)

    def _parse_result(self, result) -> PreviewResult:
        """从 submit_preview_result 的入参里取出结果。"""
        for block in result.tool_uses:
            if block["name"] != "submit_preview_result":
                continue
            data = block["input"] or {}
            return PreviewResult(
                answer=data.get("answer", "") or "",
                clue=self._normalize_clue(data.get("clue")),
            )

        # 兜底：模型没有调用提交工具，直接把文本当作答案
        return PreviewResult(answer=result.text)

    @staticmethod
    def _normalize_clue(clue) -> dict | None:
        """线索可能是 dict，也可能是 JSON 字符串。"""
        if not clue:
            return None
        if isinstance(clue, str):
            try:
                return json.loads(clue)
            except json.JSONDecodeError:
                return {"raw": clue}
        return clue

            # ------------------------------------------------------------------
    # 线索表：同一张表在本轮只生成一次
    # ------------------------------------------------------------------

    @staticmethod
    def _table_key(path) -> str:
        """表的线索表键：去空白、统一斜杠、只取文件名并小写。  """
        text = str(path or "").strip().replace("\\", "/")
        return os.path.basename(text).strip().lower()

    def reset_clues(self) -> None:
        """重置线索表。"""
        self._clue_cache_tables = []

    def has_clue(self, table: str) -> bool:
        """这张表本轮是否已经生成过线索（已经交给 Loader 处理）。"""
        return self._table_key(table) in self._clue_cache_tables

    def mark_clue(self, table: str) -> None:
        """登记：这张表本轮已经生成过线索，后续子任务不再重复感知。"""
        key = self._table_key(table)
        if key not in self._clue_cache_tables:
            self._clue_cache_tables.append(key)

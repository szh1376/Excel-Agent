"""Loader Agent：读取、清洗、标准化加载物理数据，并整理出 Analysis 需要的数据文件。"""

from dataclasses import dataclass
from typing import List

from core.context_manager import TEMP_DATA_DIR, temp_extracted_file
from core.llm import LLM
from tools import BaseTool, ToolManager, ToolParameter
from tools.builtin import SaveCleanedTool
from .base_agent import BaseAgent

LOADER_SYSTEM_PROMPT = """你是 Loader Agent：负责物理数据的**读取、清洗、标准化加载**，并整理出 Analysis 要用的数据。
你**不做**任何统计、汇总、计算（计数、求和、平均、分组、占比等），那都是 Analysis 的工作。

用户消息里会给出：涉及的表、哪些**已清洗**（给了清洗表路径）、哪些**未清洗**（给了线索），
以及本子任务的临时文件路径（清洗临时文件、抽取结果文件、脚本示例）。
一个子任务可能涉及多张表，可能有的是已清洗的、有的是待清洗的。

工作流程：
1. 清洗所有**未清洗**的表：
   - 用 read_excel 看真实结构（sheet、表头行、数据区、噪声行）；
   - 写脚本用 bash 运行：.xls 用 engine="xlrd"，.xlsx 用 engine="openpyxl"；
     用 header=<真实表头行号> 读取，跳过标题行、说明行等噪声行；
     去掉全空行、全空列；列名与字符串单元格去掉首尾空格；
   - 把标准化全表写到消息给出的**清洗临时文件**（encoding="utf-8-sig"），
     再对**每张**表调用 save_cleaned_table（excel_path=该表原始路径），
     它会按内容哈希存成 cleaned/<哈希>.csv（长期保留）。

2. 整理 Analysis 要用的数据：从所有相关表的清洗表（已清洗的用消息里给的路径；
   刚清洗的用 cleaned/<哈希>.csv）选列、筛选、去重、重命名、拼接，
   只留解决问题必需的列/行，不要原样丢全表；
   保存为消息给出的**抽取结果文件**（encoding="utf-8-sig"）——
   这个文件会**直接交给 Analysis 用代码读取**，所以要完整、可直接使用。

3. 调用 submit_loader_result 登记：
   - data_path：消息里给出的抽取结果文件路径
   - summary：各清洗表的行列数与列名、抽取了哪些列/行

注意：
- 临时文件都在 temp/ 下、名字里带本子任务编号，整轮结束时系统会把 temp/ 整个删掉，你不用自己删。
- 脚本一律放 temp/scripts/ 下，并以子任务号开头（用消息里给的脚本示例的写法）。
- 工作目录是项目根目录，一律使用相对路径。
- 字符串列去空格不要用 dtype == "object"（pandas 3.x 中是 str），
  请用 pandas.api.types.is_string_dtype。
- 不要编造数据，一切以脚本真实输出为准；思考过程用中文
"""


@dataclass
class LoaderResult:
    """Loader 的产出：交给 Analysis 读取的数据文件。"""

    extracted_file_path: str = ""  # 交给 Analysis 读取的数据文件（temp/data/_extracted_<taskXX>.csv）
    summary: str = ""


class _SubmitLoaderResultTool(BaseTool):
    """终止工具：登记本次取数的结果。"""

    def __init__(self):
        super().__init__(
            name="submit_loader_result",
            description="登记抽取结果文件，结束本次取数。",
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="data_path",
                type="string",
                description=(
                    "抽取结果文件的路径（用消息里给出的那个，"
                    f"形如 {TEMP_DATA_DIR}/_extracted_task1.csv）"
                ),
                required=True,
            ),
            ToolParameter(
                name="summary",
                type="string",
                description="各清洗表的行列数与列名、抽取了哪些列/行、简要说明",
                required=True,
            ),
        ]

    def run(self, data_path: str, summary: str) -> str:
        return f"已登记取数结果：{data_path}"


class LoaderAgent(BaseAgent):
    """Loader Agent：清洗归档（未清洗的表）+ 整理 Analysis 要用的数据文件。"""

    def __init__(
        self,
        name: str = "Loader",
        system_prompt: str | None = None,
        tool_manager: ToolManager | None = None,
        max_tokens: int = 8000,
    ):
        tm = tool_manager or ToolManager()
        tm.register_tool(SaveCleanedTool())
        tm.register_tool(_SubmitLoaderResultTool())

        super().__init__(
            name=name,
            system_prompt=system_prompt or LOADER_SYSTEM_PROMPT,
            tool_manager=tm,
        )

        self.llm = LLM(
            system=self.system_prompt,
            tools=self.tool_manager.get_schemas(),
            max_tokens=max_tokens,
            label=self.name,
            silent_tools={"submit_loader_result"},
        )

    def run(
        self,
        input_text: str,
        tables: list[str] | None = None,
        cleaned: dict[str, str] | None = None,
        clue: dict | None = None,
        task_id: str = "",
        **kwargs,
    ) -> LoaderResult:
        """取数：未清洗的表先清洗归档，再把所有相关表的数据整理成一个数据文件。

        - tables：这个子任务涉及的所有原始表路径；
        - cleaned：原始表 → 已清洗路径（服务器侧查出来的）；
        - clue：Preview 为未清洗表给出的线索（{"tables": [...]}）；
        - task_id：子任务编号，用来定位本子任务在 temp/ 下那些带 taskXX 的临时文件。
        """
        result = self._run_tool_loop(
            stop_tools={"submit_loader_result"},
            question=input_text,
            tables=tables or [],
            cleaned=cleaned or {},
            clue=clue,
            task_id=task_id,
        )
        return self._parse_submit_result(result, task_id)

    def _parse_submit_result(self, result, task_id: str = "") -> LoaderResult:
        """从模型提交的 submit_loader_result 调用里取出结果。"""
        for block in result.tool_uses:
            if block["name"] == "submit_loader_result":
                arguments = block["input"] or {}
                return LoaderResult(
                    extracted_file_path=(
                        arguments.get("data_path") or temp_extracted_file(task_id)
                    ).strip(),
                    summary=arguments.get("summary", ""),
                )

        # 兜底：模型没有调用提交工具
        self._log_workflow_step(f"{self.name} 未能登记取数结果")
        return LoaderResult(summary=result.text)

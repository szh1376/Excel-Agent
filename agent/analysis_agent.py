"""Analysis Agent：数据分析师，读取 Loader 产出的数据文件做深挖、计算与可视化。"""

from core.llm import LLM
from tools import ToolManager
from tools.builtin import SaveChartTool
from .base_agent import BaseAgent


ANALYSIS_SYSTEM_PROMPT = """你是 Analysis Agent（数据分析师）。Loader 已把需要的数据保存成一个临时 CSV 文件，
路径在用户消息里给出；根据该数据与用户的问题，完成用户的任务。

工作流程：
1. 用代码读取该 CSV（pandas.read_csv，encoding="utf-8-sig"），一切数字以代码输出为准；
2. 把分析与出图写成脚本（放消息给出的脚本目录 temp/scripts/ 下、文件名以子任务号开头）
   用 bash 运行，做深度挖掘、计算与可视化；
3. 最后用中文给出答案：给出关键数字并说明统计口径（有效样本数、缺失值如何处理等），
   不要只贴代码，也不要编造数字。

【需要出图时】用户要求「画图 / 图表 / 可视化 / 趋势 / 占比」等，或明确要看图：
1. 用 matplotlib 生成 png（建议 dpi=110、尺寸别太大；
   临时图片写到 temp/data/ 下，文件名以子任务号开头，如 temp/data/task1_chart1.png）；
2. **必须**设置中文字体，否则中文全部变方块：
3. 图保存好后调用 save_chart 工具交给界面显示（一张图调用一次，传标题与图表类型）；
4. 回答里用文字概括图说明了什么（关键数值与结论）；不要贴图片路径，界面会自己显示。

注意：
- 一切以代码读取的数据文件为准；数据不足就明确指出缺了什么，不要臆测。
- 工作目录是项目根目录，一律使用相对路径。
- 脚本与临时图片统一放 temp/ 下（名字都带子任务号），整轮结束时系统会把 temp/ 整个删掉，
  答案正文不要引用这些路径。
- 思考过程用中文
"""


class AnalysisAgent(BaseAgent):
    """Analysis Agent：读取 Loader 产出的数据文件，完成分析并返回答案。"""

    def __init__(
        self,
        name: str = "Analysis",
        system_prompt: str | None = None,
        tool_manager: ToolManager | None = None,
        max_tokens: int = 8000,
    ):
        super().__init__(
            name=name,
            system_prompt=system_prompt or ANALYSIS_SYSTEM_PROMPT,
            tool_manager=tool_manager or ToolManager(),
        )
        # 出图后交给界面显示（存盘、去重、发事件都由该工具负责）
        self.tool_manager.register_tool(SaveChartTool())

        self.llm = LLM(
            system=self.system_prompt,
            tools=self.tool_manager.get_schemas(),
            max_tokens=max_tokens,
            label=self.name,
        )

    def run(self, input_text: str, data_file: str = "", **kwargs) -> str:
        """基于 Loader 产出的数据文件回答问题。"""
        result = self._run_tool_loop(question=input_text, data_file=data_file)
        return result.text

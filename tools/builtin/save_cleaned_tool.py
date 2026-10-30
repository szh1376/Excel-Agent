"""保存清洗结果到清洗仓库的工具（供 Loader 使用）。"""

from typing import List

from core.context_manager import temp_cleaned_file
from core.events import current_task
from utils.excel_store import ExcelStore
from ..base_tool import BaseTool, ToolParameter


class SaveCleanedTool(BaseTool):
    """把清洗好的表格按「原始表格内容的哈希」保存到 cleaned/ 下。"""

    def __init__(self):
        super().__init__(
            name="save_cleaned_table",
            description=(
                "把刚清洗好的表格（写在消息给出的清洗临时文件里）保存进清洗仓库 cleaned/，"
                "文件名由原始表格内容的哈希决定；返回保存后的路径。"
                "只需要传原始 Excel 路径，哈希由本工具自己计算。"
            ),
        )
        self.store = ExcelStore()

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="excel_path",
                type="string",
                description="原始 Excel 文件路径（用来计算哈希）",
                required=True,
            ),
        ]

    def run(self, excel_path: str) -> str:
        try:
            saved_path = self.store.save_cleaned_file(
                excel_path, temp_cleaned_file(current_task())
            )
        except FileNotFoundError as e:
            return f"Error: 找不到文件：{e}"
        except Exception as e:
            return f"Error: 保存清洗表失败：{e}"
        return f"已保存清洗表：{saved_path}"

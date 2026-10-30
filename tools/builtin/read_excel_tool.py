"""读取 Excel 表格预览的工具（基于 pandas）。"""

from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import safe_path

# 预览内容的最大字符数，超出会截断（并在结果里明确告知，避免模型"看不全却硬数"）
MAX_PREVIEW_CHARS = 20000


class ReadExcelTool(BaseTool):
    """预览 Excel 结构：sheet 列表、行列数、前若干行原始内容。

    返回的是「原始行」（不解析表头），便于模型判断表头行、数据区与噪声行。
    """

    def __init__(self):
        super().__init__(
            name="read_excel",
            description="预览 Excel 表格结构（sheet、行列数、前若干行原始内容），用于判断表头行与数据区。",
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="Excel 文件路径（.xls / .xlsx）",
                required=True,
            ),
            ToolParameter(
                name="rows",
                type="integer",
                description="每个 sheet 预览的行数，默认 500",
                required=False,
            ),
            ToolParameter(
                name="sheet",
                type="string",
                description="只预览指定 sheet 名（默认预览全部）",
                required=False,
            ),
        ]

    def run(self, path: str, rows: int = 500, sheet: str | None = None) -> str:
        try:
            import pandas as pd
        except ImportError:
            return "Error: 未安装 pandas，请先执行 pip install pandas xlrd"

        try:
            file_path = safe_path(path)
            xls = pd.ExcelFile(file_path)
            sheet_names = [sheet] if sheet else list(xls.sheet_names)
        except Exception as e:
            return f"Error: {e}"

        lines = [f"文件: {path}", f"Sheet 列表: {list(xls.sheet_names)}"]

        for name in sheet_names:
            try:
                full = xls.parse(name, header=None)
            except Exception as e:
                lines.append(f"\nSheet: {name}  读取失败: {e}")
                continue

            n_rows, n_cols = full.shape
            lines.append(f"\nSheet: {name}  (总行数={n_rows}, 总列数={n_cols})")

            head = full.head(rows).fillna("").values.tolist()
            lines.append(f"前 {len(head)} 行（原始，含表头/噪声行）:")
            for i, row in enumerate(head):
                cells = [str(c).strip() for c in row]
                lines.append(f"  row {i}: {cells}")

        text = "\n".join(lines)
        if len(text) > MAX_PREVIEW_CHARS:
            text = (
                text[:MAX_PREVIEW_CHARS]
                + "\n\n...（预览内容过长已截断，以上并非全部数据；"
                "数据量偏大，不要靠肉眼统计，请改走工作流）"
            )
        return text

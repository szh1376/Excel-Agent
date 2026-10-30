"""保存统计图到图表仓库的工具（供 Analysis 使用）。

模型只负责用代码把图生成出来并给出路径，归档、去重、
以及「通知界面去显示这张图」都由本工具负责，模型不需要记住任何目录约定。
"""

import os
from typing import List

from core.events import publish
from utils.chart_store import ChartStore
from ..base_tool import BaseTool, ToolParameter


class SaveChartTool(BaseTool):
    """把刚生成的统计图归档到 charts/，并让界面把它显示出来。"""

    def __init__(self):
        super().__init__(
            name="save_chart",
            description=(
                "把刚生成的统计图归档并显示到界面上。"
                "用法：先用代码（matplotlib 等）把图保存成 png 文件，"
                "再调用本工具并传入这个文件路径。"
                "调用成功后界面会自动显示这张图，你不需要在回答里贴图片路径。"
                "一张图调用一次；生成多张图就调用多次。"
            ),
        )
        self.store = ChartStore()

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="image_path",
                type="string",
                description="刚生成的图片文件路径（如 temp/data/task1_chart1.png）",
                required=True,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="这张图的标题，用中文概括它展示的内容（如「各单位男女比例」）",
                required=True,
            ),
            ToolParameter(
                name="chart_type",
                type="string",
                description="图表类型，便于界面归类展示：柱状图 / 饼图 / 折线图 / 散点图 / 其他",
                required=False,
            ),
        ]

    def run(self, image_path: str, title: str, chart_type: str = "") -> str:
        try:
            saved_path = self.store.save(image_path)
        except (FileNotFoundError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: 保存图表失败：{e}"

        filename = os.path.basename(saved_path)
        # 通知界面：事件里只带文件名与标题，图片本身走 /api/charts 静态接口
        publish(
            "chart_saved",
            filename=filename,
            title=title,
            chart_type=chart_type,
            path=saved_path,
        )
        return f"已保存图表「{title}」，界面已显示。（文件名 {filename}）"

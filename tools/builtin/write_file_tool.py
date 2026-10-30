"""写入文件内容的工具。"""

from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import safe_path


class WriteFileTool(BaseTool):
    """把内容写入文件（自动创建父目录）。"""

    def __init__(self):
        super().__init__(name="write_file", description="Write content to a file.")

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="File path to write.",
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="Content to write.",
                required=True,
            ),
        ]

    def run(self, path: str, content: str) -> str:
        try:
            file_path = safe_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

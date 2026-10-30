"""读取文件内容的工具。"""

from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import safe_path


class ReadFileTool(BaseTool):
    """读取文件内容（可限制行数）。"""

    def __init__(self):
        super().__init__(name="read_file", description="Read file contents.")

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="File path to read.",
                required=True,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Max lines to read.",
                required=False,
            ),
        ]

    def run(self, path: str, limit: int | None = None) -> str:
        try:
            lines = safe_path(path).read_text(encoding="utf-8").splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

"""精确替换文件中文本的工具。"""

from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import safe_path


class EditFileTool(BaseTool):
    """把文件中第一处匹配文本替换为新文本。"""

    def __init__(self):
        super().__init__(
            name="edit_file",
            description="Replace exact text in a file once.",
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="File path to edit.",
                required=True,
            ),
            ToolParameter(
                name="old_text",
                type="string",
                description="Exact text to find.",
                required=True,
            ),
            ToolParameter(
                name="new_text",
                type="string",
                description="Replacement text.",
                required=True,
            ),
        ]

    def run(self, path: str, old_text: str, new_text: str) -> str:
        try:
            file_path = safe_path(path)
            text = file_path.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: text not found in {path}"
            file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except Exception as e:
            return f"Error: {e}"

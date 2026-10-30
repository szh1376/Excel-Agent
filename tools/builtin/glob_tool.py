"""按 glob 模式查找文件的工具。"""

import glob as g
from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import WORKDIR


class GlobTool(BaseTool):
    """查找匹配 glob 模式的文件。"""

    def __init__(self):
        super().__init__(
            name="glob",
            description="Find files matching a glob pattern; ** matches recursively.",
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="Glob pattern to match.",
                required=True,
            ),
        ]

    def run(self, pattern: str) -> str:
        try:
            matches = sorted({
                match for match in g.glob(pattern, root_dir=WORKDIR, recursive=True)
                if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
            })
            shown = matches[:200]
            if len(matches) > 200:
                shown.append("... (more matches omitted; narrow the pattern)")
            return "\n".join(shown) if shown else "(no matches)"
        except Exception as e:
            return f"Error: {e}"

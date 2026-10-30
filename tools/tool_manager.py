"""工具管理类：负责工具的注册、注销、执行与 schema 汇总。"""

from typing import Dict, List

from .base_tool import BaseTool
from .builtin import (
    BashTool,
    EditFileTool,
    GlobTool,
    ReadExcelTool,
    ReadFileTool,
    WriteFileTool,
)


class ToolManager:
    """工具管理类：默认注册内置工具，也可选择从空开始。"""

    def __init__(self, register_defaults: bool = True):
        self._tools: Dict[str, BaseTool] = {}
        if register_defaults:
            self._register_default_tools()

    def _register_default_tools(self) -> None:
        """注册默认内置工具。"""
        for tool in (
            BashTool(),
            ReadFileTool(),
            ReadExcelTool(),
            WriteFileTool(),
            EditFileTool(),
            GlobTool(),
        ):
            self.register_tool(tool)

    def register_tool(self, tool: BaseTool) -> None:
        """注册工具对象（同名工具会被覆盖）。"""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """按名称注销工具（不存在则忽略）。"""
        self._tools.pop(name, None)

    def execute(self, name: str, **kwargs) -> str:
        """按名称执行工具，kwargs 为工具入参，返回字符串结果。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        if not tool.validate_parameters(**kwargs):
            return f"Error: missing required parameters for tool '{name}'"
        return tool.run(**kwargs)

    def get_tools(self) -> List[BaseTool]:
        """获取所有已注册的工具对象。"""
        return list(self._tools.values())

    def get_schemas(self) -> List[Dict]:
        """获取所有已注册工具的 schema，用于传给模型。"""
        return [tool.get_schema() for tool in self._tools.values()]

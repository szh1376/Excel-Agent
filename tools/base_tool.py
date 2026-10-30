"""工具基类：定义工具的通用接口（参数声明、参数校验、schema 生成、执行）。"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from pydantic import BaseModel


class ToolParameter(BaseModel):
    """工具参数定义。"""

    name: str  # 参数名称
    type: str  # 参数类型
    description: str  # 参数描述
    required: bool = True  # 参数是否必需
    default: Any = None  # 默认参数值


class BaseTool(ABC):
    """工具基类：子类只需实现 run()，并覆写 get_parameters() 声明入参。"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    def run(self, **kwargs) -> str:
        """执行工具的具体方法。

        kwargs 为工具入参，键名与 get_parameters() 声明的参数名一致。
        返回：字符串格式的执行结果。
        """

    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义（默认无参数，子类按需覆写）。"""

    def validate_parameters(self, **kwargs) -> bool:
        """验证参数：所有必需参数都已传入才算通过。"""
        for param in self.get_parameters():
            if param.required and param.name not in kwargs:
                return False
        return True

    def get_schema(self) -> Dict[str, Any]:
        """生成给模型使用的工具 schema（Anthropic tools 格式）"""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param in self.get_parameters():
            properties[param.name] = {
                "type": param.type,
                "description": param.description,
            }
            if param.default is not None:
                properties[param.name]["default"] = param.default
            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

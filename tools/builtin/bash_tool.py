"""执行 shell 命令的工具。"""

import os
import subprocess
from typing import List

from ..base_tool import BaseTool, ToolParameter
from ._fs import WORKDIR


class BashTool(BaseTool):
    """运行 shell 命令并返回输出。"""

    # 危险命令黑名单
    _DANGEROUS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    def __init__(self):
        super().__init__(name="bash", description="Run a shell command.")

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The shell command to run.",
                required=True,
            ),
        ]

    def run(self, command: str) -> str:
        if any(d in command for d in self._DANGEROUS):
            return "Error: Dangerous command blocked"

        # 统一 UTF-8：避免中文输出在 Windows 控制台（GBK）下乱码
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            out = (proc.stdout + proc.stderr).strip()
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (120s)"
        except (FileNotFoundError, OSError) as e:
            return f"Error: {e}"

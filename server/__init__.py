"""后端服务包：把 ExcelAgent 的工作流通过 HTTP + WebSocket 暴露给界面。"""

from .app import app

__all__ = ["app"]

"""内置文件类工具共用的工作目录与路径安全校验。"""

from pathlib import Path

# 工具运行的工作目录（与 main.py 的 WORKDIR 保持一致）
WORKDIR = Path.cwd()


def safe_path(p: str) -> Path:
    """把相对路径解析到工作目录内，防止路径逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

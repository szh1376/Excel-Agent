"""后端服务启动入口（不带界面，方便用浏览器 / curl 调试）。

用法：
    .venv\\Scripts\\python.exe run_server.py               # 自动找空闲端口（默认从 8765 起）
    .venv\\Scripts\\python.exe run_server.py --port 9000   # 指定端口
    .venv\\Scripts\\python.exe run_server.py --reload      # 改代码自动重启

端口约定（Electron 主进程依赖这个约定）：
- 默认从 8765 开始找，被占用就往后试，最多试 20 个；
- 就绪后向 stdout 打印一行「ExcelAgent 后端已就绪 http://127.0.0.1:PORT」，
  Electron 主进程读这一行拿到真实地址。

工作目录必须是项目根目录，因为 sessions/ 与 cleaned/ 都是相对路径。
"""

import argparse
import socket

import uvicorn

from server.app import app

DEFAULT_PORT = 8765
MAX_PORT_TRIES = 20


def find_free_port(host: str, start: int) -> int:
    """从 start 开始找一个能绑定的端口。"""
    for port in range(start, start + MAX_PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{start} 起连续 {MAX_PORT_TRIES} 个端口都被占用，请用 --port 指定")


def main() -> None:
    parser = argparse.ArgumentParser(description="ExcelAgent 后端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    args = parser.parse_args()

    port = args.port or find_free_port(args.host, DEFAULT_PORT)
    base = f"http://{args.host}:{port}"

    # Electron 主进程靠这一行拿到真实地址，格式不要改（这是启动握手，不是日志）
    print(f"ExcelAgent 后端已就绪 {base}", flush=True)

    # 用 reload 时必须传 import 字符串（uvicorn 会另起进程监视文件变化）
    target = "server.app:app" if args.reload else app
    # 除上面那行握手外，后端进程不再往控制台打任何内容
    uvicorn.run(
        target,
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="critical",
        access_log=False,
    )


if __name__ == "__main__":
    main()

# ExcelAgent

用中文自然语言分析本地 Excel 表格的桌面应用。Python 后端通过多智能体工作流读取、预览和分析数据；Electron + React 前端展示会话、执行过程、结论和图表。

## 快速开始

需要 Python 3.14、Node.js 和 npm。后端依赖见 `requirements.txt`，前端依赖锁定在 `Frontend/package-lock.json`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 填入自己所用模型服务的 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL_ID`。然后从项目根目录启动后端：

```powershell
.\.venv\Scripts\python.exe run_server.py
```

另开终端启动桌面端：

```powershell
cd Frontend
npm ci
npm run dev
```

可用 `npm run typecheck` 检查 TypeScript，`npm run build` 生成桌面端构建产物。后端默认监听本机 `127.0.0.1:8765`；若要手动指定端口，可运行 `run_server.py --port 9000`。桌面端会尝试启动后端进程，若本机 Python 路径不同，可设置 `EXCEL_AGENT_PYTHON` 指向虚拟环境解释器。

## 目录

| 路径 | 内容 |
| --- | --- |
| `agent/` | 主控、预览、加载和分析智能体 |
| `core/` | 模型调用、消息、调度、事件与会话 |
| `tools/` | 文件、表格、图表工具 |
| `utils/` | 表格及图表存储 |
| `server/`、`run_server.py` | FastAPI 服务和启动入口 |
| `Frontend/` | Electron、React 和 TypeScript 界面 |

## 数据说明

表格内容会被本地后端读取，相关上下文可能发送给 `.env` 所配置的模型服务。使用前请确认表格可以交给该服务处理。

`.env`、原始表格、会话、清洗结果、图表、缓存和构建产物均不纳入仓库。首次运行会按需生成运行目录。项目目前面向个人本地使用，尚未提供多用户隔离或公网部署配置。

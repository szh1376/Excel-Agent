"""ExcelAgent 后端服务：给桌面界面提供 HTTP + WebSocket 接口。

设计要点：
1. Agent 依然是同步阻塞的，跑在一个后台线程里，不改写成 async。
2. 事件经 asyncio.Queue 转交给 event loop —— 必须用 call_soon_threadsafe，
   因为事件是在 Agent 线程里同步发出的。
3. 同一时刻只允许跑一轮问答（全局锁），避免多客户端同时改同一份会话状态。
4. 事件带自增 seq，前端断线重连时用 since_seq 补齐。
5. 由于项目无user标识，因此答案是广播所有客户端的，即桌面端，Web端，以及其余客户端。

接口一览：
    GET  /api/health                        健康检查
    GET  /api/config                        前端需要的配置（模型名等）
    GET  /api/events?since_seq=N            历史事件（断线重连补齐）
    GET  /api/sessions                      会话列表
    POST /api/sessions                      新建会话
    POST /api/sessions/{id}/rename          重命名
    DELETE /api/sessions/{id}               删除
    GET  /api/sessions/{id}/messages        某个会话的完整记录
    POST /api/sessions/{id}/switch          切换当前会话
    WS   /ws                                query / switch_session / ping
"""

import asyncio
import glob
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from agent.master_agent import MasterAgent
from core.context_manager import CONTEXT
from core.events import BUS, Event
from utils.chart_store import ChartStore

# 前端（Electron 渲染进程 / Vite dev server / 浏览器）可能来自这些来源
ALLOWED_ORIGINS = ["*"]

# 握手包重发间隔（秒）：界面靠它校正「是否正在运行」
HEARTBEAT_SECONDS = 3


# ----------------------------------------------------------------------
# 事件桥：把 Agent 线程里发出的事件转交给 event loop
# ----------------------------------------------------------------------


class EventStream:
    """管理所有 WebSocket 订阅者，负责把事件投递到各自的队列。"""

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue] = {}   # key -> 客户端队列
        self._lock = threading.Lock()                 # 保护 _queues 的线程锁
        self._loop: asyncio.AbstractEventLoop | None = None  # 主 event loop 引用

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录 event loop，供 Agent 线程跨线程投递使用。"""
        self._loop = loop

    def register(self, queue: asyncio.Queue) -> int:
        """登记一个客户端队列，返回它的 key。"""
        with self._lock:
            key = id(queue)
            self._queues[key] = queue
            return key

    def unregister(self, key: int) -> None:
        """注销客户端队列。"""
        with self._lock:
            self._queues.pop(key, None)

    def subscriber_count(self) -> int:
        """当前连接的客户端数。"""
        with self._lock:
            return len(self._queues)

    def __call__(self, event: Event) -> None:
        """
        在Bus中每publish一个event调用该函数
        Bus.publish为同步且在agent线程中调用，因此需要将该线程转到FastAPI/WebSocket的异步线程中
        因此fanout的功能即为将该线程转到FastAPI/WebSocket中，即event loop
        """
        self.fanout(event.to_dict())     # 事件对象转 dict 后统一分发

    def fanout(self, payload: dict) -> None:
        """把事件投递给所有客户端。"""
        with self._lock:                          # 加锁
            queues = list(self._queues.values())
        if not queues:                            # 没有订阅者
            return

        for queue in queues:                      # 遍历每个客户端队列
            try:
                # 事件在 Agent 线程里发出，必须切回 event loop 才能安全入队
                if self._loop is not None and self._loop.is_running():
                    # 把"入队"这个动作打包成回调，交给 loop 线程去执行
                    self._loop.call_soon_threadsafe(queue.put_nowait, payload)
                else:
                    queue.put_nowait(payload)     # 兜底：同线程直接入队
            except RuntimeError:
                # loop 已关闭（服务正在退出），忽略即可
                pass


STREAM = EventStream()          # 全局唯一事件桥实例
BUS.subscribe(STREAM)           # 模块导入时就把事件桥挂到事件总线上

# 图表仓库：把 Agent 生成的统计图当静态文件发给界面
CHARTS = ChartStore()


# ----------------------------------------------------------------------
# 应用状态
# ----------------------------------------------------------------------


class AppState:
    """进程级状态：Master、全局锁、当前会话。"""

    def __init__(self) -> None:
        self._master: MasterAgent | None = None
        self._init_lock = threading.Lock()       # 保护 Master 的懒加载
        self._lock = threading.Lock()            # 保证同一时刻只跑一轮
        self.busy = False                        # 是否正在处理一轮问答

    @property
    def master(self) -> MasterAgent:
        """懒加载 Master（首次用到时才建，避免启动即读会话）。"""
        # 双检索创建master实例
        if self._master is None:
            with self._init_lock:
                if self._master is None:
                    self._master = MasterAgent()
        return self._master

    def run_query(self, text: str) -> str:
        """在调用线程里跑一轮问答（由后台线程调用）。"""
        with self._lock:
            self.busy = True
            try:
                return self.master.run(text)
            finally:
                self.busy = False

    def switch_session(self, session_id: str) -> str:
        """切换当前会话。"""
        with self._lock:
            self.busy = True
            try:
                return CONTEXT.switch_session(session_id)
            finally:
                self.busy = False


STATE = AppState()              # 全局唯一应用状态


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def session_to_dict(entry) -> dict:
    """会话索引条目转成前端友好的字典（时间补上可读格式）。"""
    data = asdict(entry)
    data["updated_at_text"] = time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(entry.updated_at)
    )
    return data                                  # 返回给前端


def detect_excel_files() -> list[str]:
    """列出项目根目录下的 Excel 文件，供界面选择。"""
    files = sorted(glob.glob("*.xls") + glob.glob("*.xlsx"))  # 两种后缀合并后排序
    return files                                 # 返回文件列表


# ----------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时把当前 event loop 交给事件桥，Agent 线程才能跨线程投递。"""
    STREAM.bind_loop(asyncio.get_running_loop())  # 记录当前 loop
    yield                                          # yield 之前是启动，之后是关闭


app = FastAPI(title="ExcelAgent 后端", version="0.1.0", lifespan=lifespan)  # 创建应用

app.add_middleware(                              # 注册 CORS 中间件
    CORSMiddleware,                              # 中间件类
    allow_origins=ALLOWED_ORIGINS,               # 允许的来源
    allow_credentials=False,                     # 不带 cookie
    allow_methods=["*"],                         # 允许所有方法
    allow_headers=["*"],                         # 允许所有头
)


@app.get("/api/health")
async def health() -> dict:
    """健康检查，Electron 主进程启动时用它等后端就绪。"""
    return {                                     # 一次性返回状态
        "ok": True,                              # 服务存活
        "busy": STATE.busy,                      # 当前是否在跑问答
        "session_id": CONTEXT.session_id,        # 当前会话 id
        "subscribers": STREAM.subscriber_count(),# 当前 WS 连接数
    }


@app.get("/api/config")
async def config() -> dict:
    """前端需要的配置信息。"""
    return {                                     # 返回配置
        "model": os.getenv("LLM_MODEL_ID", ""),  # 模型 id（环境变量）
        "session_id": CONTEXT.session_id,        # 当前会话 id
        "excel_files": detect_excel_files(),     # 可选的 Excel 文件
    }


@app.get("/api/events")
async def events(since_seq: int = 0) -> dict:
    """历史事件：前端断线重连后用它补齐。"""
    return {"events": BUS.history(since_seq)}    # 从事件总线取 since_seq 之后的事件


# ------------------------------ 图表 ------------------------------


@app.get("/api/charts")
async def list_charts() -> dict:
    """已归档的图表列表（按修改时间倒序）。"""
    return {"charts": CHARTS.list_charts()}      # 委托给图表仓库


@app.get("/api/charts/{name}")
async def get_chart(name: str) -> FileResponse:
    """取一张统计图。

    图片不走事件流（一张图几百 KB，塞进 WebSocket 会拖慢流式输出），
    事件里只带文件名，界面用这个接口按需拉取。
    """
    path = CHARTS.resolve(name)                  # 解析文件名到安全路径
    if path is None:                             # 解析失败（不存在/非法）
        raise HTTPException(status_code=404, detail="图表不存在")  # 404
    return FileResponse(path)                    # 返回文件内容


# ------------------------------ 会话管理 ------------------------------


@app.get("/api/sessions")
async def list_sessions() -> dict:
    """会话列表（按最近使用倒序）。"""
    return {                                     # 返回列表 + 当前会话
        "sessions": [session_to_dict(e) for e in CONTEXT.store.list_sessions()],  # 逐条转换
        "current": CONTEXT.session_id,           # 当前会话 id
    }


@app.post("/api/sessions")
async def create_session(payload: dict | None = None) -> dict:
    """新建会话并切换过去。"""
    payload = payload or {}                      # None 保护
    entry = CONTEXT.store.create(                # 创建会话
        title=payload.get("title", ""),          # 可选标题
        excel_file=payload.get("excel_file", ""),# 可选 Excel 文件
    )
    STATE.switch_session(entry.id)               # 立即切到新会话
    return {"session": session_to_dict(entry)}   # 返回新会话信息


@app.post("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, payload: dict) -> dict:
    """重命名会话。"""
    title = (payload or {}).get("title", "").strip()  # 取标题并去空白
    if not title:                                # 空标题
        raise HTTPException(status_code=400, detail="标题不能为空")  # 400
    entry = CONTEXT.store.rename(session_id, title)  # 执行重命名
    if entry is None:                            # 会话不存在
        raise HTTPException(status_code=404, detail="会话不存在")    # 404
    return {"session": session_to_dict(entry)}   # 返回更新后的会话


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """删除会话。若删的是当前会话，自动切到最近的一个会话。"""
    if not CONTEXT.store.delete(session_id):     # 删除失败
        raise HTTPException(status_code=404, detail="会话不存在")    # 404

    result: dict[str, Any] = {"deleted": session_id, "current": CONTEXT.session_id}  # 初始结果
    if CONTEXT.session_id == session_id:         # 删的是当前会话
        remaining = CONTEXT.store.list_sessions()# 看还剩哪些
        if remaining:                            # 还有剩余
            STATE.switch_session(remaining[0].id)# 切到最近一个
        else:
            # 一个都没有了：建一个新的，保证界面永远有一个可用会话
            entry = CONTEXT.store.create()       # 新建空会话
            STATE.switch_session(entry.id)       # 切过去
        result["current"] = CONTEXT.session_id   # 更新 current
    return result                                # 返回结果


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict:
    """某个会话的完整对话记录（含各 Agent 的思考与工具调用）。"""
    path = CONTEXT.store.session_path(session_id)  # 会话文件路径
    if not os.path.exists(path):                   # 文件不存在
        return {"session_id": session_id, "turns": []}  # 返回空记录

    try:
        with open(path, encoding="utf-8") as f:    # 打开文件
            data = json.load(f)                    # 读 JSON
    except (OSError, ValueError):                  # IO/解析错误
        return {"session_id": session_id, "turns": []}  # 也返回空
    return {"session_id": session_id, "turns": data.get("turns", [])}  # 返回 turns


@app.post("/api/sessions/{session_id}/switch")
async def switch_session(session_id: str) -> dict:
    """切换当前会话。"""
    if not CONTEXT.store.exists(session_id):       # 会话不存在
        raise HTTPException(status_code=404, detail="会话不存在")    # 404
    current = STATE.switch_session(session_id)     # 执行切换
    return {"current": current}                    # 返回当前会话 id


# ------------------------------ WebSocket ------------------------------


def hello_payload() -> dict:
    """握手包内容：让界面能校正自己的「是否正在运行」状态。"""
    return {
        "type": "hello",                           # 类型
        "agent": "Server",                         # 来源
        "data": {
            "session_id": CONTEXT.session_id,      # 当前会话
            "busy": STATE.busy,                    # 后端是否正在跑一轮
            "last_seq": BUS._seq,                  # 后端事件序号进度
        },
        "time": time.time(),                       # 时间
        "seq": 0,                                  # 系统事件不带序号
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket：接收 query / switch_session，推送事件流。"""
    await websocket.accept()                       # 建立连接
    # websocket一对一，为每个连接服务器的客户端创建一个异步队列
    queue: asyncio.Queue = asyncio.Queue()         # 该客户端专属事件队列
    key = STREAM.register(queue)                   # 注册到事件桥
    # 告知客户端当前的初始状态
    await websocket.send_json(hello_payload())

    async def pump_events() -> None:
        """把队列里的事件持续写给客户端。服务器——>客户端"""
        while True:
            # 挂起等队列有事件
            payload = await queue.get()
            # 写给客户端
            await websocket.send_json(payload)

    async def heartbeat() -> None:
        """定期重发握手包，让界面自愈。

        它带着后端的真实 busy / last_seq：
        - 万一 turn_end 在断线期间丢了，界面不会永远卡在「正在运行」；
        - 后端进程重启后事件序号会从 0 重来，界面据此把本地序号校正回去，
          否则之后每条事件都会被去重逻辑当成「重复」丢掉。
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)  # 间隔
            await websocket.send_json(hello_payload())  # 重发

    pump = asyncio.create_task(pump_events())      # 后台推事件任务
    beat = asyncio.create_task(heartbeat())        # 后台心跳任务

    try:
        while True:                                # 主循环：持续收消息
            message = await websocket.receive_json()  # 读一条 JSON
            await handle_client_message(websocket, message)  # 分发处理
    except WebSocketDisconnect:
        # 正常断开
        pass
    except Exception:
        # 客户端异常断开：正常收尾
        pass
    finally:
        pump.cancel()                              # 取消推送任务
        beat.cancel()                              # 取消心跳任务
        STREAM.unregister(key)                     # 从事件桥注销


async def handle_client_message(websocket: WebSocket, message: dict) -> None:
    """处理来自客户端的一条消息。"""
    kind = message.get("type")                     # 取消息类型

    if kind == "ping":
        await websocket.send_json({"type": "pong", "data": {}, "seq": 0, "time": time.time()})
        return

    if kind == "switch_session":                   # 切会话
        session_id = message.get("session_id", "")
        if not CONTEXT.store.exists(session_id):
            await send_error(websocket, f"会话不存在：{session_id}")
            return
        await asyncio.to_thread(STATE.switch_session, session_id)     # 丢线程执行（可能 IO）
        STREAM.fanout(                             # 广播切换事件
            {
                "type": "session_switched",        # 类型
                "agent": "Server",                 # 来源
                "data": {"session_id": session_id},# 数据
                "time": time.time(),               # 时间
                "seq": 0,                          # 系统事件不带序号
            }
        )
        return                                     # 结束

    if kind == "query":                            # 提问
        text = (message.get("text") or "").strip()
        if not text:
            await send_error(websocket, "问题不能为空")
            return                                 # 结束
        if STATE.busy:                             # 正在跑
            await send_error(websocket, "正在处理上一轮问题，请稍候")
            return
        # Agent 是同步阻塞的，放到线程里跑，避免卡住 event loop
        threading.Thread(
            target=run_query_safely, args=(text,), daemon=True  # 守护线程
        ).start()
        return

    if kind == "turn_elapsed":                     # 前端回报本轮耗时，落盘供重新加载时显示
        elapsed = message.get("elapsed")
        session_id = message.get("session_id", "")
        if isinstance(elapsed, (int, float)) and session_id == CONTEXT.session_id:
            await asyncio.to_thread(CONTEXT.set_last_turn_elapsed, float(elapsed))
        return

    await send_error(websocket, f"未知的消息类型：{kind}")


def run_query_safely(text: str) -> None:
    """后台线程里跑一轮问答，出错也要发事件通知前端。"""
    try:
        STATE.run_query(text)                      # 正常执行
    except Exception as exc:
        BUS.publish(                               # 发 error 事件
            "error",
            agent="Server",
            message=f"{type(exc).__name__}: {exc}", # 异常类型 + 信息
        )
        BUS.publish("turn_end", agent="Master", query=text, answer="")  # 再发 turn_end 收尾（耗时由前端算）


async def send_error(websocket: WebSocket, message: str) -> None:
    """给客户端回一条错误提示。"""
    await websocket.send_json(
        {
            "type": "error",
            "agent": "Server",
            "data": {"message": message},
            "time": time.time(),
            "seq": 0,                              # 系统事件不带序号
        }
    )
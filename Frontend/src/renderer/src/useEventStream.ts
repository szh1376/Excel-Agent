/**
 * WebSocket 连接：接收事件流并按帧批量交给 store。
 *
 * 三个关键点：
 * 1. 事件先进 buffer，用 requestAnimationFrame 整帧合并成一次 setState。
 *    实测一轮问答约 350 个 thinking_delta，逐个 setState 会把 React 打爆。
 * 2. 断线自动重连，重连后先用 /api/events?since_seq= 补齐断线期间的事件。
 * 3. 全应用共用一条连接，提问和切换会话都通过它发，不再临时建连接。
 */

import { useEffect } from 'react'

import { api, wsUrl } from './api'
import { useStore } from './store'
import type { AgentEvent } from './types/events'

// 全应用唯一的一条连接
let socket: WebSocket | null = null
let buffer: AgentEvent[] = []
let frame = 0
let closedByUs = false
let retry = 0
let retryTimer = 0
/** 连接就绪前先攒下来的待发消息 */
let pending: Record<string, unknown>[] = []

/**
 * 每帧最多处理多少条事件。
 *
 * 一轮问答实测能产生 500+ 条事件（大多是 thinking_delta）。一次性全塞给
 * store，会让这一帧的 setState + 重渲染变成几十毫秒的长任务，连续几轮
 * 就容易把渲染进程拖死。分批处理，代价只是慢几帧，但主线程一直是活的。
 */
const MAX_EVENTS_PER_FRAME = 120

/** 整帧刷新：把缓冲区里的事件分批交给 store。 */
function flush(): void {
  frame = 0
  if (buffer.length === 0) return

  // 溢出的话题留到下一轮继续处理
  if (buffer.length > MAX_EVENTS_PER_FRAME) {
    const batch = buffer.slice(0, MAX_EVENTS_PER_FRAME)
    buffer = buffer.slice(MAX_EVENTS_PER_FRAME)
    useStore.getState().applyEvents(batch)
    schedule()
    return
  }

  const batch = buffer
  buffer = []
  useStore.getState().applyEvents(batch)
}

/**
 * 安排下一轮刷新。
 *
 * 用 setTimeout 而不是 requestAnimationFrame：窗口不可见或被遮挡时
 * （Electron 窗口最小化、被别的窗口盖住）rAF 会完全停摆，事件就会一直
 * 堆在缓冲区里不再应用，界面看着像死了。setTimeout 虽然也被降频，
 * 但至少会继续跑。
 */
function schedule(): void {
  if (frame !== 0) return
  frame = window.setTimeout(flush, 0)
}

/** 本轮开始时间（前端计时用）。后端不再计算耗时。 */
let turnStartedAt = 0

/**
 * 把事件放进缓冲区，并安排刷新。
 *
 * 耗时统一由前端算：
 * - turn_start：记下开始时间，并塞进事件里给界面做实时计时；
 * - turn_end：算出本轮总耗时，塞进事件给界面显示，同时回报后端落盘
 *   —— 重新加载历史时才能显示同样的时间。
 */
function push(event: AgentEvent): void {
  if (event.type === 'turn_start') {
    turnStartedAt = Date.now()
    event = { ...event, data: { ...event.data, started_at: turnStartedAt } }
  } else if (event.type === 'turn_end') {
    const elapsed = turnStartedAt > 0 ? (Date.now() - turnStartedAt) / 1000 : 0
    turnStartedAt = 0
    event = { ...event, data: { ...event.data, elapsed } }
    send({ type: 'turn_elapsed', elapsed, session_id: useStore.getState().current })
  }
  buffer.push(event)
  schedule()
}

/** 建立连接（含断线重连与事件补齐）。 */
async function connect(): Promise<void> {
  // 已经有可用连接就别再建一条（React StrictMode 下 effect 会跑两次）
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return
  }

  // 重连时先补齐断线期间漏掉的事件
  const { lastSeq } = useStore.getState()
  if (lastSeq > 0) {
    try {
      const history = await api.events(lastSeq)
      history.events.forEach(push)
    } catch {
      // 后端可能刚重启，事件缓冲已清空，忽略
    }
  }

  socket = new WebSocket(wsUrl())

  socket.onopen = () => {
    retry = 0
    useStore.getState().setConnected(true)
    // 把连接建立前攒下的消息补发出去
    const queue = pending
    pending = []
    queue.forEach((payload) => socket?.send(JSON.stringify(payload)))
  }

  socket.onmessage = (message) => {
    try {
      push(JSON.parse(message.data as string) as AgentEvent)
    } catch {
      // 非法消息直接丢掉
    }
  }

  socket.onclose = () => {
    useStore.getState().setConnected(false)
    if (closedByUs) return
    // 退避重连：0.5s、1s、2s… 最多 5s
    retry += 1
    const delay = Math.min(500 * 2 ** (retry - 1), 5000)
    retryTimer = window.setTimeout(() => void connect(), delay)
  }

  socket.onerror = () => {
    // 交给 onclose 统一处理重连
  }
}

/** 关闭连接（仅组件卸载时调用）。 */
function disconnect(): void {
  closedByUs = true
  if (frame !== 0) window.clearTimeout(frame)
  if (retryTimer !== 0) window.clearTimeout(retryTimer)
  socket?.close()
  socket = null
}

/** 发一条消息给后端；连接还没就绪时先排队。 */
export function send(payload: Record<string, unknown>): void {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload))
  } else {
    pending.push(payload)
  }
}

/** 发送一个问题。 */
export function askQuery(text: string): void {
  send({ type: 'query', text })
}

/** 让后端把上下文切到指定会话。 */
export function tellBackendSession(sessionId: string): void {
  send({ type: 'switch_session', session_id: sessionId })
}

/** 组件挂载时建立连接。 */
export function useEventStream(): void {
  useEffect(() => {
    closedByUs = false
    void connect()
    return () => disconnect()
  }, [])
}

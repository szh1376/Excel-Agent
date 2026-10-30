/**
 * 后端接口客户端：REST 调用 + WebSocket 连接。
 *
 * 后端地址由 preload 注入；在浏览器里直接调试时退回 127.0.0.1:8765。
 */

import type { AgentEvent, BackendConfig, SessionInfo } from './types/events'

const FALLBACK_URL = 'http://127.0.0.1:8765'

/** 后端 HTTP 地址。 */
export function backendUrl(): string {
  return window.__EXCEL_AGENT__?.backendUrl ?? FALLBACK_URL
}

/** 后端 WebSocket 地址。 */
export function wsUrl(): string {
  return window.__EXCEL_AGENT__?.wsUrl ?? FALLBACK_URL.replace(/^http/, 'ws') + '/ws'
}

/**
 * 一张统计图的完整地址。
 *
 * 图片不走事件流（一张几百 KB，塞进 WebSocket 会拖慢流式输出），
 * 事件里只带文件名，这里拼成 URL 交给 <img> 按需加载。
 */
export function chartUrl(filename: string): string {
  return `${backendUrl()}/api/charts/${encodeURIComponent(filename)}`
}

/** 仓库里的一张图。 */
export interface ChartInfo {
  filename: string
  size: number
  updated_at: number
  updated_at_text: string
}

/** 统一的 JSON 请求。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(backendUrl() + path, {
    headers: { 'content-type': 'application/json' },
    ...init
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

export const api = {
  /** 健康检查（主进程用它等后端就绪，界面也用来判断连通性）。 */
  health: () => request<{ ok: boolean; busy: boolean; session_id: string }>('/api/health'),

  /** 前端需要的配置：模型名、Excel 文件列表。 */
  config: () => request<BackendConfig>('/api/config'),

  /** 会话列表。 */
  sessions: () => request<{ sessions: SessionInfo[]; current: string }>('/api/sessions'),

  /** 新建会话并切换过去。 */
  createSession: (title = '') =>
    request<{ session: SessionInfo }>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ title })
    }),

  /** 重命名会话。 */
  renameSession: (id: string, title: string) =>
    request<{ session: SessionInfo }>(`/api/sessions/${id}/rename`, {
      method: 'POST',
      body: JSON.stringify({ title })
    }),

  /** 删除会话。 */
  deleteSession: (id: string) =>
    request<{ deleted: string; current: string }>(`/api/sessions/${id}`, { method: 'DELETE' }),

  /** 切换当前会话。 */
  switchSession: (id: string) =>
    request<{ current: string }>(`/api/sessions/${id}/switch`, { method: 'POST' }),

  /** 某个会话的完整记录（含各 Agent 的思考与工具调用）。 */
  messages: (id: string) =>
    request<{ session_id: string; turns: RawTurn[] }>(`/api/sessions/${id}/messages`),

  /** 历史事件，用于断线重连补齐。 */
  events: (sinceSeq: number) =>
    request<{ events: AgentEvent[] }>(`/api/events?since_seq=${sinceSeq}`)
}

/** 落盘里的一个子任务（Master 的拆解计划）。 */
export interface RawPlannedTask {
  task_id: string
  query: string
  intent: string
  tables?: string[]
  missing_tables?: string[]
  status?: string
}

/** 落盘里的一轮对话（后端 ContextManager 的形状）。 */
export interface RawTurn {
  query: string
  answer: string
  agents?: RawAgentRecord[]
  /** 本轮的拆解计划（还原「任务拆分与分配」用） */
  tasks?: RawPlannedTask[]
  /** Master 的过程说明（还原步骤列表用） */
  steps?: string[]
  /** 本轮耗时（秒，前端算好后回报给后端保存的） */
  elapsed?: number
}

/** 落盘里的一个 Agent 记录。 */
export interface RawAgentRecord {
  agent: string
  /** 该记录属于哪个子任务（并行工作流） */
  task_id?: string
  decision?: string
  model_calls?: { thinking?: string; text?: string; tools?: string[] }[]
  tool_calls?: { name: string; input?: Record<string, unknown>; output?: string }[]
  /** 本 Agent 生成的统计图（由 Master 落盘） */
  charts?: { filename: string; title?: string; chart_type?: string }[]
  /** 本 Agent 的过程说明（由 Master 落盘） */
  steps?: string[]
}

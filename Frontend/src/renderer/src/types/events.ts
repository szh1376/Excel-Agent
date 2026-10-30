/**
 * 事件协议：与 Python 侧 core/events.py 的 EVENT_TYPES 一一对应。
 *
 * 后端发来的每条消息都是这个形状：
 *   { type, agent, task_id, data, time, seq }
 * seq 是后端全局自增序号，用来排序和断线重连补齐；
 * task_id 是子任务 id —— 并行工作流下同一个 Agent 可能同时跑多个子任务。
 */

/** 所有事件类型（与 core/events.py 保持一致）。 */
export type EventType =
  | 'session_loaded'
  | 'turn_start'
  | 'turn_end'
  // 计划 / 子任务
  | 'plan'
  | 'task_start'
  | 'task_end'
  | 'workflow_step'
  | 'agent_start'
  | 'agent_end'
  | 'clue'
  | 'agent_message'
  | 'stream_start'
  | 'thinking_delta'
  | 'thinking_done'
  | 'answer_delta'
  | 'stream_end'
  | 'tool_use'
  | 'tool_call'
  | 'tool_result'
  // 图表
  | 'chart_saved'
  // 以下由后端服务层发出，不属于 Agent 事件
  | 'hello'
  | 'pong'
  | 'error'
  | 'session_switched'

/** Master 拆解出的一个子任务。 */
export interface PlannedTask {
  task_id: string
  query: string
  intent: string
  tables?: string[]
  /** 模型声明还缺哪些表（缺表时整轮不执行，只向用户要表） */
  missing_tables?: string[]
  status?: string
}

/** 事件负载。字段按类型可选，读取时注意兜底。 */
export interface EventData {
  // 会话 / 轮次
  session_id?: string
  turns?: number
  query?: string
  answer?: string
  elapsed?: number
  /** 本轮开始时间（前端在 turn_start 上补的，用于实时计时） */
  started_at?: number
  busy?: boolean
  last_seq?: number

  // 计划 / 子任务
  tasks?: PlannedTask[]
  task_id?: string
  intent?: string
  tables?: string[]
  status?: string

  // 工作流
  step?: string

  // 模型流
  call_id?: number
  index?: number
  text?: string
  label?: string
  model?: string
  max_tokens?: number
  stop_reason?: string
  truncated?: boolean

  // 工具
  tool?: string
  input?: Record<string, unknown>
  output?: string
  tool_use_id?: string

  // Preview 交给 Loader 的结构化线索（clue）
  clue?: Record<string, unknown>

  // 图表（由 save_chart 工具发出）
  filename?: string
  title?: string
  chart_type?: string

  // 错误
  message?: string
}

/** 一条事件。 */
export interface AgentEvent {
  type: EventType
  agent: string
  /** 所属子任务 id（Master / 服务层事件可能为空串） */
  task_id?: string
  data: EventData
  time: number
  seq: number
}

/** 会话清单里的一条（对应后端 SessionEntry）。 */
export interface SessionInfo {
  id: string
  title: string
  created_at: number
  updated_at: number
  updated_at_text: string
  turns: number
  excel_file: string
  renamed: boolean
}

/** 后端 /api/config 的返回。 */
export interface BackendConfig {
  model: string
  session_id: string
  excel_files: string[]
}

/** 类别判断：这些事件属于「模型流」，用于时间线聚合。 */
export const STREAM_EVENTS: ReadonlySet<EventType> = new Set<EventType>([
  'stream_start',
  'thinking_delta',
  'thinking_done',
  'answer_delta',
  'stream_end'
])

/** 类别判断：这些事件是工作流推进说明。 */
export const WORKFLOW_EVENTS: ReadonlySet<EventType> = new Set<EventType>([
  'plan',
  'task_start',
  'task_end',
  'workflow_step',
  'agent_start',
  'agent_end',
  'tool_use',
  'tool_call',
  'tool_result',
  'chart_saved'
])

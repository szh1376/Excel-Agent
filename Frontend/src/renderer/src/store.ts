/**
 * 全局状态。
 *
 * 三条设计原则：
 * 1. Agent 进程才是状态的真相，界面只做渲染，不自造业务状态。
 * 2. 事件量大（一轮问答约 350 个 thinking_delta），所以事件先进缓冲区，
 *    再用 requestAnimationFrame 整帧合并成一次 setState，避免每个 delta 都触发渲染。
 * 3. seq 用来去重：重连补齐时可能拿到已经处理过的事件。
 */

import { create } from 'zustand'

import { api } from './api'
import type { AgentEvent, BackendConfig, SessionInfo } from './types/events'

/** 一次工具调用（含结果）。 */
export interface ToolCall {
  name: string
  input: Record<string, unknown>
  output: string
  elapsed?: number
  status: 'running' | 'done'
}

/**
 * Agent 活动流水线上的一条记录。
 *
 * 关键点：思考与工具调用必须**按真实先后顺序交错存放**。
 * 模型一轮里通常是「先想 → 调工具 → 拿到结果 → 再想 → 再调工具」，
 * 如果按类型各自合并成一块，先后顺序就丢了。
 */
export type TimelineEntry =
  | { kind: 'reasoning'; text: string; status: 'running' | 'done' }
  | ({ kind: 'tool' } & ToolCall)

/** 一张统计图（对应 save_chart 工具的一次调用）。 */
export interface ChartRef {
  filename: string
  title: string
  chartType: string
}

/** 某个 Agent 在本轮里的活动记录。 */
export interface AgentActivity {
  agent: string
  /** 所属子任务 id（并行时同一个 Agent 可能跑多个子任务；Master 自身为空串） */
  taskId: string
  /** 事件首次出现的时间 */
  startedAt: number
  finished: boolean
  /** 思考与工具调用按真实顺序交错存放 —— 这是界面要展示的主体 */
  timeline: TimelineEntry[]
  /** 模型输出的回答文本（流式累积） */
  answer: string
  /** 本 Agent 的模型调用次数 */
  modelCalls: number
  /** 本 Agent 生成的统计图 */
  charts: ChartRef[]
  /** 挂在这张卡片下面的过程说明（workflow_step，按 Agent + 子任务归属） */
  steps: string[]
}

/** 取一个 Agent 用过的工具名（按出现顺序，用于折叠摘要）。 */
export function toolNames(activity: AgentActivity): string[] {
  return activity.timeline.filter((item) => item.kind === 'tool').map((item) => item.name)
}

/** 取一个 Agent 的全部思考文本（用于折叠摘要 / 逐字节还原）。 */
export function thinkingText(activity: AgentActivity): string {
  return activity.timeline
    .filter((item): item is Extract<TimelineEntry, { kind: 'reasoning' }> => item.kind === 'reasoning')
    .map((item) => item.text)
    .join('\n\n')
}

/** 一轮里所有 Agent 用过的工具名（按出现顺序）。 */
export function turnTools(turn: Turn): string[] {
  return turn.agents.flatMap((activity) => toolNames(activity))
}

/** Master 拆解出的一个子任务（界面展示用）。 */
export interface TurnTask {
  id: string
  query: string
  intent: string
  tables: string[]
  /** 还缺哪些表（缺表时整轮不执行，只向用户要表） */
  missingTables: string[]
  status: 'pending' | 'running' | 'done' | 'blocked'
  answer?: string
}

/** 一轮问答。 */
export interface Turn {
  id: string
  query: string
  answer: string
  /** 本轮开始时间（前端计时用；后端不再计算耗时） */
  startedAt: number
  /** 本轮参与的 Agent，按出现顺序 */
  agents: AgentActivity[]
  /** Master 拆解出的子任务，按拆解顺序 */
  tasks: TurnTask[]
  elapsed?: number
  done: boolean
  /** 工作流推进说明，按时间顺序 */
  steps: string[]
}

interface State {
  // 连接
  connected: boolean
  lastSeq: number

  // 配置
  config: BackendConfig | null

  // 会话
  sessions: SessionInfo[]
  current: string
  messages: Turn[]

  // 当前进行中的一轮（turn_end 后并入 messages）
  live: Turn | null
  busy: boolean

  // 错误提示
  error: string

  // ---- 动作 ----
  setConnected: (connected: boolean) => void
  setError: (error: string) => void
  applyEvents: (events: AgentEvent[]) => void
  setConfig: (config: BackendConfig) => void
  setSessions: (sessions: SessionInfo[], current: string) => void
  setMessages: (turns: Turn[]) => void
  setCurrent: (sessionId: string) => void
  resetForSession: (sessionId: string) => void
}

let turnCounter = 0

/** 找出某个 Agent 的活动记录，没有就新建一条。 */
function ensureActivity(turn: Turn, agent: string, taskId: string, time: number): AgentActivity {
  const name = agent || 'Master'
  let activity = turn.agents.find((item) => item.agent === name && item.taskId === taskId)
  if (!activity) {
    activity = {
      agent: name,
      taskId,
      startedAt: time,
      finished: false,
      timeline: [],
      answer: '',
      modelCalls: 0,
      charts: [],
      steps: []
    }
    turn.agents.push(activity)
  }
  return activity
}

/** 取当前「还开着」的推理段；没有就新开一段。 */
function openReasoning(activity: AgentActivity): Extract<TimelineEntry, { kind: 'reasoning' }> {
  const last = activity.timeline[activity.timeline.length - 1]
  if (last && last.kind === 'reasoning' && last.status === 'running') {
    return last
  }
  const entry: Extract<TimelineEntry, { kind: 'reasoning' }> = {
    kind: 'reasoning',
    text: '',
    status: 'running'
  }
  activity.timeline = [...activity.timeline, entry]
  return entry
}

/** 收尾：把还开着的推理段标记为结束（一次模型调用结束时调用）。 */
function closeReasoning(activity: AgentActivity): void {
  activity.timeline = activity.timeline.map((item) =>
    item.kind === 'reasoning' && item.status === 'running'
      ? { ...item, status: 'done' as const }
      : item
  )
}

/** 把一条事件应用到某一轮上。事件监听器里的核心逻辑。 */
function applyEvent(turn: Turn | null, event: AgentEvent): Turn | null {
  const { type, agent, data } = event

  if (type === 'turn_start') {
    turnCounter += 1
    return {
      id: `turn-${turnCounter}`,
      query: data.query ?? '',
      answer: '',
      startedAt: data.started_at ?? Date.now(),
      agents: [],
      tasks: [],
      done: false,
      steps: []
    }
  }

  if (!turn) return null

  switch (type) {
    // Master 的拆解结果：本轮有哪些子任务
    case 'plan': {
      turn.tasks = (data.tasks ?? []).map((task) => ({
        id: task.task_id,
        query: task.query ?? '',
        intent: task.intent ?? '',
        tables: task.tables ?? [],
        missingTables: task.missing_tables ?? [],
        status: 'pending' as const
      }))
      return { ...turn }
    }

    // 子任务开始 / 结束（task_id 在事件顶层）
    case 'task_start': {
      const taskId = event.task_id ?? ''
      turn.tasks = turn.tasks.map((task) =>
        task.id === taskId ? { ...task, status: 'running' as const } : task
      )
      return { ...turn }
    }

    case 'task_end': {
      const taskId = event.task_id ?? ''
      // 后端可能用 status 标记 blocked（缺表搁置），默认视为 done
      const status = (data.status as TurnTask['status']) ?? 'done'
      turn.tasks = turn.tasks.map((task) =>
        task.id === taskId ? { ...task, status, answer: data.answer ?? '' } : task
      )
      return { ...turn }
    }

    // 过程说明：带 task_id 的挂到那个子任务的 Agent 卡片下，Master 自己的留在本轮步骤里
    case 'workflow_step': {
      const step = data.step ?? ''
      const taskId = event.task_id ?? ''
      if (taskId) {
        const activity = ensureActivity(turn, agent, taskId, event.time)
        activity.steps = [...activity.steps, step]
        return { ...turn, agents: [...turn.agents] }
      }
      turn.steps = [...turn.steps, step]
      return { ...turn }
    }

    case 'agent_start':
      ensureActivity(turn, agent, event.task_id ?? '', event.time)
      return { ...turn, agents: [...turn.agents] }

    case 'agent_end': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      activity.finished = true
      closeReasoning(activity)
      return { ...turn, agents: [...turn.agents] }
    }

    case 'stream_start': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      activity.modelCalls += 1
      return { ...turn, agents: [...turn.agents] }
    }

    case 'thinking_delta': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      const entry = openReasoning(activity)
      entry.text += data.text ?? ''
      return { ...turn, agents: [...turn.agents] }
    }

    case 'stream_end': {
      // 一次模型调用结束：把这一段思考封口，保证下一段思考单独成条
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      closeReasoning(activity)
      return { ...turn, agents: [...turn.agents] }
    }

    case 'answer_delta': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      activity.answer += data.text ?? ''
      return { ...turn, agents: [...turn.agents] }
    }

    case 'tool_call': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      // 工具调用前先把当前思考收尾，顺序才不会错位
      closeReasoning(activity)
      activity.timeline = [
        ...activity.timeline,
        {
          kind: 'tool',
          name: data.tool ?? '',
          input: data.input ?? {},
          output: '',
          status: 'running'
        }
      ]
      return { ...turn, agents: [...turn.agents] }
    }

    case 'tool_result': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      // 回填到最近一个同名且未完成的调用上
      const list = [...activity.timeline]
      for (let i = list.length - 1; i >= 0; i -= 1) {
        const item = list[i]
        if (item.kind === 'tool' && item.name === data.tool && item.status === 'running') {
          list[i] = {
            ...item,
            output: data.output ?? '',
            elapsed: data.elapsed,
            status: 'done'
          }
          break
        }
      }
      activity.timeline = list
      return { ...turn, agents: [...turn.agents] }
    }

    case 'chart_saved': {
      const activity = ensureActivity(turn, agent, event.task_id ?? '', event.time)
      const filename = data.filename ?? ''
      if (filename && !activity.charts.some((item) => item.filename === filename)) {
        activity.charts = [
          ...activity.charts,
          {
            filename,
            title: data.title ?? '统计图',
            chartType: data.chart_type ?? ''
          }
        ]
      }
      return { ...turn, agents: [...turn.agents] }
    }

    case 'turn_end': {
      // 本轮结束：所有 Agent 一律收尾。
      // 有些活动卡片拿不到 agent_end，只靠它收尾的话会永远停在「运行中」。
      const agents = turn.agents.map((activity) =>
        activity.finished
          ? activity
          : {
              ...activity,
              finished: true,
              timeline: activity.timeline.map((item) =>
                item.kind === 'reasoning' && item.status === 'running'
                  ? { ...item, status: 'done' as const }
                  : item.kind === 'tool' && item.status === 'running'
                    ? { ...item, status: 'done' as const }
                    : item
              )
            }
      )
      const tasks = turn.tasks.map((task) =>
        task.status === 'done' || task.status === 'blocked'
          ? task
          : { ...task, status: 'done' as const }
      )
      return {
        ...turn,
        agents,
        tasks,
        answer: data.answer ?? '',
        elapsed: data.elapsed,
        done: true
      }
    }

    default:
      return turn
  }
}

/**
 * 后端拒绝了这次提问（例如「正在处理上一轮问题」）时调用。
 *
 * 前端是乐观启动的：一发消息就先建一条「进行中」的轮次。
 * 如果后端根本没跑起来，这条轮次就没有任何事件来收尾，
 * 会永远停在「正在运行」。这里按后端真实状态决定收尾还是丢弃。
 */
export function discardOrphanTurn(backendBusy: boolean): void {
  useStore.setState((state) => {
    if (!state.live || state.live.done) return {}
    // 后端真的在跑：保留这条轮次，等它的事件
    if (backendBusy) return {}
    // 后端没在跑：这条是空轮次，直接扔掉
    if (state.live.agents.length === 0) {
      return { live: null, busy: false }
    }
    // 已经有部分内容：收尾后归入历史，避免丢内容
    return {
      live: null,
      busy: false,
      messages: [...state.messages, { ...state.live, done: true }]
    }
  })
}

export const useStore = create<State>((set, get) => ({
  connected: false,
  lastSeq: 0,
  config: null,
  sessions: [],
  current: '',
  messages: [],
  live: null,
  busy: false,
  error: '',

  setConnected: (connected) => set({ connected }),
  setError: (error) => set({ error }),

  /** 整帧合并：一批事件只触发一次渲染。 */
  applyEvents: (events) => {
    const state = get()
    let live = state.live
    let messages = state.messages
    let lastSeq = state.lastSeq
    let busy = state.busy
    let error = state.error
    let needsRecovery = false

    for (const event of events) {
      // 去重：重连补齐时可能重复收到
      if (event.seq > 0 && event.seq <= lastSeq) continue
      if (event.seq > 0) lastSeq = event.seq

      if (event.type === 'hello') {
        // 后端进程重启后，它的事件序号会从 0 重新开始，
        // 而本地 lastSeq 还停在旧的大数值上 —— 那样之后每条事件
        // 都会被上面的去重逻辑当成「重复」丢掉。按后端进度校正一次。
        const serverSeq = event.data.last_seq
        if (typeof serverSeq === 'number' && lastSeq > serverSeq) {
          lastSeq = serverSeq
        }

        // 后端说它没在跑，而本地还挂着一轮没结束 —— 说明 turn_end 丢了
        // （断线、或事件被去重误丢）。把它收尾并归入历史，否则界面会一直
        // 停在「正在运行」。
        if (!event.data.busy && live && !live.done) {
          messages = [
            ...messages,
            {
              ...live,
              done: true,
              agents: live.agents.map((activity) =>
                activity.finished ? activity : { ...activity, finished: true }
              )
            }
          ]
          live = null
        }

        busy = Boolean(event.data.busy)
        continue
      }
      if (event.type === 'error') {
        error = event.data.message ?? '未知错误'
        // 单独处理：这类错误意味着本轮可能压根没跑起来
        needsRecovery = true
        continue
      }
      if (event.type === 'session_loaded') continue

      live = applyEvent(live, event)

      if (event.type === 'turn_start') {
        busy = true
      } else if (event.type === 'turn_end') {
        busy = false
        // 本轮结束：并入历史，清空进行中的一轮
        if (live) messages = [...messages, live]
        live = null
      }
    }

    set({ live, messages, lastSeq, busy, error })

    // 收到 error 后，按后端真实状态决定那条乐观轮次怎么处理
    if (needsRecovery) {
      void api
        .health()
        .then((health) => discardOrphanTurn(health.busy))
        .catch(() => undefined)
    }
  },

  setConfig: (config) => set({ config }),
  setSessions: (sessions, current) => set({ sessions, current }),
  setMessages: (turns) => set({ messages: turns, live: null }),
  setCurrent: (sessionId) => set({ current: sessionId }),

  resetForSession: (sessionId) =>
    set({ current: sessionId, messages: [], live: null, error: '' })
}))

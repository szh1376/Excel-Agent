import { useCallback, useEffect, useState } from 'react'

import { api, type RawAgentRecord, type RawTurn } from './api'
import { ChatPanel } from './components/ChatPanel'
import { SessionList } from './components/SessionList'
import { useStore, type AgentActivity, type TimelineEntry, type Turn } from './store'
import { askQuery, tellBackendSession, useEventStream } from './useEventStream'

/**
 * 把落盘的记录还原成界面用的 Turn（画历史记录用）。
 *
 * 落盘时思考与工具调用是分开存的两个数组，直接拼起来会丢掉先后顺序。
 * 好在每条 model_call 里记着「这次调用用了哪些工具」，
 * 于是可以按「第 N 次思考 → 它调用的工具 → 第 N+1 次思考」重新交织回去。
 */
function rawToTimeline(record: RawAgentRecord): TimelineEntry[] {
  const calls = record.model_calls ?? []
  const pool = (record.tool_calls ?? []).map((call) => ({
    kind: 'tool' as const,
    name: call.name,
    input: call.input ?? {},
    output: String(call.output ?? ''),
    status: 'done' as const
  }))

  const timeline: TimelineEntry[] = []
  let cursor = 0

  calls.forEach((call) => {
    const thinking = (call.thinking ?? '').trim()
    if (thinking) {
      timeline.push({ kind: 'reasoning', text: thinking, status: 'done' })
    }
    // 这次调用点名了哪些工具，就按顺序取出来放进流水线
    ;(call.tools ?? []).forEach((name) => {
      const at = pool.findIndex((item, i) => i >= cursor && item.name === name)
      if (at >= 0) {
        timeline.push(pool[at])
        cursor = at + 1
      }
    })
  })

  // 兜底：没被任何 model_call 点名的工具，追加到末尾
  pool.slice(cursor).forEach((item) => timeline.push(item))

  return timeline
}

/** 把落盘的记录还原成界面用的 Turn（画历史记录用）。 */
function rawToTurn(raw: RawTurn, index: number): Turn {
  const agents: AgentActivity[] = (raw.agents ?? []).map((record) => {
    const calls = record.model_calls ?? []
    return {
      agent: record.agent,
      taskId: record.task_id ?? '',
      startedAt: 0,
      finished: true,
      timeline: rawToTimeline(record),
      answer: calls.map((call) => call.text ?? '').filter(Boolean).join('\n\n'),
      modelCalls: calls.length,
      // 图表记录跟着 Agent 记录一起落盘，所以刷新界面后依然能显示
      charts: (record.charts ?? [])
        .filter((chart) => chart.filename)
        .map((chart) => ({
          filename: chart.filename,
          title: chart.title || '统计图',
          chartType: chart.chart_type ?? ''
        })),
      // 过程说明跟着 Agent 记录一起落盘
      steps: record.steps ?? []
    }
  })

  return {
    id: `history-${index}`,
    query: raw.query,
    answer: raw.answer,
    // 历史轮次不再计时，直接用落盘的耗时
    startedAt: 0,
    elapsed: raw.elapsed,
    agents,
    // 拆解计划也落盘了，历史轮次能还原出与运行结束时一致的界面
    tasks: (raw.tasks ?? []).map((task) => ({
      id: task.task_id,
      query: task.query ?? '',
      intent: task.intent ?? '',
      tables: task.tables ?? [],
      missingTables: task.missing_tables ?? [],
      status: 'done' as const
    })),
    done: true,
    steps: raw.steps ?? []
  }
}

export function App(): React.JSX.Element {
  useEventStream()

  const connected = useStore((state) => state.connected)
  const config = useStore((state) => state.config)
  const sessions = useStore((state) => state.sessions)
  const current = useStore((state) => state.current)
  const busy = useStore((state) => state.busy)
  const error = useStore((state) => state.error)

  const [ready, setReady] = useState(false)

  /** 拉一次会话列表 + 配置。 */
  const refresh = useCallback(async (): Promise<void> => {
    const [cfg, list] = await Promise.all([api.config(), api.sessions()])
    useStore.getState().setConfig(cfg)
    useStore.getState().setSessions(list.sessions, list.current)
  }, [])

  /** 切到某个会话并载入它的历史记录。 */
  const openSession = useCallback(async (sessionId: string): Promise<void> => {
    const data = await api.messages(sessionId)
    useStore.getState().resetForSession(sessionId)
    useStore.getState().setMessages(data.turns.map(rawToTurn))
  }, [])

  // 启动：拉配置与会话，并把当前会话的历史载入
  useEffect(() => {
    void (async () => {
      try {
        await refresh()
        const currentId = useStore.getState().current
        if (currentId) await openSession(currentId)
      } catch (err) {
        useStore.getState().setError(err instanceof Error ? err.message : String(err))
      } finally {
        setReady(true)
      }
    })()
  }, [refresh, openSession])

  const handleAsk = useCallback(
    (text: string): void => {
      useStore.getState().setError('')
      askQuery(text)
    },
    []
  )

  const handleCreate = useCallback(async (): Promise<void> => {
    const { session } = await api.createSession()
    await refresh()
    await openSession(session.id)
    tellBackendSession(session.id)
  }, [refresh, openSession])

  const handleSwitch = useCallback(
    async (sessionId: string): Promise<void> => {
      if (sessionId === current) return
      await api.switchSession(sessionId)
      await refresh()
      await openSession(sessionId)
      tellBackendSession(sessionId)
    },
    [current, refresh, openSession]
  )

  const handleRename = useCallback(
    async (sessionId: string, title: string): Promise<void> => {
      await api.renameSession(sessionId, title)
      await refresh()
    },
    [refresh]
  )

  const handleDelete = useCallback(
    async (sessionId: string): Promise<void> => {
      const result = await api.deleteSession(sessionId)
      await refresh()
      await openSession(result.current)
      tellBackendSession(result.current)
    },
    [refresh, openSession]
  )

  return (
    <div className="app">
      <header className="titlebar">
        <span className="brand">ExcelAgent</span>
        {config && <span className="meta">模型 {config.model}</span>}
        <span className="spacer" />
        {busy && <span className="pill busy">执行中</span>}
        <span className={`pill ${connected ? 'ok' : 'bad'}`}>
          {connected ? '已连接' : '未连接'}
        </span>
      </header>

      {error && (
        <div className="error-bar" onClick={() => useStore.getState().setError('')}>
          {error}
          <span className="error-close">×</span>
        </div>
      )}

      <main className="layout">
        <SessionList
          sessions={sessions}
          current={current}
          disabled={busy}
          onCreate={handleCreate}
          onSwitch={handleSwitch}
          onRename={handleRename}
          onDelete={handleDelete}
        />
        <ChatPanel ready={ready} busy={busy} onAsk={handleAsk} />
      </main>
    </div>
  )
}

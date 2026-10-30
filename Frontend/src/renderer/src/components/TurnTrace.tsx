/**
 * 一轮问答的执行过程。
 *
 * 执行中的布局（从上到下）：
 *   Master 思考 · 拆解问题            ← 与末尾的「整理结果」同款卡片
 *   Master 任务拆分与分配
 *     第 1 个 [意图] 子问题 … 状态     ← 整行可点，折叠 / 展开它自己的执行过程
 *        └ 该任务的 Agent 卡片（默认折叠，只看执行状态）
 *     第 2 个 …
 *   Master · 整理结果
 *
 * 最终结果完成后，最外层「执行过程」自动折叠，标题后显示用时与工具次数。
 * 这块内容显示在「用户问题」与「最终答案」之间。
 */

import { memo, useEffect, useState } from 'react'

import { turnTools, type AgentActivity, type TimelineEntry, type Turn, type TurnTask } from '../store'

/**
 * 运行中每隔一秒返回一次当前时间，用于在「执行过程」后面实时显示已执行多久。
 * 不运行时不动，避免无谓的渲染。
 */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  return now
}
import { ChartGallery } from './ChartGallery'

/** Master 拆解 / 汇总阶段的伪 taskId（与后端 master_agent 保持一致）。 */
const TASK_PLAN = 'plan'
const TASK_SUMMARY = 'summary'

/** 子任务意图的中文标签。 */
const INTENT_LABELS: Record<string, string> = {
  answer: '直答',
  excel_direct: '读表直答',
  excel_workflow: '工作流'
}

/** 子任务状态的中文标签。 */
const STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '执行中',
  done: '已完成',
  blocked: '等待补表'
}

function taskStatusText(status: string): string {
  return STATUS_LABELS[status] ?? status
}

/** 命令入参压成一行 JSON，过长则截断。 */
const MAX_COMMAND_CHARS = 160

function commandText(input: Record<string, unknown>): string {
  const text = JSON.stringify(input)
  const body = !text || text === '{}' ? '{}' : text
  return body.length > MAX_COMMAND_CHARS ? `${body.slice(0, MAX_COMMAND_CHARS)}…` : body
}

/** 工具结果预览：这里再截断一次，完整内容由后端事件流本身限制。 */
const MAX_RESULT_CHARS = 4000

/**
 * 单段思考内容的渲染上限。
 *
 * 实测一段思考可以到几十 KB。渲染时每帧都要为它建文本节点，是流式过程中
 * 最重的开销之一。超出部分不渲染（完整内容仍会落盘在 sessions/ 里）。
 */
const MAX_THINKING_CHARS = 20000

function thinkingText(text: string): string {
  if (text.length <= MAX_THINKING_CHARS) return text
  return `${text.slice(0, MAX_THINKING_CHARS)}\n\n…（思考内容过长，已省略后续 ${text.length - MAX_THINKING_CHARS} 字）`
}

function resultText(output: string): string {
  const text = output.trim()
  if (text.length <= MAX_RESULT_CHARS) return text
  return `${text.slice(0, MAX_RESULT_CHARS)}\n\n…（结果过长，已截断，共 ${text.length} 字）`
}

/**
 * 单条工具命令。
 *
 * 默认只显示「工具名 + 截断后的命令」，点一下才展开
 * 完整命令 + 截断后的结果。
 */
function ToolCommand({
  name,
  input,
  output,
  elapsed,
  status
}: {
  name: string
  input: Record<string, unknown>
  output: string
  elapsed?: number
  status: 'running' | 'done'
}): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const hasResult = output.trim().length > 0

  return (
    <div className={`tool-command${status === 'running' ? ' running' : ''}`}>
      <button className="tool-command-head" onClick={() => setOpen((v) => !v)}>
        <span className="caret">{open ? '▾' : '▸'}</span>
        {/* 与 Agent 名字同色的状态圈 */}
        <span className={`status-dot small${status === 'running' ? ' busy' : ''}`} />
        <span className="tool-name">{name}</span>
        <span className="tool-args">{commandText(input)}</span>
        {status === 'running' ? (
          <span className="tool-status">正在执行中……</span>
        ) : (
          elapsed !== undefined && <span className="tool-status">{elapsed.toFixed(2)}s</span>
        )}
      </button>

      {open && (
        <div className="tool-detail">
          <div className="tool-detail-label">完整命令</div>
          <pre className="tool-command-full">{JSON.stringify(input, null, 2)}</pre>
          {hasResult && (
            <>
              <div className="tool-detail-label">执行结果（已截断）</div>
              <pre className="tool-result">{resultText(output)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * 把流水线切成「思考段 + 它调用的一批工具」。
 *
 * 模型一次调用里的思考是连续的 thinking_delta，紧跟着的 tool_call 就是
 * 这次思考决定要用的工具。所以按「一段思考 + 紧跟其后的工具」成组，
 * 就能还原成：
 *     思考A → 执行工具（2 个）→ 思考B → 执行工具（1 个）→ …
 * 模型没写思考就直接调工具时，会出现只有工具、没有思考的块。
 */
interface Segment {
  reasoning: string
  tools: Extract<TimelineEntry, { kind: 'tool' }>[]
}

function buildSegments(timeline: TimelineEntry[]): Segment[] {
  const segments: Segment[] = []

  timeline.forEach((item) => {
    if (item.kind === 'reasoning') {
      // 已经有过思考、且还没挂工具 —— 说明这是新的一段思考，另起一块
      const last = segments[segments.length - 1]
      if (!last || last.tools.length > 0) {
        segments.push({ reasoning: item.text, tools: [] })
      } else {
        last.reasoning += item.text
      }
      return
    }

    // 工具归到它前面那段思考名下
    const last = segments[segments.length - 1]
    if (!last) segments.push({ reasoning: '', tools: [item] })
    else last.tools = [...last.tools, item]
  })

  return segments
}

/**
 * 一批工具命令：默认收起成一行浅色汇总。
 *
 * 每批对应它前面那段思考：这一批还没跑完时显示「正在执行工具中……」，
 * 跑完就变成「执行工具 N 个」。
 */
function ToolGroup({
  tools,
  running
}: {
  tools: Extract<TimelineEntry, { kind: 'tool' }>[]
  /** 所在 Agent 是否仍在运行（兜底用，不直接决定本批状态） */
  running: boolean
}): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const busy = running && tools.some((tool) => tool.status === 'running')

  return (
    <div className="tool-group">
      <button className="tool-group-head" onClick={() => setOpen((v) => !v)}>
        <span className={`status-dot${busy ? ' busy' : ''}`} />
        <span className="tool-group-label">
          {busy ? '正在执行工具中……' : `执行工具 ${tools.length} 个`}
        </span>
        <span className="tool-group-names">{tools.map((tool) => tool.name).join('、')}</span>
        <span className="caret">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="tool-command-list">
          {tools.map((tool, index) => (
            <ToolCommand
              key={`${tool.name}-${index}`}
              name={tool.name}
              input={tool.input}
              output={tool.output}
              elapsed={tool.elapsed}
              status={tool.status}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** 工作流推进说明（Master 的拆解 / 路由、Preview 复用线索等）。 */
function StepList({ steps }: { steps: string[] }): React.JSX.Element | null {
  if (steps.length === 0) return null
  return (
    <ul className="step-list">
      {steps.map((step, index) => (
        <li key={index}>{step}</li>
      ))}
    </ul>
  )
}

/**
 * 一个 Agent 的活动。
 *
 * 按「思考 → 它调用的一批工具」分块展示。
 * **默认折叠**：运行过程中只看执行状态（名字 / 运行中 / 耗时 / 调用与工具次数），
 * 点开才看它想了什么、执行过哪些工具。
 */
function AgentCard({
  activity,
  title,
  steps
}: {
  activity: AgentActivity
  /** 卡片标题（默认用 Agent 名字） */
  title?: string
  /** 卡片自己的过程说明（跟着卡片一起折叠） */
  steps?: string[]
}): React.JSX.Element {
  const running = !activity.finished
  const [open, setOpen] = useState(false)

  const segments = buildSegments(activity.timeline)
  const toolCount = activity.timeline.filter((item) => item.kind === 'tool').length

  return (
    <div className={`agent-card${activity.finished ? ' done' : ' running'}`}>
      <button className="agent-head" onClick={() => setOpen((v) => !v)}>
        <span className="caret">{open ? '▾' : '▸'}</span>
        <span className={`agent-dot${running ? ' pulse' : ''}`} />
        <span className="agent-name">{title ?? activity.agent}</span>
        {/* 子 Agent 不展示运行时间，只展示执行状态 */}
        {running && <span className="agent-time">运行中…</span>}
        <span className="spacer" />
        <span className="agent-stat">
          {activity.modelCalls} 次调用
          {toolCount > 0 ? ` · ${toolCount} 次工具` : ''}
        </span>
      </button>

      {open && (
        <div className="agent-body">
          <div className="agent-timeline">
            {segments.map((segment, index) => (
              <div className="segment" key={`seg-${index}`}>
                {segment.reasoning.trim() && (
                  <div className="reasoning">{thinkingText(segment.reasoning)}</div>
                )}
                {segment.tools.length > 0 && (
                  <ToolGroup tools={segment.tools} running={running} />
                )}
              </div>
            ))}
          </div>

          <ChartGallery charts={activity.charts} compact />

          {/* 过程说明属于卡片内容：卡片折叠时一起收起 */}
          <StepList steps={steps ?? []} />
        </div>
      )}
    </div>
  )
}

/**
 * 任务拆分里的一个子任务：点标题行即可折叠 / 展开它自己的执行过程。
 *
 * - 任务还在跑或等待时默认展开，收尾后自动折叠（用户手动点过之后就按用户的）
 * - Master 直答的子任务不派发给 Agent，没有执行过程，只有那一行
 */
const TaskItem = memo(function TaskItem({
  task,
  index,
  agents
}: {
  task: TurnTask
  index: number
  agents: AgentActivity[]
}): React.JSX.Element {
  const expandable = task.intent !== 'answer'
  const settled = task.status === 'done' || task.status === 'blocked'
  const [open, setOpen] = useState(!settled)

  useEffect(() => {
    setOpen(!settled)
  }, [settled])

  return (
    <li className={`plan-item ${task.status}`}>
      <button
        className="plan-item-row"
        onClick={() => expandable && setOpen((v) => !v)}
        disabled={!expandable}
      >
        <span className="caret">{expandable ? (open ? '▾' : '▸') : ''}</span>
        <span className={`task-dot ${task.status}`} />
        <span className="plan-index">第{index + 1}个</span>
        <span className="task-intent">{INTENT_LABELS[task.intent] ?? task.intent}</span>
        <span className="task-query">{task.query}</span>
        {task.missingTables.length > 0 && (
          <span className="task-missing">缺：{task.missingTables.join('、')}</span>
        )}
        <span className="spacer" />
        <span className={`plan-status ${task.status}`}>{taskStatusText(task.status)}</span>
      </button>

      {expandable && open && (
        <div className="task-block-body">
          {agents.length > 0 ? (
            agents.map((activity) => (
              <AgentCard
                key={`${activity.agent}::${activity.taskId}`}
                activity={activity}
                steps={activity.steps}
              />
            ))
          ) : (
            <div className="empty small">
              {task.status === 'blocked' ? '等待用户补充表格' : '等待分配…'}
            </div>
          )}
        </div>
      )}
    </li>
  )
})

interface Props {
  turn: Turn
  /** 本轮是否正在进行（决定是否默认展开） */
  live: boolean
}

/**
 * 一轮的完整执行过程。
 *
 * - 正在运行：整块展开，按「计划 → Master 思考 → 各任务块 → Master 整理结果」展示
 * - 运行完毕：整块折叠，标题后显示用时与工具次数
 *
 * 用 memo 包住：历史轮次的对象引用不变时跳过重渲染。
 */
export const TurnTrace = memo(function TurnTrace({ turn, live }: Props) {
  const running = live && !turn.done
  const [open, setOpen] = useState(running)

  useEffect(() => {
    setOpen(running)
  }, [running, turn.id])

  // 实时计时：运行中每秒刷新一次，用于「执行过程」后面的耗时
  const now = useNow(running)
  const liveElapsed = Math.max(0, (now - (turn.startedAt || now)) / 1000)

  const tools = turnTools(turn)

  // Master 的两段活动：拆解（TASK_PLAN）/ 汇总（TASK_SUMMARY）
  const planActivity = turn.agents.find((a) => a.taskId === TASK_PLAN)
  const summaryActivity = turn.agents.find((a) => a.taskId === TASK_SUMMARY)

  // 子任务都收尾后，Master 进入「整理结果」阶段
  const tasksSettled =
    turn.tasks.length > 0 &&
    turn.tasks.every((t) => t.status === 'done' || t.status === 'blocked')
  const summarizing = running && tasksSettled
  const doneCount = turn.tasks.filter((t) => t.status === 'done').length

  // 不属于任何子任务、也不是计划/汇总的活动（兜底展示）
  const taskIds = new Set(turn.tasks.map((t) => t.id))
  const looseAgents = turn.agents.filter(
    (a) => a.taskId !== TASK_PLAN && a.taskId !== TASK_SUMMARY && !taskIds.has(a.taskId)
  )

  // 耗时：运行中走实时计时，结束后用最终值（前端算好回报给后端保存）
  const elapsed = running ? liveElapsed : turn.elapsed
  const meta = [
    elapsed !== undefined ? `${elapsed.toFixed(1)}s` : '',
    tools.length > 0 ? `${tools.length} 次工具` : ''
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className={`trace${running ? ' is-running' : ''}`}>
      <button className="trace-head" onClick={() => setOpen((v) => !v)}>
        <span className="caret">{open ? '▾' : '▸'}</span>
        <span className="trace-label">{running ? '正在运行' : '执行过程'}</span>
        {/* 紧跟在「执行过程」后面：运行中实时计时，完成后显示最终耗时与工具次数 */}
        {meta && <span className="trace-meta">{meta}</span>}
        <span className="spacer" />
        {running && (
          <span className="trace-brief">
            {turn.tasks.length > 0 ? `子任务 ${doneCount}/${turn.tasks.length}` : '准备中…'}
          </span>
        )}
        {running && <span className="trace-live">进行中</span>}
      </button>

      {open && (
        <div className="trace-body">
          {/* 1. Master 思考（拆解问题）+ 拆解 / 路由说明
              （Master 的说明放在卡片外面：卡片折叠时也始终可见） */}
          {planActivity && <AgentCard activity={planActivity} title="Master 思考 · 拆解问题" />}
          <StepList steps={turn.steps} />

          {/* 2. Master 的任务拆分与分配：每个任务下面挂它自己的执行过程块 */}
          {turn.tasks.length > 0 && (
            <section className="plan-block">
              <div className="plan-head">Master 任务拆分与分配</div>
              <ul className="plan-list">
                {turn.tasks.map((task, index) => (
                  <TaskItem
                    key={task.id}
                    task={task}
                    index={index}
                    agents={turn.agents.filter((a) => a.taskId === task.id)}
                  />
                ))}
              </ul>
            </section>
          )}

          {/* 4. Master 整理结果：与「Master 思考 · 拆解问题」同款的卡片 */}
          {summaryActivity ? (
            <AgentCard activity={summaryActivity} title="Master · 整理结果" />
          ) : summarizing ? (
            <div className="agent-card running">
              <div className="agent-head">
                <span className="caret">▸</span>
                <span className="agent-dot pulse" />
                <span className="agent-name">Master · 整理结果中....</span>
              </div>
            </div>
          ) : null}

          {/* 兜底：不属于任何子任务的活动 */}
          {looseAgents.length > 0 && (
            <section className="task-block">
              <div className="task-block-head">
                <span className="task-dot done" />
                <span className="task-block-title">其它</span>
              </div>
              <div className="task-block-body">
                {looseAgents.map((activity) => (
                  <AgentCard
                    key={`${activity.agent}::${activity.taskId}`}
                    activity={activity}
                    steps={activity.steps}
                  />
                ))}
              </div>
            </section>
          )}

          {turn.agents.length === 0 && turn.tasks.length === 0 && (
            <div className="empty small">本轮还没有 Agent 事件</div>
          )}
        </div>
      )}
    </div>
  )
})

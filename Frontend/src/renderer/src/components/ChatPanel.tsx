import { memo, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'

import { useStore, type Turn } from '../store'
import { ChartGallery } from './ChartGallery'
import { TurnTrace } from './TurnTrace'

interface Props {
  ready: boolean
  busy: boolean
  onAsk: (text: string) => void
}

/**
 * 把一轮问答渲染成：问题 → 执行过程 → 最终答案。
 *
 * 用 memo 包住：正在进行的那一轮每帧都会产生新对象，如果不隔离，
 * 历史轮次（含大段 Markdown 表格）会被一起重新渲染，重会话下会把
 * 渲染进程拖垮。props 不变时直接跳过。
 */
const TurnView = memo(function TurnView({ turn, live }: { turn: Turn; live: boolean }) {
  // 本轮所有 Agent 生成的统计图，汇总在回答下面
  const charts = turn.agents.flatMap((activity) => activity.charts)

  return (
    <div className="turn">
      <div className="bubble user">{turn.query}</div>

      {/* 执行过程夹在问题与答案中间 */}
      <TurnTrace turn={turn} live={live} />

      {/* 运行中不放「正在生成答案…」占位：执行过程里已经实时显示进度与耗时 */}
      {turn.done && (
        <div className="bubble assistant">
          <Markdown>{turn.answer || '（空回答）'}</Markdown>
          <ChartGallery charts={charts} />
          {turn.elapsed !== undefined && (
            <div className="bubble-meta">
              用时 {turn.elapsed.toFixed(1)}s · 参与 {turn.agents.length} 个 Agent
            </div>
          )}
        </div>
      )}
    </div>
  )
})

/**
 * 默认渲染多少轮历史。
 *
 * 会话跑几十轮之后，一次性把所有轮次的 Markdown（大表格很长）铺进 DOM，
 * 首帧渲染会长到把主线程占死，界面看着就是「卡住不动」。只渲染最近若干轮，
 * 更早的按需展开 —— 完整记录仍会从 sessions/ 读出并保留在内存里。
 */
const INITIAL_TURNS = 12

/** 中间：对话区 + 输入框。 */
export function ChatPanel({ ready, busy, onAsk }: Props): React.JSX.Element {
  const messages = useStore((state) => state.messages)
  const live = useStore((state) => state.live)
  const [text, setText] = useState('')
  const [showAll, setShowAll] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // 用户是否停留在底部附近（决定新消息到达时要不要自动跟随）
  const atBottomRef = useRef(true)

  const hiddenCount = Math.max(0, messages.length - INITIAL_TURNS)
  const visible = showAll || hiddenCount === 0 ? messages : messages.slice(hiddenCount)

  /**
   * 滚动位置监听：只有用户本来就在最下方时，才继续自动跟随。
   * 用户往上翻看历史时，新消息不会把他的视图拽走。
   *
   * 滚动事件很密集，用 requestAnimationFrame 合并成每帧最多判断一次。
   */
  useEffect(() => {
    const list = listRef.current
    if (!list) return

    let frame = 0
    const measure = (): void => {
      frame = 0
      const distance = list.scrollHeight - list.scrollTop - list.clientHeight
      atBottomRef.current = distance < 80
    }
    const onScroll = (): void => {
      if (frame === 0) frame = requestAnimationFrame(measure)
    }

    measure()
    list.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      list.removeEventListener('scroll', onScroll)
      if (frame !== 0) cancelAnimationFrame(frame)
    }
  }, [])

  // 新内容到达时：仅在用户处于底部时贴到底部
  useEffect(() => {
    if (!atBottomRef.current) return
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, live?.query, live?.done, live?.agents.length])

  const submit = (): void => {
    const value = text.trim()
    if (!value || busy) return
    onAsk(value)
    setText('')
  }

  return (
    <section className="panel chat">
      <div className="panel-head">
        <span>对话</span>
        {live && !live.done && <span className="pill busy">本轮进行中</span>}
      </div>

      <div className="chat-list" ref={listRef}>
        {!ready && <div className="empty">正在连接后端…</div>}
        {ready && messages.length === 0 && !live && (
          <div className="empty">
            还没有对话内容。
            <br />
            试试问：「统计这张表里男女生各有多少人？」
            <br />
            或者：「把男女比例画成柱状图」
          </div>
        )}

        {!showAll && hiddenCount > 0 && (
          <button className="show-earlier" onClick={() => setShowAll(true)}>
            显示更早的 {hiddenCount} 轮对话
          </button>
        )}

        {visible.map((turn) => (
          <TurnView key={turn.id} turn={turn} live={false} />
        ))}
        {live && <TurnView turn={live} live />}

        <div ref={bottomRef} />
      </div>

      <div className="composer">
        <textarea
          value={text}
          rows={3}
          placeholder={busy ? '正在处理上一轮问题…' : '输入问题，Enter 发送，Shift+Enter 换行'}
          disabled={busy || !ready}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
        />
        <button className="btn-primary send" disabled={busy || !text.trim() || !ready} onClick={submit}>
          发送
        </button>
      </div>
    </section>
  )
}

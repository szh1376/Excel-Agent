import { useState } from 'react'

import type { SessionInfo } from '../types/events'

interface Props {
  sessions: SessionInfo[]
  current: string
  disabled: boolean
  onCreate: () => Promise<void>
  onSwitch: (id: string) => Promise<void>
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}

/** 左侧：会话列表，支持新建、切换、重命名、删除。 */
export function SessionList({
  sessions,
  current,
  disabled,
  onCreate,
  onSwitch,
  onRename,
  onDelete
}: Props): React.JSX.Element {
  const [editingId, setEditingId] = useState('')
  const [draft, setDraft] = useState('')
  const [confirmId, setConfirmId] = useState('')
  const [pending, setPending] = useState(false)

  /** 包一层：跑异步动作时禁用按钮，并把错误交给 store。 */
  const run = async (action: () => Promise<void>): Promise<void> => {
    if (pending) return
    setPending(true)
    try {
      await action()
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err))
    } finally {
      setPending(false)
    }
  }

  const startRename = (session: SessionInfo): void => {
    setEditingId(session.id)
    setDraft(session.title)
    setConfirmId('')
  }

  const commitRename = (): void => {
    const title = draft.trim()
    const id = editingId
    setEditingId('')
    if (!id || !title) return
    void run(() => onRename(id, title))
  }

  return (
    <aside className="panel sessions">
      <div className="panel-head">
        <span>对话</span>
        <button
          className="btn-primary"
          disabled={disabled || pending}
          onClick={() => void run(onCreate)}
          title="新建对话"
        >
          ＋ 新建
        </button>
      </div>

      <div className="session-items">
        {sessions.length === 0 && <div className="empty">还没有对话</div>}

        {sessions.map((session) => {
          const active = session.id === current
          const editing = session.id === editingId

          return (
            <div key={session.id} className={`session-item${active ? ' active' : ''}`}>
              {editing ? (
                <input
                  className="rename-input"
                  value={draft}
                  autoFocus
                  onChange={(event) => setDraft(event.target.value)}
                  onBlur={commitRename}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') commitRename()
                    if (event.key === 'Escape') setEditingId('')
                  }}
                />
              ) : (
                <button
                  className="session-main"
                  disabled={disabled || pending}
                  onClick={() => void run(() => onSwitch(session.id))}
                >
                  <span className="session-title" title={session.title}>
                    {session.title}
                  </span>
                  <span className="session-meta">
                    {session.turns} 轮 · {session.updated_at_text}
                  </span>
                </button>
              )}

              {!editing && (
                <div className="session-actions">
                  <button
                    className="icon-btn"
                    title="重命名"
                    disabled={disabled || pending}
                    onClick={() => startRename(session)}
                  >
                    ✎
                  </button>
                  <button
                    className="icon-btn danger"
                    title="删除"
                    disabled={disabled || pending}
                    onClick={() => setConfirmId(confirmId === session.id ? '' : session.id)}
                  >
                    ✕
                  </button>
                </div>
              )}

              {confirmId === session.id && (
                <div className="confirm">
                  <span>删除这个对话？</span>
                  <button
                    className="btn-danger"
                    disabled={pending}
                    onClick={() => {
                      setConfirmId('')
                      void run(() => onDelete(session.id))
                    }}
                  >
                    删除
                  </button>
                  <button className="btn-ghost" onClick={() => setConfirmId('')}>
                    取消
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

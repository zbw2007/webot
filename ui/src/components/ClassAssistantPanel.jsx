import { useCallback, useEffect, useState } from 'react'

const API = 'http://127.0.0.1:7327'

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || '请求失败')
  return data
}

export default function ClassAssistantPanel() {
  const [status, setStatus] = useState(null)
  const [digests, setDigests] = useState([])
  const [todos, setTodos] = useState([])
  const [drafts, setDrafts] = useState([])
  const [groups, setGroups] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setError('')
      const [s, d, t, r, g] = await Promise.all([
        request('/api/class-assistant/status'),
        request('/api/class-assistant/digests'),
        request('/api/class-assistant/todos?status=open'),
        request('/api/class-assistant/drafts'),
        request('/api/class-assistant/groups'),
      ])
      setStatus(s.status)
      setDigests(d.items || [])
      setTodos(t.items || [])
      setDrafts(r.items || [])
      setGroups(g.items || [])
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => { refresh(); const timer = setInterval(refresh, 30000); return () => clearInterval(timer) }, [refresh])

  async function approve(draft) {
    setBusy(true)
    try { await request(`/api/class-assistant/drafts/${encodeURIComponent(draft.id)}/approve`, { method: 'POST', body: JSON.stringify({ version: draft.version }) }); await refresh() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function reject(draft) {
    setBusy(true)
    try { await request(`/api/class-assistant/drafts/${encodeURIComponent(draft.id)}/reject`, { method: 'POST', body: JSON.stringify({ version: draft.version }) }); await refresh() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function edit(draft) {
    const text = window.prompt('编辑回复内容', draft.text)
    if (text == null || !text.trim()) return
    setBusy(true)
    try { await request(`/api/class-assistant/drafts/${encodeURIComponent(draft.id)}/edit`, { method: 'POST', body: JSON.stringify({ text }) }); await refresh() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function send(draft) {
    if (!window.confirm('确认发送到白名单群？真实发送仍受 DRY_RUN/REAL_SEND_ENABLED 双重保护。')) return
    setBusy(true)
    try {
      const token = await request('/api/class-assistant/token', { method: 'POST', body: '{}' })
      await request(`/api/class-assistant/drafts/${encodeURIComponent(draft.id)}/send`, {
        method: 'POST',
        body: JSON.stringify({ version: draft.version, confirmation_token: token.confirmation_token }),
      })
      await refresh()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function emergencyStop() {
    if (!window.confirm('立即停止采集和定时分析？')) return
    setBusy(true)
    try { const result = await request('/api/class-assistant/stop', { method: 'POST', body: '{}' }); setStatus(result.status) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="max-w-6xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-semibold text-text-main">班级事务助手</h3>
          <p className="text-sm text-text-muted mt-1">08:00 / 20:00 汇总 · 白名单群 · 审核后发送</p>
        </div>
        <button onClick={emergencyStop} disabled={busy || status?.emergency_stopped} className="px-4 py-2 rounded-lg bg-red-500/10 text-red-400 border border-red-500/30 disabled:opacity-50">紧急停止</button>
      </div>

      {error && <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-sm text-red-300">{error}</div>}

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {[
          ['状态', status?.emergency_stopped ? '已停止' : (status?.enabled ? '已启用' : '未启用')],
          ['白名单群', groups.length || status?.groups?.length || 0],
          ['待办', todos.length],
          ['待审核', drafts.filter(d => ['pending_review', 'edited'].includes(d.status)).length],
          ['模式', status?.dry_run ? 'DRY RUN' : '真实发送关闭'],
        ].map(([label, value]) => <div key={label} className="p-4 rounded-xl bg-bg-raised border border-border-main"><div className="text-xs text-text-muted">{label}</div><div className="mt-1 text-base font-semibold text-text-main">{value}</div></div>)}
      </div>

      <section className="p-5 rounded-xl bg-bg-raised border border-border-main">
        <h4 className="font-semibold text-text-main mb-3">待办清单</h4>
        {todos.length === 0 ? <p className="text-sm text-text-muted">暂无待办</p> : <div className="space-y-2">{todos.map(todo => <div key={todo.id} className="flex justify-between gap-3 text-sm"><span className="text-text-main">{todo.title}</span><span className="text-text-muted">{todo.due_at || '待确认日期'}</span></div>)}</div>}
      </section>

      <section className="p-5 rounded-xl bg-bg-raised border border-border-main">
        <h4 className="font-semibold text-text-main mb-3">回复审核队列</h4>
        {drafts.length === 0 ? <p className="text-sm text-text-muted">暂无草稿</p> : <div className="space-y-3">{drafts.map(draft => <div key={`${draft.id}:${draft.version}`} className="p-4 rounded-lg bg-bg-main border border-border-main"><div className="flex items-center justify-between gap-2"><span className="text-xs text-text-muted">{draft.group_name || draft.chat_id} · v{draft.version} · {draft.status}</span><span className="text-xs text-text-muted">风险：{draft.risk_level}</span></div><p className="mt-2 text-sm whitespace-pre-wrap text-text-main">{draft.text}</p><div className="mt-3 flex gap-2">{['pending_review', 'edited'].includes(draft.status) && <><button disabled={busy} onClick={() => edit(draft)} className="px-3 py-1.5 rounded-md border border-border-main text-xs">编辑</button><button disabled={busy} onClick={() => approve(draft)} className="px-3 py-1.5 rounded-md bg-brand-green text-black text-xs">批准</button><button disabled={busy} onClick={() => reject(draft)} className="px-3 py-1.5 rounded-md border border-red-500/30 text-red-300 text-xs">拒绝</button></>}{draft.status === 'approved' && <button disabled={busy} onClick={() => send(draft)} className="px-3 py-1.5 rounded-md bg-brand-green text-black text-xs">确认发送</button>}</div></div>)}</div>}
      </section>

      <section className="p-5 rounded-xl bg-bg-raised border border-border-main">
        <h4 className="font-semibold text-text-main mb-3">最近汇总</h4>
        {digests.length === 0 ? <p className="text-sm text-text-muted">暂无汇总记录</p> : <div className="space-y-2">{digests.slice(-5).reverse().map(run => <div key={run.scheduled_slot} className="flex justify-between text-sm"><span className="text-text-muted">{run.scheduled_slot}</span><span className={run.status === 'succeeded' ? 'text-brand-green' : 'text-red-300'}>{run.status}</span></div>)}</div>}
      </section>
    </div>
  )
}

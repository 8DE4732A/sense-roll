import { useEffect, useState } from 'react'
import { getRequests } from '../api/client'
import type { RequestRow } from '../api/client'

const PAGE_SIZE = 30

export default function Requests() {
  const [rows, setRows] = useState<RequestRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [combo, setCombo] = useState('')
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [successFilter, setSuccessFilter] = useState<'' | 'true' | 'false'>('')
  const [err, setErr] = useState('')

  const load = async () => {
    try {
      const res = await getRequests({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        combo: combo || undefined,
        provider: provider || undefined,
        model: model || undefined,
        success: successFilter === '' ? undefined : successFilter === 'true',
      })
      setRows(res.items)
      setTotal(res.total)
    } catch (e: unknown) { setErr(String(e)) }
  }

  useEffect(() => { load() }, [page, combo, provider, model, successFilter])

  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="page">
      <div className="page-header">
        <span className="page-title">请求明细</span>
        <span className="page-sub">最近 {total} 条记录</span>
      </div>

      {err && <div className="alert err">{err}</div>}

      <div className="filters">
        <input
          placeholder="Combo"
          value={combo}
          onChange={e => { setCombo(e.target.value); setPage(0) }}
          style={{ minWidth: 110 }}
        />
        <input
          placeholder="Provider"
          value={provider}
          onChange={e => { setProvider(e.target.value); setPage(0) }}
          style={{ minWidth: 110 }}
        />
        <input
          placeholder="Model"
          value={model}
          onChange={e => { setModel(e.target.value); setPage(0) }}
          style={{ minWidth: 140 }}
        />
        <select
          value={successFilter}
          onChange={e => { setSuccessFilter(e.target.value as '' | 'true' | 'false'); setPage(0) }}
          style={{ minWidth: 90 }}
        >
          <option value="">全部状态</option>
          <option value="true">仅成功</option>
          <option value="false">仅失败</option>
        </select>
        <span className="filter-count">{total} 条</span>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>Combo</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Key</th>
              <th>状态</th>
              <th>流式</th>
              <th>Prompt T</th>
              <th>Compl T</th>
              <th>Cache R</th>
              <th>Cache W</th>
              <th>耗时</th>
              <th>轮换规则</th>
              <th>Payload 改写</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={13} style={{ textAlign: 'center', color: 'var(--text-3)', padding: 32 }}>暂无数据</td></tr>
            )}
            {rows.map(r => (
              <tr key={r.id} className={r.success ? '' : 'row-err'}>
                <td className="text-muted" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                  {new Date(r.ts * 1000).toLocaleString('zh-CN')}
                </td>
                <td><code>{r.combo ?? '—'}</code></td>
                <td>{r.provider ?? '—'}</td>
                <td style={{ fontSize: 12, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.model ?? '—'}
                </td>
                <td><code>{r.key_prefix ?? '—'}…</code></td>
                <td>
                  {r.success
                    ? <span className="badge ok">{r.status_code}</span>
                    : <span className="badge err">{r.status_code ?? 'ERR'}</span>}
                </td>
                <td style={{ color: r.is_stream ? 'var(--ok-fg)' : 'var(--text-3)' }}>
                  {r.is_stream ? '✓' : '—'}
                </td>
                <td>{r.prompt_tokens ?? '—'}</td>
                <td>{r.completion_tokens ?? '—'}</td>
                <td style={{ color: r.cache_read_tokens ? 'var(--ok-fg)' : undefined }}>
                  {r.cache_read_tokens ?? '—'}
                </td>
                <td style={{ color: r.cache_write_tokens ? 'var(--warn-fg)' : undefined }}>
                  {r.cache_write_tokens ?? '—'}
                </td>
                <td className="text-muted">{r.duration_ms != null ? r.duration_ms + ' ms' : '—'}</td>
                <td style={{ fontSize: 12, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-2)' }}>
                  {r.matched_rule ?? r.error ?? '—'}
                </td>
                <td style={{ fontSize: 12, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: r.matched_payload ? 'var(--accent)' : 'var(--text-3)' }}>
                  {r.matched_payload ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pagination">
        <button disabled={page === 0} onClick={() => setPage(p => p - 1)}>← 上一页</button>
        <span>第 {page + 1} / {totalPages || 1} 页</span>
        <button disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)}>下一页 →</button>
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { getLogs, getLogSettings, putLogSettings } from '../api/client'
import type { LogRecord } from '../api/client'
import { FMT_COLOR } from '../api/client'

const PAGE_SIZE = 20

export default function Logs() {
  const [rows, setRows] = useState<LogRecord[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [page, setPage] = useState(0)
  const [successFilter, setSuccessFilter] = useState<'' | 'true' | 'false'>('')
  const [err, setErr] = useState('')

  const [verbose, setVerbose] = useState(false)
  const [settingsErr, setSettingsErr] = useState('')
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)

  // Load verbose_logging setting on mount
  useEffect(() => {
    getLogSettings()
      .then(s => setVerbose(s.verbose_logging))
      .catch(e => setSettingsErr(String(e)))
  }, [])

  const load = async () => {
    try {
      const res = await getLogs({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        success: successFilter === '' ? undefined : successFilter === 'true',
      })
      setRows(res.items)
      setHasMore(res.has_more)
    } catch (e: unknown) { setErr(String(e)) }
  }

  useEffect(() => { load() }, [page, successFilter])

  const toggleVerbose = async (next: boolean) => {
    setSettingsErr('')
    try {
      const res = await putLogSettings(next)
      setVerbose(res.verbose_logging)
    } catch (e: unknown) {
      setSettingsErr(String(e))
    }
  }

  const toggleRow = (i: number) => {
    setExpandedIdx(prev => prev === i ? null : i)
  }

  return (
    <div className="page">
      <div className="page-header">
        <span className="page-title">日志</span>
        <span className="page-sub">完整请求报文记录</span>
      </div>

      {/* Verbose toggle */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '6px 12px',
            border: `1px solid ${verbose ? 'var(--accent)' : 'var(--border-md)'}`,
            borderRadius: 6,
            background: verbose ? 'var(--accent-light)' : 'var(--bg-input)',
            cursor: 'pointer',
            userSelect: 'none',
          }}
        >
          <input
            type="checkbox"
            checked={verbose}
            onChange={e => toggleVerbose(e.target.checked)}
          />
          <span style={{ fontWeight: 600, fontSize: 13 }}>详细记录</span>
        </label>
        <span style={{ fontSize: 12, color: 'var(--warn-fg)', display: 'flex', alignItems: 'center', gap: 4 }}>
          ⚠ 完整记录报文含明文 API 密钥，仅限本地使用，切勿暴露公网
        </span>
        {settingsErr && <span style={{ fontSize: 12, color: 'var(--err-fg)' }}>{settingsErr}</span>}
      </div>

      {err && <div className="alert err">{err}</div>}

      <div className="filters">
        <select
          value={successFilter}
          onChange={e => { setSuccessFilter(e.target.value as '' | 'true' | 'false'); setPage(0) }}
          style={{ minWidth: 90 }}
        >
          <option value="">全部状态</option>
          <option value="true">仅成功</option>
          <option value="false">仅失败</option>
        </select>
        <button onClick={() => { setPage(0); load() }} style={{ fontSize: 12 }}>刷新</button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>Combo</th>
              <th>Provider › Model</th>
              <th>格式</th>
              <th>流式</th>
              <th>状态</th>
              <th>耗时</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-3)', padding: 32 }}>
                  {verbose ? '暂无数据' : '请先开启详细记录'}
                </td>
              </tr>
            )}
            {rows.map((r, i) => (
              <>
                <tr
                  key={i}
                  className={r.success ? '' : 'row-err'}
                  style={{ cursor: 'pointer' }}
                  onClick={() => toggleRow(i)}
                >
                  <td className="text-muted" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                    {new Date(r.ts * 1000).toLocaleString('zh-CN')}
                  </td>
                  <td><code>{r.combo ?? '—'}</code></td>
                  <td style={{ fontSize: 12 }}>
                    {r.provider ?? '—'} › {r.model ?? '—'}
                  </td>
                  <td>
                    {r.api_format && (
                      <span className={`tag ${(FMT_COLOR as Record<string, string>)[r.api_format] ?? 'amber'}`}>
                        {r.api_format}
                      </span>
                    )}
                  </td>
                  <td style={{ color: r.is_stream ? 'var(--ok-fg)' : 'var(--text-3)' }}>
                    {r.is_stream ? '✓' : '—'}
                  </td>
                  <td>
                    {r.success
                      ? <span className="badge ok">{r.status_code}</span>
                      : <span className="badge err">{r.status_code ?? 'ERR'}</span>}
                  </td>
                  <td className="text-muted">{r.duration_ms != null ? r.duration_ms + ' ms' : '—'}</td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>
                    {expandedIdx === i ? '▲' : '▼'}
                  </td>
                </tr>
                {expandedIdx === i && (
                  <tr key={`${i}-detail`}>
                    <td colSpan={8} style={{ padding: 0 }}>
                      <LogDetail record={r} />
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pagination">
        <button disabled={page === 0} onClick={() => setPage(p => p - 1)}>← 上一页</button>
        <span>第 {page + 1} 页</span>
        <button disabled={!hasMore} onClick={() => setPage(p => p + 1)}>下一页 →</button>
      </div>
    </div>
  )
}

function LogDetail({ record }: { record: LogRecord }) {
  const sections: [string, unknown][] = [
    ['客户端请求 (Client Request)', record.request?.client],
    ['上游请求 (Upstream Request)', record.request?.upstream],
    ['响应 (Response)', record.response],
  ]

  return (
    <div style={{
      background: 'var(--bg-panel)',
      borderTop: '1px solid var(--border)',
      borderBottom: '1px solid var(--border)',
      padding: '12px 16px',
    }}>
      {sections.map(([title, data]) => (
        <div key={title} style={{ marginBottom: 12 }}>
          <div style={{
            fontSize: 11,
            fontWeight: 600,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            color: 'var(--text-3)',
            marginBottom: 4,
          }}>
            {title}
          </div>
          <pre style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowY: 'auto',
            maxHeight: 360,
            background: 'var(--bg)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            padding: '8px 10px',
            margin: 0,
          }}>
            {JSON.stringify(data ?? null, null, 2)}
          </pre>
        </div>
      ))}
    </div>
  )
}

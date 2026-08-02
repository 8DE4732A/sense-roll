import { useEffect, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer,
} from 'recharts'
import { getStatsKeys, getStatsSummary, getStatsTrend } from '../api/client'
import type { KeysStatus, SummaryRow, TrendRow } from '../api/client'

function fmtTs(ts: number, bucket: string) {
  if (bucket === 'minute') {
    return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  // hour / day — show date+hour when spanning multiple days
  const d = new Date(ts * 1000)
  const now = new Date()
  if (now.toDateString() === d.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fmtNum(n: number | null | undefined): string {
  if (n == null) return '—'
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n)
}

function RefreshIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M2 8a6 6 0 1 1 1.5 4"/>
      <polyline points="2 12 2 8 6 8"/>
    </svg>
  )
}

function CooldownBadge({ available, seconds_remaining }: { available: boolean; seconds_remaining?: number }) {
  if (available) return <span className="badge ok">可用</span>
  const h = seconds_remaining ? Math.ceil(seconds_remaining / 3600) : '?'
  return <span className="badge err">冷却 ~{h}h</span>
}

type TimeRange = '4h' | '8h' | '12h' | 'today' | '3d' | '7d' | 'all'

const TIME_RANGE_OPTIONS: { value: TimeRange; label: string }[] = [
  { value: '4h',    label: '最近 4 小时' },
  { value: '8h',    label: '最近 8 小时' },
  { value: '12h',   label: '最近 12 小时' },
  { value: 'today', label: '今天' },
  { value: '3d',    label: '最近 3 天' },
  { value: '7d',    label: '最近 7 天' },
  { value: 'all',   label: '所有' },
]

function toSince(range: TimeRange): number | undefined {
  const now = Date.now() / 1000
  if (range === '4h')    return now - 4 * 3600
  if (range === '8h')    return now - 8 * 3600
  if (range === '12h')   return now - 12 * 3600
  if (range === 'today') {
    const d = new Date(); d.setHours(0, 0, 0, 0)
    return d.getTime() / 1000
  }
  if (range === '3d')  return now - 3 * 86400
  if (range === '7d')  return now - 7 * 86400
  return undefined // all
}

function trendBucket(range: TimeRange): string {
  if (range === '4h' || range === '8h' || range === '12h') return 'minute'
  return 'hour'
}

export default function Overview() {
  const [keys, setKeys] = useState<KeysStatus | null>(null)
  const [summary, setSummary] = useState<SummaryRow[]>([])
  const [trend, setTrend] = useState<TrendRow[]>([])
  const [groupBy, setGroupBy] = useState('combo')
  const [timeRange, setTimeRange] = useState<TimeRange>('today')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  const refresh = async (range = timeRange) => {
    setErr('')
    const since = toSince(range)
    try {
      const [k, s, t] = await Promise.all([
        getStatsKeys(),
        getStatsSummary({ group_by: groupBy, since }),
        getStatsTrend({ bucket: trendBucket(range), since }),
      ])
      setKeys(k)
      setSummary(s.data)
      setTrend(t.data)
    } catch (e: unknown) { setErr(String(e)) }
    setLoading(false)
  }

  useEffect(() => { refresh() }, [groupBy, timeRange])

  // Compute totals from summary
  const totalReqs = summary.reduce((s, r) => s + r.total, 0)
  const totalOk = summary.reduce((s, r) => s + r.success_count, 0)
  const totalTokens = summary.reduce((s, r) => s + (r.total_tokens ?? 0), 0)
  const totalCacheRead = summary.reduce((s, r) => s + (r.cache_read_tokens ?? 0), 0)
  const totalCacheWrite = summary.reduce((s, r) => s + (r.cache_write_tokens ?? 0), 0)
  const avgDur = summary.length
    ? Math.round(summary.reduce((s, r) => s + (r.avg_duration_ms ?? 0), 0) / summary.length)
    : null

  if (loading) return <div className="page"><div className="empty-state">加载中…</div></div>

  return (
    <div className="page">
      <div className="page-header">
        <span className="page-title">概览</span>
        <select
          value={timeRange}
          onChange={e => setTimeRange(e.target.value as TimeRange)}
          style={{ width: 'auto', minWidth: 130 }}
        >
          {TIME_RANGE_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <button className="refresh-btn" onClick={() => refresh()}>
          <RefreshIcon />刷新
        </button>
      </div>

      {err && <div className="alert err">{err}</div>}

      {/* Stat cards */}
      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">总请求</div>
          <div className="stat-value">{fmtNum(totalReqs)}</div>
          <div className="stat-sub">
            <span className="badge ok">{fmtNum(totalOk)} 成功</span>
            {totalReqs - totalOk > 0 && (
              <span className="badge err" style={{ marginLeft: 4 }}>{fmtNum(totalReqs - totalOk)} 失败</span>
            )}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Token 消耗</div>
          <div className="stat-value">{fmtNum(totalTokens)}</div>
          <div className="stat-sub">所有 combo 合计</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Cache Read Tokens</div>
          <div className="stat-value" style={{ color: totalCacheRead > 0 ? 'var(--ok-fg)' : undefined }}>
            {fmtNum(totalCacheRead)}
          </div>
          <div className="stat-sub">命中缓存，节省费用</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Cache Write Tokens</div>
          <div className="stat-value" style={{ color: totalCacheWrite > 0 ? 'var(--warn-fg)' : undefined }}>
            {fmtNum(totalCacheWrite)}
          </div>
          <div className="stat-sub">写入缓存（Anthropic）</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">平均耗时</div>
          <div className="stat-value">{avgDur != null ? avgDur + ' ms' : '—'}</div>
          <div className="stat-sub">按分组均值</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">活跃 Providers</div>
          <div className="stat-value">{keys?.providers.length ?? '—'}</div>
          <div className="stat-sub">{keys?.combos.length ?? 0} 个 combo</div>
        </div>
      </div>

      {/* Trend chart */}
      <div className="chart-wrap" style={{ marginBottom: 24 }}>
        <div className="chart-head">
          <span className="chart-title">请求趋势（按小时）</span>
        </div>
        {trend.length === 0
          ? <div className="empty-state" style={{ padding: 32 }}>暂无数据</div>
          : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={trend} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e8e4de" vertical={false} />
                <XAxis dataKey="bucket_ts" tickFormatter={v => fmtTs(Number(v), trendBucket(timeRange))} tick={{ fontSize: 11, fill: '#9e9b96' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: '#9e9b96' }} axisLine={false} tickLine={false} />
                <Tooltip
                  labelFormatter={v => fmtTs(Number(v), trendBucket(timeRange))}
                  contentStyle={{ fontSize: 12, borderRadius: 6, border: '1px solid #e2ded8', background: '#fff' }}
                  cursor={{ fill: 'rgba(0,0,0,0.03)' }}
                />
                <Bar dataKey="success_count" name="成功" fill="#1a5c3a" radius={[3, 3, 0, 0]} stackId="a" />
                <Bar dataKey={(r: TrendRow) => r.total - r.success_count} name="失败" fill="#f87171" radius={[3, 3, 0, 0]} stackId="a" />
              </BarChart>
            </ResponsiveContainer>
          )}
      </div>

      {/* Summary table */}
      <div style={{ marginBottom: 24 }}>
        <div className="toolbar">
          <h2>聚合统计</h2>
          <select
            value={groupBy}
            onChange={e => setGroupBy(e.target.value)}
            style={{ width: 'auto', minWidth: 120 }}
          >
            <option value="combo">按 Combo</option>
            <option value="model">按模型</option>
            <option value="provider">按 Provider</option>
            <option value="key_prefix">按 Key</option>
          </select>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>分组</th>
                <th>总计</th>
                <th>成功</th>
                <th>失败</th>
                <th>Total Tokens</th>
                <th>Prompt T</th>
                <th>Compl T</th>
                <th>Cache R</th>
                <th>Cache W</th>
                <th>平均耗时</th>
              </tr>
            </thead>
            <tbody>
              {summary.length === 0 && (
                <tr><td colSpan={10} style={{ textAlign: 'center', color: 'var(--text-3)', padding: 24 }}>暂无数据</td></tr>
              )}
              {summary.map(row => (
                <tr key={row.group_key ?? '(null)'}>
                  <td><span className="mono">{row.group_key ?? <em className="text-muted">未知</em>}</span></td>
                  <td>{row.total}</td>
                  <td style={{ color: 'var(--ok-fg)' }}>{row.success_count}</td>
                  <td style={{ color: row.error_count > 0 ? 'var(--err-fg)' : 'var(--text-3)' }}>{row.error_count}</td>
                  <td>{row.total_tokens?.toLocaleString() ?? '—'}</td>
                  <td>{row.prompt_tokens?.toLocaleString() ?? '—'}</td>
                  <td>{row.completion_tokens?.toLocaleString() ?? '—'}</td>
                  <td style={{ color: row.cache_read_tokens > 0 ? 'var(--ok-fg)' : 'var(--text-3)' }}>
                    {row.cache_read_tokens > 0 ? row.cache_read_tokens.toLocaleString() : '—'}
                  </td>
                  <td style={{ color: row.cache_write_tokens > 0 ? 'var(--warn-fg)' : 'var(--text-3)' }}>
                    {row.cache_write_tokens > 0 ? row.cache_write_tokens.toLocaleString() : '—'}
                  </td>
                  <td>{row.avg_duration_ms != null ? Math.round(row.avg_duration_ms) + ' ms' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Key pool */}
      <div className="toolbar">
        <h2>密钥池状态</h2>
      </div>
      {keys?.providers.length === 0 && <div className="empty-state">暂无 Provider</div>}
      {keys?.providers.map(p => (
        <div key={p.provider} className="provider-block">
          <div className="provider-head">
            <span className="provider-name">{p.provider}</span>
            <span className="tag">{p.strategy}</span>
            <span className="tag" style={{ marginLeft: 'auto' }}>{p.keys.length} keys</span>
          </div>
          <div className="table-wrap" style={{ border: 'none', borderRadius: 0, boxShadow: 'none' }}>
            <table>
              <thead>
                <tr>
                  <th>Key 前缀</th>
                  <th>成功</th>
                  <th>错误</th>
                  <th>最后使用</th>
                  <th>模型冷却</th>
                </tr>
              </thead>
              <tbody>
                {p.keys.map(k => (
                  <tr key={k.key_prefix}>
                    <td><code>{k.key_prefix}…</code></td>
                    <td>{k.use_count}</td>
                    <td style={{ color: k.error_count > 0 ? 'var(--err-fg)' : undefined }}>{k.error_count}</td>
                    <td className="text-muted" style={{ fontSize: 12 }}>
                      {k.last_used_at ? new Date(k.last_used_at * 1000).toLocaleString('zh-CN') : '—'}
                    </td>
                    <td>
                      {Object.entries(k.model_cooldowns).length === 0
                        ? <span className="badge ok">全部可用</span>
                        : Object.entries(k.model_cooldowns).map(([m, cd]) => (
                          <span key={m} style={{ marginRight: 6, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11 }}>
                            <code>{m}</code> <CooldownBadge {...cd} />
                          </span>
                        ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  )
}

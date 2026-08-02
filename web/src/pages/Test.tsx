import { useEffect, useRef, useState } from 'react'
import { getConfig } from '../api/client'
import type { AppConfig, ApiFormat } from '../api/client'

type SendState = 'idle' | 'sending' | 'streaming' | 'done' | 'error'

const FMT_ENDPOINT: Record<ApiFormat, string> = {
  'openai': '/v1/chat/completions',
  'anthropic': '/v1/messages',
  'openai-responses': '/v1/responses',
  'openai-images': '/v1/images/generations',
}

function buildBody(fmt: ApiFormat, model: string, prompt: string, stream: boolean, imageSize?: string): unknown {
  if (fmt === 'anthropic') {
    return { model, messages: [{ role: 'user', content: prompt }], max_tokens: 2048 }
  }
  if (fmt === 'openai-responses') {
    return { model, input: prompt }
  }
  if (fmt === 'openai-images') {
    return { model, prompt, n: 1, size: imageSize || '1024x1024' }
  }
  // openai
  return {
    model,
    messages: [{ role: 'user', content: prompt }],
    stream,
    ...(stream ? { stream_options: { include_usage: true } } : {}),
  }
}

function parseUsage(text: string, fmt: ApiFormat): { prompt?: number; completion?: number; total?: number } | null {
  try {
    const obj = JSON.parse(text)
    if (fmt === 'anthropic') {
      const u = obj.usage
      if (!u) return null
      return { prompt: u.input_tokens, completion: u.output_tokens, total: (u.input_tokens ?? 0) + (u.output_tokens ?? 0) }
    }
    const u = obj.usage
    if (!u) return null
    return { prompt: u.prompt_tokens, completion: u.completion_tokens, total: u.total_tokens }
  } catch { return null }
}

function extractStreamText(chunk: string, fmt: ApiFormat): { text?: string; usage?: ReturnType<typeof parseUsage> } {
  const lines = chunk.split('\n')
  let text = ''
  let usage: ReturnType<typeof parseUsage> = null
  for (const line of lines) {
    if (!line.startsWith('data: ')) continue
    const data = line.slice(6).trim()
    if (data === '[DONE]') continue
    try {
      const obj = JSON.parse(data)
      if (fmt === 'openai') {
        const delta = obj.choices?.[0]?.delta?.content
        if (delta) text += delta
        if (obj.usage) usage = { prompt: obj.usage.prompt_tokens, completion: obj.usage.completion_tokens, total: obj.usage.total_tokens }
      } else if (fmt === 'anthropic') {
        if (obj.type === 'content_block_delta' && obj.delta?.text) text += obj.delta.text
        if (obj.type === 'message_start' && obj.message?.usage) {
          const u = obj.message.usage
          usage = { prompt: u.input_tokens, completion: u.output_tokens, total: (u.input_tokens ?? 0) + (u.output_tokens ?? 0) }
        }
      } else if (fmt === 'openai-responses') {
        if (obj.type === 'response.output_text.delta' && obj.delta) text += obj.delta
        if (obj.usage) usage = { prompt: obj.usage.input_tokens, completion: obj.usage.output_tokens, total: obj.usage.total_tokens }
      }
    } catch { /* skip malformed */ }
  }
  return { text, usage: usage ?? undefined }
}

function fmtElapsed(ms: number) {
  return ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : ms + 'ms'
}

function SendIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
      <line x1="2" y1="8" x2="14" y2="8"/>
      <polyline points="9 3 14 8 9 13"/>
    </svg>
  )
}
function StopIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
      <rect x="4" y="4" width="8" height="8" rx="1"/>
    </svg>
  )
}
function CopyIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7">
      <rect x="5" y="5" width="9" height="9" rx="1"/><path d="M4 11H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v1"/>
    </svg>
  )
}

function normalizeFormats(v: ApiFormat | ApiFormat[]): ApiFormat[] {
  return Array.isArray(v) ? v : [v]
}

export default function TestPage() {
  const [cfg, setCfg] = useState<AppConfig | null>(null)
  const [comboName, setComboName] = useState('')
  const [fmt, setFmt] = useState<ApiFormat>('openai')
  const [prompt, setPrompt] = useState('')
  const [stream, setStream] = useState(true)
  const [imageSize, setImageSize] = useState('1024x1024')
  const [imageSizeCustom, setImageSizeCustom] = useState('')
  const [state, setState] = useState<SendState>('idle')
  const [output, setOutput] = useState('')
  const [imageUrls, setImageUrls] = useState<string[]>([])
  const [errMsg, setErrMsg] = useState('')
  const [usage, setUsage] = useState<{ prompt?: number; completion?: number; total?: number } | null>(null)
  const [elapsed, setElapsed] = useState<number | null>(null)
  const [statusCode, setStatusCode] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const outputRef = useRef<HTMLDivElement>(null)
  const t0Ref = useRef<number>(0)

  useEffect(() => {
    getConfig().then(c => {
      setCfg(c)
      if (c.combos.length > 0) {
        setComboName(c.combos[0].name)
        setFmt(normalizeFormats(c.combos[0].api_format)[0])
      }
    }).catch(() => {})
  }, [])

  // When combo changes, default format to its first format
  const handleComboChange = (name: string) => {
    setComboName(name)
    if (!cfg) return
    const cb = cfg.combos.find(c => c.name === name)
    if (cb) setFmt(normalizeFormats(cb.api_format)[0])
  }

  // Available formats for selected combo
  const availableFormats: ApiFormat[] = cfg
    ? normalizeFormats(cfg.combos.find(c => c.name === comboName)?.api_format ?? 'openai')
    : ['openai']

  const stop = () => {
    abortRef.current?.abort()
    setState('done')
  }

  const send = async () => {
    if (!prompt.trim() || !comboName) return
    setOutput('')
    setImageUrls([])
    setErrMsg('')
    setUsage(null)
    setElapsed(null)
    setStatusCode(null)
    setState('sending')
    t0Ref.current = performance.now()

    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const body = buildBody(fmt, comboName, prompt.trim(), stream, imageSizeCustom.trim() || imageSize)
      const resp = await fetch(FMT_ENDPOINT[fmt], {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer test' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      })
      setStatusCode(resp.status)

      if (!resp.ok) {
        const txt = await resp.text()
        throw new Error(`HTTP ${resp.status}: ${txt}`)
      }

      if (stream && resp.headers.get('content-type')?.includes('text/event-stream')) {
        setState('streaming')
        const reader = resp.body!.getReader()
        const decoder = new TextDecoder()
        let accText = ''
        let lastUsage: ReturnType<typeof parseUsage> = null
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const chunk = decoder.decode(value, { stream: true })
          const { text, usage: u } = extractStreamText(chunk, fmt)
          if (text) { accText += text; setOutput(accText) }
          if (u) lastUsage = u
        }
        if (lastUsage) setUsage(lastUsage)
      } else {
        const text = await resp.text()
        const parsed = parseUsage(text, fmt)
        if (parsed) setUsage(parsed)
        // Extract content from JSON for display
        try {
          const obj = JSON.parse(text)
          if (fmt === 'openai-images') {
            const urls: string[] = (obj.data ?? []).map((d: { url?: string; b64_json?: string }) =>
              d.url ?? (d.b64_json ? `data:image/png;base64,${d.b64_json}` : null)
            ).filter(Boolean)
            setImageUrls(urls)
            setOutput(urls.length ? '' : text)
          } else if (fmt === 'openai') {
            setOutput(obj.choices?.[0]?.message?.content ?? text)
          } else if (fmt === 'anthropic') {
            setOutput(obj.content?.[0]?.text ?? text)
          } else if (fmt === 'openai-responses') {
            setOutput(obj.output?.[0]?.content?.[0]?.text ?? text)
          } else {
            setOutput(text)
          }
        } catch { setOutput(text) }
      }

      setElapsed(Math.round(performance.now() - t0Ref.current))
      setState('done')
    } catch (e: unknown) {
      if ((e as Error).name === 'AbortError') {
        setElapsed(Math.round(performance.now() - t0Ref.current))
        setState('done')
        return
      }
      setErrMsg(String(e))
      setElapsed(Math.round(performance.now() - t0Ref.current))
      setState('error')
    }
  }

  const copy = () => {
    navigator.clipboard.writeText(output).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div className="page">
      <div className="page-header">
        <span className="page-title">测试</span>
        <span className="page-sub">直接调用代理端点验证当前配置</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '360px 1fr', gap: 20, alignItems: 'start' }}>

        {/* ── Left: config panel ── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

          {/* Combo + format */}
          <div className="card">
            <div className="card-body-simple">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div>
                  <div className="field-label" style={{ marginBottom: 5 }}>Combo <span className="dim">（客户端 model 值）</span></div>
                  <select
                    value={comboName}
                    onChange={e => handleComboChange(e.target.value)}
                    disabled={state === 'streaming' || state === 'sending'}
                  >
                    {cfg?.combos.length === 0 && <option value="">— 暂无 combo —</option>}
                    {cfg?.combos.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
                  </select>
                </div>

                <div>
                  <div className="field-label" style={{ marginBottom: 5 }}>API 格式</div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {availableFormats.map(f => (
                      <button
                        key={f}
                        className={fmt === f ? 'btn-primary' : ''}
                        style={{ fontSize: 12, padding: '4px 10px', ...(fmt !== f ? {} : {}) }}
                        onClick={() => setFmt(f)}
                        disabled={state === 'streaming' || state === 'sending'}
                      >
                        {f}
                        <span style={{ marginLeft: 4, opacity: 0.55, fontSize: 10 }}>{FMT_ENDPOINT[f]}</span>
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={stream}
                      onChange={e => setStream(e.target.checked)}
                      disabled={fmt === 'openai-responses' || fmt === 'openai-images' || state === 'streaming' || state === 'sending'}
                    />
                    <span className="field-label">流式响应（SSE）</span>
                    {(fmt === 'openai-responses' || fmt === 'openai-images') && (
                      <span className="dim">— {fmt === 'openai-images' ? '图像 API 不支持流式' : 'responses API 固定流式'}</span>
                    )}
                  </label>
                </div>

                {fmt === 'openai-images' && (
                  <div style={{ marginTop: 12 }}>
                    <div className="field-label" style={{ marginBottom: 6 }}>图像尺寸</div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      <select
                        value={imageSize}
                        onChange={e => setImageSize(e.target.value)}
                        disabled={state === 'sending'}
                      >
                        <optgroup label="标准（OpenAI）">
                          <option value="256x256">256×256</option>
                          <option value="512x512">512×512</option>
                          <option value="1024x1024">1024×1024</option>
                          <option value="1792x1024">1792×1024（横）</option>
                          <option value="1024x1792">1024×1792（竖）</option>
                        </optgroup>
                        <optgroup label="SenseNova 横向">
                          <option value="2752x1536">2752×1536</option>
                          <option value="2560x720">2560×720</option>
                          <option value="3072x1376">3072×1376</option>
                          <option value="3072x864">3072×864</option>
                          <option value="2496x1664">2496×1664</option>
                          <option value="2368x1760">2368×1760</option>
                          <option value="2272x1824">2272×1824</option>
                        </optgroup>
                        <optgroup label="SenseNova 方形">
                          <option value="2048x2048">2048×2048</option>
                        </optgroup>
                        <optgroup label="SenseNova 竖向">
                          <option value="1536x2752">1536×2752</option>
                          <option value="1664x2496">1664×2496</option>
                          <option value="1760x2368">1760×2368</option>
                          <option value="1824x2272">1824×2272</option>
                          <option value="1344x3136">1344×3136</option>
                        </optgroup>
                        <option value="custom">自定义…</option>
                      </select>
                      {imageSize === 'custom' && (
                        <input
                          value={imageSizeCustom}
                          onChange={e => setImageSizeCustom(e.target.value)}
                          placeholder="例如：1664x2496"
                          style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}
                          disabled={state === 'sending'}
                        />
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
          <div className="card">
            <div className="card-body-simple">
              <div className="field-label" style={{ marginBottom: 6 }}>
                {fmt === 'openai-images' ? '图像描述（prompt）' : '提示词'}
              </div>
              <textarea
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder={fmt === 'openai-images' ? '描述你想生成的图像，例如：a cute cat sitting on a cloud' : '输入测试消息，例如：你好，简单介绍一下自己'}
                disabled={state === 'streaming' || state === 'sending'}
                style={{
                  width: '100%',
                  minHeight: 120,
                  resize: 'vertical',
                  fontFamily: 'var(--font-sans)',
                  fontSize: 13,
                  padding: '8px 10px',
                  background: 'var(--bg-input)',
                  border: '1px solid var(--border-md)',
                  borderRadius: 'var(--radius)',
                  color: 'var(--text)',
                  lineHeight: 1.6,
                  boxSizing: 'border-box',
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send()
                }}
              />
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>⌘↵ 发送</div>
            </div>
          </div>

          {/* Send / Stop */}
          {state === 'sending' || state === 'streaming' ? (
            <button
              onClick={stop}
              style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'center', padding: '10px', width: '100%' }}
            >
              <StopIcon />
              {state === 'sending' ? '连接中…' : '停止'}
            </button>
          ) : (
            <button
              className="btn-primary"
              onClick={send}
              disabled={!prompt.trim() || !comboName || !cfg}
              style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'center', padding: '10px', width: '100%' }}
            >
              <SendIcon />发送请求
            </button>
          )}

          {/* Meta info */}
          {(elapsed != null || statusCode != null || usage) && (
            <div className="card">
              <div className="card-body-simple">
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  {statusCode != null && (
                    <div>
                      <div className="stat-label">状态码</div>
                      <div>
                        <span className={`badge ${statusCode < 300 ? 'ok' : 'err'}`}>{statusCode}</span>
                      </div>
                    </div>
                  )}
                  {elapsed != null && (
                    <div>
                      <div className="stat-label">耗时</div>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600 }}>
                        {fmtElapsed(elapsed)}
                      </div>
                    </div>
                  )}
                  {usage?.prompt != null && (
                    <div>
                      <div className="stat-label">Prompt Tokens</div>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600 }}>{usage.prompt}</div>
                    </div>
                  )}
                  {usage?.completion != null && (
                    <div>
                      <div className="stat-label">Completion Tokens</div>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, fontWeight: 600 }}>{usage.completion}</div>
                    </div>
                  )}
                  {usage?.total != null && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <div className="stat-label">Total Tokens</div>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 18, fontWeight: 600 }}>{usage.total}</div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* ── Right: output ── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="card" style={{ flex: 1 }}>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              padding: '10px 14px',
              borderBottom: '1px solid var(--border)',
              gap: 8,
            }}>
              <span style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--text-2)', flex: 1 }}>
                响应输出
              </span>
              {state === 'streaming' && (
                <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: 'var(--ok-fg)' }}>
                  <span style={{
                    display: 'inline-block',
                    width: 7, height: 7,
                    borderRadius: '50%',
                    background: 'var(--ok-fg)',
                    animation: 'pulse 1.2s ease-in-out infinite',
                  }} />
                  流式传输中
                </span>
              )}
              {(output || imageUrls.length > 0) && state !== 'streaming' && state !== 'sending' && (
                <button className="btn-ghost" onClick={copy} style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
                  <CopyIcon />{copied ? '已复制' : '复制'}
                </button>
              )}
            </div>
            <div
              ref={outputRef}
              style={{
                padding: '14px 16px',
                minHeight: 400,
                maxHeight: 'calc(100vh - 260px)',
                overflowY: 'auto',
                fontFamily: 'var(--font-sans)',
                fontSize: 14,
                lineHeight: 1.7,
                color: 'var(--text)',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {state === 'idle' && !output && imageUrls.length === 0 && (
                <div className="empty-state" style={{ padding: '60px 20px' }}>
                  选择 Combo、输入提示词后发送请求
                </div>
              )}
              {state === 'sending' && !output && imageUrls.length === 0 && (
                <div className="empty-state" style={{ padding: '60px 20px' }}>
                  连接中…
                </div>
              )}
              {errMsg && (
                <div className="alert err" style={{ marginBottom: 12 }}>{errMsg}</div>
              )}
              {imageUrls.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
                  {imageUrls.map((url, i) => (
                    <a key={i} href={url} target="_blank" rel="noopener noreferrer">
                      <img
                        src={url}
                        alt={`生成图像 ${i + 1}`}
                        style={{ maxWidth: '100%', maxHeight: 480, borderRadius: 6, border: '1px solid var(--border)', display: 'block' }}
                      />
                    </a>
                  ))}
                </div>
              )}
              {output}
              {state === 'streaming' && (
                <span style={{
                  display: 'inline-block',
                  width: 2,
                  height: '1em',
                  background: 'var(--text)',
                  marginLeft: 2,
                  verticalAlign: 'text-bottom',
                  animation: 'blink 1s step-end infinite',
                }} />
              )}
            </div>
          </div>
        </div>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
      `}</style>
    </div>
  )
}

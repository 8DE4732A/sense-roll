/** Thin fetch wrapper with baseURL /admin/api */

const BASE = '/admin/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: resp.statusText }))
    throw new Error(err.error ?? resp.statusText)
  }
  return resp.json() as Promise<T>
}

// ---- Config ----

export type HealthCheckRule = {
  description: string
  jsonpath: string
  match_value: string
  match_type: 'equals' | 'contains' | 'regex'
  action: string
  cooldown_seconds: number
  models: string[]
}

export type KeyConfig = { key: string }

export type ApiEndpoint = {
  api_format: 'openai' | 'anthropic' | 'openai-responses' | 'openai-images'
  base_url: string
}

export type ProviderConfig = {
  name: string
  api: ApiEndpoint[]
  max_retries: number
  key_strategy: 'fill-first' | 'round-robin'
  keys: KeyConfig[]
  health_check_rules: HealthCheckRule[]
}

export type ComboMember = { provider: string; model: string }

export type ApiFormat = 'openai' | 'anthropic' | 'openai-responses' | 'openai-images'

export type ComboConfig = {
  name: string
  api_format: ApiFormat | ApiFormat[]
  strategy: 'fill-first' | 'round-robin'
  members: ComboMember[]
}

export type AppConfig = {
  providers: ProviderConfig[]
  combos: ComboConfig[]
}

export const getConfig = () => request<AppConfig>('/config')
export const putConfig = (cfg: AppConfig) =>
  request<AppConfig>('/config', { method: 'PUT', body: JSON.stringify(cfg) })

// ---- Stats ----

export type ModelCooldownEntry = { available: boolean; seconds_remaining?: number }
export type KeyStat = {
  key_prefix: string
  use_count: number
  error_count: number
  last_used_at: number | null
  model_cooldowns: Record<string, ModelCooldownEntry>
}
export type ProviderStat = { provider: string; strategy: string; keys: KeyStat[] }
export type KeysStatus = { combos: string[]; providers: ProviderStat[] }

export const getStatsKeys = () => request<KeysStatus>('/stats/keys')

export type SummaryRow = {
  group_key: string
  total: number
  success_count: number
  error_count: number
  total_tokens: number
  prompt_tokens: number
  completion_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  avg_duration_ms: number | null
}
export type SummaryResp = { data: SummaryRow[]; group_by: string }

export const getStatsSummary = (params: {
  group_by?: string
  since?: number
  until?: number
}) => {
  const qs = new URLSearchParams()
  if (params.group_by) qs.set('group_by', params.group_by)
  if (params.since != null) qs.set('since', String(params.since))
  if (params.until != null) qs.set('until', String(params.until))
  return request<SummaryResp>(`/stats/summary?${qs}`)
}

export type TrendRow = { bucket_ts: number; total: number; success_count: number; total_tokens: number }
export type TrendResp = { data: TrendRow[]; bucket: string }

export const getStatsTrend = (params: {
  bucket?: string
  since?: number
  until?: number
}) => {
  const qs = new URLSearchParams()
  if (params.bucket) qs.set('bucket', params.bucket)
  if (params.since != null) qs.set('since', String(params.since))
  if (params.until != null) qs.set('until', String(params.until))
  return request<TrendResp>(`/stats/trend?${qs}`)
}

// ---- Requests ----

export type RequestRow = {
  id: number
  ts: number
  combo: string | null
  provider: string | null
  model: string | null
  key_prefix: string | null
  api_format: string | null
  is_stream: number
  status_code: number | null
  success: number
  matched_rule: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  duration_ms: number | null
  error: string | null
}
export type RequestsResp = { total: number; items: RequestRow[] }

export const getRequests = (params: {
  limit?: number
  offset?: number
  combo?: string
  provider?: string
  model?: string
  success?: boolean
  since?: number
  until?: number
}) => {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v != null) qs.set(k, String(v))
  })
  return request<RequestsResp>(`/requests?${qs}`)
}

// ---- Health ----
export type AdminHealth = {
  status: string
  python: string
  config: { providers: number; combos: number }
  db: { queue_size: number; dropped_count: number; db_path: string }
}
export const getAdminHealth = () => request<AdminHealth>('/health')

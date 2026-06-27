# sense-roll

API 密钥轮换代理服务，用于 `https://token.sensenova.cn/v1/chat/completions`。

当上游返回配额超限等指定错误时，自动轮换到下一个 API 密钥并重试请求。

## 功能

- **透明代理** — 原封不动转发请求体和其他请求头，只替换 `Authorization` 头
- **错误检测** — 通过 JSONPath 表达式匹配响应中的错误内容
- **密钥轮换** — Round-robin 轮换策略，自动切换到下一个可用密钥
- **Streaming 支持** — 正确处理 SSE (text/event-stream) 流式响应
- **密钥状态查看** — 内置 `/keys/status` 端点查看各密钥使用情况

## 快速开始

```bash
# 安装依赖
uv sync

# 编辑配置文件
cp config-example.yaml config.yaml
# 编辑 config.yaml 填入你的 API 密钥

# 启动服务
uvicorn main:app --reload --port 8000
```

## 配置说明

参考 `config.yaml`：

```yaml
proxy:
  target_url: "https://token.sensenova.cn/v1/chat/completions"
  max_retries: 3
  key_cooldown_seconds: 60

keys:
  - key: "sk-your-key-1"
  - key: "sk-your-key-2"

rotation_rules:
  - description: "quota_exceeded_error"
    jsonpath: "$.error.type"
    match_value: "quota_exceeded_error"
    match_type: "equals"
    action: "rotate"
```

### `proxy`

| 字段 | 说明 |
|------|------|
| `target_url` | 上游目标地址 |
| `max_retries` | 单次请求最大重试次数，实际尝试次数不会超过可用密钥数 |
| `key_cooldown_seconds` | 密钥失败后的冷却时间（秒），冷却期内跳过该密钥 |

### `keys`

API 密钥列表。轮换时按顺序使用，跳过冷却期内的密钥。

### `rotation_rules`

| 字段 | 说明 |
|------|------|
| `jsonpath` | JSONPath 表达式，用于定位响应中的错误字段 |
| `match_value` | 匹配的目标值 |
| `match_type` | 匹配方式：`equals`、`contains`、`regex`，默认 `equals` |
| `action` | 匹配后执行的操作（当前仅支持 `rotate`）|

## API

### `POST /v1/chat/completions`

透明代理到上游。只需要传入原本的请求体，`Authorization` 头会被自动替换。

### `GET /health`

健康检查。

### `GET /keys/status`

查看当前密钥状态和使用统计。

```json
{
  "current_key": "sk-xxxx",
  "total_keys": 3,
  "keys": [
    {"key_prefix": "sk-xxxx", "use_count": 5, "error_count": 0, "last_used_at": 1234567890.0}
  ]
}
```

## 测试

```bash
# 健康检查
curl http://localhost:8000/health

# 代理请求（非 streaming）
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SENSENOVA_API_KEY" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}]}'

# 代理请求（streaming）
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Authorization: Bearer $SENSENOVA_API_KEY" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}],"stream":true}'

# 密钥状态
curl http://localhost:8000/keys/status
```

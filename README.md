# sense-roll

多 Provider API 网关，支持 Combo 路由、密钥轮换和本地管理页面。

当上游返回配额超限等指定错误时，自动轮换密钥或切换到备用 Provider，对客户端完全透明。

## 功能

- **多 Provider** — 同时管理多个上游服务（SenseNova、DeepSeek 等），每个 Provider 独立配置密钥和规则
- **Combo 路由** — 将一个虚拟模型名映射到多个 `(provider, model)` 成员，按策略依次尝试
- **两级重试** — 先在当前 Provider 内轮换密钥，密钥全部耗尽后自动切换到 Combo 的下一个成员
- **细粒度冷却** — 冷却粒度为 `(key, model)`，同一个 key 下不同模型的配额相互独立
- **多格式支持** — 同时支持 OpenAI Chat、Anthropic Messages、OpenAI Responses、OpenAI Images 四种 API 格式
- **透明代理** — 只改写 `model` 字段和 `Authorization` 头，其余请求体原样透传
- **Streaming 支持** — 正确处理 SSE (text/event-stream) 流式响应，含 token usage 嗅探
- **管理页面** — 内置 Web UI，支持实时统计、请求明细、热重载配置（`/admin/`）

## 安装

### 从 PyPI 安装（推荐）

```bash
pip install sense-roll
# 或
uv add sense-roll
```

管理页面前端已打包在 wheel 内，安装即可使用，**无需额外构建**。

### 从源码运行

```bash
git clone https://github.com/yourname/sense-roll
cd sense-roll

# 构建前端（需要 Node.js 18+）
cd web && npm ci && npm run build && cd ..

# 安装 Python 依赖
uv sync

# 启动
uv run sense-roll -c config.yaml --port 8000
```

## 快速开始

```bash
# 编辑配置文件
cp config-example.yaml config.yaml
# 填入你的 API 密钥和 Combo

# 启动服务
sense-roll -c config.yaml --port 8000
```

访问 `http://localhost:8000/admin/` 打开管理页面。

> **注意**：管理页面无认证，默认绑定 `127.0.0.1`，请勿暴露到公网。

## 管理页面

| 页面 | 功能 |
|------|------|
| **概览** | 请求量、Token 消耗（含 Cache Read/Write）、趋势图、密钥池状态 |
| **请求明细** | 分页日志，含 combo / provider / model / key / 状态码 / token 用量 |
| **配置** | 主从布局编辑 Provider 和 Combo，保存后热重载无需重启 |
| **测试** | 直接调用代理端点验证配置，支持流式展示和图像生成 |
| **日志** | 详细请求报文记录，可展开查看完整 client/upstream 请求与响应 |
| **配置 > Payload 脚本** | 在转发前执行 Python 脚本改写请求内容（body / header），用于隐藏客户端标识、调整 thinking 参数等 |

### 详细日志（Verbose Logging）

> ⚠️ **安全警告**：详细日志会完整记录 HTTP header，其中包含上游 Provider 的**明文 API 密钥**。日志文件仅限本地使用，已自动加入 `.gitignore`，请勿将 `logs/` 目录暴露至公网或提交至版本控制。

在管理页面「日志」菜单可通过开关启用/停用，开关状态持久化写入 `config.yaml`，重启后保留。

- **日志位置**：`<启动目录>/logs/requests.jsonl`（即 `cwd/logs/`，与 `config.yaml` 无关）。
- **滚动策略**：单文件超过 **20MB** 自动 gzip 压缩归档为 `requests.jsonl.1.gz`，旧归档依次后移，最多保留 **10 个** `.gz`，总磁盘占用通常不超过 ~200MB（视报文体积而定）。
- **记录内容**：每次对 Provider 的一次尝试写一条 JSONL，包含：
  - 元数据（ts / combo / provider / model / status_code / duration_ms …）
  - `request.client`：客户端发来的原始 HTTP 方法、路径、header、body
  - `request.upstream`：转发给 Provider 的 URL、header（含明文 API key）、body
  - `response`：Provider 返回的状态码、header、body

也可通过 API 直接查询：`GET /admin/api/logs`、`GET/PUT /admin/api/logs/settings`。

## 配置说明

完整字段说明见 `config-schema.yaml`，完整示例见 `config-example.yaml`。

### 顶层结构

```yaml
providers:
  - ...   # 上游 Provider 列表

combos:
  - ...   # 虚拟模型名到 Provider 的映射

verbose_logging: false  # 可选，true 时在 cwd/logs/ 记录完整请求报文（含明文密钥）

payload_scripts:       # 可选，按顺序执行的 Python 脚本列表
  - name: "..."        # 名称
    enabled: true      # false = 跳过（保留配置）
    script: |
      # request.body / request.headers 可直接修改
```

### Payload 改写脚本列表

在转发请求给上游 Provider 之前，按顺序执行已启用的脚本。每个脚本的输出是下一个脚本的输入。在管理页面「配置 > Payload 脚本」Tab 中管理，每条脚本可单独命名、启用/禁用、排序，保存后立即生效。

**`request` 对象可用属性：**

| 属性 | 类型 | 说明 |
|---|---|---|
| `request.body` | `dict` | 请求 body（可直接改；非 JSON body 时为 `{}`）|
| `request.headers` | `dict` | 请求 headers（可直接改）|
| `request.combo` | `str` | 客户端传的 combo 名（只读参考）|
| `request.path` | `str` | 请求路径，如 `/v1/chat/completions`（只读）|
| `request.raw_body` | `bytes` | 原始 body，仅非 JSON 时有值 |

**示例：**

```python
# 脚本 1：隐藏客户端标识
request.headers.pop('user-agent', None)

# 脚本 2：对 fast combo 限制 thinking 预算
if request.combo == 'fast' and 'thinking' in request.body:
    request.body['thinking']['budget_tokens'] = 1024
```

各脚本的执行情况（名称 + `ok` 或错误摘要）记录在请求明细的 `matched_payload` 字段。

⚠️ 脚本以 `exec()` 执行，具有完整 Python 权限，仅限本地/私有网络使用。

### `providers`

```yaml
providers:
  - name: sensenova               # 唯一名称，供 combo 引用
    api:
      - api_format: openai        # openai | anthropic | openai-responses | openai-images
        base_url: "https://token.sensenova.cn/v1"
      - api_format: anthropic
        base_url: "https://token.sensenova.cn/v1"
    max_retries: 3                # 单个 Provider 内最多尝试次数（含首次）
    key_strategy: "fill-first"    # fill-first | round-robin
    keys:
      - key: "sk-xxxx-1"
      - key: "sk-xxxx-2"
    health_check_rules:
      - description: "quota_exceeded"
        jsonpath: "$.error.type"
        match_value: "quota_exceeded_error"
        match_type: "equals"      # equals | contains | regex
        action: "rotate"
        cooldown_seconds: 18000
        models: ["deepseek-v4-flash"]  # 空列表 = 所有模型
```

### `combos`

```yaml
combos:
  - name: "fast"                  # 客户端请求中 model 字段填此值
    api_format: openai            # 单值或列表，决定监听哪些端点
    strategy: "fill-first"        # fill-first | round-robin
    members:
      - provider: sensenova
        model: "deepseek-v4-flash"
      - provider: deepseek        # 备用
        model: "deepseek-chat"
```

`api_format` 可以是列表，使同一个 Combo 同时服务多个端点：

```yaml
api_format:
  - openai      # → POST /v1/chat/completions
  - anthropic   # → POST /v1/messages
```

## API 端点

| 端点 | 格式 | 说明 |
|------|------|------|
| `POST /v1/chat/completions` | openai | OpenAI Chat Completions |
| `POST /v1/messages` | anthropic | Anthropic Messages |
| `POST /v1/responses` | openai-responses | OpenAI Responses API |
| `POST /v1/images/generations` | openai-images | OpenAI Images |
| `GET /v1/models` | — | 返回所有可用 combo（含 alias），OpenAI 兼容格式 |
| `GET /health` | — | 健康检查 |
| `GET /keys/status` | — | 实时密钥池状态 |
| `GET /admin/` | — | 管理页面（需前端已打包） |
| `GET /admin/api/*` | — | 管理 API |

## 示例

```bash
# OpenAI 格式（streaming）
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}],"stream":true}'

# Anthropic 格式
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}],"max_tokens":1024}'

# 密钥状态
curl http://localhost:8000/keys/status
```

## 本地开发

```bash
# 后端（带热重载）
uv run sense-roll -c config.yaml --port 8000

# 前端开发服务器（代理到后端 8000）
cd web && npm run dev
# 访问 http://localhost:5173/admin/
```

## 运行测试

```bash
uv run pytest
```

## 发布

推送 `v*` 格式的 tag 会触发 GitHub Actions，自动：
1. 安装 Node.js 并执行 `npm run build`（输出到 `sense_roll/web/dist/`）
2. 执行 `uv build` 打包（前端文件随 wheel 一并打包）
3. 发布到 PyPI

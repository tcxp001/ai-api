# Ai-api 公益服管理

个人本地公益服 Provider 管理与 AIProxy 配置工具。

本项目聚焦一件事：**管理自己手里的多个公益服 API Provider，并让 Codex / Hermes 通过本地 AIProxy 快速、明确、稳定地使用它们。**

它不是多人 API 平台、商业网关或桌面全家桶；初期不做登录、多用户、计费、全局 `/v1`、混合模型组、MCP/Skills/Prompts 管理等能力。

## 核心能力

- Provider 管理：维护 `base_url`、`api_key`、模型、Headers、User-Agent、API 模式等。
- Provider 测活：检测 Chat / Responses / reasoning / UA 兼容性。
- 本地 AIProxy：为每个 Provider 提供固定本地入口。
- Codex/Hermes 配置：生成或同步 Codex / Hermes 可用配置。
- 配置安全：保存配置前自动备份，支持恢复。

## 文件说明

| 文件 | 说明 |
|---|---|
| `dashboard.py` | 本地 Web 管理台后端 |
| `dashboard.html` | 本地 Web 管理台前端 |
| `proxy.py` | 本地 AIProxy，负责 Header 注入、Provider 路由、Responses/Chat 兼容转换 |
| `api.py` | Provider 测活/兼容性测试逻辑 |
| `config.example.yaml` | Provider 配置样例 |
| `config.yaml` | 实际 Provider 配置，首次使用可由样例复制生成 |
| `PRODUCT_PLAN.md` | 产品定位与边界 |
| `WORK_PLAN.md` | 推进工作计划 |

## 安装依赖

项目依赖很轻：

```bash
pip install -r requirements.txt
```

`requirements.txt` 当前包含：

```text
requests
PyYAML
```

## 初始化配置

首次使用时复制样例配置：

```bash
cd /mnt/project/ai-api
cp config.example.yaml config.yaml
```

然后编辑 `config.yaml`，填入自己的公益服 API Key，或通过 Web 管理台维护。

## 启动 Web 管理台

```bash
cd /mnt/project/ai-api
python3 dashboard.py --host 127.0.0.1 --port 18080
```

访问：

```text
http://127.0.0.1:18080
```

如果需要在局域网其他设备访问，可以改成：

```bash
python3 dashboard.py --host 0.0.0.0 --port 18080 --public-host <你的局域网IP>
```

> 注意：管理台会显示和写入 API Key，默认建议只监听 `127.0.0.1`。

## 启动 AIProxy

直接命令行启动：

```bash
cd /mnt/project/ai-api
python3 proxy.py --config config.yaml --listen 127.0.0.1 --port 18006
```

也可以在 Web 管理台的 **AIProxy 服务** 页面创建/启动 systemd 服务。

## Provider 本地 URL

每个 Provider 都会通过 AIProxy 暴露固定本地入口：

```text
http://127.0.0.1:18006/{provider}/v1
```

例如 Provider 名为 `provider-a`：

```text
http://127.0.0.1:18006/provider-a/v1
```

常见接口：

```text
GET  http://127.0.0.1:18006/provider-a/v1/models
POST http://127.0.0.1:18006/provider-a/v1/responses
POST http://127.0.0.1:18006/provider-a/v1/chat/completions
```

这个固定 Provider 路径是核心能力，适合同时开启多个 Codex CLI，让每个 CLI 绑定不同公益服。

## Codex/Hermes 使用方式

推荐让 Codex/Hermes 指向本地 AIProxy，而不是直接指向公益服上游。

示例：

```text
base_url = http://127.0.0.1:18006/provider-a/v1
model = gpt-5.5
```

这样真实上游的 API Key、Headers、User-Agent 都由 AIProxy 从 `config.yaml` 注入，Codex/Hermes 不需要直接持有公益服 Key。

Web 管理台会逐步提供：

- 复制 Provider 本地 URL
- 复制 Codex 配置片段
- 复制 Hermes 配置片段
- 一键同步 Codex/Hermes 配置
- 同步前自动备份
- 同步后测试

## Provider 配置字段说明

`config.yaml` 根节点可以是 Provider 列表，也可以是：

```yaml
providers:
  - name: provider-a
    base_url: https://example-a.invalid/v1
    api_key: ${PROVIDER_A_API_KEY}
```

推荐使用列表形式。

### `name`

Provider 名称，也是本地 URL path 的一部分。

要求：

- 必填
- 不重复
- URL 安全
- 只能使用英文字母、数字、点号、下划线、连字符
- 必须以字母或数字开头

示例：

```yaml
name: provider-a
```

对应本地入口：

```text
http://127.0.0.1:18006/provider-a/v1
```

### `base_url`

上游公益服 API 基础地址。

要求：

- 必填
- 必须以 `http://` 或 `https://` 开头
- 通常包含 `/v1`
- 保存时会自动去掉末尾 `/`

示例：

```yaml
base_url: https://example-a.invalid/v1
```

### `api_key`

上游公益服 API Key。

示例：

```yaml
api_key: ${PROVIDER_A_API_KEY}
```

也兼容旧字段：

```yaml
key: ${PROVIDER_A_API_KEY}
```

保存时会规范为 `api_key`。

### `api_mode`

Provider API 模式。

常用值：

| 值 | 说明 |
|---|---|
| `codex_responses` | 优先按 Responses API 使用，适合 Codex |
| `chat_completions` | 上游只支持 Chat Completions 时使用 |
| `custom_endpoint` | 上游需要自定义 endpoint 时使用 |

缺省值：

```yaml
api_mode: codex_responses
```

### `custom_endpoint`

当 `api_mode: custom_endpoint` 时必填。

示例：

```yaml
api_mode: custom_endpoint
custom_endpoint: /custom/path
```

如果没写开头 `/`，保存时会自动补齐。

也兼容旧字段：

```yaml
endpoint: /custom/path
```

保存时会规范为 `custom_endpoint`。

### `models`

该 Provider 可用模型列表。

推荐使用字典形式，以便维护模型元信息：

```yaml
models:
  gpt-5.5:
    context_length: 400000
    reasoning_effort: high
  glm-5.1:
    context_length: 200000
```

也支持简写：

```yaml
models:
  - gpt-5.5
  - glm-5.1
```

或单模型旧字段：

```yaml
model: gpt-5.5
```

保存时会规范为：

```yaml
models:
  gpt-5.5: {}
```

### `headers`

需要注入给上游的额外请求头。

必须是对象。

示例：

```yaml
headers:
  Originator: codex_cli_rs
  User-Agent: codex_cli_rs/0.139.0 (Debian 12.0.0; x86_64) xterm-256color
```

空 UA 示例：

```yaml
headers:
  User-Agent: ''
```

### `remove_headers`

转发前需要移除的请求头列表。

必须是数组。

示例：

```yaml
remove_headers:
  - User-Agent
```

### `enabled`

是否启用该 Provider。

示例：

```yaml
enabled: true
```

禁用后不会出现在代理可用 Provider 中。

### `reasoning_effort`

Provider 级 reasoning 默认强度。

示例：

```yaml
reasoning_effort: high
```

也可以写在模型元信息中，模型级配置优先级更高：

```yaml
models:
  gpt-5.5:
    reasoning_effort: high
```

如果值为 `none`，代理会移除 Responses 请求中的 reasoning 参数。

### `fallback_responses_to_chat`

当请求 `/responses` 但上游不支持 Responses API 时，是否自动回退到 `/chat/completions`。

示例：

```yaml
fallback_responses_to_chat: true
```

默认：`true`。

### 超时与连接池字段

可选字段：

```yaml
pool_maxsize: 20
connect_timeout: 30
read_timeout: 115
trust_env_proxy: false
```

说明：

- `pool_maxsize`：Provider HTTP session 池大小
- `connect_timeout`：连接超时秒数
- `read_timeout`：读取超时秒数
- `trust_env_proxy`：是否使用系统环境变量代理

## 配置示例

```yaml
- name: provider-a
  base_url: https://example-a.invalid/v1
  api_key: ${PROVIDER_A_API_KEY}
  api_mode: chat_completions
  models:
    gpt-5.5:
      context_length: 400000
      reasoning_effort: high
  headers:
    Originator: codex_cli_rs
    User-Agent: codex_cli_rs/0.139.0 (Debian 12.0.0; x86_64) xterm-256color
  remove_headers: []
  enabled: true
```

## 配置保存校验

管理台保存配置时会进行基础校验：

- `name` 不能为空
- `name` 不能重复
- `name` 必须适合作为 URL path
- `base_url` 必须以 `http://` 或 `https://` 开头
- `headers` 必须是对象
- `remove_headers` 必须是数组
- `custom_endpoint` 模式下必须填写 `custom_endpoint`

以下情况允许保存，但会提示警告：

- `api_key` 为空
- `models` 为空

## 产品与工作计划

- 产品定位与边界：[`docs/PRODUCT_PLAN.md`](docs/PRODUCT_PLAN.md)
- 推进工作计划：[`docs/WORK_PLAN.md`](docs/WORK_PLAN.md)

后续开发以这两份文档为准：不直接提升个人公益服管理与 Codex/Hermes 使用体验的功能，先不做。

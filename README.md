# Ai-api

本地公益服 Provider 管理与 AIProxy 工具。

用途：维护多个上游 API Provider，并通过本地固定入口给 Codex、Claude Code 使用。

## 文件

- `dashboard.py`：Web 管理台后端
- `dashboard.html`：Web 管理台前端
- `proxy.py`：AIProxy 转发与协议转换、抢通 + 保温
- `proxy_content_encoding.py`、`proxy_reasoning.py`、`proxy_state.py`、`proxy_compaction.py`：
  请求解压、显式推理参数映射、协议状态与原子摘要
- `proxy_claude.py`、`proxy_claude_stream.py`：Claude Messages 的 JSON 与 SSE 转换
- `model_capabilities.py`：管理台和代理共用的模型能力校验
- `codex_keepalive.py`：可选的真实交互式 Codex 会话保活（部署时与 proxy.py 一起复制）
- `api.py`：测活逻辑
- `prompts.py`：探活提示词轮换（proxy.py 与 api.py 共用）
- `config.example.yaml`：配置示例
- `requirements.txt`：依赖列表
- `dashboard_security.py`、`secret_utils.py`：管理台鉴权、私有文件写入与诊断脱敏（部署时须一起复制）

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制示例配置：

```bash
mkdir -p config
cp config.example.yaml config/config.yaml
```

最小配置：

```yaml
- name: provider-a
  base_url: https://example.com/v1
  api_key: your-api-key
  api_mode: codex_responses
  models:
    gpt-5.5:
      reasoning_effort: high
  headers:
    User-Agent: curl/8.0
  enabled: true
```

常用 `api_mode`：

- `codex_responses`：上游原生支持 OpenAI `/responses`。
- `chat_completions`：上游只支持 OpenAI `/chat/completions`；当 Codex 调用本地 `/responses` 时，代理会自动转换请求/响应，并支持 Responses 工具调用 ↔ Chat `tool_calls`，保留 reasoning/`<think>`、工具调用 reasoning、文件/音频内容块，合并中途 `system/developer` 指令，包含流式 `response.function_call_arguments.*` 与 `response.reasoning_summary_text.*` 事件；上游 4xx/5xx 也会转成 Responses JSON error 或 `response.failed` SSE。
- `messages`：上游为 Anthropic `/messages`；当 Codex 调用本地 `/responses` 时，代理会转换为 Anthropic Messages，并保留工具调用/工具结果、工具定义、`tool_choice`、图片内容与流式 tool_use 事件。

因此可给 Codex 配置多个本地入口，例如 `http://127.0.0.1:18006/DS/v1`、`http://127.0.0.1:18006/bohe/v1`，每个 provider 独立选择自己的 `api_mode`，不需要像单全局 provider 那样切换。

### Codex / Claude Code 协议兼容

`api_mode` 指定**上游协议**，不由 `auth_mode` 推断。认证可独立选择
`bearer` 或 `anthropic`（`x-api-key`）；配置了 `api_key` 时替换客户端凭据。
Messages 上游即使用 Bearer 认证，也仍发送 Messages 请求体和 `anthropic-version`。

| 本地请求 | `chat_completions` 上游 | `messages` 上游 | `codex_responses` / `responses` 上游 |
| --- | --- | --- | --- |
| Codex `POST /responses` | 转为 Chat，回复转回 Responses | 转为 Messages，回复转回 Responses | 原生转发 |
| Claude Code `POST /messages` | 转为 Chat，回复转回 Messages | 原生转发 | 转为 Responses，回复转回 Messages |

路径均位于 `/{provider}/v1` 之后。转换支持普通函数工具的多轮调用/结果、文本、
图片、可表达的 thinking、缓存 usage 和 JSON/SSE；Claude 的上游 JSON 回复可包装为
完整的 Messages SSE。原生 Messages 在没有配置推理覆盖时保留请求体原始字节。

- Codex Chat 桥将图片 `detail: original` 降为 `auto`；只注册明确的
  `additional_tools` / `tool_search_output` 工具声明，保留命名空间，不把普通工具数据当定义。
- 可见推理不是可伪造的签名。Anthropic 签名和 Responses reasoning 用不同的版本化状态
  包往返，绑定上游地址、实际认证身份及模型；跨账号、模型或不可表示的协议切换返回
  `incompatible_conversation_state`。此时应回到原上游，或开始不带该状态的新会话。
  状态包使用 base64 编码，**不加密、不认证内容**，不能当作密钥存储机制。
  身份校验使用最终有效请求头（包括 Session 自动携带的 Cookie）；账号 Cookie 改变时
  旧状态也会失效，不会猜测新旧 Cookie 是否属于同一账号。
  Claude→Responses 使用标准 `include: ["reasoning.encrypted_content"]` 请求可回放推理，
  不修改上游 `store` 策略；实际能否返回该字段仍取决于上游实现。
- Claude 桥接保留显式 `custom_endpoint` 及查询参数；`api_mode: messages` 的自定义
  原生端点同样执行状态检查、版本头补齐，并保留未修改请求的原始字节。
- 转换流中断/错误不会被当作正常成功结束。没有终止信号的 Codex 部分输出标为 incomplete；
  未关闭工具不会发送“参数已完成”事件。不会因已开始输出后的失败而重放整次请求。
- Claude 的上游 HTTP 错误采用 Anthropic error envelope，SSE 中途错误发 `error`，
  不伪造成功 `message_stop`；原生 Responses→Chat 的旧状态码 fallback 不适用于 Claude 请求。
- 本次不模拟服务端托管搜索等特殊工具，不实现 OAuth 或 WebSocket。
  Responses 无 `stop_sequences` 等价字段，Claude→Responses 时省略该字段；
  无法表示的 opaque thinking、工具类型或损坏的工具参数明确报错，不用占位内容替代。
  Chat 的 `reasoning_content` 需要上游支持。
- `GET /responses` 的 WebSocket Upgrade 返回 **426** 和 `websocket_not_supported`，
  供支持回退的客户端改用 HTTP `POST /responses` + SSE；服务端不建立 WS、不代替客户端重发。

### 模型能力与显式推理映射（可选）

只在确认上游真实支持时声明能力，不按模型名自动开启。模型 `capabilities` 可设置
`supports_parallel_tool_calls`、`input_modalities`（`text` / `image`）、
`supports_image_detail_original`、`supports_search_tool` 和 `supported_reasoning_levels`。
后三项中的搜索标志只是目录声明，不会凭空增加代理的托管搜索转换能力。
Chat 模式始终不宣称支持图片 `original`。

```yaml
models:
  vendor-model:
    reasoning_effort: high
    capabilities:
      supports_parallel_tool_calls: true
      input_modalities: [text, image]
      supports_image_detail_original: false
      supports_search_tool: false
      supported_reasoning_levels: [low, medium, high]
    chat_reasoning:
      thinking_param: enable_thinking
      effort_param: none
```

`chat_reasoning` 可放在 provider 或模型下；**模型级整组配置优先**，不合并两组。
未配置时保留旧模型门控和推理字段行为：

- `thinking_param`：`none`（不写）、`thinking`（`{"type":"enabled"/"disabled"}`）、
  `enable_thinking`（布尔）。
- `effort_param`：`none`、`reasoning_effort`、`reasoning.effort`。
  省略时使用旧模型门控；不认识的 vendor model 不自动写 effort。
- `effort_map`：显式字符串映射，例如 `{ultra: max, xhigh: high}`，不自动猜档位。
- 配置 `reasoning_effort: none` 时，显式 thinking 开关会关闭；disabled effort 默认不发，
  除非在 `effort_map` 中明确映射。既有 provider/model effort 覆盖优先级保持不变。
- 显式限制 reasoning levels 时，目录、生成的 Codex profile 与代理使用同一默认值：
  配置值优先；未配置取 `medium`；不在列表则取第一项。未声明能力保持旧默认值。

### 压缩请求与长会话摘要

请求体支持 `identity`、gzip、deflate（zlib/raw）和 zstd；叠加编码按反向顺序解码，
成功后移除过期实体头，按解码后的 JSON 做协议转换。需要 `requirements.txt` 中的
`zstandard`；缺少该依赖时仅 zstd 请求返回 503，不影响其他编码。
不支持的编码返回 415，损坏/截断的压缩体返回 400，不转发到上游。

在 Chat/Messages 桥上，Codex 的 `compaction_trigger` 和 `/responses/compact`
使用完整历史生成摘要：去掉本轮工具声明，强制上游非流式；**收到完整、成功、非空的
最终摘要之后**才发布单个 compaction item。错误、工具调用、截断或上游不遵守
`stream:false` 时返回 502 `compaction_failed`，不能用失败摘要替换历史。
不新增摘要 token 上限，保留请求预算及既有 Messages 默认预算。

自有摘要是可读文字的版本化 base64 包（不是加密），下一轮还原为带上下文前缀的
普通消息，可在支持的协议间回放。外部不透明 compaction 状态只能交还原生 Responses
上游，转换桥返回 400 而不是静默删历史。原生上游自己的压缩接口默认继续原生转发；
只有原有显式 fallback 配置及 404/405/501 状态满足时才尝试 Chat 桥。

### 发布注意

更新时上述新 Python 模块必须与 `proxy.py`、`dashboard.py` 一起提供，并安装对应依赖，
不能只复制单个 `proxy.py`。开发验证可使用隔离目录和本地假上游，不必访问真实 provider。
源文件同步不等于运行进程已加载新版本。当前服务器没有在途请求无损排空/切换机制：
有活跃 Codex/Claude 会话时不要直接重启；发布应另行安排空闲窗口，本次兼容性代码
不提供或自动执行热同步、重启。

## 启动管理台

直接启动：

```bash
python3 dashboard.py --host 127.0.0.1 --port 18080
```

访问：

```text
http://127.0.0.1:18080
```

### 管理台登录与密钥保护

首次访问管理台时，页面会弹出设置密码窗口；之后登录时只需要填写密码。
密码内容不设长度、字符类型或非空要求，确认密码与第一次输入一致即可。
- 仅保存带随机盐的 PBKDF2-SHA256 密码哈希（600,000 次迭代），默认文件为
  `config/dashboard-auth.json`，权限为 `0600`，不能从文件查看原密码。
  哈希文件同样需要私下保管，不要上传 Git 或分享。
- 登录后，点击网页右上角“修改登录密码”，输入当前密码及两次新密码即可修改。
  修改后旧密码立即失效（已通过认证的在途请求不撤回），重新登录时使用新密码。
- 忘记密码时，在服务器终端重新执行 `python3 dashboard.py --set-password` 即可重设；
  该本机命令不需要旧密码，因此仅应允许可信用户访问服务器账户和密码文件。
  修改或重设都会被运行中的新版本管理台自动读取，无需重启。
- 自定义路径时，设置和启动都须使用同一个 `--auth-file /private/path/dashboard-auth.json`。
  不支持把密码放在命令参数、环境变量或管道中，避免泄漏到命令历史或进程信息。
  未设置密码时仅开放页面和首次设置接口，其他管理 API 仍需先完成设置并登录。
- 管理密码不是 Provider API Key，不会作为上游请求凭据使用，也不改变 18006 代理接口的认证。

管理台登录使用 HttpOnly 会话 Cookie。HTTP 不提供传输加密，不要把管理端口暴露到公网或不可信网络。
推荐保持 `--host 127.0.0.1`，通过 SSH 隧道访问：

```bash
ssh -L 18080:127.0.0.1:18080 user@server
```

也可在可信的 HTTPS 反向代理后使用；反向代理应保留原始 `Host`，不要对管理 API 放开跨站访问。
管理写请求必须使用 `Content-Type: application/json`，浏览器跨站请求会被拒绝。

常规配置接口不再返回原始 API Key 或请求头凭据。编辑已有 Provider 时，页面只显示 Key 的头尾各 6 位，
中间部分用掩码代替；保持掩码或留空表示保留，填写完整新值表示替换，点击 Key 右侧“清除”按钮才会删除。
重命名、克隆、排序及获取模型仍可使用
服务端保存的凭据，不需要浏览器先取得明文。

本项目面向可信的家庭局域网，不开放公网。密钥保护主要防范上游错误回显、诊断日志及备份
意外泄漏，不对正常 AI 回复做全面密钥扫描：

- 上游 HTTP 状态码 `>=400` 的错误体按 `1 MiB` 限制读取；超大或无法解码时仅返回固定诊断，
  保留上游 HTTP 状态码，不回传未经检查的错误正文。
- 常见原生 HTTP 200 JSON 和 SSE 中的显式错误会在 `1 MiB` 窗口内检查、脱敏；
  正常正文保留，超出窗口且尚未确认为错误的内容继续原样透传。
  **检查窗口不是回复大小上限**，不会给正常 AI 回复增加 8/32 MB 等大小限制。
- 支持 gzip/deflate 的相应检查路径，但不保证 Brotli 全面覆盖；任意巨大或畸形内容、
  错误的 `Content-Type`、未知压缩中的错误仍可能漏检。正常成功内容中的敏感信息也会保留，
  因此不能把此保护视为所有响应均已脱敏的保证。

`GET /config/export` 默认导出脱敏配置，不能作为完整的凭据备份；界面的“导出 YAML”会先要求
确认，再通过已鉴权的 `POST /config/export`（`{"includeSecrets": true}`）导出完整配置。
这类导出及配置备份仍含原始密钥，**备份未加密**。当前管理台写入的备份仅以目录 `0700`、
文件 `0600` 限制本机访问，不能防止运行账户或管理员读取。
`.gitignore` 忽略默认配置及备份路径，但不保护另存、复制或分享的文件。
诊断脱敏不会追溯清洗旧日志，新写入的权限保护也不代表所有历史备份已修复；
分享任何导出、日志或备份前仍须检查并脱敏。

## 启动 AIProxy

```bash
python3 proxy.py --config config/config.yaml --listen 127.0.0.1 --port 18006
```

本地 Provider 入口：

```text
http://127.0.0.1:18006/{provider}/v1
```

示例：

```text
http://127.0.0.1:18006/provider-a/v1
```

默认端口为主通道 `18006`、专用通道 `18007`。自定义端口或部署拓扑时可显式指定：

```bash
python3 proxy.py --config config/config.yaml --listen 127.0.0.1 --port 18006 \
  --exclude-keepalive
python3 proxy.py --config config/config.yaml --listen 127.0.0.1 --port 18007 \
  --keepalive-only
```

### 请求统计

代理会把每次 Provider 请求写入 `data/request_stats.sqlite3`。管理台的“数据统计”
页面展示状态码、响应头/首字/总耗时、输入输出 Token、缓存读写 Token 以及费用估算。
费用只使用配置中的本地
模型价格，不配置价格时显示为未定价，不会伪造零费用。

### 真实 Codex 会话保活（显式启用）

在需要此模式的 Provider 上设置 `keepalive: true` 和
`keepalive_backend: codex_cli`；未设置时默认使用真实 Codex CLI。
需要 HTTP 模式时必须明确填写 `keepalive_backend: http`，不影响其他 Provider。
可用 `keepalive_codex_path` 指定 Codex 可执行文件（默认 `codex`）。
该模式仅支持 POSIX、本机交互式 Codex、原生 Responses 和 Bearer 认证，
不支持 `remove_headers`。不会在找不到 Codex 时悄悄退回 HTTP 探测。

冷启动用独立目录并发启动 Codex，输入保活题库中的一道题，按本次输入后的实际助手文本判定成功，
保留获胜进程及对话，关闭并清理其他进程。后续在同一对话发送短保活消息；
失效后关闭旧进程，以单并发重新抢通。沿用 Provider 的并发、重试间隔、保活间隔，
每次 CLI 检测期限取 `keepalive_timeout` 与 `keepalive_total_timeout` 的较小值。
尝试/失败计数仍按抢通轮次统计。
“首字（s）”显示最近成功探测的首段助手文字耗时：HTTP 流从发出请求计时，
CLI 从提交提示计时，不包含进程启动或回复后的稳定等待；没有文字时显示 `-`。
“启动时间”在本次保活任务启动时固定，“成功时间”随最近一次成功更新。

推理深度先读 `keepalive_model`（未指定时取第一个模型）的模型配置，再读 Provider
级 `reasoning_effort`；都未配置时不写入该项，由 Codex 决定默认值。
不会使用 HTTP 探测的 `keepalive_reasoning_effort` 或 32-token 输出限制。
隔离配置仅使用目标 Provider 的地址、Key 和请求头，不复制主 Codex 配置、登录、
历史、hooks 或 MCP；使用独立空工作目录、只读 sandbox 并关闭 shell 工具和网页搜索。
临时配置可能包含私有请求头，目录为 `0700`、配置为 `0600`，结束时清理；
Provider Key 通过子进程环境传入，不放入命令行或诊断。

CLI 直接连接目标上游，业务请求继续走原有代理，不合并到保活对话，
因此 CLI 探测不会计入代理的业务请求统计。跨 Key 的可用性收益依赖上游的账户/模型
调度策略，不能仅凭一个 CLI 会话成功就保证所有业务请求都成功。
Codex 本身可能额外发起标题生成等内部请求，一个探测轮次不等于一次 HTTP 请求。
保存模式后需重启所属代理才能切换，代码更新本身不会替换已运行的进程。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前回归测试覆盖 Codex `/responses` ↔ Chat `/chat/completions` 的工具调用转换、命名空间工具恢复、reasoning/`<think>` 保留、文件/音频内容块、流式 `tool_calls`/reasoning SSE 转换、上游错误转 Responses error/`response.failed`、`/responses` 流式 fallback 到 `/chat/completions`，以及 Codex `/responses` ↔ Anthropic `/messages` 的工具调用、thinking、usage 与流式 tool_use 转换和统计。

## 目录约定

运行目录建议保持：

```text
ai-api/
  api.py
  proxy.py
  prompts.py
  dashboard.py
  dashboard.html
  config/
    config.yaml
  backup/
  data/
  log/
```

- `backup/`：配置备份
- `data/`：运行状态
- `log/`：日志

## 注意

`config/config.yaml` 包含 API Key，不要提交到 GitHub。

### Telegram 抢通保活控制

在管理台的“抢通保活”卡片中填写 Telegram Bot Token 和 Chat ID，点击“保存 Telegram”即可启用机器人。配置会保存到私有文件 `config/telegram.json`，管理台运行期间会立即生效，不需要重启。

配置文件格式如下（也可以手动创建）：

```json
{
  "bot_token": "从 @BotFather 获取的 Bot Token",
  "chat_id": "你的个人或群组 Chat ID"
}
```

可复制根目录的 `telegram.example.json` 作为模板。机器人只接受配置的 `chat_id` 发来的指令：

- `/keepalive_on`：打开所有 Provider 的抢通保活
- `/keepalive_off`：关闭所有 Provider 的抢通保活

配置文件包含 Bot Token，权限应限制为当前用户可读（建议 `chmod 600 config/telegram.json`）。机器人通过 Telegram long polling 工作，不需要公网回调地址。

# Ai-api

本地公益服 Provider 管理与 AIProxy 工具。

用途：维护多个上游 API Provider，并通过本地固定入口给 Codex 使用。

## 文件

- `dashboard.py`：Web 管理台后端
- `dashboard.html`：Web 管理台前端
- `proxy.py`：AIProxy 转发与协议转换、抢通 + 保温
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

## 启动管理台

首次启动前，在服务器终端自行设置管理密码（输入不回显）：

```bash
python3 dashboard.py --set-password
```

然后启动：

```bash
python3 dashboard.py --host 127.0.0.1 --port 18080
```

访问：

```text
http://127.0.0.1:18080
```

### 管理台登录与密钥保护

管理台所有页面及 API 都需要 HTTP Basic 登录，包括本机访问：

- 用户名：`admin`
- 密码由你自行设置，**不生成随机密码、不设默认密码**。允许 5–128 个字符，不含控制字符，
  且不能全部为空白；可以使用中文或空格，建议至少 12 个字符。交互命令会要求输入两次，不回显密码。
- 仅保存带随机盐的 PBKDF2-SHA256 密码哈希（600,000 次迭代），默认文件为
  `config/dashboard-auth.json`，权限为 `0600`，不能从文件查看原密码。
  哈希文件同样需要私下保管，不要上传 Git 或分享。
- 登录后，点击网页右上角“修改登录密码”，输入当前密码及两次新密码即可修改。
  修改后旧密码立即失效（已通过认证的在途请求不撤回），重新登录时使用新密码；
  浏览器仍缓存旧 Basic 登录信息时，重新打开页面并在登录提示中输入新密码。
- 忘记密码时，在服务器终端重新执行 `python3 dashboard.py --set-password` 即可重设；
  该本机命令不需要旧密码，因此仅应允许可信用户访问服务器账户和密码文件。
  修改或重设都会被运行中的新版本管理台自动读取，无需重启。
- 自定义路径时，设置和启动都须使用同一个 `--auth-file /private/path/dashboard-auth.json`。
  不支持把密码放在命令参数、环境变量或管道中，避免泄漏到命令历史或进程信息。
  未设置密码或密码文件非法时，管理台拒绝启动，不会退回无认证模式，也没有开放的远程初始化接口。
- 管理密码不是 Provider API Key，不会作为上游请求凭据使用，也不改变 18006 代理接口的认证。

**HTTP Basic 不提供传输加密。** 不要把 HTTP 管理端口暴露到公网或不可信网络。
推荐保持 `--host 127.0.0.1`，通过 SSH 隧道访问：

```bash
ssh -L 18080:127.0.0.1:18080 user@server
```

也可在可信的 HTTPS 反向代理后使用；反向代理应保留原始 `Host`，不要对管理 API 放开跨站访问。
管理写请求必须使用 `Content-Type: application/json`，浏览器跨站请求会被拒绝。

常规配置接口不再返回原始 API Key 或请求头凭据。编辑已有 Provider 时，Key 留空表示保留，
填写新值表示替换，勾选“清除已保存的 Key”才会删除。重命名、克隆、排序及获取模型仍可使用
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

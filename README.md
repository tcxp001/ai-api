# Ai-api

本地公益服 Provider 管理与 AIProxy 工具。

用途：维护多个上游 API Provider，并通过本地固定入口给 Codex 使用。

## 文件

- `dashboard.py`：Web 管理台后端
- `dashboard.html`：Web 管理台前端
- `proxy.py`：AIProxy 转发与协议转换
- `api.py`：测活逻辑
- `config.example.yaml`：配置示例
- `requirements.txt`：依赖列表

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制示例配置：

```bash
cp config.example.yaml config.yaml
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

```bash
python3 dashboard.py --host 127.0.0.1 --port 18080
```

访问：

```text
http://127.0.0.1:18080
```

## 启动 AIProxy

```bash
python3 proxy.py --config config.yaml --listen 127.0.0.1 --port 18006
```

本地 Provider 入口：

```text
http://127.0.0.1:18006/{provider}/v1
```

示例：

```text
http://127.0.0.1:18006/provider-a/v1
```


## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前回归测试覆盖 Codex `/responses` ↔ Chat `/chat/completions` 的工具调用转换、命名空间工具恢复、reasoning/`<think>` 保留、文件/音频内容块、流式 `tool_calls`/reasoning SSE 转换、上游错误转 Responses error/`response.failed`、`/responses` 流式 fallback 到 `/chat/completions`，以及 Codex `/responses` ↔ Anthropic `/messages` 的工具调用、thinking、usage 与流式 tool_use 转换。

## 目录约定

运行目录建议保持：

```text
ai-api/
  api.py
  proxy.py
  dashboard.py
  dashboard.html
  config.yaml
  backup/
  data/
  log/
```

- `backup/`：配置备份
- `data/`：运行状态
- `log/`：日志

## 注意

`config.yaml` 包含 API Key，不要提交到 GitHub。

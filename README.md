# Ai-api

本地公益服 Provider 管理与 AIProxy 工具。

用途：维护多个上游 API Provider，并通过本地固定入口给 Codex / Hermes 使用。

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
    Originator: codex_cli_rs
    User-Agent: codex_cli_rs/0.139.0
  enabled: true
```

常用 `api_mode`：

- `codex_responses`：OpenAI `/responses`
- `chat_completions`：OpenAI `/chat/completions`
- `messages`：Anthropic `/messages`

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

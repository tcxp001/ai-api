# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Local API Provider manager + AIProxy protocol-conversion service (Python). Maintains many upstream API providers and exposes each behind a stable local entrypoint so Codex / Claude Code always speak one wire protocol while the proxy translates per-provider to whatever the upstream actually supports.

## Commands

```bash
pip install -r requirements.txt            # deps: requests, PyYAML (no build step)
cp config.example.yaml config.yaml         # create local config; edit secrets only here

python3 proxy.py --config config.yaml --listen 127.0.0.1 --port 18006   # run the proxy
python3 proxy.py --config config.yaml --check                           # validate config and exit
python3 proxy.py --config config.yaml --verbose                         # log every proxied request
python3 dashboard.py --host 127.0.0.1 --port 18080                      # run the web management UI

python3 -m unittest discover -s tests -v                    # full regression suite
python3 -m unittest tests.test_proxy_codex_chat -v          # one module
python3 -m unittest tests.test_proxy_codex_chat.ClassName.test_method -v   # single test
```

`config.yaml`, `.env`, `tests/`, `docs/`, `backup/`, `data/`, `log/` are gitignored (local-only).

## Architecture

Local entrypoints are `http://127.0.0.1:18006/{provider}/v1/...`. `ProxyHandler._route()` (in `proxy.py`) takes the first path segment as the provider name, looks it up in the loaded config, and strips `/v1` (the upstream `base_url` already includes `/v1`).

Each provider declares an `api_mode` that decides how an incoming request to local `/responses` is translated before forwarding:

- `codex_responses` — upstream natively supports OpenAI `/responses`; pass through.
- `chat_completions` — convert Responses ↔ OpenAI `/chat/completions` both ways (request body, streaming SSE, non-streaming JSON).
- `messages` — convert Responses ↔ Anthropic `/messages` (`auth_mode: anthropic`; sends `x-api-key` + `anthropic-version`).

The conversion layer is the bulk of `proxy.py` and the delicate part. It preserves tool calls (Responses `function_call`/`function_call_output` ↔ Chat `tool_calls`/`tool` messages ↔ Anthropic `tool_use`/`tool_result`), namespaced/custom/tool-search tools, reasoning and `<think>` blocks, image/file/audio content blocks, `tool_choice`, and merged mid-stream `system`/`developer` instructions. Upstream 4xx/5xx are rewritten into Responses JSON errors or `response.failed` SSE. There is also a streaming fallback from `/responses` to `/chat/completions` (`fallback_responses_to_chat`).

Request flow: `_route()` → `_handle()`. Streaming is re-emitted through the `_send_chat_stream_as_responses` / `_send_messages_stream_as_responses` state machines (nested classes tracking output-item indices, reasoning/text/tool blocks, and usage). `ProviderSessionPool` keeps a per-provider `requests.Session` pool for keep-alive.

**`dashboard.py` + `dashboard.html`** — separate ThreadingHTTPServer (default 18080) with a plain-JSON HTTP API and no frontend build step. Manages the provider list, writes `config.yaml` (backed up before write, written atomically), and generates client-side config: Codex model catalogs (`write_codex_model_catalog`) and Claude Code shell functions (`sync_claude_code_functions`) pointing at the local proxy. Endpoints: `/config`, `/providers/models`, `/health`, `/monitor/check`, `/checkins`, `/backups`, `/aiproxy`, `/app-configs/*`.

**`api.py`** — standalone provider liveness/capability checker ("测活"). Probes each provider's endpoints across variants and UA/header combos with a bounded worker pool (`run_limited_checks`) and renders result tables. Used by the dashboard's monitor/checkin features.

## Conventions

- Python 3, 4-space indent, `snake_case`. Keep dependency-light (stdlib + requests + PyYAML only).
- The three conversion paths (Responses, Chat, Anthropic Messages) are deliberately explicit and separate. Fix one `api_mode` without broad rewrites, and update the matching `tests/test_*.py` fixture for streaming events, tool calls, reasoning, content blocks, and error handling. Tests use local fake upstreams, not live network.
- Never commit `config.yaml`, `.env`, API keys, provider domains/tokens, logs, or backups. `config.example.yaml` uses `.invalid` hosts and `${ENV}` placeholders — keep examples sanitized.
- Commit style: short imperative subjects (e.g. `Handle empty upstream SSE streams`); explain behavior changes in the body when protocol compatibility is affected.

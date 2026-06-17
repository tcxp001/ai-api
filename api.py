import sys
sys.dont_write_bytecode = True

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
import unicodedata

import requests
import yaml

# ==========================================
# 顶部配置：选择 UA 测试方式
# ==========================================
# single：只用 UA_CHOICE 指定的一种 UA，输出 chat / responses / reasoning 耗时表
# matrix：六种 UA 全部测试，输出 Python / Curl / Chrome / 空UA / Codex / Claude Code 对比表
# 默认用 Py 默认 UA；如 provider 需要固定 UA，就在 config.yaml 的 headers 里显式写。
UA_TEST_MODE = "matrix"  # 可选: "single" / "matrix"
UA_CHOICE = 1            # single 模式生效：1 Python | 2 Curl | 3 Chrome电脑 | 4 空字符串 | 5 Codex | 6 Claude Code

# Provider 选择：直接写逗号分隔 name；空字符串表示全体候选。
PROVIDERS_TO_TEST = ""                    # 参与测试，例如: provider-a, provider-b；空 = 全部
PROVIDERS_TO_SKIP = ""  # 不参与测试；始终从参与名单里排除，例如: provider-a, provider-b

CONNECT_TIMEOUT = 15
CHAT_READ_TIMEOUT = 15
RESPONSES_READ_TIMEOUT = 30
REASONING_READ_TIMEOUT = 45
MAX_OUTPUT_TOKENS = 20
MAX_WORKERS = 20
INNER_MAX_WORKERS = 3
INNER_MAX_WORKERS_1_PROVIDERS = "any"
DEFAULT_TRUST_ENV_PROXY = False
CLAUDE_CODE_USER_AGENT = "claude-code/2.1.153 (linux; x64; node/v24.16.0)"

UA_PROFILES = {
    1: ("Python默认UA", None),
    2: ("Curl", "curl/8.0"),
    3: ("Chrome电脑", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
    4: ("空字符串", ""),
    5: ("Codex", {"Originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/0.139.0 (Debian 12.0.0; x86_64) xterm-256color"}),
    6: ("Claude Code", CLAUDE_CODE_USER_AGENT),
}
UA_ORDER = ["Python默认UA", "Curl", "Chrome电脑", "空字符串", "Codex", "Claude Code"]
TEST_UAS = {name: value for name, value in UA_PROFILES.values()}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")

print_lock = threading.Lock()

HTTP_ERROR_LABELS = {
    400: "参数/格式",
    401: "Key无效",
    403: "WAF/权限",
    404: "路径/模型",
    408: "请求超时",
    409: "请求冲突",
    412: "账号欠费",
    422: "参数/格式",
    429: "限流/额度",
    500: "服务端",
    502: "网关",
    503: "无通道",
    504: "网关超时",
}

EXCEPTION_SHORT_LABELS = {
    "ProxyError": "代理失败",
    "ConnectionError": "连接失败",
    "SSLError": "SSL失败",
    "ReadTimeout": "读超时",
    "ConnectTimeout": "连接超时",
    "RemoteDisconnected": "远端断开",
}

ENDPOINT_VARIANTS = [
    ("/chat/completions", "basic", "chat"),
    ("/messages", "basic", "messages"),
    ("/responses", "basic", "responses"),
    ("/responses", "reasoning", "reasoning"),
]


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"找不到配置文件，预期路径为: {CONFIG_FILE}")
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or []
        if isinstance(cfg, dict) and isinstance(cfg.get("providers"), list):
            return cfg["providers"]
        if isinstance(cfg, list):
            return cfg
        print(f"配置文件根节点必须是 provider 列表，实际是: {type(cfg).__name__}")
        return []
    except Exception as e:
        print(f"读取 YAML 配置文件失败: {e}")
        return []


def coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def normalize_models(provider):
    models = provider.get("models") or provider.get("model")
    if isinstance(models, str):
        return [models]
    if isinstance(models, list):
        return models
    if isinstance(models, dict):
        return list(models.keys())
    return []


def get_provider_identity(provider):
    return (
        provider.get("name", "未命名接口"),
        provider.get("base_url") or provider.get("url"),
        provider.get("api_key") or provider.get("key"),
        normalize_models(provider),
        provider.get("headers") or {},
        provider.get("remove_headers") or [],
        coerce_bool(provider.get("trust_env_proxy"), DEFAULT_TRUST_ENV_PROXY),
        str(provider.get("api_mode") or "").strip().lower(),
    )


def provider_name(provider):
    return str(provider.get("name") or "未命名接口").strip()


def normalize_provider_filter(value):
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
    else:
        parts = list(value)
    return [str(part).strip() for part in parts if str(part).strip()]


def provider_inner_max_workers(name):
    single_thread_providers = {
        provider.lower()
        for provider in normalize_provider_filter(INNER_MAX_WORKERS_1_PROVIDERS)
    }
    if str(name).strip().lower() in single_thread_providers:
        return 1
    return INNER_MAX_WORKERS


def resolve_provider_selection(providers, include=None, exclude=None):
    include_names = normalize_provider_filter(PROVIDERS_TO_TEST if include is None else include)
    exclude_names = normalize_provider_filter(PROVIDERS_TO_SKIP if exclude is None else exclude)
    include_all = not include_names or any(name.lower() == "all" for name in include_names)
    include_set = {name.lower() for name in include_names}
    exclude_set = {name.lower() for name in exclude_names}

    selected = []
    skipped = []
    for provider in providers:
        name = provider_name(provider)
        name_key = name.lower()
        enabled = coerce_bool(provider.get("enabled"), True)
        should_include = enabled and (include_all or name_key in include_set)
        if name_key in exclude_set:
            should_include = False
        if should_include:
            selected.append(provider)
        else:
            skipped.append(provider)
    return selected, skipped


def format_provider_names(providers):
    names = [provider_name(provider) for provider in providers]
    return ", ".join(names) if names else "无"


def print_provider_selection(selected_providers, skipped_providers):
    print(f"✅ 参与测试: {format_provider_names(selected_providers)}")
    print(f"⏭️  不参与测试: {format_provider_names(skipped_providers)}")
    print()


def build_headers(api_key, user_agent, provider_headers=None, remove_headers=None, auth_mode="bearer", anthropic_version="2023-06-01"):
    auth_mode = str(auth_mode or "bearer").strip().lower()
    headers = {"Content-Type": "application/json"}
    if auth_mode == "anthropic":
        if api_key:
            headers["x-api-key"] = str(api_key)
        headers["anthropic-version"] = str(anthropic_version or "2023-06-01")
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    # 同一份 config.yaml 里可给 provider 写额外 headers，例如 Originator。
    # 鉴权和 Content-Type 由测试脚本统一生成，避免配置里误覆盖 key。
    for key, value in (provider_headers or {}).items():
        key_s = str(key)
        if key_s.lower() in {"authorization", "content-type", "x-api-key", "anthropic-version"}:
            continue
        headers[key_s] = "" if value is None else str(value)

    for key in (remove_headers or []):
        for existing in list(headers.keys()):
            if existing.lower() == str(key).lower():
                headers.pop(existing, None)

    # None 表示“不主动修改 UA”：若 provider_headers 写了 User-Agent，就保留配置值；
    # dict 表示覆盖/追加一组测试 headers，例如 Codex 的 Originator + User-Agent。
    # 空字符串表示发送 User-Agent 头但值为空。
    if isinstance(user_agent, dict):
        for key, value in user_agent.items():
            key_s = str(key)
            if key_s.lower() in {"authorization", "content-type", "x-api-key", "anthropic-version"}:
                continue
            headers[key_s] = "" if value is None else str(value)
    elif user_agent is not None:
        headers["User-Agent"] = user_agent
    return headers


def endpoint_path(endpoint):
    return str(endpoint or "").split("?", 1)[0].rstrip("/")


def is_responses_endpoint(endpoint):
    return endpoint_path(endpoint).endswith("/responses")


def is_chat_endpoint(endpoint):
    return endpoint_path(endpoint).endswith("/chat/completions")


def is_messages_endpoint(endpoint):
    return endpoint_path(endpoint).endswith("/messages")


def build_payload(model, endpoint, variant="basic"):
    if is_responses_endpoint(endpoint):
        payload = {
            "model": model,
            "input": [{"role": "user", "content": "在吗？"}],
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        if variant == "reasoning":
            payload["reasoning"] = {"effort": "high", "summary": "auto"}
        return payload

    return {
        "model": model,
        "messages": [{"role": "user", "content": "在吗？"}],
        "max_tokens": MAX_OUTPUT_TOKENS,
    }


SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ak|pk|rk)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|authorization|token)[\s:=\"]+)[^\s,;\"'}]{8,}"),
]


def redact_sensitive(value, limit=None):
    text = "" if value is None else str(value)
    for pattern in SENSITIVE_VALUE_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "<redacted>", text)
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def response_text(response):
    text = getattr(response, "text", "") or ""
    return redact_sensitive(" ".join(text.split()))


def validate_success_response(response, endpoint):
    content_type = str(response.headers.get("content-type") or "").lower()
    body = response_text(response)
    lower_body = body.lower()
    if "text/html" in content_type or lower_body.startswith("<!doctype html") or lower_body.startswith("<html"):
        return False, "返回HTML"
    try:
        payload = response.json()
    except Exception:
        return False, "非JSON"
    if not isinstance(payload, dict):
        return False, "格式异常"
    if payload.get("error"):
        return False, "返回错误"
    if is_chat_endpoint(endpoint):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            return True, ""
        return False, "chat 格式异常"
    if is_messages_endpoint(endpoint):
        content = payload.get("content")
        if isinstance(content, list) or isinstance(content, str) or payload.get("stop_reason") or payload.get("role") == "assistant":
            return True, ""
        return False, "messages 格式异常"
    if is_responses_endpoint(endpoint):
        if payload.get("output_text") or isinstance(payload.get("output"), list) or payload.get("status") in {"completed", "in_progress"}:
            return True, ""
        return False, "responses 格式异常"
    return True, ""


def format_http_error(status_code, response):
    body = response_text(response)
    lower_body = body.lower()

    if "insufficient_user_quota" in lower_body or "用户额度不足" in body:
        return f"{status_code} 额度不足"
    if "invalid api key" in lower_body or "invalid token" in lower_body:
        return f"{status_code} Key无效"
    if "unauthorized client detected" in lower_body or "unauthorized_client" in lower_body:
        return f"{status_code} 客户端未授权"
    if "model_cooldown" in lower_body or "cooling down" in lower_body:
        return f"{status_code} 渠道冷却"
    if "no available channel" in lower_body or "无可用渠道" in body:
        return f"{status_code} 无模型通道"
    if "model_price_error" in lower_body or "价格尚未" in body or "has not been priced" in lower_body:
        return f"{status_code} 模型未定价/未启用"
    if "invalid url" in lower_body and "/responses" in lower_body:
        return f"{status_code} 不支持responses"
    if "insufficient account balance" in lower_body or "bad_response_status_code" in lower_body:
        return f"{status_code} 上游拒绝"

    return f"{status_code} {HTTP_ERROR_LABELS.get(status_code, 'HTTP错误')}"


def request_timeout_for(endpoint, variant="basic"):
    if is_chat_endpoint(endpoint) or is_messages_endpoint(endpoint):
        return CONNECT_TIMEOUT, CHAT_READ_TIMEOUT
    if is_responses_endpoint(endpoint) and variant == "reasoning":
        return CONNECT_TIMEOUT, REASONING_READ_TIMEOUT
    return CONNECT_TIMEOUT, RESPONSES_READ_TIMEOUT


def request_total_timeout_for(endpoint, variant="basic"):
    connect_timeout, read_timeout = request_timeout_for(endpoint, variant)
    return connect_timeout + read_timeout


def _check_endpoint_direct(base_url, api_key, model, endpoint, user_agent, variant="basic", provider_headers=None, remove_headers=None, trust_env_proxy=DEFAULT_TRUST_ENV_PROXY, auth_mode="bearer", anthropic_version="2023-06-01"):
    url = base_url.rstrip("/") + endpoint
    request_timeout = request_timeout_for(endpoint, variant)
    start_time = time.time()
    try:
        with requests.Session() as session:
            session.headers.clear()
            session.trust_env = trust_env_proxy
            response = session.post(
                url,
                headers=build_headers(api_key, user_agent, provider_headers, remove_headers, auth_mode=auth_mode, anthropic_version=anthropic_version),
                json=build_payload(model, endpoint, variant),
                timeout=request_timeout,
            )
        elapsed_time = time.time() - start_time
        if response.status_code == 200:
            ok, reason = validate_success_response(response, endpoint)
            if ok:
                return "✅ 成功", f"耗时 {elapsed_time:.2f}s"
            return "❌ 失败", reason
        return "❌ 失败", format_http_error(response.status_code, response)
    except requests.exceptions.ConnectTimeout:
        return "❌ 失败", f"连接超时(>{CONNECT_TIMEOUT}s)"
    except requests.exceptions.ReadTimeout:
        _, read_timeout = request_timeout_for(endpoint, variant)
        return "❌ 失败", f"读超时(>{read_timeout}s)"
    except requests.exceptions.Timeout:
        _, read_timeout = request_timeout_for(endpoint, variant)
        return "❌ 失败", f"请求超时(connect>{CONNECT_TIMEOUT}s/read>{read_timeout}s)"
    except Exception as e:
        return "❌ 失败", f"{type(e).__name__}: {str(e)[:100]}"


def check_endpoint(base_url, api_key, model, endpoint, user_agent, variant="basic", provider_headers=None, remove_headers=None, trust_env_proxy=DEFAULT_TRUST_ENV_PROXY, auth_mode="bearer", anthropic_version="2023-06-01"):
    total_timeout = request_total_timeout_for(endpoint, variant)
    payload = {
        "args": [base_url, api_key, model, endpoint, user_agent],
        "kwargs": {
            "variant": variant,
            "provider_headers": provider_headers,
            "remove_headers": remove_headers,
            "trust_env_proxy": trust_env_proxy,
            "auth_mode": auth_mode,
            "anthropic_version": anthropic_version,
        },
    }
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--check-endpoint-json"],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=total_timeout,
        )
    except subprocess.TimeoutExpired:
        return "❌ 失败", f"总超时(>{total_timeout}s)"
    except Exception as e:
        return "❌ 失败", f"{type(e).__name__}: {str(e)[:100]}"

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "子进程异常").strip().splitlines()[-1:]
        return "❌ 失败", detail[0][:100] if detail else "子进程异常"
    try:
        result = json.loads(completed.stdout)
        if isinstance(result, list) and len(result) == 2:
            return result[0], result[1]
    except Exception as e:
        return "❌ 失败", f"结果解析失败: {type(e).__name__}"
    return "❌ 失败", "结果格式异常"


def check_endpoint_child_main():
    payload = json.loads(sys.stdin.read() or "{}")
    result = _check_endpoint_direct(*payload.get("args", []), **payload.get("kwargs", {}))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def char_display_width(c):
    code = ord(c)
    if code == 0:
        return 0
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return 0
    if unicodedata.combining(c) or unicodedata.category(c) in {"Mn", "Me", "Cf"}:
        return 0
    if unicodedata.east_asian_width(c) in {"F", "W"}:
        return 2
    if 0x2600 <= code <= 0x27BF or 0x1F000 <= code <= 0x1FAFF:
        return 2
    return 1


def display_width(text):
    return sum(char_display_width(c) for c in str(text))


def pad(text, width):
    text = str(text)
    clipped = []
    current_width = 0
    for c in text:
        char_width = char_display_width(c)
        if current_width + char_width > width:
            if width >= 2:
                while clipped and current_width + 2 > width:
                    last = clipped.pop()
                    current_width -= char_display_width(last)
                clipped.append("..")
                current_width += 2
            break
        clipped.append(c)
        current_width += char_width
    return "".join(clipped) + " " * max(0, width - current_width)


def compact_result(status, detail, include_latency=False):
    if "成功" in status:
        if include_latency:
            return "✅ " + str(detail).replace("耗时 ", "")
        return "✅"

    if "跳过" in status:
        if detail:
            return compact_result("失败", detail, include_latency=include_latency)
        return "跳过"

    detail_text = str(detail or "")
    format_labels = {"chat格式异常": "chat 格式异常", "messages格式异常": "messages 格式异常", "responses格式异常": "responses 格式异常"}
    if detail_text in format_labels:
        return format_labels[detail_text]
    if detail_text.startswith("连接超时"):
        return f"连接>{CONNECT_TIMEOUT}s"
    if detail_text.startswith("读超时"):
        return detail_text.replace("读超时(>", "读>").replace(")", "")
    if detail_text.startswith("总超时"):
        return detail_text.replace("总超时(>", "总>").replace(")", "")
    if detail_text.startswith("请求超时"):
        return "超时"
    if detail_text in {"返回HTML", "非JSON", "格式异常", "返回错误", "chat 格式异常", "messages 格式异常", "responses 格式异常", "chat格式异常", "messages格式异常", "responses格式异常"}:
        return detail_text

    code, _, reason = detail_text.partition(" ")
    code = code.rstrip(":")
    if code.isdigit():
        label = reason.strip() or HTTP_ERROR_LABELS.get(int(code), "HTTP错误")
        return f"{code} {label.replace(' ', '')}"

    if ":" in detail_text:
        exc_name = detail_text.split(":", 1)[0]
        return EXCEPTION_SHORT_LABELS.get(exc_name, exc_name)

    if detail_text:
        return detail_text.replace(" ", "")
    return str(status).replace("❌ ", "").replace("✅ ", "")


def check_endpoint_compact(*args, **kwargs):
    status, detail = check_endpoint(*args, **kwargs)
    return status, detail, compact_result(status, detail, include_latency=True)


def skipped_result():
    status, detail = "⚠️ 跳过", "404 不支持responses"
    return status, detail, compact_result(status, detail, include_latency=True)


def untested_result():
    return "➖", "➖", "➖"


def is_success_result(result):
    return bool(result and "成功" in str(result[0]))


def all_failed(results):
    return not any(is_success_result(result) for result in results)


def is_responses_unsupported(result):
    if not result:
        return False
    return "不支持responses" in str(result[1]) or "不支持responses" in str(result[2])


def run_limited_checks(tasks, max_workers=None, progress_label=None, provider_headers=None):
    if not tasks:
        return ({}, []) if progress_label else {}
    max_threads = min(max_workers or INNER_MAX_WORKERS, len(tasks)) or 1
    results = {}
    progress_lines = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_key = {
            executor.submit(check_endpoint_compact, *args, **kwargs): key
            for key, args, kwargs in tasks
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e)[:100]}"
                results[key] = ("❌ 失败", detail, compact_result("❌ 失败", detail, include_latency=True))
            if progress_label:
                progress_lines.append(f"⏱️  {progress_label} {format_task_key(key, provider_headers)} -> {results[key][2]}")
    return (results, progress_lines) if progress_label else results


def format_task_key(key, provider_headers=None):
    if isinstance(key, tuple):
        if len(key) >= 3:
            model, test_name, ua_name = key[:3]
            return f"{model}/{test_name}/{ua_name}"
        if len(key) >= 2:
            model, test_name = key[:2]
            return f"{model}/{test_name}"
    return str(key)


def single_table_row(name, model, chat, responses, reasoning, messages):
    return f"| {pad(name, 10)} | {pad(model, 18)} | {pad(chat, 22)} | {pad(responses, 22)} | {pad(reasoning, 22)} | {pad(messages, 22)} |"


def matrix_table_row(name, model, test, py_default, curl, chrome, empty_ua, codex, claude_code):
    return f"| {pad(name, 10)} | {pad(model, 18)} | {pad(test, 11)} | {pad(py_default, 22)} | {pad(curl, 22)} | {pad(chrome, 22)} | {pad(empty_ua, 22)} | {pad(codex, 22)} | {pad(claude_code, 22)} |"



def table_separator(row, char="-"):
    return char * display_width(row)


def single_header_row():
    return single_table_row("服务商", "模型", "chat", "responses", "reasoning", "messages")


def matrix_header_row():
    return matrix_table_row("服务商", "模型", "测试", "Python", "Curl", "Chrome", "空UA", "Codex", "Claude Code")


def format_active_ua_value(ua_choice, active_ua):
    if active_ua is None:
        return "[不主动设置UA，requests默认]"
    if isinstance(active_ua, dict):
        return "[" + ", ".join(f"{k}={v!r}" for k, v in active_ua.items()) + "]"
    if active_ua == "":
        return "[发送空键值]"
    return f"'{active_ua}'"


def selected_ua():
    return UA_PROFILES.get(UA_CHOICE, UA_PROFILES[1])


def test_single_provider(provider):
    name, base_url, api_key, models_to_test, provider_headers, remove_headers, trust_env_proxy, _api_mode = get_provider_identity(provider)
    output_lines = []
    _, active_ua = selected_ua()
    inner_workers = provider_inner_max_workers(name)

    if not base_url or not api_key:
        output_lines.append(single_table_row(name, "-", "⚠️缺URL/Key", "-", "-", "-"))
    elif not models_to_test:
        output_lines.append(single_table_row(name, "-", "⚠️未配模型", "-", "-", "-"))
    else:
        common_kwargs = {
            "provider_headers": provider_headers,
            "remove_headers": remove_headers,
            "trust_env_proxy": trust_env_proxy,
        }
        reasoning_tasks = []
        for model in models_to_test:
            reasoning_tasks.append(((model, "reasoning"), (base_url, api_key, model, "/responses", active_ua), {**common_kwargs, "variant": "reasoning"}))
        reasoning_results = run_limited_checks(reasoning_tasks, inner_workers)

        responses_tasks = []
        responses_results = {}
        chat_results = {}
        for model in models_to_test:
            reasoning_result = reasoning_results.get((model, "reasoning"))
            if all_failed([reasoning_result]):
                responses_tasks.append(((model, "responses"), (base_url, api_key, model, "/responses", active_ua), common_kwargs))
            else:
                responses_results[(model, "responses")] = untested_result()
                chat_results[(model, "chat")] = untested_result()
        responses_results.update(run_limited_checks(responses_tasks, inner_workers))

        chat_tasks = []
        for model in models_to_test:
            if (model, "chat") in chat_results:
                continue
            responses_result = responses_results.get((model, "responses"))
            if all_failed([responses_result]):
                chat_tasks.append(((model, "chat"), (base_url, api_key, model, "/chat/completions", active_ua), common_kwargs))
            else:
                chat_results[(model, "chat")] = untested_result()
        chat_results.update(run_limited_checks(chat_tasks, inner_workers))

        messages_tasks = []
        messages_results = {}
        for model in models_to_test:
            chat_result = chat_results.get((model, "chat"))
            if all_failed([chat_result]):
                messages_tasks.append(((model, "messages"), (base_url, api_key, model, "/messages", active_ua), common_kwargs))
            else:
                messages_results[(model, "messages")] = untested_result()
        messages_results.update(run_limited_checks(messages_tasks, inner_workers))

        for model in models_to_test:
            chat = chat_results.get((model, "chat"), (None, None, "-"))[2]
            responses = responses_results.get((model, "responses"), (None, None, "-"))[2]
            reasoning = reasoning_results.get((model, "reasoning"), (None, None, "-"))[2]
            messages = messages_results.get((model, "messages"), (None, None, "-"))[2]
            output_lines.append(single_table_row(name, model, chat, responses, reasoning, messages))

    with print_lock:
        for line in output_lines:
            print(line)


def test_provider_ua_matrix(provider):
    name, base_url, api_key, models_to_test, provider_headers, remove_headers, trust_env_proxy, _api_mode = get_provider_identity(provider)
    output_lines = []
    progress_lines = []
    inner_workers = provider_inner_max_workers(name)

    if not base_url or not api_key:
        output_lines.append(matrix_table_row(name, "-", "-", "⚠️缺URL/Key", "-", "-", "-", "-", "-"))
    elif not models_to_test:
        output_lines.append(matrix_table_row(name, "-", "-", "⚠️未配模型", "-", "-", "-", "-", "-"))
    else:
        common_kwargs = {
            "provider_headers": provider_headers,
            "remove_headers": remove_headers,
            "trust_env_proxy": trust_env_proxy,
        }
        reasoning_tasks = []
        for model in models_to_test:
            for ua_name, ua_string in TEST_UAS.items():
                reasoning_tasks.append(((model, "reasoning", ua_name), (base_url, api_key, model, "/responses", ua_string), {**common_kwargs, "variant": "reasoning"}))
        reasoning_results, new_progress = run_limited_checks(reasoning_tasks, inner_workers, progress_label=f"{name} reasoning", provider_headers=provider_headers)
        progress_lines.extend(new_progress)

        responses_tasks = []
        responses_results = {}
        chat_results = {}
        for model in models_to_test:
            model_reasoning_results = [reasoning_results.get((model, "reasoning", ua_name)) for ua_name in UA_ORDER]
            if all_failed(model_reasoning_results):
                for ua_name, ua_string in TEST_UAS.items():
                    responses_tasks.append(((model, "responses", ua_name), (base_url, api_key, model, "/responses", ua_string), common_kwargs))
            else:
                for ua_name in UA_ORDER:
                    responses_results[(model, "responses", ua_name)] = untested_result()
                    chat_results[(model, "chat", ua_name)] = untested_result()
        new_responses, new_progress = run_limited_checks(responses_tasks, inner_workers, progress_label=f"{name} responses", provider_headers=provider_headers)
        responses_results.update(new_responses)
        progress_lines.extend(new_progress)

        chat_tasks = []
        for model in models_to_test:
            if all((model, "chat", ua_name) in chat_results for ua_name in UA_ORDER):
                continue
            model_responses_results = [responses_results.get((model, "responses", ua_name)) for ua_name in UA_ORDER]
            if all_failed(model_responses_results):
                for ua_name, ua_string in TEST_UAS.items():
                    chat_tasks.append(((model, "chat", ua_name), (base_url, api_key, model, "/chat/completions", ua_string), common_kwargs))
            else:
                for ua_name in UA_ORDER:
                    chat_results[(model, "chat", ua_name)] = untested_result()
        new_chat, new_progress = run_limited_checks(chat_tasks, inner_workers, progress_label=f"{name} chat", provider_headers=provider_headers)
        chat_results.update(new_chat)
        progress_lines.extend(new_progress)

        messages_tasks = []
        messages_results = {}
        for model in models_to_test:
            model_chat_results = [chat_results.get((model, "chat", ua_name)) for ua_name in UA_ORDER]
            if all_failed(model_chat_results):
                for ua_name, ua_string in TEST_UAS.items():
                    messages_tasks.append(((model, "messages", ua_name), (base_url, api_key, model, "/messages", ua_string), common_kwargs))
            else:
                for ua_name in UA_ORDER:
                    messages_results[(model, "messages", ua_name)] = untested_result()
        new_messages, new_progress = run_limited_checks(messages_tasks, inner_workers, progress_label=f"{name} messages", provider_headers=provider_headers)
        messages_results.update(new_messages)
        progress_lines.extend(new_progress)

        for model in models_to_test:
            output_lines.append(matrix_table_row(
                name,
                model,
                "chat",
                *(chat_results.get((model, "chat", ua_name), (None, None, "-"))[2] for ua_name in UA_ORDER),
            ))
            output_lines.append(matrix_table_row(
                name,
                model,
                "responses",
                *(responses_results.get((model, "responses", ua_name), (None, None, "-"))[2] for ua_name in UA_ORDER),
            ))
            output_lines.append(matrix_table_row(
                name,
                model,
                "reasoning",
                *(reasoning_results.get((model, "reasoning", ua_name), (None, None, "-"))[2] for ua_name in UA_ORDER),
            ))
            output_lines.append(matrix_table_row(
                name,
                model,
                "messages",
                *(messages_results.get((model, "messages", ua_name), (None, None, "-"))[2] for ua_name in UA_ORDER),
            ))

    return output_lines, progress_lines


def run_concurrently(providers, worker):
    max_threads = min(MAX_WORKERS, len(providers)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(worker, provider) for provider in providers]
        concurrent.futures.wait(futures)


def stream_concurrently(providers, worker):
    max_threads = min(MAX_WORKERS, len(providers)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_index = {
            executor.submit(worker, provider): index
            for index, provider in enumerate(providers)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                output_lines, progress_lines = future.result()
            except Exception as e:
                detail = f"{type(e).__name__}: {str(e)[:100]}"
                name = provider_name(providers[index])
                output_lines = [matrix_table_row(name, "-", "-", compact_result("❌ 失败", detail), "-", "-", "-", "-")]
                progress_lines = []
            yield index, output_lines, progress_lines


def run_single_mode(providers, skipped_providers=None):
    ua_name, active_ua = selected_ua()
    display_ua_val = format_active_ua_value(UA_CHOICE, active_ua)
    header = single_header_row()
    separator = table_separator(header)
    banner = table_separator(header, "=")

    print(banner)
    print("🚀 正在批量并发测试 API 接口...")
    print(f"ℹ️  [当前配置] UA测试方式: single  |  模式: {ua_name}  |  实际出站值: {display_ua_val}")
    print(f"ℹ️  [代理环境] trust_env_proxy 默认: {DEFAULT_TRUST_ENV_PROXY}；单个 provider 可显式覆盖")
    print(f"ℹ️  [超时] chat={CONNECT_TIMEOUT}/{CHAT_READ_TIMEOUT}s(硬{request_total_timeout_for('/chat/completions')}s) | responses={CONNECT_TIMEOUT}/{RESPONSES_READ_TIMEOUT}s(硬{request_total_timeout_for('/responses')}s) | reasoning={CONNECT_TIMEOUT}/{REASONING_READ_TIMEOUT}s(硬{request_total_timeout_for('/responses', 'reasoning')}s)")
    print(f"ℹ️  [并发] provider并发={MAX_WORKERS} | provider内默认={INNER_MAX_WORKERS} | provider内单线程={INNER_MAX_WORKERS_1_PROVIDERS or '无'}")
    print_provider_selection(providers, skipped_providers or [])
    print(banner)
    print()

    print(separator)
    print(header)
    print(separator)
    run_concurrently(providers, test_single_provider)
    print(separator)
    print(f"🎉 测试完毕！当前使用的 UA 策略为: {ua_name}\n")


def run_matrix_mode(providers, skipped_providers=None):
    header = matrix_header_row()
    separator = table_separator(header)
    print("🚀 正在启动多线程 WAF / User-Agent 嗅探测试...")
    print("ℹ️  [当前配置] UA测试方式: matrix  |  五种 UA/headers 逐项对比")
    print(f"ℹ️  [代理环境] trust_env_proxy 默认: {DEFAULT_TRUST_ENV_PROXY}；单个 provider 可显式覆盖")
    print(f"ℹ️  [超时] chat={CONNECT_TIMEOUT}/{CHAT_READ_TIMEOUT}s(硬{request_total_timeout_for('/chat/completions')}s) | responses={CONNECT_TIMEOUT}/{RESPONSES_READ_TIMEOUT}s(硬{request_total_timeout_for('/responses')}s) | reasoning={CONNECT_TIMEOUT}/{REASONING_READ_TIMEOUT}s(硬{request_total_timeout_for('/responses', 'reasoning')}s)")
    print(f"ℹ️  [并发] provider并发={MAX_WORKERS} | provider内默认={INNER_MAX_WORKERS} | provider内单线程={INNER_MAX_WORKERS_1_PROVIDERS or '无'}")
    print_provider_selection(providers, skipped_providers or [])
    print()
    print(separator)
    print(header)
    print(separator)
    for _index, output_lines, _progress_lines in stream_concurrently(providers, test_provider_ua_matrix):
        for line in output_lines:
            print(line)
        print(separator)
    print("🎉 嗅探完毕！如某个服务只在特定 UA 成功，就把 UA_TEST_MODE 改回 single 并设置 UA_CHOICE。\n")


def main(mode=None):
    providers = load_config()
    if not providers:
        return 1

    selected_providers, skipped_providers = resolve_provider_selection(providers)
    if not selected_providers:
        print_provider_selection(selected_providers, skipped_providers)
        print("没有 provider 参与测试，请检查 PROVIDERS_TO_TEST / PROVIDERS_TO_SKIP。")
        return 1

    selected_mode = (mode or UA_TEST_MODE).strip().lower()
    if selected_mode in {"single", "fixed", "one"}:
        run_single_mode(selected_providers, skipped_providers)
        return 0
    if selected_mode in {"matrix", "ua", "all", "probe"}:
        run_matrix_mode(selected_providers, skipped_providers)
        return 0

    print(f"UA_TEST_MODE 配置错误: {UA_TEST_MODE!r}，只能填 'single' 或 'matrix'")
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-endpoint-json":
        raise SystemExit(check_endpoint_child_main())
    raise SystemExit(main())

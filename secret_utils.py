"""Conservative, stdlib-only redaction for diagnostics, never for forwarding."""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

_REDACTED = "<redacted>"
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:api[-_]?key|x[-_]?api[-_]?key|key|token|access[-_]?token|"
    r"refresh[-_]?token|auth(?:orization)?|password|passwd|pwd|secret|"
    r"client[-_]?secret|api[-_]?secret|secret[-_]?key|access[-_]?key|"
    r"auth[-_]?token|subscription[-_]?key|credential|signature|sig|"
    r"x-amz-credential|x-amz-security-token|x-amz-signature|x-goog-signature)"
)
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://|//)([^/\s?#]*@)")
_QUERY_VALUE = re.compile(r"([?&;])([^=&#;\s]+)=([^&#;\s]*)")
_TOKEN = re.compile(r"(?i)\b(?:sk|ak|pk|rk)-[a-z0-9_-]+")
_AUTH = re.compile(r"(?i)(\b(?:bearer|basic)\s+)[^\s,;\"'<>]+")
_ASSIGNMENT = re.compile(
    r"""(?ix)(\b(?:api[-_]?key|x-api-key|authorization|proxy-authorization|
    access[-_]?token|refresh[-_]?token|token|password|passwd|pwd|secret|
    client[-_]?secret|credential|signature|cookie|set-cookie)\b
    ["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}"'<>]+)"""
)


def _text(value) -> str:
    return "" if value is None else str(value)


def _literal_pattern(secrets):
    if secrets is None:
        secrets = ()
    if isinstance(secrets, str):
        secrets = (secrets,)
    values = {_text(secret) for secret in secrets if secret is not None}
    values.discard("")
    if not values:
        return None
    return re.compile("|".join(re.escape(s) for s in sorted(values, key=lambda s: (-len(s), s))))


def _redact_part(text: str, literals) -> str:
    # Match known literals on the ORIGINAL text, longest first. Generic
    # patterns must not split a known secret and leave its unmatched suffix.
    spans = [m.span() for m in literals.finditer(text)] if literals else []
    # Keep existing markers intact even when a known secret is one character.
    spans.extend(m.span() for m in re.finditer(re.escape(_REDACTED), text))
    spans.extend((m.start(2), m.end(2) - 1) for m in _USERINFO.finditer(text))
    for match in _QUERY_VALUE.finditer(text):
        if _SENSITIVE_NAME.fullmatch(unquote_plus(match.group(2))):
            spans.append(match.span(3))
    spans.extend(m.span() for m in _TOKEN.finditer(text))
    for pattern in (_AUTH, _ASSIGNMENT):
        spans.extend((m.end(1), m.end()) for m in pattern.finditer(text))
    if not spans:
        return text
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    output = []
    previous = 0
    for start, end in merged:
        output.extend((text[previous:start], _REDACTED))
        previous = end
    output.append(text[previous:])
    return "".join(output)


def redact_text(value, secrets=(), limit=None) -> str:
    """Hide known literals (including one-character secrets) before truncation.

    Generic credential patterns are a fallback, not a way to discover arbitrary
    opaque keys. Pass the request/provider's known secrets whenever available.
    """
    literals = _literal_pattern(secrets)
    text = _redact_part(_text(value), literals)
    return text if limit is None else text[:max(0, int(limit))]


def redact_url(value, secrets=()) -> str:
    """Hide URL userinfo and sensitive query values, also in malformed URLs."""
    return redact_text(value, secrets)


def provider_secrets(provider) -> list[str]:
    """Collect credentials without mutating a raw or normalized provider dict.

    All configured header values are private for diagnostics, even values of
    headers whose names do not conventionally identify credentials.
    """
    if not isinstance(provider, dict):
        return []
    values: list[str] = []

    def add(value):
        text = _text(value)
        # requests/urllib3 may quote invalid header values in exception text,
        # and the proxy may quote that exception once more before logging.
        candidates = {text, text.strip()}
        rendered = text
        for _ in range(3):
            rendered = repr(rendered)
            candidates.add(rendered)
            candidates.add(rendered[1:-1])
        candidates.update({
            json.dumps(text, ensure_ascii=True),
            json.dumps(text, ensure_ascii=True)[1:-1],
        })
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)

    for key in ("api_key", "key"):
        add(provider.get(key))
    headers = provider.get("headers")
    if isinstance(headers, dict):
        for value in headers.values():
            add(value)
            auth = re.fullmatch(r"(?i)(?:bearer|basic)\s+(.+)", _text(value).strip())
            if auth:
                add(auth.group(1))
    for key in ("base_url", "url", "custom_endpoint", "endpoint"):
        value = _text(provider.get(key))
        if not value:
            continue
        try:
            parts = urlsplit(value)
            for credential in (parts.username, parts.password):
                if credential:
                    add(credential)
                    add(unquote(credential))
            if parts.username is not None:
                basic = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
                # requests.auth._basic_auth_str encodes string credentials as
                # Latin-1 for compatibility; mirror that exact wire token.
                try:
                    basic_bytes = basic.encode("latin1")
                except UnicodeEncodeError:
                    basic_bytes = basic.encode("utf-8")
                add(base64.b64encode(basic_bytes).decode("ascii"))
            for name, secret in parse_qsl(parts.query, keep_blank_values=True):
                if _SENSITIVE_NAME.fullmatch(name):
                    add(secret)
        except ValueError:
            # Invalid authorities should still not leak credentials in errors.
            for match in _USERINFO.finditer(value):
                for credential in match.group(2).rstrip("@").split(":"):
                    add(credential)
                    add(unquote(credential))
        for match in _QUERY_VALUE.finditer(value):
            if _SENSITIVE_NAME.fullmatch(unquote_plus(match.group(2))):
                add(match.group(3))
                add(unquote_plus(match.group(3)))
    return values

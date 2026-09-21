"""Bounded local event queue for keepalive notifications.

This is a delivery queue, not a history store. Events are removed after the
Telegram consumer acknowledges successful delivery, and the queue is bounded
so a broken Telegram connection cannot grow runtime state without limit.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


QUEUE_PATH = Path(__file__).with_name("data") / "keepalive-events.json"
MAX_EVENTS = 64
_thread_lock = threading.Lock()


def _read(path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else []
    return [item for item in events if isinstance(item, dict) and item.get("id")]


def _update(mutator) -> Any:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock:
        with QUEUE_PATH.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            events = _read_from_handle(handle)
            result = mutator(events)
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"events": events[-MAX_EVENTS:]}, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return result


def _read_from_handle(handle) -> list[dict[str, Any]]:
    try:
        handle.seek(0)
        payload = json.load(handle)
    except (OSError, ValueError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else []
    return [item for item in events if isinstance(item, dict) and item.get("id")]


def publish(provider: str, kind: str, detail: str = "") -> dict[str, Any]:
    event = {
        "id": uuid.uuid4().hex,
        "provider": str(provider or "Provider"),
        "kind": str(kind or ""),
        "detail": str(detail or ""),
        "at": time.time(),
    }

    def add(events):
        events.append(event)
        return dict(event)

    return _update(add)


def pending() -> list[dict[str, Any]]:
    return _update(lambda events: list(events))


def acknowledge(event_id: str) -> bool:
    target = str(event_id or "")

    def remove(events):
        before = len(events)
        events[:] = [event for event in events if str(event.get("id") or "") != target]
        return len(events) != before

    return bool(_update(remove))

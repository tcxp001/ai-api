"""Minimal Telegram control bot for ai-api keepalive."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests


LOG = logging.getLogger("ai-api.telegram")


class TelegramKeepAliveBot:
    """Long-poll Telegram and expose only keepalive on/off commands."""

    COMMANDS = [
        {"command": "keepalive_on", "description": "打开抢通保活"},
        {"command": "keepalive_off", "description": "关闭抢通保活"},
    ]

    def __init__(
        self,
        token: str,
        chat_id: str,
        set_keepalive: Callable[[bool], dict[str, Any]],
        *,
        poll_timeout: int = 30,
    ) -> None:
        self.token = str(token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.set_keepalive = set_keepalive
        self.poll_timeout = max(1, int(poll_timeout))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._session = requests.Session()
        self._base_url = f"https://api.telegram.org/bot{self.token}"
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monitored_states: dict[str, str] | None = None

    @classmethod
    def from_file(
        cls,
        path: Path,
        set_keepalive: Callable[[bool], dict[str, Any]],
    ) -> "TelegramKeepAliveBot | None":
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.error("无法读取 Telegram 配置: %s", type(exc).__name__)
            return None
        if not isinstance(payload, dict):
            LOG.error("Telegram 配置必须是 JSON 对象")
            return None
        token = str(payload.get("bot_token") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        if not token or not chat_id:
            LOG.warning("Telegram 配置不完整，需要 bot_token 和 chat_id")
            return None
        return cls(token, chat_id, set_keepalive)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self._api("setMyCommands", {"commands": self.COMMANDS}, (5, 15))
        except Exception:
            LOG.exception("Telegram 命令菜单注册失败")
        self._thread = threading.Thread(target=self._run, name="telegram-keepalive-bot", daemon=True)
        self._thread.start()
        LOG.info("Telegram keepalive bot started")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._monitor_stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._monitor_thread:
            self._monitor_thread.join(timeout=timeout)
        self._session.close()

    def reconfigure(self, token: str, chat_id: str) -> None:
        self.stop()
        self.token = str(token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0
        self._session = requests.Session()
        self._base_url = f"https://api.telegram.org/bot{self.token}"
        self._monitor_stop = threading.Event()
        self._monitor_thread = None
        self._monitored_states = None
        if self.token and self.chat_id:
            self.start()

    def set_controller(self, set_keepalive: Callable[[bool], dict[str, Any]]) -> None:
        self.set_keepalive = set_keepalive

    def _api(self, method: str, payload: dict[str, Any], timeout: tuple[float, float]) -> dict[str, Any]:
        response = self._session.post(
            f"{self._base_url}/{method}",
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise RuntimeError(f"Telegram API {method} failed")
        return data

    def _send(self, text: str) -> None:
        self._api("sendMessage", {"chat_id": self.chat_id, "text": text}, (5, 15))

    def send_message(self, text: str) -> None:
        """Send one message synchronously to the configured chat."""
        self._send(text)

    def start_keepalive_monitor(self, status_provider: Callable[[], dict[str, Any]], *, interval: float = 2.0) -> None:
        """Notify on keepalive acquisition and warm-session loss transitions."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitored_states = None

        def monitor() -> None:
            delay = max(float(interval), 0.5)
            while not self._monitor_stop.is_set() and not self._stop.is_set():
                try:
                    payload = status_provider()
                    providers = payload.get("providers") if isinstance(payload, dict) else {}
                    if not isinstance(providers, dict):
                        providers = {}
                    current = {
                        str(name): str((entry or {}).get("state") or "")
                        for name, entry in providers.items()
                        if isinstance(entry, dict)
                    }
                    previous = self._monitored_states
                    if previous is None:
                        self._monitored_states = current
                    else:
                        names = set(previous) | set(current)
                        for name in sorted(names):
                            old_state = previous.get(name, "")
                            new_state = current.get(name, "")
                            if old_state != "warm" and new_state == "warm":
                                self._send(f"{name} 抢通成功，已进入保活。")
                            elif old_state == "warm" and new_state != "warm":
                                state_text = new_state or "已停止"
                                self._send(f"{name} 从保活中断开，当前状态：{state_text}。")
                        self._monitored_states = current
                except Exception:
                    if not self._monitor_stop.is_set() and not self._stop.is_set():
                        LOG.warning("Telegram 保活状态通知检查失败", exc_info=True)
                self._monitor_stop.wait(delay)

        self._monitor_thread = threading.Thread(
            target=monitor,
            name="telegram-keepalive-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _handle_action(self, action: str) -> None:
        if action == "keepalive_on":
            result = self.set_keepalive(True)
            self._send(self._result_text("已打开抢通保活", result))
        elif action == "keepalive_off":
            result = self.set_keepalive(False)
            self._send(self._result_text("已关闭抢通保活", result))

    def _handle(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        incoming_chat_id = str(chat.get("id") or "")
        if incoming_chat_id != self.chat_id:
            return
        text = str(message.get("text") or "").strip().split()[0] if str(message.get("text") or "").strip() else ""
        if text.split("@", 1)[0] == "/keepalive_on":
            self._handle_action("keepalive_on")
        elif text.split("@", 1)[0] == "/keepalive_off":
            self._handle_action("keepalive_off")
        elif text in {"/start", "/help"}:
            self._send("可用命令：\n/keepalive_on\n/keepalive_off")

    @staticmethod
    def _result_text(prefix: str, result: dict[str, Any]) -> str:
        if result.get("ok") is True:
            changed = int(result.get("changed") or 0)
            return f"{prefix}\n已更新 Provider：{changed} 个"
        return f"操作失败：{str(result.get('error') or '未知错误')[:300]}"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": self.poll_timeout,
                        "allowed_updates": ["message"],
                    },
                    (5, self.poll_timeout + 10),
                )
                for update in data.get("result") or []:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    try:
                        message = update.get("message")
                        if isinstance(message, dict):
                            self._handle(message)
                    except Exception:
                        LOG.exception("处理 Telegram 指令失败")
            except Exception as exc:
                if not self._stop.is_set():
                    LOG.warning("Telegram 轮询失败: %s", type(exc).__name__)
                    self._stop.wait(5)

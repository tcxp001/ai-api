"""线程安全的轮换提示词。

旧短句仅保留给既有普通探测调用；抢通/保活使用独立的 200 道逻辑题库，
两条路径不会互相降级或混用。
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import threading

LEGACY_PROBE_PROMPTS: tuple[str, ...] = (
    "在吗？短回",
    "还在线吗？短答",
    "能收到吗？回一句",
    "现在通吗？短回",
    "还顺吗？短答",
    "方便吗？回短点",
    "收到就短回",
    "帮我确认下，短答",
    "能应一下吗？短回",
    "这边还通吗？短回",
    "OK吗？回一句",
    "可以了吗？短答",
    "还好吗？短回",
    "看看还在不，短回",
    "现在正常吗？短答",
    "能用吗？短回",
)
# 兼容普通探测代码及旧调用方；抢通/保活不再使用该常量。
KEEPALIVE_PROMPTS = LEGACY_PROBE_PROMPTS
KEEPALIVE_QUESTIONS_PATH = Path(__file__).with_name("keepalive_questions.json")


def load_keepalive_questions(path: Path = KEEPALIVE_QUESTIONS_PATH) -> tuple[str, ...]:
    """读取保活题库，只返回题目；参考答案永不进入请求内容。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items") if isinstance(payload, dict) else None
        questions = tuple(
            str(item.get("question") or "").strip()
            for item in (items or [])
            if isinstance(item, dict) and str(item.get("question") or "").strip()
        )
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"cannot load keepalive question bank: {type(exc).__name__}") from exc
    if len(questions) != 200 or len(set(questions)) != 200:
        raise RuntimeError("keepalive question bank must contain 200 unique questions")
    return questions


class PromptDeck:
    """线程安全的洗牌发牌轮换器：发完一副牌才重洗，不出现相邻重复。"""

    def __init__(self, prompts: tuple[str, ...] | list[str] = KEEPALIVE_PROMPTS, rng: random.Random | None = None) -> None:
        pool = [str(prompt).strip() for prompt in prompts if str(prompt).strip()]
        if not pool:
            raise ValueError("prompt pool must not be empty")
        self._pool = tuple(pool)
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._deck: list[str] = []
        self._last = ""

    def next(self) -> str:
        with self._lock:
            if not self._deck:
                self._deck = list(self._pool)
                self._rng.shuffle(self._deck)
                # pop() 从尾部取，所以 _deck[-1] 是下一张要发的牌。新洗的牌堆若正好
                # 以上一次发过的那句开头，就把它换到底部，避免跨牌堆边界的相邻重复。
                if len(self._deck) > 1 and self._deck[-1] == self._last:
                    self._deck[0], self._deck[-1] = self._deck[-1], self._deck[0]
            prompt = self._deck.pop()
            self._last = prompt
            return prompt


_default_deck = PromptDeck(LEGACY_PROBE_PROMPTS)
_keepalive_deck: PromptDeck | None = None
_keepalive_deck_lock = threading.Lock()


def next_prompt() -> str:
    """普通探测的旧短句入口（保留兼容，不供抢通/保活使用）。"""
    return _default_deck.next()


def next_keepalive_prompt() -> str:
    """从 200 道题的进程级共享牌堆取下一道保活题。"""
    global _keepalive_deck
    if _keepalive_deck is None:
        with _keepalive_deck_lock:
            if _keepalive_deck is None:
                _keepalive_deck = PromptDeck(load_keepalive_questions())
    return _keepalive_deck.next()

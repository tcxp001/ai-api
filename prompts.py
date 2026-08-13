"""轮换探活提示词，供 AIProxy 保温线程与 api.py 测活共用。

提示词照搬 atry(`/mnt/test/atry/main.go:100-117`)那 16 条中文短句：都要求“短回”，
所以每次探活的请求和回复都很小。

atry 用纯 `rand` 取词，可能连续两次取到同一句。这里改成洗牌发牌：一副牌按序发完再重洗，
保证每句都用过一次才会重复，并且跨牌堆边界也不出现相邻重复。
"""

from __future__ import annotations

import random
import threading

KEEPALIVE_PROMPTS: tuple[str, ...] = (
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


_default_deck = PromptDeck()


def next_prompt() -> str:
    """从进程级共享牌堆取下一句探活提示词。"""
    return _default_deck.next()

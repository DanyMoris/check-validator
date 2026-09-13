"""Список допущенных пользователей и ограничение частоты запросов."""

from __future__ import annotations

import time
from collections import defaultdict


class Allowlist:
    """Пустой список означает «пока пускаем всех» — удобно при первом запуске.

    Как только в ALLOWED_USER_IDS появится хотя бы один номер, вход закрывается
    для всех остальных.
    """

    def __init__(self, allowed: set[int]):
        self.allowed = set(allowed)

    @property
    def is_open(self) -> bool:
        return not self.allowed

    def permits(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.is_open:
            return True
        return user_id in self.allowed


class RateLimiter:
    """Не больше одного запроса от пользователя за заданный интервал."""

    def __init__(self, interval_seconds: float):
        self.interval = interval_seconds
        self._last: dict[int, float] = defaultdict(float)

    def check(self, user_id: int, now: float | None = None) -> float:
        """Возвращает 0, если можно продолжать, иначе сколько секунд ждать."""
        now = time.monotonic() if now is None else now
        elapsed = now - self._last[user_id]
        if elapsed < self.interval:
            return self.interval - elapsed
        self._last[user_id] = now
        return 0.0

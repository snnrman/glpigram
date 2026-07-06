"""Tiny async-safe TTL cache for a single computed value.

Used to cache the ITILCategory list for the /new dialog (10 min per CLAUDE.md).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLValue(Generic[T]):
    def __init__(self, loader: Callable[[], Awaitable[T]], ttl: float) -> None:
        self._loader = loader
        self._ttl = ttl
        self._value: T | None = None
        self._expires_at = 0.0

    async def get(self) -> T:
        now = time.monotonic()
        if self._value is None or now >= self._expires_at:
            self._value = await self._loader()
            self._expires_at = now + self._ttl
        return self._value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0

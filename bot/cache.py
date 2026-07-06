"""Tiny async-safe TTL cache for a single computed value.

Used to cache the ITILCategory list for the /new dialog (10 min per CLAUDE.md).
A lock prevents the thundering herd: on expiry, concurrent callers trigger one
loader call, not N parallel GLPI sweeps.
"""

from __future__ import annotations

import asyncio
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
        self._lock = asyncio.Lock()

    async def get(self) -> T:
        if self._value is not None and time.monotonic() < self._expires_at:
            return self._value
        async with self._lock:
            # A concurrent caller may have reloaded while we waited on the lock.
            now = time.monotonic()
            if self._value is None or now >= self._expires_at:
                self._value = await self._loader()
                self._expires_at = time.monotonic() + self._ttl
        return self._value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0

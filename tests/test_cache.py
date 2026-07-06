"""TTLValue: caching, expiry, invalidation, and loader-failure behaviour."""

from __future__ import annotations

import pytest

from bot.cache import TTLValue


class Loader:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return f"v{self.calls}"


async def test_caches_within_ttl():
    loader = Loader()
    cache = TTLValue(loader, ttl=60)
    assert await cache.get() == "v1"
    assert await cache.get() == "v1"
    assert loader.calls == 1  # second get served from cache


async def test_reloads_after_expiry(monkeypatch):
    loader = Loader()
    cache = TTLValue(loader, ttl=60)
    now = [1000.0]
    monkeypatch.setattr("bot.cache.time.monotonic", lambda: now[0])
    assert await cache.get() == "v1"
    now[0] += 61  # past the ttl
    assert await cache.get() == "v2"
    assert loader.calls == 2


async def test_invalidate_forces_reload():
    loader = Loader()
    cache = TTLValue(loader, ttl=60)
    await cache.get()
    cache.invalidate()
    assert await cache.get() == "v2"


async def test_loader_error_propagates_and_is_not_cached():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("glpi down")
        return "ok"

    cache = TTLValue(flaky, ttl=60)
    with pytest.raises(RuntimeError):
        await cache.get()
    # the failure must not be cached: next call retries the loader
    assert await cache.get() == "ok"

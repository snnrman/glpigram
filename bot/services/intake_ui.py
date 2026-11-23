"""Per-update presentation mode; no process-global menu changes across dispatchers."""

from __future__ import annotations

from contextvars import ContextVar

from aiogram import BaseMiddleware

single_text_mode: ContextVar[bool] = ContextVar("single_text_mode", default=False)


class IntakeUIMiddleware(BaseMiddleware):
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    async def __call__(self, handler, event, data):
        token = single_text_mode.set(self.enabled)
        try:
            return await handler(event, data)
        finally:
            single_text_mode.reset(token)

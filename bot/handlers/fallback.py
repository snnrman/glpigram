"""Last-resort handlers: stale buttons and unhandled handler exceptions.

* ``build_fallback_router`` — included **last** in the dispatcher, so its
  catch-all callback handler fires only for buttons no other router matched
  (stale keyboards after a restart cleared the FSM, wrong-state presses).
  Without it the client shows an endless spinner.
* ``register_error_handler`` — aiogram error hook: any exception that escapes a
  handler (SQLite failures, bugs) is logged with a traceback and the user gets
  a generic reply instead of silence. The polling loop itself never dies
  (CLAUDE.md).
"""

from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import Dispatcher, Router
from aiogram.types import CallbackQuery, ErrorEvent

from .. import texts

log = logging.getLogger(__name__)


def build_fallback_router() -> Router:
    router = Router(name="fallback")

    @router.callback_query()
    async def on_stale_callback(cb: CallbackQuery) -> None:
        log.info("stale_callback data=%s from=%s", cb.data, cb.from_user.id)
        await cb.answer(texts.STALE_BUTTON, show_alert=True)

    return router


def register_error_handler(dp: Dispatcher) -> None:
    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("handler_failed", exc_info=event.exception)
        # Best-effort user feedback; never raise from the error handler itself.
        with suppress(Exception):
            if event.update.callback_query is not None:
                await event.update.callback_query.answer(texts.GENERIC_ERROR, show_alert=True)
            elif event.update.message is not None:
                await event.update.message.answer(texts.GENERIC_ERROR)
        return True

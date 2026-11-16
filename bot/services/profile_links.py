"""Resolve Telegram ID mentions before sending or editing HTML messages.

Telegram can silently remove tg://user mentions when the user's forwarding
privacy forbids them outside their own chat. Prefer their public username;
when no Telegram link is available, link to the known GLPI profile instead.
Keeping this at the session boundary covers cards, details and approvals,
including re-renders of cards already stored with just a Telegram ID.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import EditMessageText, Response, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType

from ..db.repo import Repo

log = logging.getLogger(__name__)

_MENTION = re.compile(r'href="tg://user\?id=([1-9][0-9]*)"')
_USERNAME = re.compile(r"[A-Za-z0-9_]+\Z")


class ProfileLinksMiddleware(BaseRequestMiddleware):
    def __init__(
        self,
        repo: Repo,
        *,
        front_base: str | None,
        ttl: float = 600,
        now_provider: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._front_base = front_base.rstrip("/") if front_base else None
        self._ttl = ttl
        self._now = now_provider
        self._cache: OrderedDict[int, tuple[float, str | None]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def profile_url(self, bot: Bot, tg_id: int) -> str:
        """Use getChat, with a bounded cache and a short timeout for enrichment."""
        async with self._lock:
            cached = self._cache.get(tg_id)
            now = self._now()
            if cached is not None and cached[0] > now:
                url = cached[1]
                self._cache.move_to_end(tg_id)
            else:
                ttl = self._ttl
                try:
                    chat = await bot.get_chat(tg_id, request_timeout=3)
                    usernames = [chat.username, *(chat.active_usernames or [])]
                    username = next(
                        (name for name in usernames if name and _USERNAME.fullmatch(name)), None
                    )
                    if username:
                        url = f"https://t.me/{username}"
                    elif not chat.has_private_forwards:
                        url = f"tg://user?id={tg_id}"
                    else:
                        url = None
                except (TelegramAPIError, TimeoutError, OSError) as exc:
                    # A temporary lookup failure must not remove a known link
                    # or prevent a ticket notification. Retry on a later render.
                    url = cached[1] if cached is not None else None
                    ttl = 30
                    log.warning("profile_lookup_failed user=%s error=%s", tg_id, type(exc).__name__)
                self._cache[tg_id] = (self._now() + ttl, url)
                self._cache.move_to_end(tg_id)
                if len(self._cache) > 512:
                    self._cache.popitem(last=False)

        if url is not None:
            return url
        # Resolve the current mapping, never cache an old GLPI account link.
        link = await self._repo.get_by_tg(tg_id)
        if link is not None and self._front_base:
            return f"{self._front_base}/front/user.form.php?id={link.glpi_users_id}"
        # An unlinked account may have no accessible profile. Keep the original
        # mention rather than inventing a username or linking a different user.
        return f"tg://user?id={tg_id}"

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if isinstance(method, (SendMessage, EditMessageText)) and not method.entities:
            mode = method.parse_mode
            if isinstance(mode, Default):
                mode = bot.default.parse_mode
            if mode == "HTML":
                ids = dict.fromkeys(int(match[1]) for match in _MENTION.finditer(method.text))
                urls = {tg_id: await self.profile_url(bot, tg_id) for tg_id in ids}
                if urls:
                    text = _MENTION.sub(
                        lambda match: f'href="{html.escape(urls[int(match[1])], quote=True)}"',
                        method.text,
                    )
                    method = method.model_copy(update={"text": text})
        return await make_request(bot, method)

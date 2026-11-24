"""Requester links based on known Telegram or GLPI identity, never guessed names."""

from __future__ import annotations

import html
from urllib.parse import urlsplit, urlunsplit


def mention(
    name: str,
    tg_id: int | None,
    *,
    glpi_id: int | None = None,
    ticket_url: str | None = None,
) -> str:
    safe = html.escape(name)
    if type(tg_id) is int and tg_id > 0:
        return f'<a href="tg://user?id={tg_id}">{safe}</a>'
    if type(glpi_id) is int and glpi_id > 0 and ticket_url:
        parts = urlsplit(ticket_url)
        if parts.scheme in ("https", "http") and parts.netloc and "/front/" in parts.path:
            path = parts.path.rsplit("/front/", 1)[0] + "/front/user.form.php"
            profile = urlunsplit((parts.scheme, parts.netloc, path, f"id={glpi_id}", ""))
            return f'<a href="{html.escape(profile, quote=True)}">{safe}</a>'
    return safe

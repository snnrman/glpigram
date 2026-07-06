"""User-facing strings with a language switch.

``BOT_LANGUAGE`` (a real environment variable: ``ru`` | ``en``, default ``ru``)
selects the language module at import time; everything it defines is re-exported
here so the rest of the code keeps using ``from .. import texts`` / ``texts.FOO``.

Note: this reads ``os.environ`` directly (like ``HTTPS_PROXY``) — under systemd
the ``EnvironmentFile`` makes .env entries real environment variables.
"""

from __future__ import annotations

import os

_lang = os.environ.get("BOT_LANGUAGE", "ru").strip().lower()

if _lang == "en":
    from .en import *  # noqa: F401,F403
elif _lang in ("ru", ""):
    from .ru import *  # noqa: F401,F403
else:  # fail fast: a typo would silently fall back otherwise
    raise ValueError(f"BOT_LANGUAGE={_lang!r} is not supported (expected 'ru' or 'en')")


def custom_welcome() -> str | None:
    """Operator-defined /start greeting (``WELCOME_MESSAGE``), or None.

    Read at call time (not import time) so it is testable and reload-friendly.
    The value is used verbatim in both languages — no translation. aiogram HTML
    markup passes through, and a literal ``\\n`` becomes a line break (systemd
    EnvironmentFile values are single-line).
    """
    value = os.environ.get("WELCOME_MESSAGE", "").strip()
    if not value:
        return None
    return value.replace("\\n", "\n")

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import main
from bot.config import Settings


@pytest.mark.parametrize(
    "enabled,key,expected", [(False, "test", False), (True, "", False), (True, "test", True)]
)
async def test_optional_worker_startup_and_cleanup(tmp_path, monkeypatch, enabled, key, expected):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        glpi_api_url="http://example.invalid",
        glpi_user_token="test",
        db_path=str(tmp_path / "lifecycle.sqlite3"),
        ai_shadow_enabled=enabled,
        openai_api_key=key,
    )
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(main, "Bot", MagicMock(return_value=bot))
    glpi = AsyncMock()
    monkeypatch.setattr(main, "GlpiClient", MagicMock(return_value=glpi))
    ai = AsyncMock()
    ai.model = settings.openai_model
    factory = MagicMock(return_value=ai)
    monkeypatch.setattr(main, "OpenAIClassifier", factory)
    dp = MagicMock()

    async def poll(*args, **kwargs):
        await asyncio.sleep(0)

    dp.start_polling = AsyncMock(side_effect=poll)
    dispatcher = MagicMock(return_value=dp)
    monkeypatch.setattr(main, "build_dispatcher", dispatcher)
    sync = AsyncMock()
    monkeypatch.setattr(main, "SyncService", MagicMock(return_value=sync))
    await main._run(settings)
    dp.start_polling.assert_awaited_once()
    injected = dispatcher.call_args.kwargs["shadow"]
    assert (injected is not None) is expected
    if expected:
        factory.assert_called_once()
        ai.close.assert_awaited_once()
        assert injected.repo._db is None
    else:
        factory.assert_not_called()
    ai.classify.assert_not_awaited()
    bot.session.close.assert_awaited_once()
    glpi.close.assert_awaited_once()


def test_api_key_is_hidden_in_settings_repr():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="x",
        glpi_api_url="http://localhost",
        glpi_user_token="x",
        openai_api_key="private-api-key",
    )
    assert "private-api-key" not in repr(settings)

# Contributing

Thanks for considering a contribution! This project is small and pragmatic —
so is this guide.

## Development setup

```bash
git clone https://github.com/snnrman/glpi-tgbot.git
cd glpi-tgbot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install pytest pytest-asyncio pytest-cov respx ruff
```

Run the checks the CI runs:

```bash
venv/bin/ruff check . && venv/bin/ruff format --check bot tests
venv/bin/pytest
```

Tests use mocked HTTP (respx) and in-memory SQLite — **no live GLPI or Telegram
is needed** to develop.

## Ground rules

- **Python 3.11+**, type hints everywhere, `ruff` + `ruff format`, line
  length 100. CI must be green.
- **Conventional commits** (`feat:`, `fix:`, `test:`, `docs:`, …).
- **All GLPI HTTP goes through `bot/glpi/client.py`** — never call httpx from
  handlers or services. Client methods raise `GlpiError` subclasses with the
  raw API response attached; httpx exceptions must not leak.
- **Retry policy**: idempotent requests retry on network errors and 5xx;
  side-effecting POSTs retry only on clearly pre-execution failures.
- **User-facing strings live in `bot/texts/`** — add every new string to
  **both** `ru.py` and `en.py` (a test enforces that the key sets match).
- **Handlers stay thin**; put logic in `services/` and cover it with tests.
  New client methods and sync/service logic need unit tests.
- Errors in background loops are logged and the loop continues — never let a
  single bad ticket/user kill polling.

## Pull requests

1. Fork, create a branch from `main`.
2. Make the change + tests; keep the diff focused.
3. Make sure `ruff` and `pytest` pass locally.
4. Open a PR describing *what* and *why*; link related issues.

## Reporting bugs / requesting features

Please use the issue templates. For bugs, include the bot log lines
(`journalctl -u glpi-tgbot`) around the failure — the GLPI response body is
logged and usually pinpoints the problem.

# GLPI Telegram Bot

Telegram bot for IT service management on top of **GLPI 11.0.4** (legacy REST
API v1). Employees create and track tickets from Telegram; technicians manage
the queue. See `CLAUDE.md` for the full spec and roadmap.

## Status

Implemented (feature 1 — *Core client + `/new` dialog*):

- **GLPI client** (`bot/glpi/client.py`) — `initSession` with `App-Token` +
  `user_token`, transparent session re-init on `401` (retried once), ticket
  creation, ITILCategory listing with `Range`/`206` pagination. Retries with
  backoff on network errors / `5xx`; side-effecting `POST`s are retried only on
  clearly pre-execution failures (connect errors). Every failure surfaces as a
  `GlpiError` subclass with the raw API response attached — `httpx` exceptions
  never leak.
- **`/new` FSM dialog** (`bot/handlers/new_ticket.py`) —
  category → urgency → title → description → confirm → create. Categories come
  from GLPI and are cached 10 min. Replies with the ticket number and a link.
  A persistent reply keyboard and free-text "turn this into a ticket" flow wrap
  the dialog.
- **Account linking (feature 2, AD-based)** (`bot/handlers/linking.py`) —
  `/start` prompts unlinked users for their AD login; the bot resolves the
  active GLPI user by `name`, posts a confirmation card into the tech group, and
  a technician approves it with a button (group membership is the trust
  boundary). Mapping is stored in SQLite (`bot/db/`). An `AuthMiddleware`
  (`bot/middleware.py`) gates all business handlers behind a confirmed link,
  auto-unlinks accounts deactivated in GLPI (cached ~5 min), and refreshes
  `is_tech` from the configured GLPI group. Admin commands `/link` and
  `/unlink` (technicians only).
- systemd unit, `install.sh`, `.env.example`, and pytest/respx tests
  (client, repo, linking helpers).

Not yet implemented: `/tickets`, sync loop, tech actions, attachments
(features 3–6).

## Development

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -e ".[dev]"

ruff check bot tests && ruff format --check bot tests   # lint
pytest -x                                               # tests
```

Copy `.env.example` to `.env` and fill in the tokens to run locally:

```bash
cp .env.example .env      # then edit
python -m bot.main
```

Required env vars: `TELEGRAM_BOT_TOKEN`, `GLPI_API_URL`, `GLPI_USER_TOKEN`.
The bot fails fast at startup if any are missing. `GLPI_APP_TOKEN` is optional
(the localhost API client usually does not need one). Account linking also
needs `TECH_GROUP_CHAT_ID` (the Telegram group where requests are confirmed);
`TECH_GROUP_ID` (the GLPI group granting `is_tech`) and `LINK_RECHECK_TTL` are
optional. Set `HTTPS_PROXY` to route outbound traffic through a proxy (honoured
by both the GLPI client and the Telegram session).

## Deployment (systemd, no Docker)

On the GLPI host, as root:

```bash
sudo bash deploy/install.sh          # installs to /opt/glpi-tgbot, venv, service user
sudo nano /var/lib/glpi-tgbot/.env   # fill in real tokens
sudo systemctl restart glpi-tgbot
journalctl -u glpi-tgbot -f          # logs
```

`install.sh` creates the `glpibot` service user, a venv at
`/opt/glpi-tgbot/venv`, and keeps data (`.env`, SQLite) in `/var/lib/glpi-tgbot`.
The unit keeps only `NoNewPrivileges` — the host is an unprivileged LXC without
mount namespacing, so namespace-based sandboxing (`ProtectSystem`, `PrivateTmp`,
…) would fail the unit with `226/NAMESPACE` (see `CLAUDE.md`).

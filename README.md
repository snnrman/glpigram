# GLPI Telegram Bot

> 🇷🇺 [Читать по-русски](README.ru.md)

A Telegram bot for IT service management on top of **GLPI 11** (legacy REST API
v1). Employees create and track tickets straight from Telegram; technicians get
notified in a group chat and manage the queue with inline buttons — no GLPI-side
plugins, no inbound ports, long polling only.

## Features

- **Ticket creation** — a guided dialog: category (live from GLPI) → urgency →
  title → description → optional photo/document attachments → confirm. Free text
  sent to the bot offers to become a ticket, too.
- **Account linking (AD-based)** — users identify themselves by AD login *or*
  full name; a technician approves the link with one button in the tech group
  (anti-spoofing). Accounts disabled in GLPI/AD are unlinked automatically.
- **My tickets** — list of the user's open tickets with a detail view (status,
  assignee, recent comments, link to GLPI), adding comments and attachments,
  self-closing with a reason, and a rate-limited "remind the team" button.
- **GLPI → Telegram sync** — a polling loop announces new tickets to the tech
  group and pushes status changes / technician replies to the requester.
  Cursors are persisted in SQLite: restarts never duplicate notifications.
- **Technician actions** — Take / Comment / Close buttons right on the group
  card; solution and comment texts are collected in the technician's DM.
- **Quiet hours** — outside working hours, low-urgency notifications are queued
  and delivered next morning in a batch; urgent tickets go through immediately;
  requesters are told when the team will actually see their ticket.
- **Two languages** — all user-facing strings in Russian or English
  (`BOT_LANGUAGE`).

## Requirements

- **GLPI 11** (tested against 11.0.4) with the **legacy REST API** enabled
  (`apirest.php`) and a service account with an API token. The GLPI 11
  high-level API (`/api.php`, OAuth2) is *not* used.
- **Python 3.11+**
- Outbound HTTPS to `api.telegram.org` (an `HTTPS_PROXY` is honoured). No
  inbound connectivity needed — the bot uses long polling.
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a
  technician group chat.

## Installation

1. **Create the Telegram bot**: talk to @BotFather, `/newbot`, save the token.
   Add the bot to your technicians' group and note the group's chat id
   (e.g. via @getidsbot).

2. **Prepare GLPI**: enable the API (*Setup → General → API*), create a service
   account with rights to read users/tickets and create tickets/followups, and
   generate its **API token** (*user preferences → API tokens*). An App-Token
   is optional when the bot talks to GLPI over localhost.

3. **Install the bot** (as root on the GLPI host):

   ```bash
   git clone https://github.com/snnrman/glpi-tgbot.git /opt/glpi-tgbot
   cd /opt/glpi-tgbot
   sudo bash deploy/install.sh
   ```

   The script creates the `glpibot` service user, a venv at
   `/opt/glpi-tgbot/venv`, a data directory `/var/lib/glpi-tgbot` and a systemd
   unit `glpi-tgbot.service`.

4. **Configure**:

   ```bash
   sudo nano /var/lib/glpi-tgbot/.env    # fill in the tokens, see the table below
   ```

5. **Start**:

   ```bash
   sudo systemctl restart glpi-tgbot
   journalctl -u glpi-tgbot -f           # JSON logs
   ```

6. In Telegram: send `/start` to the bot, link your account, create a ticket.

## Configuration

All configuration comes from environment variables (`/var/lib/glpi-tgbot/.env`
under systemd). See [.env.example](.env.example).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TECH_GROUP_CHAT_ID` | for linking | — | Telegram group where linking is confirmed and tickets are announced |
| `GLPI_API_URL` | ✅ | — | Base URL of `apirest.php`, e.g. `http://127.0.0.1/apirest.php` |
| `GLPI_APP_TOKEN` | — | empty | GLPI App-Token; usually not needed over localhost |
| `GLPI_USER_TOKEN` | ✅ | — | API token of the GLPI service account |
| `GLPI_TIMEOUT` | — | `20` | HTTP timeout for GLPI calls, seconds |
| `TECH_GROUP_ID` | — | — | GLPI group id whose members count as technicians |
| `LINK_RECHECK_TTL` | — | `300` | Seconds between re-checks that a linked account is still active |
| `SYNC_INTERVAL` | — | `45` | GLPI polling interval, seconds |
| `REMIND_COOLDOWN_HOURS` | — | `4` | Min hours between reminders about the same ticket |
| `TZ` | — | system | Bot timezone (GLPI dates are UTC and converted to it) |
| `WORK_HOURS` | — | `09:00-18:00` | Working hours for quiet-hours handling |
| `WORK_DAYS` | — | `1-5` | Working ISO weekdays, Mon=1..Sun=7 |
| `QUIET_MIN_URGENCY` | — | `4` | Off-hours: urgency ≥ this bypasses the quiet queue (GLPI scale 1–5) |
| `BOT_LANGUAGE` | — | `ru` | UI language: `ru` or `en` |
| `CATEGORY_CACHE_TTL` | — | `600` | Category list cache, seconds |
| `DB_PATH` | — | `/var/lib/glpi-tgbot/glpi-tgbot.sqlite3` | SQLite database path |
| `LOG_LEVEL` | — | `INFO` | Logging level |
| `HTTPS_PROXY` | — | — | Optional outbound proxy (honoured by both httpx and aiogram) |

## Architecture

```
bot/
  main.py            # entrypoint: dispatcher, polling, sync task
  config.py          # pydantic-settings (env only)
  schedule.py        # working-hours model for quiet hours
  timeutil.py        # GLPI dates are UTC -> local conversions
  middleware.py      # auth gate: linked users only, auto-unlink
  texts/             # all user-facing strings (ru.py / en.py)
  glpi/
    client.py        # the only module that talks HTTP to GLPI
    models.py        # dataclasses for tickets, users, followups
  handlers/          # aiogram routers: linking, /new, /tickets, tech actions
  services/
    sync.py          # GLPI -> Telegram polling loop + quiet-hours queue
    notify.py        # message rendering/sending, Telegram edge cases
    attachments.py   # Telegram file handling (20 MB Bot API limit)
  db/
    schema.sql       # SQLite schema (links, cursors, queues)
    repo.py          # thin repository over one aiosqlite connection
deploy/              # systemd unit + install script
tests/               # pytest + respx (mocked HTTP), no live GLPI needed
```

Key design points:

- **All GLPI HTTP lives in `glpi/client.py`** — sessions are renewed
  transparently (including GLPI's undocumented 400-on-dead-token behaviour),
  retries are idempotency-aware, and every failure surfaces as a `GlpiError`
  with the raw API response attached.
- **State is SQLite** (aiosqlite, WAL): account links, sync cursors, quiet-hours
  queue, reminder cooldowns. The bot survives restarts without duplicating any
  notification.
- **The tech group is the trust boundary**: linking requests and destructive
  actions are confirmed by buttons that only work inside that group.

## Development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/pip install pytest pytest-asyncio pytest-cov respx ruff

venv/bin/ruff check . && venv/bin/ruff format --check bot tests
venv/bin/pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 Oleg K.

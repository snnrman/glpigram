# GLPI Telegram Bot

Telegram bot for IT service management on top of **GLPI 11.0.4** (legacy REST
API v1). Employees create and track tickets from Telegram; technicians manage
the queue. See `CLAUDE.md` for the full spec and roadmap.

## Status

All roadmap features (1–6) implemented:

- **GLPI client** (`bot/glpi/client.py`) — `initSession` with `App-Token` +
  `user_token`, transparent session re-init on `401` (retried once), ticket
  creation, ITILCategory listing with `Range`/`206` pagination. Retries with
  backoff on network errors / `5xx`; side-effecting `POST`s are retried only on
  clearly pre-execution failures (connect errors). Every failure surfaces as a
  `GlpiError` subclass with the raw API response attached — `httpx` exceptions
  never leak.
- **`/new` FSM dialog** (`bot/handlers/new_ticket.py`) —
  category → urgency → title → description → attachments → confirm → create.
  Categories come from GLPI and are cached 10 min. Replies with the ticket number
  and a link. A persistent reply keyboard and free-text "turn this into a ticket"
  flow wrap the dialog.
- **Account linking (feature 2, AD-based)** (`bot/handlers/linking.py`) —
  `/start` prompts unlinked users for their AD login; the bot resolves the
  active GLPI user by `name`, posts a confirmation card into the tech group, and
  a technician approves it with a button (group membership is the trust
  boundary). Mapping is stored in SQLite (`bot/db/`). An `AuthMiddleware`
  (`bot/middleware.py`) gates all business handlers behind a confirmed link,
  auto-unlinks accounts deactivated in GLPI (cached ~5 min), and refreshes
  `is_tech` from the configured GLPI group. Admin commands `/link` and
  `/unlink` (technicians only).
- **My tickets (feature 3)** (`bot/handlers/my_tickets.py`) — `/tickets` (or the
  "📋 Мои заявки" button) lists the requester's not-yet-closed tickets (number,
  title, status, assignee) via `/search/Ticket`; tapping one shows a detail view
  with the last 5 public followups; "add comment" collects text via FSM and posts
  a followup on behalf of the requester. A "✅ Закрыть заявку" button lets the
  requester close their own ticket: FSM asks for a reason and confirmation, then
  posts the reason as a followup, sets the ticket to Closed, and notifies the tech
  group (mentioning the assignee). Bot-authored followups/closes advance the
  ticket cursor so the sync loop doesn't echo them back to the requester.
- **Sync loop (feature 4)** (`bot/services/sync.py`, `notify.py`) — polls GLPI
  every `SYNC_INTERVAL` seconds: new tickets → tech group (with action buttons
  for feature 5), status changes on bot-created tickets → the requester, and new
  followups by others → forwarded to the requester. Cursors (`bot_tickets`,
  `sync_state`) live in SQLite so restarts don't duplicate notifications; on a
  fresh DB the ticket cursor is seeded to the current max id. The loop survives
  any per-tick error and continues.
- **Tech actions (feature 5)** (`bot/handlers/tech_actions.py`) — the tech-group
  card's buttons: **Take** (assign the pressing technician + move to
  *processing*), **Comment** (add a public followup), **Close** (add a solution
  → *solved*). Only linked technicians (`is_tech`) may act; others get a toast.
  Comment/Close collect free text via FSM in the technician's private chat
  (Telegram group privacy hides plain group messages), then edit the group card.
- **Attachments (feature 6)** (`bot/services/attachments.py`) — photos/documents
  from Telegram are uploaded to GLPI (multipart `Document` + `Document_Item`) on
  ticket creation (the `attaching` step of `/new`) and in comments (both the
  requester's and a technician's). Files over the Bot API's 20 MB `getFile` limit
  are rejected with a clear message.
- systemd unit, `install.sh`, `.env.example`, and pytest/respx tests
  (client, repo, linking helpers, sync logic, my-tickets rendering, attachments).

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

# GLPIgram

GLPIgram — a Telegram bot for IT service management, integrated with GLPI 11.0.4.
Company employees create and track tickets from Telegram; technicians
receive notifications and manage the queue with inline buttons.

## Environment & Constraints

- GLPI 11.0.4, self-hosted. The bot runs **on the same LXC container as GLPI** and talks to
  the API via localhost (`http://127.0.0.1/apirest.php` or the local vhost URL).
- Container is behind a firewall: **long polling only**, no inbound ports, no webhooks.
  Outbound HTTPS to api.telegram.org is required (support optional proxy via the standard
  `HTTPS_PROXY` env var — httpx and aiogram sessions must respect it).
- Use the **legacy REST API (v1, `/apirest.php`)** with `App-Token` + `User-Token` (service account).
  Do NOT use the GLPI 11 High-Level API (`/api.php`, OAuth2) — it is not yet stable for
  ticket tasks/followups. Isolate all GLPI calls in one client module (`glpi/client.py`)
  so a future migration to API v2 touches only that module.
- GLPI ↔ bot sync is done by polling the GLPI API (30–60 s interval). No GLPI-side plugins.
- **Unprivileged LXC — no mount namespacing.** systemd's namespace-based sandboxing
  (`ProtectSystem`, `PrivateTmp`, `ProtectHome`, `ProtectKernel*`, `ProtectControlGroups`,
  `ReadWritePaths`, `RestrictSUIDSGID`, …) fails the unit with `226/NAMESPACE`. The service
  unit keeps **only `NoNewPrivileges`**; do not re-add other hardening directives.

## Stack

- Python 3.11+ (system Python of the GLPI host; do not require 3.12-only features)
- aiogram 3.x (FSM for dialogs, inline keyboards)
- httpx (async) for GLPI API
- SQLite via aiosqlite — user mapping and sync state. No ORM; plain SQL with a thin helper.
- Config from environment variables only (pydantic-settings). `.env.example` in repo, `.env` gitignored.
- Deployment: **systemd unit + venv, no Docker**. Install to `/opt/glpi-tgbot`,
  venv at `/opt/glpi-tgbot/venv`, data (SQLite, .env) in `/var/lib/glpi-tgbot`,
  dedicated non-privileged user `glpibot`. Provide `deploy/glpi-tgbot.service`
  (Restart=always, EnvironmentFile=/var/lib/glpi-tgbot/.env, `NoNewPrivileges` — the only
  hardening directive that works here; see the LXC namespacing note above) and
  a short `deploy/install.sh`.
- Logging: stdlib `logging`, JSON-ish structured lines to stdout (journald/docker logs collect them).

## Architecture

```
bot/
  main.py            # entrypoint: dispatcher, polling, background tasks
  config.py          # pydantic-settings
  glpi/
    client.py        # GLPI API client: session init/refresh, retries, all endpoints
    models.py        # dataclasses for Ticket, Followup, User, Document
  handlers/
    new_ticket.py    # /new FSM dialog
    my_tickets.py    # /tickets list + detail view
    tech_actions.py  # technician inline-button callbacks
    linking.py       # TG <-> GLPI account linking
  services/
    sync.py          # polling loop: new tickets, status changes, new followups
    notify.py        # message rendering + sending (users and tech group)
  db/
    schema.sql
    repo.py
tests/
deploy/
  glpi-tgbot.service
  install.sh
.env.example
```

## GLPI API specifics (legacy v1)

- `initSession` with `App-Token` header + `user_token` -> `Session-Token`.
  Sessions expire; client must transparently re-init on 401 and retry once.
- Create ticket: `POST /Ticket` with `input: {name, content, itilcategories_id, urgency, ...}`.
  To set the requester to the real employee (not the service account), pass
  `_users_id_requester` on creation, or add a `Ticket_User` link (type 1 = requester).
- Followups: ITILFollowup with `itemtype: "Ticket"`, `items_id: <ticket_id>`.
- Attachments: `POST /Document` as multipart (`uploadManifest` + file), then link via
  `Document_Item` to the ticket.
- Listing/search: `GET /search/Ticket` with `criteria[]` (searchOptions IDs).
  Common ones: 12 = status, 4 = requester, 5 = technician. Verify IDs via
  `listSearchOptions/Ticket` before hardcoding, put them in constants with comments.
- `Range` header pagination on list endpoints; handle 206 responses.

## Features (build in this order)

1. **Core client + /new dialog.** FSM: category (inline buttons from GLPI ITILCategory list,
   cached 10 min) -> urgency -> title -> description -> optional photos/files ->
   confirm -> create ticket. Reply with ticket number and link.
   - **Urgency levels.** The keyboard lists the dedicated «🔴 Срочно (прод)»
     level (GLPI urgency 5, `URGENCY_URGENT`) FIRST, alone on its own row, then
     the three ordinary levels — «🟠 Высокая» (GLPI urgency 4), «🟡 Средняя» (3),
     «🟢 Низкая» (2) — then «❌ Отмена» last. Red 🔴 is reserved for «Срочно» as
     the visual marker of exclusivity; ordinary «Высокая» is orange 🟠 everywhere
     (buttons, cards, detail views). Selecting «Срочно» shows a warning
     («⚠️ Категория для срочных задач, связанных с продакшеном. Уведомление придёт
     команде в любое время суток…») with «Подтвердить» / «Отмена»; without
     confirmation the ticket is NOT created as urgent (declining returns to the
     level choices). Only «Срочно (прод)» breaks through quiet hours (see
     feature 4); ordinary «Высокая» does NOT.
2. **Account linking (AD-based).** GLPI users are synced from Active Directory via LDAP;
   `User.name` equals the AD sAMAccountName and is the linking key.
   `/start` requires linking. The user may send **either an AD login or their full name**:
   - **Login path** (single ASCII token, no spaces): strip any `login@domain` / `DOMAIN\login`,
     find the GLPI user by exact `name` (must be active: not deleted, is_active=1).
   - **Name path** (input has a space or Cyrillic): search GLPI users by partial,
     case-insensitive `realname`/`firstname` match (active only). **1** candidate → an
     "Это вы?" confirm button; **2–5** → an inline pick-list; **0** → ask for the AD login.
     The user's pick/confirm resolves which GLPI account they mean.
   In **all** cases the resolved account then goes to an admin for confirmation with a button
   in the tech group (anti-spoofing). Mapping stored in SQLite (tg_id, glpi_users_id, display
   name, is_tech). Unlinked users can do nothing except /start.
   - **Auto-unlink:** on every action by a linked user, verify (with a short cache, ~5 min)
     that the GLPI account is still active; if deactivated/deleted in GLPI (i.e. disabled
     in AD), silently unlink and answer as if the user was never linked. This is the
     offboarding path — no manual cleanup.
   - **is_tech from GLPI group:** technician rights are determined by membership in a
     configured GLPI group (env `TECH_GROUP_ID`), checked via API and cached ~5 min —
     not by a manually maintained flag. The SQLite is_tech column is a cache, refreshed
     on check.
   - Admin commands (usable only by techs): `/link <tg_id or @username reply> <ad_login>`
     to link someone manually, `/unlink <ad_login>` to remove a mapping.
3. **My tickets.** `/tickets` — the linked user's not-yet-closed tickets (statuses
   new/assigned/planned/waiting/solved): number, title, status, assignee.
   Tap -> detail view with the ticket description and the last 5 followups. Button
   "add comment" -> FSM -> followup created on behalf of the requester.
   - **Requester self-close:** detail view shows a "✅ Закрыть заявку" button for the
     user's own not-yet-closed tickets. Tapping it prompts "напишите причину или закройте
     без комментария" with a "Закрыть без комментария" button. No confirmation step:
     **any text is the reason** — add it as a followup on behalf of the requester, then
     close; the button closes immediately **without** a followup. Either way set the ticket
     to Closed (6) and notify the tech group ("закрыта заявителем: <reason>", or "закрыта
     заявителем без комментария"), mentioning the assigned technician if any. The sync loop
     must NOT echo this close back to the requester (advance the ticket's followup cursor
     past the reason and mark it inactive/closed in SQLite).
   - **Requester remind ("🔔 Напомнить о себе"):** detail view shows this button only for
     the user's own tickets still in status New (no technician assigned). Tapping posts
     "🔔 Заявитель напоминает о заявке #N: <title> (создана X ч назад)" to the tech group
     with the standard Take/Comment/Close buttons, and confirms to the requester. Rate-limited
     to once per `REMIND_COOLDOWN_HOURS` (default 4) per ticket — the last-remind time is
     stored in SQLite (`ticket_reminders`); an earlier retry gets "повторно можно через X ч"
     and nothing is sent. If the ticket was taken meanwhile (status ≠ New), the button/action
     is refused.
4. **Sync loop (GLPI -> TG).** Poll every 45 s:
   - **Unassigned reminder:** during working hours, New (unassigned) tickets older than
     `UNASSIGNED_REMIND_HOURS` (default 2 WORKING hours; GLPI's UTC dates converted via
     the schedule) are collected into ONE summary to the tech group — «⚠️ Заявки без
     исполнителя: №44 «...» (2ч)…» with a Take button per ticket (the regular take
     handler). Per-ticket anti-spam: not more often than `REMIND_INTERVAL_HOURS`
     (default 3 working hours), state in SQLite (survives restarts). A taken ticket
     stops matching status=New and drops out automatically.
   - new tickets (id > last_seen_id) -> notify tech group with inline buttons
     (the card shows the ticket **description**, see below)
   - status changes on tickets created via the bot -> notify the requester
   - new followups by others on the user's tickets -> forward text to the requester,
     **with any attached files**. A followup added from the GLPI web UI links its
     documents to the `ITILFollowup` item; the loop reads them
     (`list_followup_documents` -> `Document_Item`), downloads via the GLPI API
     (`download_document`) and forwards them alongside the text: images as a
     photo / media group, other files as documents, files above Telegram's upload
     cap (~50 MB) as a GLPI link. Best-effort throughout — a followup with no
     files, a lookup failure or one bad download never breaks the text. The
     followup cursor advances past every followup each tick, so attachments are
     never re-sent on later ticks.
   - **Attachments both ways.** Files a requester or technician attaches through
     the bot's own comment dialog are uploaded and linked to the *ticket* (not the
     followup), so the loop's followup-document forwarding would miss them — the
     comment handlers therefore forward the file directly to the counterpart
     (requester's file -> tech group; technician's file -> requester's DM) using
     the same `notify.send_attachments` helper.
   Persist cursor state (last ids / timestamps) in SQLite; survive restarts without
   duplicate notifications.
   - **Quiet hours (off-hours).** Config: `WORK_HOURS` ("09:00-18:00"), `WORK_DAYS`
     ("1-5", ISO Mon=1..Sun=7), timezone from `TZ` (`bot/schedule.py`). Off-hours
     **tech-group** notifications are held: any ordinary ticket (low/medium/**high**)
     is queued in SQLite (`deferred_notifications`, survives restart) instead of
     sent. The **only** level that breaks through and pings the group immediately,
     any time, is the dedicated «🔴 Срочно (прод)» level (`URGENCY_URGENT`, GLPI
     urgency 5) from the /new dialog — there is intentionally no urgency-threshold
     knob; the breakthrough is tied to that product-level choice, not a number.
     The first sync tick after work resumes flushes the backlog with a header
     "🌅 За нерабочее время поступило N заявок:" then the standard cards. Its
     card carries the explicit banner "🔴 СРОЧНО (прод)". "Напомнить о себе"
     off-hours is likewise deferred to the morning (its cooldown counts from the
     tap). **Requester-facing** messages — status changes, forwarded followups —
     are NOT affected by quiet hours. On ticket creation off-hours the requester
     is told when support will see it: ordinary urgency →
     "🌙 …увидит вашу заявку <в понедельник в 09:00>" (nearest work-day start);
     urgent (prod) → "заявка помечена как срочная (прод) …". In work hours nothing
     changes. GLPI dates are UTC; the bot converts to `TZ` for all hour math
     (`bot/timeutil.py`).
   - **Ticket description in cards / detail views.** The new-ticket group card and
     both detail views («Мои заявки», «👨‍💻 В работе») show the ticket `content`:
     bold title, then the description as plain text below it. GLPI stores the body
     as HTML — strip tags, decode entities, cap at ~200 chars with «…» (the full
     text stays behind the GLPI link), then re-escape for the HTML parse mode. An
     empty/markup-only description renders no line at all (`texts.description_block`).
     The living card stores the description (`ticket_cards.description`) so every
     re-render keeps it.
5. **Tech actions.** Inline buttons on the tech-group notification: "Take" (assign to the
   pressing technician, status -> Processing), "Close" (asks for a solution text via FSM),
   "Comment". Only users with is_tech may press; others get a toast.
   - **ITIL solution cycle:** a tech's "Close" sets the ticket to **solved (5), not closed**;
     the solution goes into ITILSolution. The requester gets «По заявке №N предложено
     решение — <техник>: <текст>. Проблема решена?» with «✅ Подтвердить» /
     «↩️ Вернуть в работу» buttons (same prompt when the ticket is solved from the GLPI
     web UI, detected by the sync loop). Confirm -> closed (6), thank-you to the requester,
     "заявитель подтвердил решение" to the card history + group ping. Return -> ask for a
     reason (text), status back to assigned (2), reason as a requester followup; the solving
     tech (DM, when linked) and the group get «↩️ Заявитель вернул заявку №N в работу:
     <причина>». Card buttons follow the status: solved -> Reply + passive «ждёт
     подтверждения»; closed -> only an "Open in GLPI" URL button. If the requester never
     reacts the ticket stays solved — GLPI's own auto-close timer may close it (the bot
     does nothing).
   - **Handoff («🔄 Передать»):** on taken cards and in the «👨‍💻 В работе» detail view
     (techs only, is_tech re-checked in handlers). The pick list — all linked techs from
     SQLite (is_tech cache of TECH_GROUP_ID membership) — is sent to the pressing tech's
     DM, never the group. Picking one calls `reassign_ticket` (drop old Ticket_User
     type=2 links, add the new one, status stays assigned/2). Notifications: new tech's
     DM («на вас переназначена…» with title + urgency; skipped on self-handoff),
     requester's DM («вашу заявку теперь ведёт…»), card history «🔄 Передано: кто → кому»
     + the card header now renders the assignee («🙋 Исполнитель: …») on every edit.
6. **Attachments both ways.** Photos/documents from TG uploaded to GLPI on ticket creation
   and in comments. TG file size limits apply (20 MB via Bot API) — reject larger with a
   clear message. The reverse direction is covered by the sync loop / comment handlers
   (feature 4): files on a technician's followup reach the requester, and files a requester
   attaches reach the tech group — images as photo/media group, other files as documents,
   oversized ones as a GLPI link.

7. **Role-based menu, /stats, «👨‍💻 В работе».** The persistent reply menu is built per
   role at render time (after /start and after every finished dialog): everyone gets
   «🆕 Новая заявка» + «📋 Мои заявки»; technicians (is_tech, refreshed from the GLPI
   group by the auth middleware, ~5 min cache) additionally get a second SHORT
   two-button row (labels must not wrap): «👨‍💻 В работе» + «📊 Статистика».
   - «👨‍💻 В работе» lists the tech's open assigned tickets (Ticket_User type=2,
     searchOption 5) in two groups — «В работе» (assigned/processing/waiting) and
     «Ждут подтверждения» (solved); empty -> «На вас нет активных заявок». Tapping a
     ticket opens a detail view whose Reply/Close buttons reuse the `ta:` callbacks
     (same DM dialogs and living-card updates as the group card); solved tickets hide
     Close. Handlers re-check is_tech themselves.
   - «📊 Статистика» -> /stats — an open-queue
   breakdown by status (one paginated `/search/Ticket` with the virtual status value
   `notclosed`, grouped client-side). Hiding the button is NOT the access control: the
   handler re-checks is_tech itself, so a direct /stats (or the button text typed by
   hand) is refused for regular users. A role change in the GLPI group updates the menu
   on the user's next interaction.

Out of scope for now: SLA warnings, Claude-based auto-classification, multi-entity support.
Keep the code structured so these can be added later.

## Dialog cancellation

Every FSM state that awaits text input (comment, solution, close reason, return
reason, /new title & description, login) must offer an explicit way out: an inline
«❌ Отмена» button on the prompt (callback `dlg:cancel`, `nt:cancel` inside /new) AND
the /cancel command. The prompt text mentions the button («…или нажмите Отмена»).
On cancel: clear the state, reply «❌ Отменено.» with the role menu attached
(tech buttons for techs), never touch the ticket in GLPI.

## Group hygiene

In group chats the bot reacts ONLY to its inline buttons (Take/Reply/Close, linking
approvals) and the explicit admin commands /link and /unlink. Free text, menu-button
texts, /start and every FSM dialog are private-chat only — router-level
`router.message.filter(F.chat.type == "private")` on all business routers (linking:
per-handler, so the admin commands keep working in the group). A tech's solution text
with confirm/return buttons goes ONLY to the requester's DM; the group card gets just a
history line («Решение предложено: <техник>»), never the solution body.

## Conventions

- All user-facing strings live in `bot/texts/` (`ru.py` + `en.py`, selected by the
  `BOT_LANGUAGE` env var, default ru; no i18n framework). Every new string goes into
  BOTH files — a test enforces that the key sets match.
- Every GLPI client method: typed signature, raises `GlpiError` subclass with the raw API
  response attached; never leaks httpx exceptions to handlers.
- Retries: 3 attempts with backoff on network errors and 5xx; never retry POSTs that may
  have side effects unless the error is clearly pre-execution (connect timeout).
- All secrets (bot token, GLPI tokens) only from env. Fail fast at startup if any missing.
- Tests: pytest + respx for the GLPI client (mock HTTP), plain unit tests for services.
  Handlers are tested minimally; the client and sync logic thoroughly.
- Type hints everywhere, `ruff` + `ruff format`, line length 100.
- Conventional commits.

## Commands

```
ruff check . && ruff format --check .   # lint
pytest -x                               # tests
sudo systemctl restart glpi-tgbot       # apply after git pull + pip install
journalctl -u glpi-tgbot -f             # logs
```

## Definition of done for each feature

- Works against the live GLPI instance (I test manually and paste errors back)
- Unit tests for the new client methods / service logic pass
- No unhandled exceptions in the polling loop: any error is logged and the loop continues
- README section updated if setup steps changed

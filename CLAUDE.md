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
- **A 201 from `POST /Document` is not proof the file was stored.** If the extension is
  not a registered DocumentType (Setup > Dropdowns > Document types), GLPI 11 still
  creates an empty Document row (NULL filename/sha1sum) and only reports the refusal in
  `upload_result.filename[0].error` («Тип файла не разрешен»). `upload_document` parses
  that, purges the stub (`DELETE /Document/{id}?force_purge=true`) and raises
  `GlpiDocumentRejected(filename, reason)`; handlers tell the user to rename to `.txt`
  or paste as text (`texts.attach_rejected` / `attachments_rejected`) and never post a
  followup for a rejected file. Bit us on ticket #349 (`*.pub` SSH key silently lost).
- Listing/search: `GET /search/Ticket` with `criteria[]` (searchOptions IDs).
  Common ones: 12 = status, 4 = requester, 5 = technician. Verify IDs via
  `listSearchOptions/Ticket` before hardcoding, put them in constants with comments.
- `Range` header pagination on list endpoints; handle 206 responses.

## Features (build in this order)

### Optional single-text intake

`AI_INTAKE_ENABLED` defaults to false. The legacy feature descriptions below still
apply when it is off. Enabling the new form requires `AI_INTAKE_CATEGORY_IDS`, a
JSON object mapping `accounts`, `network`, `hardware`, `software`, `infrastructure`,
`purchases`, `other` to seven distinct positive GLPI category IDs. Prepare new
categories matching `services/intake.py:CATEGORY_NAMES`; do not rename historical
categories or reclassify existing tickets. IDs are installation-specific and are
never created automatically by starting the bot. Submission and correction verify
the current ID and normalized leaf name, rejecting deleted/mismatched categories.

The new flow is description -> urgency -> attachments -> send. Free-text entry
reuses the original message. The separate paid-services menu is hidden in this
mode; old /access and menu entry points start the new form. Existing lead approval
callbacks remain routed. There is no additional requester confirmation screen.
One AI response supplies a short factual title, work category, environment and
approval recommendation. Full original text remains GLPI content; the generated
title is used in GLPI/lists. New Telegram cards show the description once.
Review history uses its own localized formatter rather than emoji-bearing button labels.

For a limited pilot, set `AI_INTAKE_PILOT_TG_IDS` to a JSON array of trusted numeric
Telegram IDs and `AI_INTAKE_LEGACY_CATEGORY_IDS` to the existing category IDs.
An empty pilot set preserves the all-user meaning of `AI_INTAKE_ENABLED=true`;
always verify the configured set before a limited rollout. Separate message and
callback router filters enforce membership using Telegram's sender identity.
The same eligibility rule controls menus, including menus sent to another user.
The legacy flow excludes reserved intake categories and validates fresh allowed
IDs again before creation. The explicit legacy list also hides a new taxonomy
parent without changing existing GLPI category names or defaults.

Keep new pilot categories hidden from GLPI self-service. Missing metadata in a
reserved category is recovered only when its live requester is a linked pilot
user; an unavailable identity check must stop the action, not allow a bypass.
Existing metadata is never discarded because pilot membership or the form flag
changed. Keep the category map and pilot settings when disabling new submissions
so existing intake tickets can finish their review and approval lifecycle.

`AI_INTAKE_MODEL` inherits `OPENAI_MODEL` when empty. Timeout, input limit and daily
limit are bounded; calls reserve a durable UTC-day budget before a single POST.
No retries, tools, URL fetching or retained provider response storage are used.
Missing credentials, exhausted budget, oversized input, invalid JSON and network
failure produce an explicit fallback with an excerpt title and the Other category.
AI fallback is NOT an ordinary/approved decision; every new ticket awaits a tech.
The new flow does not enqueue a second shadow-classification call.

Category, environment, urgency and approval are independent. Production covers
the agreed running game services, delivery/API dependencies, public websites,
production data systems and operational monitoring. Jenkins/build/repository and
explicit dev/test contexts are development. Undecided internal scope stays unknown.
Urgency is always requester-selected. The pilot asks how the problem affects work:
2 Can continue working, 3 Work is disrupted, 4 Work is blocked, 5 Production incident.
The four buttons run from low to high, with explanations separated by blank lines.
Only 5 bypasses quiet hours, after an explicit live-production confirmation; a
pending selection marker rejects stale/direct confirmation callbacks. Declining
returns to the same choices. Cards, details and handoff share the impact labels.
The selection does not override the independently classified AI environment.
Legacy urgency labels and behavior remain unchanged for employees outside the pilot.
Queue notices are a separate optional feature described below.

Every new intake ticket has durable metadata and technician review history separate
from the 30-day shadow table. Pending review blocks take, resolve and handoff;
comments remain available. The technician chooses Ordinary or Lead approval and
may independently correct the category. Lead approval invokes the existing
technician approval dialog and validation recovery, not an AI-triggered message.
Version checks, per-ticket locks and fresh GLPI reads protect stale buttons.
Read GLPI and refresh its intake snapshot under the SAME ticket action lock;
locking only after an earlier read can undo a newer technician category decision.
Text changes invalidate review and retain the original/model output in the audit.
When a granted approval already exists, an explicit technician recheck may confirm
that it still covers the edited text; pending/refused approvals cannot be bypassed.
The existing one-approval-cycle constraint remains: a new purchase requiring a new
approval after a completed cycle needs a separate request. No validation is revoked
or replaced automatically by an AI or snapshot change.
Legacy GLPI category PUT has no conditional-write/CAS support: a GET before and
after PUT detects most conflicts but cannot eliminate the final external race.

Prepare and test changes in an isolated checkout. Rollout requires a separate
operator action: back up code/config/SQLite, create and inspect the new category
IDs, configure the complete mapping, then enable the flag. Disabling the flag
returns new submissions to the legacy form; retain the mapping and additive
tables so existing intake review buttons and records remain usable. Do not restore
an old SQLite snapshot over newer tickets or decisions. Merely installing this
code does not create GLPI categories, rewrite old tickets or turn on AI intake.

### Optional queue notices

`WORKLOAD_NOTICE_ENABLED=false` by default. When enabled it requires either
`WORKLOAD_NOTICE_ALL_USERS=true` or a nonempty `WORKLOAD_NOTICE_PILOT_TG_IDS`.
The explicit all-users flag defaults to false; an empty pilot list alone never
enables all users. The dispatcher attaches notices only to the new intake router
and the service checks the authenticated requester again. Global intake plus
global notices includes employees linked after startup, without maintaining an ID
list. For a full rollout set `AI_INTAKE_ENABLED=true`, `AI_INTAKE_PILOT_TG_IDS=[]`,
`WORKLOAD_NOTICE_ENABLED=true` and `WORKLOAD_NOTICE_ALL_USERS=true`.
After a global rollout, safe rollback disables `AI_INTAKE_ENABLED` and
`WORKLOAD_NOTICE_ENABLED`, retaining the empty intake pilot list, category mappings
and persisted review/approval data. This keeps recovery available for all reserved
category tickets, including a successful GLPI creation whose local metadata write
failed. Do not narrow intake recovery back to a pilot until those tickets have been
reconciled. Workload scope alone can return to its pilot by setting
`WORKLOAD_NOTICE_ALL_USERS=false` with a nonempty workload pilot list.

After the real ticket number has been delivered, a bounded best-effort lookup may
send one queue notice. Three to five other unassigned tickets produce the yellow
warning; six or more produce orange. No green availability claim, ETA or FIFO
position is shown. Notices mention only the queue: urgency never adds paragraphs
or independently triggers a notice. Category and AI environment never add weight.
Urgency breakdowns remain internal audit data. This reports queue state, not measured labour.

All GLPI intake channels in `WORKLOAD_NOTICE_ENTITY_IDS` (default `[0]`) are read
through a complete, sorted, paginated search with assignment IDs and validation
status. Only statuses 1/2 are actionable; 3 planned, 4 waiting, 5 solved, 6 closed
and deleted tickets are omitted. Pending/refused GLPI or local bot approvals are
excluded. Technician review still counts. Multiple assignees never multiply a
ticket; active work without an assignee joins the queue. The just-created ticket
and explicit `WORKLOAD_EXCLUDED_TICKET_IDS` are excluded; never infer a test from
its requester or title, and never silently discard old assignments based on age.

Outside configured working hours, skip the lookup/notice; the existing work-hours
message remains. Fresh complete GLPI snapshots can be cached for up to 30 seconds
(`WORKLOAD_NOTICE_CACHE_TTL`, at most 60); local approval exclusions are read fresh.
Incomplete pages, changing totals, over 2,000 tickets, invalid data, or request
failure suppress the notice without using an expired snapshot. The service's
single deadline is `WORKLOAD_NOTICE_TIMEOUT` (default 3 seconds, at most 5); the
handler also bounds optional lookup/delivery and cannot report creation failure
after its confirmation. No second GLPI POST or OpenAI call is involved.

`workload_observations` keeps IDs/counts and snapshot metadata for 30 days, without
ticket text or staff names. `notice_prepared` records the calculation, not proof
of Telegram delivery. Disabling only `WORKLOAD_NOTICE_ENABLED` stops the notices;
no business data or SQLite restoration is needed.

### Existing workflows

1. **Core client + /new dialog.** FSM: category (inline buttons from GLPI ITILCategory list,
   cached 10 min) -> urgency -> title -> description -> optional photos/files ->
   «📨 Отправить заявку» -> create ticket. Reply with ticket number and link.
   - **No separate review/confirm screen.** The attachments step is the last one
     and its primary button IS the send (`nt:send`; text fallback «готово» /
     «отправить»). A review step existed before and people kept stopping at
     «Готово» thinking the ticket was sent — don't reintroduce it.
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
     исполнителя: №44 «...» (2ч)…» with a Take button per ticket. The Take button
     fires the SAME `ta:take:{id}` handler as the new-ticket card, so taking from
     the reminder gives identical feedback (see feature 5): the ticket's living
     card is updated (status + «Взял в работу: <имя>» history), the requester is
     notified, and — because the button is under a reminder — that reminder is
     edited to mark the ticket taken («… — 🙋 взял <имя>», its button removed).
     Anti-spam is GLOBAL, not per ticket: at most ONE digest per
     `REMIND_INTERVAL_HOURS` (default 3 working hours), and it always lists the
     WHOLE overdue queue regardless of when each ticket crossed the age threshold —
     tickets never fire their own separate reminders (no one-message-per-ticket
     drip). Top 10 listed with Take buttons, the rest as «…и ещё N».
     Each ticket number/title links to its original Telegram card using the
     saved `ticket_cards.chat_id` + `message_id` (supergroup/channel `t.me/c`
     link); tickets without a supported saved card remain plain text. Taking
     a ticket marks the visible reminder line while preserving its HTML links.
     Previously sent digests are not rewritten. The gate
     timestamp lives in sync_state (`last_unassigned_summary_ts`, survives
     restarts; the old per-ticket `unassigned_reminders` table is unused). A taken
     ticket stops matching status=New and drops out of the next digest
     automatically.
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
   - **Take feedback (one handler for card & reminder).** On "Take" the bot: assigns
     the ticket in GLPI; updates the living card by its stored id (status header +
     «Взял в работу: <имя>» history, no group ping); notifies the requester «🙋 …
     начата работа. Исполнитель: <имя>»; and advances the tracked `last_status` to
     assigned so the sync loop does NOT also send a generic status-change (dedup) and
     does not re-list it as unassigned. If the press came from an unassigned reminder
     (detected by its header), that reminder message is edited to mark the ticket taken
     and its Take button removed. All feedback is best-effort; failures are logged.
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
   - **One-tap solution rating (😞/😕/🙂/🤩, 4-point, no neutral).** Zero-friction by design: no separate survey
     message, no reminders, silently ignorable. The rating row (four mood buttons,
     values 1..3, callback `rate:{id}:{n}`) rides ON the existing final message — the
     thank-you after the requester confirms the solution, and the solved/closed notice
     when a ticket is closed from the GLPI web UI with a solution. A tap swaps the
     prompt for «🤩 Спасибо за отзыв!» in place (buttons gone) and stores the rating in
     SQLite (`ticket_ratings`, one row per ticket, re-rating overwrites). Only the
     tracked ticket's requester may rate (callback data is spoofable — verified against
     `bot_tickets`). Low ratings do NOT ping the group (deliberate); the only trace is a
     quiet history line on the living card («😞 Оценка заявителя») and the /stats block
     «⭐ Оценки решений: N» (shown once at least one rating exists). Requester
     self-closes are not rated.
     - **Mirrored into GLPI's native satisfaction survey.** The rating is also written
       to `TicketSatisfaction` (😞→1, 😕→2, 🙂→4, 🤩→5; `SATISFACTION_BY_RATE`), so GLPI's own
       satisfaction reports work. Requires the entity survey enabled (Администрирование →
       Организации → корневая → Помощь: internal survey, rate 100%, delay 0) and GLPI's
       cron (creates the survey row after closure — verified live on 11.0.4: created
       instantly/synchronously on close; the cron path is a fallback). Push is
       try-now-then-retry: the rate handler attempts immediately; `ticket_ratings.glpi_pushed`
       (0 pending / 1 done / -1 gave up) drives a sync-loop retry each tick until the row
       appears, giving up after 3 days.
     - **API quirk (verified live):** updating the survey is
       `PUT /Ticket/{tid}/TicketSatisfaction` with `input.id` = the TICKET id (not the
       satisfaction row id) + `satisfaction` + `date_answered`. Direct
       `/TicketSatisfaction/{row_id}` and the sub-route with the row id both return
       `ERROR_GLPI_UPDATE`.
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

7. **Role-based menu, /stats, «👨‍💻 В работе», «📥 Все заявки».** The persistent reply
   menu is built per role at render time (after /start and after every finished dialog):
   regular users get «🆕 Новая заявка» + «📋 Мои заявки»; technicians (is_tech, refreshed
   from the GLPI group by the auth middleware, ~5 min cache) get «🆕 Новая заявка» +
   «📥 Все заявки» (REPLACES «Мои заявки» — a tech's own requester tickets stay reachable
   via /tickets) plus a second SHORT two-button row (labels must not wrap):
   «👨‍💻 В работе» + «📊 Статистика».
   - «📥 Все заявки» lists tickets nobody has taken yet (status New — the same
     "unassigned" convention as the reminder), newest first
     (`search_unassigned_tickets`); empty -> «Заявок без исполнителя нет. 🎉». Tapping
     one opens the same detail view with a full-width «🙋 Взять в работу» button
     (`ta:take:` — the same unified take handler as the group card/reminder, so all
     take feedback applies) plus Reply/Close; no Handoff (nobody to hand off from);
     back returns to the unassigned queue (`tt:all`). Techs only, re-checked in
     handlers.
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

8. **Lead access approval («💳 Платные сервисы», feature: access requests).** Access requests
   go through a lead's approval before technicians may take them; GLPI's native
   **TicketValidation** is the source of truth for the approval fact.
   - **Requester dialog** (menu button «💳 Платные сервисы» (label deliberately avoids the word «доступ»; the old «🔑 Доступ» text is still accepted for stale keyboards) / `/access`, everyone) is
     deliberately SHORT: first a new-access-only notice (the urgent-prod warning
     pattern: «Здесь запрашивают новые доступы… если доступ сломался/пропал —
     обычная заявка», «✅ Продолжить»/«❌ Отмена») — broken-access reports must not
     land behind a lead approval; then ONE free-text question («К чему нужен доступ?»),
     then a
     combined confirm screen showing the request + the approving lead with a
     full-width «📨 Отправить на согласование» (plus «Выбрать другого» / «Отмена»).
     No separate system/justification/duration steps — everything lives in the one
     message; the ticket title is the request's first line (capped at 60). Creates a
     ticket (category `ACCESS_CATEGORY_ID`, structured plain-text body, requester =
     linked user) + a TicketValidation → `global_validation` = *waiting* (2).
   - **Leads** come from config: `LEAD_LOGINS` (comma-separated AD logins), resolved
     against GLPI + bot links with a TTL cache (`build_lead_directory`). The pick list
     shows only leads LINKED to the bot; self-approval is excluded (the requesting lead
     doesn't see themselves). Changing the composition = .env edit + restart.
   - **Auto-suggest from the org map.** SQLite `user_leads` (glpi_users_id ->
     lead_glpi_id, seeded from the org charts) pre-fills the lead: a mapped requester
     lands straight on the confirm screen (whole flow = one message + one tap).
     Unmapped users (and users whose mapped lead is unlinked or is themselves) get
     the plain pick list, and picking a lead lands on the same confirm screen. Leads' own
     requests map to their project head (Рекун / Лисник / Тарновская via the map);
     the heads themselves are unmapped -> manual pick. Maintained with the tech-only
     `/setlead <employee_login> <lead_login>` command.
   - **The lead answers in their DM**: «✅ Одобрить» one tap; «❌ Отклонить» demands a
     mandatory reason (FSM). The inline Cancel controls are sent first, followed
     by a separate ForceReply prompt that opens Telegram's message field (these
     two markup types cannot share one message). Both «Отмена» and `/cancel`
     clear the rejection dialog without deciding the approval. The reason can
     be plain text or a reply to the current prompt; blank/non-text messages
     and replies to earlier prompts ask again without changing GLPI. The role
     menu returns when the dialog finishes or is cancelled. Approving the same
     request while entering a rejection reason clears that state.
     Authority is verified against the stored approval row
     (`access_approvals.lead_tg_id`), not the button. Approve → validation answered
     ACCEPTED, followup «🔑 Доступ одобрен: <лид>» (cursor bumped — no sync echo),
     requester DM, card history «👍 Согласовано: <лид>». Reject → validation REFUSED,
     ticket CLOSED, followup + requester DM with the reason, card history. The lead's
     approval DM is edited in place on approval; rejection is confirmed with a
     separate message containing the reason.
   - **GLPI quirk (verified live on 11.0.4):** a validation can only be ANSWERED by the
     session of its target user — answering on behalf of another user is silently
     ignored (HTTP 200, status unchanged). Therefore validations always TARGET the
     service account (`get_session_user_id`), and the real approver is recorded in
     `comment_validation` («Одобрено: <лид> (лид) — через Telegram-бота») + the followup
     + the card history. Create format: `itemtype_target: "User"` + `items_id_target`
     (the old `users_id_validate` is gone in GLPI 11).
   - **Hard take gate:** `ta:take` re-reads the ticket; `global_validation == 2` →
     refusal toast, no assignment. The new-ticket group card renders
     «⏳ Ждёт согласования лида» while waiting (initial render).
   - **Escalation:** a pending approval older than `APPROVAL_REMIND_HOURS` (default 4
     WORKING hours, counted from max(requested, last reminder)) re-pings the lead's DM;
     working hours only. An approval answered elsewhere / whose ticket is gone is marked
     moot (-2) and dropped. State in `access_approvals` (SQLite).

Out of scope for now: SLA warnings, LLM-based automatic routing, multi-entity support.
Keep the code structured so these can be added later.

## OpenAI shadow classification

`AI_SHADOW_ENABLED` defaults to false. With a configured `OPENAI_API_KEY`, `_run`
starts an independent `ShadowClassifier` task, injects it into the /new and /access
routers, and cancels/closes it before closing the repository. A missing optional
key disables observation with a warning; it must not prevent the bot from starting.
The handlers enqueue only AFTER creation and the requester confirmation. There is
no classifier call on the ticket-creation path. API failures never change tickets,
menus, validators, lead selection, Claude stages, or notifications.

The new `ai_shadow` table is observational and is never consulted by routing or
approval code. Unique ticket IDs prevent repeated calls; claims reserve a UTC-day
budget under the repository transaction lock. HTTP failures and interrupted calls
consume budget and are not retried. Pending jobs survive restarts; running jobs
become interrupted errors to avoid rebilling uncertain requests. Budget excess is
skipped. Cleanup runs on startup and daily, retaining rows for 30 days.

Only title and original request text are sent; no requester/lead metadata, files,
GLPI credentials, ticket numbers, history, or backfill. `redact` masks common keys,
password assignments, emails, phones, IPv4 addresses, handles and URL details.
This is best effort, not full anonymisation of names or arbitrary prose. Sanitised
input is stored only until completion/error/skip, then cleared. Journals and the
read-only report contain labels and IDs, never request text or API error bodies.
OpenAI requests use the fixed official Responses endpoint, `store=false`, a strict
two-enum schema, no tools, a 6000-character input cap and 512 output tokens. Invalid,
refused or incomplete replies are errors, not decisions. Model and prompt version
are saved for review. Default deadline: 15s; default request cap: 100 per UTC day.

`python -m bot.ai_shadow_report` reads results without changing the DB. The `source`
column is the user's chosen flow, NOT a ground-truth label. Assess real predictions
against the corresponding GLPI tickets before considering automatic routing.
`python -m bot.ai_shadow_eval tests/fixtures/ai_shadow_cases.json` is an explicit,
paid API evaluation of synthetic cases, never part of offline pytest or startup.
No fine-tuning. Future routing changes require separate user authorisation.

Prompt `saas-access-shadow-v2` covers all external SaaS, with no closed brand list.
Under this deployment policy, public free tiers do not exempt company access.
`AI_INTERNAL_SERVICES` holds comma-separated names of existing internal deployments;
these names are injected into each classifier instance's instructions, not request
text. A name alone refers to that configured internal deployment. Explicit purchases
of new licences or external Cloud editions still require approval. Keep actual
deployment-specific exceptions in the private environment file.
Provider switches/re-activation after another service is withdrawn are paid access,
not repairs of the same existing account. Classify the target, not the discontinued
source. Short access titles count as intent. Existing-account faults remain technical
issues; acquisition of new hardware remains other. The synthetic suite has 48 cases.
Use `--internal-services` (or `AI_INTERNAL_SERVICES` in the evaluator environment) for
an explicit live evaluation with the same internal exception configuration.

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

- Profile links: renderers keep emitting `user_mention(name, tg_id)`. The shared
  `ProfileLinksMiddleware`, installed on the production Bot session in `_run`,
  resolves those anchors for HTML `sendMessage` and `editMessageText` requests.
  It also disables link previews on those requests, retaining clickable names
  without appending a Telegram profile preview below cards or approval prompts.
  Prefer `https://t.me/<username>` from `getChat` (including active usernames):
  `has_private_forwards` can cause Telegram to silently drop `tg://user` mentions
  outside the user's own chat. With no username, retain ID mentions only when
  privacy permits; otherwise use the current linked GLPI user profile. Unknown
  unlinked accounts have no guaranteed accessible profile and retain the ID
  mention. Lookup timeout is 3 seconds; the bounded cache lasts 10 minutes,
  refreshing changed/removed usernames. Lookup failures reuse the last resolved
  URL or the GLPI fallback and retry after 30 seconds. Card storage remains ID
  based, so legacy cards get the fix on their next edit. Preserve this middleware
  when constructing an operational Bot outside `_run` (e.g. a card repair).
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


## Claude-only second approval

The ordinary paid-service flow remains lead-only. New requests classified as
Claude by `services/access_target.py` additionally require approval from one of
`CLAUDE_APPROVER_GLPI_IDS` (active GLPI accounts linked to Telegram). No public
Claude button is introduced. Ambiguous aliases such as cloud/клауд ask for an
exact service name or link; multiple-service mentions and negations also ask.
The original request and the clarification are retained for the lead.

`ClaudeApprovalService` is shared by handlers and the sync task. SQLite tables
`claude_approvals` and `claude_approval_members` are additive; existing requests
keep their previous policy. Stages: lead -> extra -> approve_pending -> approved,
or reject_pending -> rejected. Decisions are protected by per-ticket locks and
conditional SQL transitions. The first completed decision at the extra stage is
final: either person's refusal prevents the other from overriding it. Starting
or cancelling the reason-entry dialog is not itself a submitted refusal.
The requester's own account cannot be an approver. If the selected lead's GLPI
identity belongs to the request's snapshotted service-approver set, that lead's
approval covers both roles: lead -> approve_pending -> approved, without sending
extra-stage prompts. A saved lead_covers_service flag explains the one-person
approval in the audit. Ordinary leads still require the extra stage. Existing
extra-stage requests with a recorded approval from such a lead are finalised by
the sync loop after confirming GLPI is still waiting; completed/rejected requests
are not reinterpreted. This is based on GLPI ids, not names or the current config.

A single GLPI TicketValidation stays waiting until the extra approval, with
both author names recorded in its final comment. A GLPI failure leaves a saved
pending-finalisation state for the next sync tick. An unsuccessful rejection
close is retried without re-answering a validation already confirmed successful.
GLPI approver identities are snapshotted, but Telegram endpoints are refreshed
from current links before sending. Unlinked/inactive recipients receive nothing;
old endpoints cannot approve, reject or receive new reminders after a relink.
Additional approver messages and completion notifications are retried after
failures; reminders use working hours and APPROVAL_REMIND_HOURS. A final audit
followup advances the tracked cursor to prevent an echo.

Claude take actions require both the persisted approved stage and a successful
live GLPI check showing validation granted. A pending, rejected, cancelled, or
unregistered Claude-policy ticket cannot be taken through the bot; API failures
do not bypass this check. A policy marker in the GLPI content blocks orphaned
requests if registration failed. Empty approver config does not disable the rule:
it blocks submission of new Claude requests while other services continue.

For deployment, back up SQLite through its online backup API, configuration,
unit and code before modifying production. Do not restore an older DB wholesale
once users have created new tickets/decisions. Rolling back to code without this
policy after new Claude requests requires a compatible plan; otherwise keep the
bot stopped while restoring service safely. Old approvals must not be
reinterpreted merely because their descriptions mention Claude.

Claude card presentation reads the saved workflow stage and approver snapshot.
The GLPI policy marker remains stored for fail-closed checks, but is hidden in
Telegram descriptions. Lead-to-service progression names the eligible approvers
in the card/audit and sends the requester a best-effort informational notice.

Service-approver DMs retain requester, original request, lead approval and the
final decision author. `claude_approval_messages` tracks every new extra-stage
prompt/reminder and retries final edits independently of requester delivery.
On connect, saved latest prompts are seeded idempotently; older reminder IDs
from releases that did not retain them cannot be recovered through Telegram.
No decisions are inferred or changed by this presentation migration.

Ordinary access reminders and lead decisions treat closed/solved tickets as
moot even if global_validation is still waiting. GLPI read failures preserve
pending state and do not authorise decisions. Technician close actions recheck
for already-closed tickets at dialog entry and before submitting a solution.
Use GLPI_ACTION_ERROR for failures of these existing-ticket actions; GLPI_ERROR
retains its ticket-creation semantics for creation failures.


## Technician-initiated approval of existing tickets

`handlers/tech_approval.py` exposes `ta:approval:ID` on open-ticket cards and
technician details, plus private `/approval ID` for older cards. The dialog uses
the same lead directory and employee-to-lead mapping as /access. It never changes
that mapping. Nonce-bound controls prevent an old dialog submitting a new one;
an existing unrelated draft must be completed or cancelled first. Unknown/ambiguous
Claude targets are clarified by the technician; the shadow AI is never consulted.

Confirmation revalidates technician rights, the live ticket, requester identity,
content digest, lead directory membership and the lead's active account/link.
Self-approval, solved/closed tickets and tickets already carrying a validation
are refused. One approval cycle per ticket is supported; completed approvals are
not overwritten. Declining closes the original ticket, as in /access, and this is
shown before confirmation. Approved tickets keep their existing status/assignee.

`TechApprovalService` stores a reservation and recipient snapshot in the additive
`tech_approval_requests` table BEFORE any GLPI write. A unique marker in
TicketValidation.comment_submission allows recovery after a lost response.
The sync loop resumes incomplete registrations and initial Telegram deliveries.
A POST of uncertain outcome is NEVER repeated automatically: recovery only reads
`GET /Ticket/ID/TicketValidation`. If the marker never appears, an operator must
reconcile the saved row against GLPI; do not delete the reservation blindly.
Explicit 400/403/404/422 rejections mark the registration failed and permit a new
manual attempt. A late-closed/changed ticket cancels an unposted registration.

The same `access_approvals` and, for Claude, `claude_approvals` decision flows are
used. Registering Claude on a tracked ticket preserves tracking/cursors. Existing
GLPI requesters without Telegram use requester_tg_id=0: approvals still go to the
lead, with no attempt to notify chat 0. Configured leads must be linked. Manual
approval decisions revalidate the recipient's GLPI identity after relinking.
Per-ticket locks serialize registration, technician mutations and lead decisions.
Take/resolve/handoff recheck pending GLPI validation; unresolved registration also
blocks them before GLPI reflects the pending state. Comments remain available.
Old group cards gain the button on their next update; reopening technician detail
or using /approval works immediately. No existing tickets are submitted in bulk.


## Solution-confirmation reminders

`SOLUTION_REMIND_MINUTES=30` enables requester reminders while the live ticket
status is Solved (5); 0 disables. `SyncService` counts elapsed working minutes
using the existing WORK_HOURS/WORK_DAYS/TZ schedule, never sends outside it, and
sends at most one overdue reminder per ticket per tick. Existing solved tickets
start from GLPI's UTC `solvedate`; missing timestamps start from first observation.
A freshly sent initial solution prompt starts a new 30-minute wait. This applies
to both technician and GLPI-web solution paths.

The additive `solution_reminders` table stores the solution timestamp, wait start,
last reserved attempt and last successful delivery. Attempts are reserved BEFORE
sending using a conditional SQLite update; failed/uncertain sends wait the full
interval instead of causing bursts after restart. A new solution timestamp resets
the cycle. `Repo.set_ticket_status` deletes the reminder when leaving Solved or
becoming inactive. No automatic closure, group reminders or AI calls are involved.

Before a due send, re-read GLPI status/requester and the current Telegram mapping.
Requesters who are unlinked, relinked or replaced in GLPI are not sent old ticket
content. Reminder buttons include a hash of the solution timestamp. Requester
confirmation and return revalidate live Solved status, identity and that token;
the return reason is checked again on submission. Existing unversioned buttons
remain compatible but also require a live solved ticket. Per-ticket locks serialize
these actions, direct requester closure, technician solutions and reminder sends.

`GlpiClient.get_ticket` returns None for is_deleted tickets as well as 404; sync
deactivates those stale tracked rows without notifying requesters. Reminder sends
do not retry a Telegram flood wait across the end of working hours.

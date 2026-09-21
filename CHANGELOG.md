# Changelog

> 🇷🇺 [Читать по-русски](CHANGELOG.ru.md)

All notable changes to GLPIgram are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Lead approval for access requests («💳 Платные сервисы», formerly «🔑 Доступ»).** A new main-menu button
  for everyone. It first shows a notice that this is for *new* access only
  (broken access → a regular ticket), then asks a single question — “What do
  you need access to?” — and shows a combined confirm screen with the
  auto-suggested approving lead (from the employee → lead map, `/setlead
  <employee> <lead>` to adjust; “Choose another” lists every configured
  lead). Sending creates the ticket in the access category plus a native GLPI
  **TicketValidation**, and DMs the lead with **Approve / Reject** buttons.
  The verdict is written back to GLPI (validation answer + a followup naming
  the real approver), the requester is notified, and «Взять» is blocked while
  the ticket awaits approval. Pending approvals are re-pinged every
  `APPROVAL_REMIND_HOURS`. New settings: `LEAD_LOGINS`, `ACCESS_CATEGORY_ID`,
  `APPROVAL_REMIND_HOURS`.
- **One-tap solution rating (😞 / 😐 / 🤩).** When a ticket is solved the
  requester’s notice carries a single row of three mood buttons; a tap is
  acknowledged silently — no follow-up questions, no group ping. Ratings are
  stored locally and mirrored into GLPI’s native **TicketSatisfaction**
  survey, so they show up in GLPI’s own reports.
- **«📥 Все заявки» for technicians** — the untaken queue (every New ticket
  with no assignee) replaces «Мои заявки» in the tech menu; a ticket opens
  with a **Take** button. A technician’s own requester tickets stay reachable
  via /tickets.
- **Ticket description on the tech-group card and in detail views**, so a
  ticket can be judged without opening GLPI.
- **Attachments on followups are forwarded both ways.** Files added to a
  followup in the GLPI web UI reach the requester in Telegram, and files a
  requester or technician attaches through the bot reach the other side.

### Changed

- **The access button is now «💳 Платные сервисы».** The word «доступ» read
  as “my login is broken” and clashed with the GLPI category (renamed to
  «Учетные записи»). The button is about seats in paid external services
  (Figma, ChatGPT, Jira, Miro…), the notice screen names them, and the
  question became “Which service do you need and what for?”. The old label
  is still accepted so persistent keyboards that haven’t re-rendered keep
  working.
- **The requester is a clickable Telegram profile link** in the tech detail
  views («Все заявки» / «В работе») and in the lead’s approval DM — the same
  `tg://user` mention the group card already used — so a technician or lead
  can write to the person directly. Plain text when the requester hasn’t
  linked the bot.
- **The confirming admin is named by GLPI name** on the «Привязка
  подтверждена» card («Обработал: Артём Ильин», not the Telegram “Artem I.”).
- **The /new dialog no longer has a review screen.** The attachments step is
  the last one and its primary button is **«📨 Отправить заявку»** (full
  width, «Отмена» on its own row below); pressing it creates the ticket at
  once. Text fallback: “готово” / “отправить”. People used to stop at «Готово»
  and walk away thinking the ticket was sent.
- **Urgency step:** «🔴 Срочно (прод)» is listed first, alone on its row;
  «Высокая» turned orange (🟠) so red is reserved for the prod level.
- **Unassigned-ticket reminders are one grouped digest.** The tech group gets
  a single «⚠️ Заявки без исполнителя» message listing every untaken ticket
  (with per-ticket Take buttons), refreshed on a global cadence — no more
  per-ticket drip regardless of ticket age.
- **«Взять» from the reminder gives the same feedback as from the card:** the
  reminder line is updated, the group card records the assignee, the
  requester is notified, and the ticket is not re-reminded.
- **Requester is shown in the tech detail views** («Все заявки» / «В
  работе») — it used to be lost when a ticket was opened from the list.

### Fixed

- **Card events that arrived before the card existed were dropped.** A lead
  approved an access request 13 seconds after creation — before the sync
  loop had posted the tech-group card — and the «👍 Согласовано» line
  vanished; the same race hit any early event and deferred (quiet-hours)
  cards. Such events are now buffered and folded into the card the moment
  it is sent.
- **Two GLPI accounts with the same full name rendered as identical buttons**
  in the name-based linking pick-list, so a person could link the wrong one
  (it happened). When display names collide the buttons now carry the login.
- **Silently lost attachments.** GLPI 11 answers *201 Created* to an upload
  whose extension is not a registered Document type, creates an empty stub
  and hides the refusal in `upload_result`. The bot took that as success,
  and a `*.pub` SSH key vanished from a ticket. The client now detects the
  refusal, purges the stub and the user gets an explicit message (“rename to
  .txt or paste as text”) — in /new and in requester/tech comments; no
  followup is posted for a rejected file. (Fifteen common text-like
  extensions — pub, md, key, pem, yaml, yml, json, log, conf, ini, toml, sh,
  ps1, crt, cer — were also registered in GLPI.)
- **Rating mirror addressing:** `TicketSatisfaction` is updated with the
  *ticket* id, not the survey row id (GLPI returned ERROR_GLPI_UPDATE).

## [0.4.0] - 2026-07-15

### Added

- **Dedicated “🔴 Urgent (prod)” urgency level** in the /new dialog, on its own
  row below the ordinary levels. Selecting it shows a warning — “⚠️ This
  category is for urgent production-related issues. The team will be notified at
  any time of day, including nights and weekends. Use it with great care.” —
  with **Confirm** / **Cancel**; without confirmation the ticket is not created
  as urgent. It maps to GLPI urgency 5 (very high), and its tech-group card
  carries an explicit **“🔴 URGENT (prod)”** banner.

### Changed

- **Quiet-hours breakthrough is decoupled from ordinary high urgency.** Outside
  working hours, every ordinary level — low, medium and **high** — is now
  deferred to the next morning. Only the new “Urgent (prod)” level pings the
  tech group immediately, at any time.

### Removed

- **`QUIET_MIN_URGENCY`** setting. The off-hours breakthrough is now tied to the
  dedicated urgent (prod) level, not to a numeric urgency threshold.

## [0.3.0] - 2026-07-14

### Added

- **Handoff («🔄 Передать»)** — a technician can reassign a ticket to another
  linked tech from the group card or the «В работе» detail view; the pick
  dialog lives in the presser's DM. GLPI swaps the Ticket_User assignee link,
  the new tech and the requester get DMs, the card history records
  «Передано: кто → кому» and the card header now shows the assignee.
- **Explicit cancel in every dialog** — each text-awaiting step (comment,
  solution, close/return reason, /new title & description, login) carries an
  inline «❌ Отмена» button and honours /cancel; cancelling clears the state,
  answers «Отменено» with the role menu and never touches the ticket.
- **«👨‍💻 В работе» for technicians** — the tech menu lists tickets assigned
  to the pressing technician in two groups (in work / awaiting the
  requester's confirmation); tapping one opens a detail view with the same
  Reply/Close actions as the group card.
- **Role-based menu + /stats** — the persistent reply menu is rendered per
  role: technicians (GLPI-group membership, ~5 min cache) get an extra
  «📊 Статистика» button; /stats posts an open-queue breakdown by status
  plus linked-user counters (total / technicians / new in 7 days). The
  handler re-checks `is_tech` itself, so a direct /stats is refused for
  regular users; a role change flips the menu on the next interaction.
- **Bilingual changelog** — CHANGELOG.md (English) and CHANGELOG.ru.md
  (Russian), cross-linked in the header like the READMEs.

### Changed

- The solved → closed confirmation cycle is finalized: if the requester never
  reacts to a proposed solution, the ticket is left in *solved* for GLPI's own
  auto-close timer — the bot never force-closes an unconfirmed ticket.

### Fixed

- The solution text was announced to the tech group; it now goes only to the
  requester (who has the confirm/return buttons), the group card gets just a
  history line.
- In group chats the bot reacted to plain text (free-text ticket offer, FSM
  steps); groups are now inline-buttons-only — all dialogs, menu buttons and
  /start work exclusively in private chats.
- Raw GLPI ticket URLs printed on their own line (creation confirmation,
  status-change and followup notifications, the /tickets detail view) are
  replaced with the ticket number as a clickable link; an escaping audit
  covers user- and GLPI-supplied strings interpolated into HTML messages.

## [0.2.0] - 2026-07-09

### Added

- **Living ticket card** — one evolving message per ticket in the tech group
  instead of scattered notifications: the status header and a «── История ──»
  block (last 10 events with author and time) are updated by editing the card;
  buttons follow the ticket state (no re-take once taken, no actions once
  closed). Short reply-pings accompany only the events that need the team's
  attention: a requester's comment and a closure.
- **ITIL solution cycle** — a technician's "Close" marks the ticket *solved*,
  not closed. The requester receives the actual solution text with
  «✅ Подтвердить» / «↩️ Вернуть в работу» buttons (also when solved from the
  GLPI web UI). Confirming closes the ticket; returning asks for a reason,
  reopens the ticket to *assigned* and pings both the solving technician and
  the group. A solved card shows a passive "awaiting confirmation" button; a
  closed card keeps only an "Open in GLPI" link.
- **Unassigned-tickets reminder** — during working hours the group gets one
  summary («⚠️ Заявки без исполнителя: №44 … (2ч)») with a Take button per
  ticket. Thresholds are counted in *working* hours
  (`UNASSIGNED_REMIND_HOURS`, `REMIND_INTERVAL_HOURS`); per-ticket anti-spam
  state survives restarts; taken tickets drop out automatically.
- **Attachments on the group card** — requester files are counted in the card
  («📎 Вложений: N») and image attachments are delivered right under it as a
  media group (10 MB / 10 items caps); everything else stays behind the GLPI
  link.
- **Urgency display** — the full GLPI 1–5 scale with emoji indicators on the
  group card and in the «Мои заявки» detail view.
- **Configurable greeting** — `WELCOME_MESSAGE` replaces the /start text for
  linked users (verbatim, HTML and `\n` supported); unlinked users always get
  the sign-in prompt.

### Changed

- The new-ticket card was redesigned for scanning: ticket number and a bold
  urgency headline on top, a compact 📝 title / 👤 author / 🔗 named-link body,
  no redundant "Status: New" line; secondary «💬 Ответить» / «✅ Закрыть»
  buttons share a row above a full-width «🙋 Взять в работу».
- /start now depends on the link state: unlinked users get an auth-first
  prompt and land straight in the linking flow; linked users get a greeting
  that points at the menu buttons.
- Requester notifications about solved/closed tickets carry the actual
  solution text and the technician's name instead of a bare "status changed".
- All inline button labels were audited to fit multi-per-row width limits.

### Fixed

- "Taken" was announced twice (card history *and* a group message); history
  records everything, pings are reserved for attention events.
- The two close flows (technician via the card vs requester via «Мои заявки»)
  are fully isolated: disjoint FSM states, solutions are recorded with the
  technician's name, and the group gets a proper closure announcement that
  works even when the card is past Telegram's 48-hour edit window.

## [0.1.0] - 2026-07-07

Initial public release.

### Added

- **Ticket creation** — a guided /new dialog (category from GLPI, urgency,
  title, description, photo/document attachments, confirmation) plus a
  free-text shortcut that offers to turn any message into a ticket.
- **Account linking** — users identify themselves by AD login *or* full name;
  a technician approves the link with one button in the tech group; accounts
  disabled in GLPI/AD are unlinked automatically; `/link` and `/unlink` admin
  commands.
- **My tickets** — open-ticket list with a detail view (status, assignee,
  recent comments, GLPI link), commenting with attachments, self-closing with
  an optional reason, and a rate-limited «🔔 Напомнить о себе» button.
- **GLPI → Telegram sync loop** — announces new tickets to the tech group and
  pushes status changes / technician replies to the requester; cursors are
  persisted in SQLite so restarts never duplicate notifications.
- **Technician actions** — Take / Comment / Close buttons on the group card
  with solution and comment texts collected in the technician's DM.
- **Quiet hours** — off-hours low-urgency notifications are queued and
  delivered as a morning batch; urgent tickets pass immediately; requesters
  are told when the team will actually see their ticket.
- **Two languages** (`BOT_LANGUAGE`: ru/en), configuration entirely from
  environment variables, systemd deployment, MIT license, CI (ruff + pytest),
  English and Russian READMEs.

### Fixed

- GLPI datetimes are parsed as UTC and rendered in the bot's timezone.
- Transparent GLPI session renewal covers the undocumented
  400-on-dead-token response of GLPI 11 (alongside the documented 401).
- A robustness audit: transactional SQLite writes, Telegram flood-wait and
  group-migration handling, stale-button and unhandled-error fallbacks,
  no-duplicate delivery for the quiet-hours queue.

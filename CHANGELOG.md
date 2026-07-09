# Changelog

All notable changes to GLPIgram are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Role-based menu + /stats** — the persistent reply menu is rendered per
  role: technicians (GLPI-group membership, ~5 min cache) get an extra
  «📊 Статистика» button; /stats posts an open-queue breakdown by status.
  The handler re-checks `is_tech` itself, so a direct /stats is refused for
  regular users; a role change flips the menu on the next interaction.

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

-- SQLite schema for the GLPI Telegram bot.
-- Applied idempotently on startup (CREATE ... IF NOT EXISTS).

-- Telegram <-> GLPI account mapping (feature 2).
-- One row per linked Telegram user.
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,      -- Telegram user id
    glpi_users_id INTEGER NOT NULL,         -- GLPI User.id
    display_name  TEXT    NOT NULL DEFAULT '',
    is_tech       INTEGER NOT NULL DEFAULT 0,  -- cached: member of the GLPI tech group
    linked_at     INTEGER NOT NULL,         -- unix seconds when the link was created
    checked_at    INTEGER NOT NULL DEFAULT 0   -- unix seconds of the last active/is_tech re-check
);

-- Fast reverse lookup (GLPI id -> mapping) for admin /unlink and de-dup on link.
CREATE INDEX IF NOT EXISTS idx_users_glpi ON users (glpi_users_id);

-- Tickets created via the bot (feature 4): lets the sync loop notify the
-- requester of status changes and new followups, and survive restarts without
-- duplicate notifications (cursors are the last status / followup id seen).
CREATE TABLE IF NOT EXISTS bot_tickets (
    ticket_id         INTEGER PRIMARY KEY,      -- GLPI Ticket.id
    requester_tg_id   INTEGER NOT NULL,         -- Telegram id to notify
    requester_glpi_id INTEGER NOT NULL DEFAULT 0,  -- GLPI requester (own followups are skipped)
    last_status       INTEGER NOT NULL DEFAULT 0,
    last_followup_id  INTEGER NOT NULL DEFAULT 0,
    active            INTEGER NOT NULL DEFAULT 1,   -- 0 once closed & notified (stop polling)
    created_at        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bot_tickets_active ON bot_tickets (active);

-- Generic integer cursor store for the sync loop (e.g. last seen ticket id).
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- Last time the requester nudged the tech group about a ticket ("remind me"),
-- for the per-ticket cooldown (feature 3). Works for any ticket, not just
-- bot-created ones.
CREATE TABLE IF NOT EXISTS ticket_reminders (
    ticket_id      INTEGER PRIMARY KEY,
    last_remind_at INTEGER NOT NULL  -- unix seconds
);

-- Tech-group notifications deferred during quiet hours (off-hours), flushed at
-- the start of the next work day. Survives restarts.
CREATE TABLE IF NOT EXISTS deferred_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,   -- 'new' (new-ticket card) | 'remind'
    ticket_id  INTEGER NOT NULL,
    created_at INTEGER NOT NULL    -- unix seconds when queued
);

-- The single evolving ("living") card per ticket in the tech group: every
-- event edits this message instead of spawning new ones. Fields are the raw
-- ingredients for a full re-render; history is a JSON array of rendered lines
-- (capped at 10 by the service). last_followup_id dedups comment events that
-- are recorded both directly (bot actions) and via the sync loop.
CREATE TABLE IF NOT EXISTS ticket_cards (
    ticket_id         INTEGER PRIMARY KEY,
    chat_id           INTEGER NOT NULL,
    message_id        INTEGER NOT NULL,
    title             TEXT    NOT NULL DEFAULT '',
    description       TEXT    NOT NULL DEFAULT '',  -- ticket content, for re-renders
    urgency           INTEGER NOT NULL DEFAULT 0,
    requester_name    TEXT    NOT NULL DEFAULT '',
    requester_tg_id   INTEGER,
    attachments_count INTEGER NOT NULL DEFAULT 0,
    status            INTEGER NOT NULL DEFAULT 1,
    taken_by          TEXT    NOT NULL DEFAULT '',
    history           TEXT    NOT NULL DEFAULT '[]',
    last_followup_id  INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL DEFAULT 0
);

-- Card events that arrived BEFORE the group card was sent (e.g. a lead approves
-- an access request seconds after creation, while the sync loop hasn't posted
-- the card yet; or overnight, while the card is deferred to the morning).
-- Folded into the card's history at register() time, then deleted.
CREATE TABLE IF NOT EXISTS card_pending_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    line       TEXT,               -- already time-stamped history line, or NULL
    status     INTEGER,            -- header status to apply, or NULL
    taken_by   TEXT,               -- assignee to apply, or NULL
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_card_pending_ticket ON card_pending_events(ticket_id);

-- Who proposed the solution (ITIL cycle): lets the bot DM the right technician
-- when the requester returns a solved ticket to work. tg_id is NULL when the
-- solver has no Telegram link (solved from the GLPI web UI by an unlinked user).
CREATE TABLE IF NOT EXISTS ticket_solvers (
    ticket_id INTEGER PRIMARY KEY,
    tg_id     INTEGER,
    name      TEXT NOT NULL DEFAULT ''
);

-- (The former per-ticket `unassigned_reminders` anti-spam table is gone: the
-- digest now uses ONE global gate stored in sync_state
-- ('last_unassigned_summary_ts'). Old databases may still carry the orphan
-- table; it is harmless and simply unused.)

-- Lead access approvals (feature: access requests). One row per access ticket;
-- the GLPI TicketValidation is the source of truth for the approval fact, this
-- row carries the bot-side context (who the lead is, DM message to edit,
-- escalation timers). status: 0 pending, 1 approved, -1 rejected.
CREATE TABLE IF NOT EXISTS access_approvals (
    ticket_id       INTEGER PRIMARY KEY,
    validation_id   INTEGER NOT NULL,
    lead_glpi_id    INTEGER NOT NULL,
    lead_tg_id      INTEGER NOT NULL,
    lead_name       TEXT    NOT NULL DEFAULT '',
    requester_tg_id INTEGER NOT NULL,
    dm_message_id   INTEGER,            -- the lead's DM prompt, edited on answer
    status          INTEGER NOT NULL DEFAULT 0,
    requested_at    INTEGER NOT NULL,   -- unix seconds
    reminded_at     INTEGER NOT NULL DEFAULT 0  -- last escalation ping
);

CREATE INDEX IF NOT EXISTS idx_access_pending ON access_approvals (status);

-- Requester's one-tap rating of a solved ticket (1=😞, 2=😐, 3=🤩).
-- One row per ticket; re-rating overwrites (only reachable on old messages).
CREATE TABLE IF NOT EXISTS ticket_ratings (
    ticket_id   INTEGER PRIMARY KEY,
    tg_id       INTEGER NOT NULL,  -- who rated (the requester)
    rating      INTEGER NOT NULL,  -- 1..4 (was 1..3 before the 4-point scale)
    rated_at    INTEGER NOT NULL,  -- unix seconds
    glpi_pushed INTEGER NOT NULL DEFAULT 0  -- 0 pending, 1 in TicketSatisfaction, -1 gave up
);

-- Employee -> their approving lead (feature: access requests, auto-suggest).
-- Both sides are GLPI user ids. Seeded from the org charts; maintained with
-- the tech-only /setlead command. A missing row = manual lead pick in /access.
CREATE TABLE IF NOT EXISTS user_leads (
    glpi_users_id INTEGER PRIMARY KEY,
    lead_glpi_id  INTEGER NOT NULL
);

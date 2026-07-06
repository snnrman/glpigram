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

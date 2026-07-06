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

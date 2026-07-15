"""SQLite persistence (aiosqlite), plain SQL via a thin repository helper.

Feature 2 stores the Telegram <-> GLPI account mapping here. Later features add
their cursor/sync state alongside (same connection, same ``Repo``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass(slots=True)
class LinkedUser:
    """A confirmed Telegram <-> GLPI mapping (one ``users`` row)."""

    tg_id: int
    glpi_users_id: int
    display_name: str
    is_tech: bool
    linked_at: int
    checked_at: int

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> LinkedUser:
        return cls(
            tg_id=row["tg_id"],
            glpi_users_id=row["glpi_users_id"],
            display_name=row["display_name"],
            is_tech=bool(row["is_tech"]),
            linked_at=row["linked_at"],
            checked_at=row["checked_at"],
        )


@dataclass(slots=True)
class TrackedTicket:
    """A bot-created ticket the sync loop watches (one ``bot_tickets`` row)."""

    ticket_id: int
    requester_tg_id: int
    requester_glpi_id: int
    last_status: int
    last_followup_id: int
    active: bool
    created_at: int

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> TrackedTicket:
        return cls(
            ticket_id=row["ticket_id"],
            requester_tg_id=row["requester_tg_id"],
            requester_glpi_id=row["requester_glpi_id"],
            last_status=row["last_status"],
            last_followup_id=row["last_followup_id"],
            active=bool(row["active"]),
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class TicketCard:
    """The living tech-group card of one ticket (one ``ticket_cards`` row)."""

    ticket_id: int
    chat_id: int
    message_id: int
    title: str
    description: str
    urgency: int
    requester_name: str
    requester_tg_id: int | None
    attachments_count: int
    status: int
    taken_by: str
    history: str  # JSON array of rendered lines
    last_followup_id: int

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> TicketCard:
        return cls(
            ticket_id=row["ticket_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            title=row["title"],
            description=row["description"],
            urgency=row["urgency"],
            requester_name=row["requester_name"],
            requester_tg_id=row["requester_tg_id"],
            attachments_count=row["attachments_count"],
            status=row["status"],
            taken_by=row["taken_by"],
            history=row["history"],
            last_followup_id=row["last_followup_id"],
        )


class Repo:
    """Owns a single aiosqlite connection; all DB access goes through here.

    Handlers run concurrently on the same connection, so every write goes
    through :meth:`_tx`: a lock keeps multi-statement operations from
    interleaving with other writers, and an explicit rollback keeps a failed
    statement from leaving half an operation on the shared connection (where
    the next unrelated ``commit()`` would silently persist it).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the connection and apply the schema (idempotent)."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        # Sane defaults for a long-running service (busy_timeout matters only
        # if an external process ever writes to the same file).
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=3000")
        await self._db.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # Idempotent column additions for DBs created before the column existed
        # (CREATE TABLE IF NOT EXISTS won't alter an existing table).
        await self._ensure_column("ticket_cards", "description", "TEXT NOT NULL DEFAULT ''")
        await self._db.commit()
        log.info("db_connected path=%s", self._db_path)

    async def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add ``column`` to ``table`` if missing (SQLite has no ADD COLUMN IF NOT EXISTS)."""
        async with self._db.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        if column not in existing:
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("db_migrated table=%s added_column=%s", table, column)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:  # pragma: no cover - programming error
            raise RuntimeError("Repo.connect() was not called")
        return self._db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialised write transaction: commit on success, rollback on error."""
        async with self._write_lock:
            db = self._conn
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    # -- account linking ---------------------------------------------------
    async def get_by_tg(self, tg_id: int) -> LinkedUser | None:
        async with self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return LinkedUser._from_row(row) if row else None

    async def get_by_glpi(self, glpi_users_id: int) -> LinkedUser | None:
        async with self._conn.execute(
            "SELECT * FROM users WHERE glpi_users_id = ?", (glpi_users_id,)
        ) as cur:
            row = await cur.fetchone()
        return LinkedUser._from_row(row) if row else None

    async def upsert_link(
        self,
        *,
        tg_id: int,
        glpi_users_id: int,
        display_name: str,
        is_tech: bool,
        now: int,
    ) -> None:
        """Create or replace a mapping.

        Any prior owner of the same GLPI account (a different Telegram id) is
        dropped so a re-link cleanly transfers the account.
        """
        async with self._tx() as db:
            await db.execute(
                "DELETE FROM users WHERE glpi_users_id = ? AND tg_id <> ?",
                (glpi_users_id, tg_id),
            )
            await db.execute(
                """
                INSERT INTO users
                    (tg_id, glpi_users_id, display_name, is_tech, linked_at, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    glpi_users_id = excluded.glpi_users_id,
                    display_name  = excluded.display_name,
                    is_tech       = excluded.is_tech,
                    linked_at     = excluded.linked_at,
                    checked_at    = excluded.checked_at
                """,
                (tg_id, glpi_users_id, display_name, int(is_tech), now, now),
            )

    async def set_tech_checked(self, tg_id: int, *, is_tech: bool, now: int) -> None:
        """Record the result of an active/is_tech re-check."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE users SET is_tech = ?, checked_at = ? WHERE tg_id = ?",
                (int(is_tech), now, tg_id),
            )

    async def unlink_tg(self, tg_id: int) -> bool:
        """Remove a mapping by Telegram id. Returns True if a row was deleted."""
        async with self._tx() as db:
            cur = await db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        return cur.rowcount > 0

    async def unlink_glpi(self, glpi_users_id: int) -> bool:
        """Remove a mapping by GLPI user id. Returns True if a row was deleted."""
        async with self._tx() as db:
            cur = await db.execute("DELETE FROM users WHERE glpi_users_id = ?", (glpi_users_id,))
        return cur.rowcount > 0

    # -- tracked tickets (feature 4) --------------------------------------
    async def track_ticket(
        self,
        *,
        ticket_id: int,
        requester_tg_id: int,
        requester_glpi_id: int,
        status: int,
        now: int,
    ) -> None:
        """Start watching a bot-created ticket (idempotent on ticket_id)."""
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO bot_tickets
                    (ticket_id, requester_tg_id, requester_glpi_id, last_status,
                     last_followup_id, active, created_at)
                VALUES (?, ?, ?, ?, 0, 1, ?)
                ON CONFLICT(ticket_id) DO NOTHING
                """,
                (ticket_id, requester_tg_id, requester_glpi_id, status, now),
            )

    async def get_tracked_ticket(self, ticket_id: int) -> TrackedTicket | None:
        async with self._conn.execute(
            "SELECT * FROM bot_tickets WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return TrackedTicket._from_row(row) if row else None

    async def active_tracked_tickets(self) -> list[TrackedTicket]:
        async with self._conn.execute(
            "SELECT * FROM bot_tickets WHERE active = 1 ORDER BY ticket_id"
        ) as cur:
            rows = await cur.fetchall()
        return [TrackedTicket._from_row(r) for r in rows]

    async def set_ticket_status(self, ticket_id: int, *, status: int, active: bool) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE bot_tickets SET last_status = ?, active = ? WHERE ticket_id = ?",
                (status, int(active), ticket_id),
            )

    async def set_ticket_followup_cursor(self, ticket_id: int, last_followup_id: int) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE bot_tickets SET last_followup_id = ? WHERE ticket_id = ?",
                (last_followup_id, ticket_id),
            )

    # -- sync cursors ------------------------------------------------------
    async def get_cursor(self, key: str) -> int | None:
        async with self._conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_cursor(self, key: str, value: int) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO sync_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # -- reminders (feature 3) --------------------------------------------
    async def get_last_remind(self, ticket_id: int) -> int | None:
        async with self._conn.execute(
            "SELECT last_remind_at FROM ticket_reminders WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["last_remind_at"] if row else None

    async def set_last_remind(self, ticket_id: int, when: int) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO ticket_reminders (ticket_id, last_remind_at) VALUES (?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET last_remind_at = excluded.last_remind_at
                """,
                (ticket_id, when),
            )

    # -- deferred (quiet-hours) notifications -----------------------------
    async def enqueue_deferred(self, kind: str, ticket_id: int, now: int) -> None:
        async with self._tx() as db:
            await db.execute(
                "INSERT INTO deferred_notifications (kind, ticket_id, created_at) VALUES (?, ?, ?)",
                (kind, ticket_id, now),
            )

    async def list_deferred(self) -> list[tuple[int, str, int]]:
        """Queued notifications oldest-first as (id, kind, ticket_id)."""
        async with self._conn.execute(
            "SELECT id, kind, ticket_id FROM deferred_notifications ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [(r["id"], r["kind"], r["ticket_id"]) for r in rows]

    async def delete_deferred(self, row_id: int) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM deferred_notifications WHERE id = ?", (row_id,))

    # -- living cards (single evolving tech-group card per ticket) ---------
    async def save_card(
        self,
        *,
        ticket_id: int,
        chat_id: int,
        message_id: int,
        title: str,
        description: str,
        urgency: int,
        requester_name: str,
        requester_tg_id: int | None,
        attachments_count: int,
        status: int,
        now: int,
    ) -> None:
        """Remember the group card at the moment it was actually sent."""
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO ticket_cards
                    (ticket_id, chat_id, message_id, title, description, urgency,
                     requester_name, requester_tg_id, attachments_count, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    chat_id = excluded.chat_id, message_id = excluded.message_id
                """,
                (
                    ticket_id,
                    chat_id,
                    message_id,
                    title,
                    description,
                    urgency,
                    requester_name,
                    requester_tg_id,
                    attachments_count,
                    status,
                    now,
                ),
            )

    async def get_card(self, ticket_id: int) -> TicketCard | None:
        async with self._conn.execute(
            "SELECT * FROM ticket_cards WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return TicketCard._from_row(row) if row else None

    async def update_card(
        self,
        ticket_id: int,
        *,
        status: int,
        taken_by: str,
        history: str,
        last_followup_id: int,
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                UPDATE ticket_cards
                SET status = ?, taken_by = ?, history = ?, last_followup_id = ?
                WHERE ticket_id = ?
                """,
                (status, taken_by, history, last_followup_id, ticket_id),
            )

    # -- solution cycle (ITIL): who solved, for return-to-work pings --------
    async def set_solver(self, ticket_id: int, *, tg_id: int | None, name: str) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO ticket_solvers (ticket_id, tg_id, name) VALUES (?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET tg_id = excluded.tg_id,
                                                     name = excluded.name
                """,
                (ticket_id, tg_id, name),
            )

    async def get_solver(self, ticket_id: int) -> tuple[int | None, str] | None:
        async with self._conn.execute(
            "SELECT tg_id, name FROM ticket_solvers WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return (row["tg_id"], row["name"]) if row else None

    async def list_techs(self) -> list[LinkedUser]:
        """All linked technicians (handoff candidates), alphabetical."""
        async with self._conn.execute(
            "SELECT * FROM users WHERE is_tech = 1 ORDER BY display_name"
        ) as cur:
            rows = await cur.fetchall()
        return [LinkedUser._from_row(r) for r in rows]

    async def user_stats(self, *, now: int) -> tuple[int, int, int]:
        """Linked-user counters for /stats: (total, of them techs, linked last 7 days).

        ``linked_at`` is a unix timestamp (like every date column here).
        """
        week_ago = now - 7 * 86400
        async with self._conn.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(is_tech), 0) AS techs,"
            " COALESCE(SUM(linked_at >= ?), 0) AS recent"
            " FROM users",
            (week_ago,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["total"]), int(row["techs"]), int(row["recent"])

    # -- unassigned-tickets reminder (anti-spam state) ----------------------
    async def get_last_unassigned_remind(self, ticket_id: int) -> int | None:
        async with self._conn.execute(
            "SELECT last_remind_at FROM unassigned_reminders WHERE ticket_id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["last_remind_at"] if row else None

    async def set_last_unassigned_remind(self, ticket_id: int, when: int) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO unassigned_reminders (ticket_id, last_remind_at) VALUES (?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET last_remind_at = excluded.last_remind_at
                """,
                (ticket_id, when),
            )

"""SQLite persistence (aiosqlite), plain SQL via a thin repository helper.

Feature 2 stores the Telegram <-> GLPI account mapping here. Later features add
their cursor/sync state alongside (same connection, same ``Repo``).
"""

from __future__ import annotations

import logging
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


class Repo:
    """Owns a single aiosqlite connection; all DB access goes through here."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection and apply the schema (idempotent)."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        # Foreign keys / sane durability defaults for a long-running service.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._db.commit()
        log.info("db_connected path=%s", self._db_path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:  # pragma: no cover - programming error
            raise RuntimeError("Repo.connect() was not called")
        return self._db

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
        db = self._conn
        await db.execute(
            "DELETE FROM users WHERE glpi_users_id = ? AND tg_id <> ?",
            (glpi_users_id, tg_id),
        )
        await db.execute(
            """
            INSERT INTO users (tg_id, glpi_users_id, display_name, is_tech, linked_at, checked_at)
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
        await db.commit()

    async def set_tech_checked(self, tg_id: int, *, is_tech: bool, now: int) -> None:
        """Record the result of an active/is_tech re-check."""
        await self._conn.execute(
            "UPDATE users SET is_tech = ?, checked_at = ? WHERE tg_id = ?",
            (int(is_tech), now, tg_id),
        )
        await self._conn.commit()

    async def unlink_tg(self, tg_id: int) -> bool:
        """Remove a mapping by Telegram id. Returns True if a row was deleted."""
        cur = await self._conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        await self._conn.commit()
        return cur.rowcount > 0

    async def unlink_glpi(self, glpi_users_id: int) -> bool:
        """Remove a mapping by GLPI user id. Returns True if a row was deleted."""
        cur = await self._conn.execute(
            "DELETE FROM users WHERE glpi_users_id = ?", (glpi_users_id,)
        )
        await self._conn.commit()
        return cur.rowcount > 0

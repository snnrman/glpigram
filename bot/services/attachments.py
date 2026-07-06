"""Telegram <-> GLPI attachment helpers (feature 6).

Extracts a downloadable file (document or largest photo) from a message,
enforces the Bot API's 20 MB ``getFile`` limit, and downloads the bytes for
upload to GLPI. Kept transport-agnostic so /new and the comment flows share it.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import Message

# Telegram Bot API can only fetch files up to 20 MB via getFile.
MAX_TG_FILE_BYTES = 20 * 1024 * 1024
# Defensive cap on how many files one ticket/comment may carry.
MAX_ATTACHMENTS = 10


@dataclass(slots=True)
class PendingFile:
    file_id: str
    filename: str
    size: int
    mime: str


def extract(message: Message) -> PendingFile | None:
    """Return the attachment on a message (document or largest photo), or None."""
    if message.document is not None:
        doc = message.document
        return PendingFile(
            file_id=doc.file_id,
            filename=doc.file_name or f"document_{doc.file_unique_id}",
            size=doc.file_size or 0,
            mime=doc.mime_type or "application/octet-stream",
        )
    if message.photo:
        photo = message.photo[-1]  # last entry is the highest resolution
        return PendingFile(
            file_id=photo.file_id,
            filename=f"photo_{photo.file_unique_id}.jpg",
            size=photo.file_size or 0,
            mime="image/jpeg",
        )
    return None


def too_large(pending: PendingFile) -> bool:
    """True if the known size exceeds the Bot API limit (0/None => unknown)."""
    return bool(pending.size) and pending.size > MAX_TG_FILE_BYTES


async def download(bot: Bot, file_id: str) -> bytes:
    """Download a Telegram file into memory as bytes."""
    buffer = await bot.download(file_id)
    if buffer is None:  # pragma: no cover - only when a destination is given
        return b""
    return buffer.read()

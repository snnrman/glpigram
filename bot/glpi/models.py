"""Dataclasses mirroring the GLPI objects the bot works with.

Only the fields the bot actually uses are modelled; ``from_api`` tolerates the
extra keys GLPI returns so the models survive minor schema differences.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ITILCategory:
    id: int
    name: str
    completename: str

    @classmethod
    def from_api(cls, raw: dict) -> ITILCategory:
        return cls(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            # completename holds the full "Parent > Child" path when present.
            completename=str(raw.get("completename") or raw.get("name", "")),
        )


@dataclass(slots=True)
class Ticket:
    id: int
    name: str
    content: str
    status: int
    urgency: int
    itilcategories_id: int | None = None

    @classmethod
    def from_api(cls, raw: dict) -> Ticket:
        cat = raw.get("itilcategories_id")
        return cls(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            content=str(raw.get("content", "")),
            status=int(raw.get("status", 0) or 0),
            urgency=int(raw.get("urgency", 0) or 0),
            itilcategories_id=int(cat) if cat else None,
        )


@dataclass(slots=True)
class TicketSummary:
    """Compact ticket view for the /tickets list (feature 3)."""

    id: int
    title: str
    status: int
    assignee: str | None = None


@dataclass(slots=True)
class Followup:
    id: int
    tickets_id: int
    content: str
    users_id: int
    is_private: bool = False
    date: str | None = None

    @classmethod
    def from_api(cls, raw: dict) -> Followup:
        return cls(
            id=int(raw["id"]),
            tickets_id=int(raw.get("items_id", 0) or 0),
            content=str(raw.get("content", "")),
            users_id=int(raw.get("users_id", 0) or 0),
            is_private=_as_bool(raw.get("is_private"), default=False),
            date=raw.get("date_creation") or raw.get("date") or None,
        )


def _as_bool(value: object, *, default: bool) -> bool:
    """Coerce GLPI's 0/1 / "0"/"1" / true/false flags to a bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


@dataclass(slots=True)
class User:
    id: int
    name: str  # AD sAMAccountName (the linking key)
    email: str | None = None
    firstname: str | None = None
    realname: str | None = None
    is_active: bool = True
    is_deleted: bool = False

    @property
    def is_usable(self) -> bool:
        """Active and not (soft-)deleted — i.e. still a valid, enabled account."""
        return self.is_active and not self.is_deleted

    @property
    def display_name(self) -> str:
        """Human name for messages: "First Last" when known, else the login."""
        full = " ".join(p for p in (self.firstname, self.realname) if p).strip()
        return full or self.name

    @classmethod
    def from_api(cls, raw: dict) -> User:
        return cls(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            email=raw.get("email") or None,
            firstname=raw.get("firstname") or None,
            realname=raw.get("realname") or None,
            is_active=_as_bool(raw.get("is_active"), default=True),
            is_deleted=_as_bool(raw.get("is_deleted"), default=False),
        )

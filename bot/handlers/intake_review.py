"""Versioned technician review and category corrections for AI intake tickets."""

from __future__ import annotations

import html
import time

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from .. import texts
from ..glpi.client import GlpiError
from ..services.intake import CATEGORY_NAMES, category_matches
from ..services.intake_snapshot import refresh_intake_snapshot
from ..texts.intake_render import CATEGORY_NAMES as DISPLAY_NAMES


class ReviewUnavailable(Exception):
    pass


def build_intake_review_router(client, repo, *, intake, cards=None):
    """Only category_id(key) is needed from intake; review works with intake disabled."""
    router = Router(name="intake_review")
    names = DISPLAY_NAMES.get(getattr(texts, "_lang", "ru"), DISPLAY_NAMES["ru"])

    async def refresh(bot, tid, line=None):
        if cards:
            await cards.record_event(bot, tid, line)

    async def current(tid, version, bot):
        row = await repo.get_ticket_intake(tid)
        if row is None or row["version"] != version:
            await refresh(bot, tid)
            raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
        ticket = await client.get_ticket(tid)
        if ticket is None or ticket.status in (5, 6):
            raise ReviewUnavailable(texts.INTAKE_REVIEW_CLOSED)
        current_row = await refresh_intake_snapshot(repo, ticket)
        if current_row["version"] != version:
            await refresh(bot, tid)
            raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
        return current_row, ticket

    async def sync_external(bot, row, ticket):
        """An external edit invalidates old keyboards; never overwrite it silently."""
        actual = ticket.itilcategories_id or 0
        if actual == row["category_id"]:
            return
        key = next((k for k in CATEGORY_NAMES if intake.category_id(k) == actual), "external")
        await repo.update_ticket_intake(
            ticket.id,
            expected_version=row["version"],
            actor_tg_id=None,
            now=int(time.time()),
            event="external_category",
            category=key,
            category_id=actual,
            details={"from": row["category_id"], "to": actual},
        )
        await refresh(bot, ticket.id)
        raise ReviewUnavailable(texts.INTAKE_CATEGORY_CHANGED_EXTERNALLY)

    async def no_approval(tid, ticket, row):
        reserved = await repo.get_tech_approval(tid)
        approval = await repo.get_approval(tid)
        # Editing an already approved request invalidates the local review.
        # Only an explicit new technician decision can reuse the granted
        # validation; pending/refused/incompletely registered cycles still block.
        if row.get("approval_recheck") and ticket.global_validation == 3:
            claude = await repo.get_claude_approval(tid)
            if (
                (approval is None or approval["status"] == 1)
                and (reserved is None or reserved["phase"] in ("done", "failed", "cancelled"))
                and (claude is None or claude["stage"] == "approved")
            ):
                return True
        if (
            ticket.global_validation not in (0, 1)
            or approval
            or (reserved and reserved["phase"] not in ("failed", "cancelled"))
        ):
            raise ReviewUnavailable(texts.INTAKE_REVIEW_APPROVAL_EXISTS)
        return False

    @router.callback_query(F.data.startswith("ir:") & ~F.data.startswith("ir:approval:"))
    async def review(cb: CallbackQuery, link, bot):
        if not link.is_tech:
            await cb.answer(texts.TECH_ONLY, show_alert=True)
            return
        try:
            parts = cb.data.split(":")
            action, tid, version = parts[1], int(parts[2]), int(parts[3])
            if tid <= 0 or version <= 0 or action not in ("routine", "category", "set"):
                raise ValueError
            if len(parts) != (6 if action == "set" else 4):
                raise ValueError
            async with repo.ticket_action_lock(tid):
                row, ticket = await current(tid, version, bot)
                await sync_external(bot, row, ticket)
                if action == "routine":
                    if row["review_state"] != "pending":
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    rechecked = await no_approval(tid, ticket, row)
                    saved = await repo.update_ticket_intake(
                        tid,
                        expected_version=version,
                        actor_tg_id=link.tg_id,
                        now=int(time.time()),
                        event="routine_recheck" if rechecked else "routine",
                        review_state="routine",
                        details={"existing_approval_rechecked": True} if rechecked else None,
                    )
                    if not saved:
                        await refresh(bot, tid)
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    await refresh(
                        bot,
                        tid,
                        texts.intake_routine_history(link.display_name),
                    )
                elif action == "category":
                    rows = [
                        [
                            InlineKeyboardButton(
                                text=names[k],
                                callback_data=f"ir:set:{tid}:{version}:{row['category_id']}:{k}",
                            )
                        ]
                        for k in CATEGORY_NAMES
                    ]
                    if cb.message is None:
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    await bot.send_message(
                        cb.message.chat.id,
                        texts.INTAKE_CATEGORY_PICK,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    )
                    await cb.answer()
                    return
                else:
                    expected, key = int(parts[4]), parts[5]
                    if key not in CATEGORY_NAMES or expected != (ticket.itilcategories_id or 0):
                        await refresh(bot, tid)
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    target = intake.category_id(key)
                    categories = await client.list_categories()
                    if not any(c.id == target and category_matches(c, key) for c in categories):
                        raise ReviewUnavailable(texts.INTAKE_CATEGORY_UNAVAILABLE)
                    # Re-read after the dropdown request, immediately before PUT.
                    latest = await client.get_ticket(tid)
                    if latest is None or latest.status in (5, 6):
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_CLOSED)
                    newest = await refresh_intake_snapshot(repo, latest)
                    if newest["version"] != version:
                        await refresh(bot, tid)
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    await sync_external(bot, row, latest)
                    if target != expected:
                        await client.set_ticket_category(tid, target)
                        after = await client.get_ticket(tid)
                        if after is None or after.itilcategories_id != target:
                            raise ReviewUnavailable(texts.INTAKE_CATEGORY_UNCERTAIN)
                    saved = await repo.update_ticket_intake(
                        tid,
                        expected_version=version,
                        actor_tg_id=link.tg_id,
                        now=int(time.time()),
                        event="category_changed",
                        category=key,
                        category_id=target,
                        details={"from": expected, "to": target},
                    )
                    if not saved:
                        await refresh(bot, tid)
                        raise ReviewUnavailable(texts.INTAKE_REVIEW_STALE)
                    await refresh(bot, tid, f"📁 {html.escape(link.display_name)}: {names[key]}")
            await cb.answer(texts.INTAKE_REVIEW_SAVED)
        except (ValueError, IndexError, KeyError):
            await cb.answer(texts.INTAKE_REVIEW_STALE, show_alert=True)
        except ReviewUnavailable as exc:
            await cb.answer(str(exc), show_alert=True)
        except GlpiError:
            # An ambiguous PUT is never blindly repeated; next click re-reads GLPI.
            await cb.answer(texts.GLPI_ACTION_ERROR, show_alert=True)

    return router

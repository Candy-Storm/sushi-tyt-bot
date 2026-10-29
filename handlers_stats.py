"""Статистика для власника: кнопка «📊 Статистика» або команда /stats.

Зведення за обраний період (сьогодні, 7 днів, 30 днів, весь час): клієнти,
прокрутки, що випало, промокоди (видано, використано, прострочено).
Період перемикається кнопками під повідомленням.
"""
from __future__ import annotations

import html
import logging
import time as time_module
from datetime import datetime, time, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from database import Database
from handlers_broadcast import IsOwner
from keyboards import BTN_STATS
from prizes import PRIZES_BY_KEY

log = logging.getLogger(__name__)

router = Router(name="stats")
router.message.filter(F.chat.type == "private", IsOwner())
router.callback_query.filter(IsOwner())

PERIODS = {"today": "Сьогодні", "7": "7 днів", "30": "30 днів", "all": "Весь час"}
DEFAULT_PERIOD = "7"


# --------------------------------------------------------------------- період
def period_bounds(key: str, now: datetime) -> tuple[int, int, str]:
    """(початок, кінець, підпис) періоду за місцевим часом; кінець — наступна опівніч."""
    tz = now.tzinfo
    today = now.date()

    def midnight(day) -> int:
        return int(datetime.combine(day, time.min, tzinfo=tz).timestamp())

    until = midnight(today + timedelta(days=1))
    if key == "today":
        return midnight(today), until, f"{today:%d.%m.%Y}"
    if key in ("7", "30"):
        first = today - timedelta(days=int(key) - 1)
        return midnight(first), until, f"{first:%d.%m}–{today:%d.%m.%Y}"
    return 0, until, "від початку роботи бота"


# --------------------------------------------------------------------- текст
def _pct(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.0f}%" if whole else "0%"


def _title(prize_key: str) -> str:
    prize = PRIZES_BY_KEY.get(prize_key)
    return html.escape(prize.title if prize else prize_key)


def render_stats(stats: dict, key: str, label: str) -> str:
    lines = [f"📊 <b>Статистика: {PERIODS[key].lower()}</b>", f"<i>{label}</i>", ""]

    lines += [
        "👥 <b>Клієнти</b>",
        f"Зареєстровано всього: <b>{stats['users_total']}</b>",
        f"Нових за період: <b>{stats['users_new']}</b>",
        f"Вимкнули сповіщення: {stats['users_muted']} · заблокували бота: {stats['users_blocked']}",
        "",
        "🎡 <b>Рулетка</b>",
    ]
    if stats["spins"] == 0:
        lines.append("За цей період прокруток не було.")
    else:
        lines.append(
            f"Прокруток: <b>{stats['spins']}</b> · крутили: <b>{stats['spinners']}</b> клієнтів"
        )
        if key != "today":
            lines.append(
                f"Повернулися (крутили в різні дні): <b>{stats['returning']}</b> "
                f"({_pct(stats['returning'], stats['spinners'])})"
            )
        lines += ["", "🎁 <b>Що випало</b>"]
        for prize_key, count in stats["spins_by_prize"]:
            lines.append(f"• {_title(prize_key)} — {count} ({_pct(count, stats['spins'])})")

    by_status = stats["promo_by_status"]
    issued = sum(by_status.values())
    used = by_status.get("used", 0)
    lines += ["", "🎟 <b>Промокоди</b>"]
    if issued == 0:
        lines.append("За цей період промокоди не видавалися.")
    else:
        lines += [
            f"Видано: <b>{issued}</b>",
            f"✅ Використано: <b>{used}</b> ({_pct(used, issued)})",
            f"🟢 Ще активні: {by_status.get('active', 0)} · ⌛ прострочені: {by_status.get('expired', 0)}",
        ]
        lines += ["", "<b>Які призи забирають</b> (використано з виданих)"]
        for prize_key, total, used_count in stats["promo_by_prize"]:
            lines.append(f"• {_title(prize_key)} — {used_count} з {total} ({_pct(used_count, total)})")
        lines.append(f"\nПогашено касирами за період: {stats['redeemed']}")

    lines += ["", "<i>Прокрутки, стерті командою /reset, у статистику не входять.</i>"]
    return "\n".join(lines)


def period_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, title in PERIODS.items():
        builder.button(text=f"• {title} •" if key == current else title, callback_data=f"st:{key}")
    builder.adjust(2)
    return builder.as_markup()


async def build_stats(db: Database, settings: Settings, key: str) -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(settings.tz)
    since, until, label = period_bounds(key, now)
    stats = await db.get_stats(since, until, int(time_module.time()))
    return render_stats(stats, key, label), period_keyboard(key)


# --------------------------------------------------------------------- хендлери
@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def show_stats(message: Message, db: Database, settings: Settings) -> None:
    text, markup = await build_stats(db, settings, DEFAULT_PERIOD)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("st:"))
async def switch_period(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in PERIODS:
        await callback.answer("Невідомий період.", show_alert=True)
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    text, markup = await build_stats(db, settings, key)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in exc.message.lower():
            log.warning("Не вдалося оновити статистику: %s", exc.message)

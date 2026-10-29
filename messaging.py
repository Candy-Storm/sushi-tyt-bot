"""Надсилання повідомлень клієнтам: ручна розсилка власника й автоматичні нагадування.

Правила, які діють для всіх повідомлень цього модуля:
  * не надсилаємо тим, хто вимкнув сповіщення або заблокував бота;
  * кожне повідомлення має кнопку «Вимкнути сповіщення»;
  * між відправками робимо невеликі паузи (ліміт Telegram — приблизно 30
    повідомлень на секунду), а при FloodWait чекаємо вказаний час;
  * якщо клієнт заблокував бота, позначаємо це в базі й більше не пишемо.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from config import (
    AUTO_REMINDER_COOLDOWN_SECONDS,
    ROULETTE_REMINDER_MAX_UNANSWERED,
    ROULETTE_REMINDER_REPEAT_DAYS,
    Settings,
)
from database import Database
from keyboards import notify_keyboard
from prizes import PRIZES_BY_KEY
from utils import fmt_dt, fmt_duration, in_quiet_hours, promo_reminder_due_ts

log = logging.getLogger(__name__)

MAX_TEXT_LEN = 4096          # ліміт Telegram для тексту повідомлення
MAX_CAPTION_LEN = 1024       # ліміт Telegram для підпису до фото
SEND_DELAY = 0.05            # пауза між відправками (≈20 повідомлень на секунду)
MIN_PROMO_LEFT_SECONDS = 10 * 60   # якщо до кінця коду менше 10 хвилин — не нагадуємо
REMINDER_CHECK_INTERVAL = 300      # як часто перевіряємо, кому пора нагадати

# Ці помилки Telegram означають, що написати клієнту вже неможливо.
_UNREACHABLE_MARKERS = ("chat not found", "user is deactivated", "bot was blocked")

ROULETTE_REMINDER_TEXT = (
    "🎡 <b>Рулетка чекає на вас!</b>\n\n"
    "Ви давно не крутили колесо. Сьогодні вам доступна безкоштовна спроба: "
    "натисніть «🎡 Крутити рулетку» в меню й вигравайте знижки, ролли та напої 🍣"
)


@dataclass(frozen=True)
class Content:
    """Те, що надсилаємо: текст або фото з підписом (текст — у форматі HTML)."""

    kind: str                      # "text" або "photo"
    text: str
    photo_id: str | None = None


@dataclass
class SendReport:
    total: int = 0
    delivered: int = 0
    blocked: int = 0
    failed: int = 0


# --------------------------------------------------------------------- відправка
async def send_content(
    bot: Bot, chat_id: int, content: Content, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Надсилає одне повідомлення. Помилки Telegram не перехоплює."""
    if content.kind == "photo" and content.photo_id:
        await bot.send_photo(
            chat_id, content.photo_id, caption=content.text or None, reply_markup=reply_markup
        )
    else:
        await bot.send_message(chat_id, content.text, reply_markup=reply_markup)


async def deliver(
    bot: Bot,
    db: Database,
    user_id: int,
    content: Content,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """Надсилає повідомлення клієнту. Повертає "ok", "blocked" або "failed"."""
    for _ in range(3):
        try:
            await send_content(bot, user_id, content, reply_markup)
            return "ok"
        except TelegramRetryAfter as exc:
            log.warning("Telegram просить зачекати %s с", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            await db.set_blocked(user_id, True)
            return "blocked"
        except TelegramBadRequest as exc:
            if any(marker in exc.message.lower() for marker in _UNREACHABLE_MARKERS):
                await db.set_blocked(user_id, True)
                return "blocked"
            log.warning("Не вдалося надіслати повідомлення %s: %s", user_id, exc.message)
            return "failed"
        except TelegramAPIError as exc:
            log.warning("Помилка Telegram для %s: %s", user_id, exc.message)
            return "failed"
    return "failed"


async def broadcast(
    bot: Bot,
    db: Database,
    recipients: Sequence[int],
    content: Content,
    delay: float = SEND_DELAY,
) -> SendReport:
    """Ручна розсилка: надсилає content усім recipients і повертає підсумок."""
    report = SendReport(total=len(recipients))
    markup = notify_keyboard(True)
    for user_id in recipients:
        result = await deliver(bot, db, user_id, content, markup)
        if result == "ok":
            report.delivered += 1
        elif result == "blocked":
            report.blocked += 1
        else:
            report.failed += 1
        await asyncio.sleep(delay)
    return report


# --------------------------------------------------------------------- автонагадування
def promo_reminder_text(promos: Sequence[Mapping], now: int, tz: ZoneInfo) -> str:
    """Текст нагадування про один чи кілька промокодів, що скоро згорять."""
    lines = ["⏰ <b>Ваш промокод скоро згорить!</b>" if len(promos) == 1
             else "⏰ <b>Ваші промокоди скоро згорять!</b>", ""]
    for promo in promos:
        prize = PRIZES_BY_KEY.get(promo["prize_key"])
        title = prize.title if prize else promo["prize_key"]
        left = fmt_duration(promo["expires_at"] - now)
        lines.append(
            f"🎟 <code>{promo['code']}</code> — {title}\n"
            f"   діє до {fmt_dt(promo['expires_at'], tz)} (ще {left})"
        )
    lines += ["", "Встигніть використати при самовиносі 🍣"]
    return "\n".join(lines)


async def run_reminders_once(
    bot: Bot, db: Database, settings: Settings, now: int, delay: float = SEND_DELAY
) -> dict[str, int]:
    """Один прохід автоматичних нагадувань. Повертає, скільки надіслано кожного виду.

    Ліміти: тихі години, не більше одного автоматичного повідомлення на клієнта
    за добу, кожне нагадування про код надсилається лише один раз, «рулетка
    чекає» — не частіше ніж раз на кілька днів і не більше двох поспіль без
    жодної прокрутки.
    """
    sent = {"promo": 0, "roulette": 0}
    if in_quiet_hours(datetime.fromtimestamp(now, settings.tz)):
        return sent

    await db.expire_old(now)
    cooldown = await db.users_reminded_since(now - AUTO_REMINDER_COOLDOWN_SECONDS)
    markup = notify_keyboard(True)

    # 1) Промокоди, що скоро згорять. Кілька кодів одного клієнта — одне повідомлення.
    due: dict[int, list] = {}
    for promo in await db.get_remindable_promos(now):
        if promo["expires_at"] - now < MIN_PROMO_LEFT_SECONDS:
            continue
        if now < promo_reminder_due_ts(promo["expires_at"], settings.tz):
            continue
        due.setdefault(promo["telegram_id"], []).append(promo)

    for user_id, promos in due.items():
        if user_id in cooldown:
            continue
        text = promo_reminder_text(promos, now, settings.tz)
        result = await deliver(bot, db, user_id, Content("text", text), markup)
        if result != "blocked":
            # Навіть після помилки не повторюємо: щоб не засипати журнал і клієнта.
            for promo in promos:
                await db.log_reminder(user_id, "promo", promo["id"], now)
        if result == "ok":
            sent["promo"] += 1
        cooldown.add(user_id)
        await asyncio.sleep(delay)

    # 2) «Рулетка чекає» для тих, хто давно не крутив.
    for row in await db.get_idle_users(now):
        user_id = row["telegram_id"]
        if user_id in cooldown:
            continue
        count, last = await db.roulette_reminder_stats(user_id, row["last_activity"])
        if count >= ROULETTE_REMINDER_MAX_UNANSWERED:
            continue
        if last is not None and now - last < ROULETTE_REMINDER_REPEAT_DAYS * 86400:
            continue
        result = await deliver(bot, db, user_id, Content("text", ROULETTE_REMINDER_TEXT), markup)
        if result != "blocked":
            await db.log_reminder(user_id, "roulette", None, now)
        if result == "ok":
            sent["roulette"] += 1
        cooldown.add(user_id)
        await asyncio.sleep(delay)

    if sent["promo"] or sent["roulette"]:
        log.info("Надіслано нагадувань: про промокод %s, «рулетка чекає» %s",
                 sent["promo"], sent["roulette"])
    return sent


async def reminder_loop(bot: Bot, db: Database, settings: Settings) -> None:
    """Фонове завдання: раз на кілька хвилин розсилає автоматичні нагадування."""
    while True:
        try:
            await run_reminders_once(bot, db, settings, int(time.time()))
        except Exception:
            log.exception("Помилка під час автоматичних нагадувань")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)

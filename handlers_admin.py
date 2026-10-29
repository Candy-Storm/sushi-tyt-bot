"""Панель касира: пошук клієнта за телефоном, перегляд кодів, погашення."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import Settings
from database import Database
from handlers_user import show_start
from keyboards import (
    BTN_ADMIN_PANEL,
    BTN_BACK,
    BTN_CANCEL,
    BTN_CHECK_CLIENT,
    admin_menu,
    cancel_menu,
    use_codes_keyboard,
)
from prizes import PRIZES_BY_KEY
from utils import fmt_dt, local_day, normalize_phone

log = logging.getLogger(__name__)

# Промокод має вигляд «префікс-номер», напр. sushityt10-5 (префікс задає власник).
CODE_RE = re.compile(r"^[a-z0-9]+-\d+$", re.IGNORECASE)


class IsAdmin(BaseFilter):
    """Пропускає лише користувачів зі списку ADMIN_IDS (.env)."""

    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return event.from_user is not None and event.from_user.id in settings.admin_ids


class AdminStates(StatesGroup):
    waiting_phone = State()


router = Router(name="admin")
router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(IsAdmin())

STATUS_TITLES = {
    "active": "🟢 Активні",
    "used": "✅ Використані",
    "expired": "⌛ Прострочені",
}


# --------------------------------------------------------------------- відображення
async def render_client(
    db: Database, settings: Settings, user
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст зі списком кодів клієнта та клавіатура з кнопками погашення."""
    tz = settings.tz
    promos = await db.get_promos_for_user(user["telegram_id"], int(time.time()))

    lines = [f"👤 Клієнт: <code>{user['phone']}</code>"]
    if not promos:
        lines += ["", "У цього клієнта ще немає промокодів."]

    for status in ("active", "used", "expired"):
        group = [p for p in promos if p["status"] == status]
        if not group:
            continue
        lines += ["", f"<b>{STATUS_TITLES[status]}</b>"]
        for promo in group:
            prize = PRIZES_BY_KEY.get(promo["prize_key"])
            title = prize.title if prize else promo["prize_key"]
            line = f"• <code>{promo['code']}</code> — {title}\n   отримано: {fmt_dt(promo['created_at'], tz)}"
            if status == "active":
                line += f"\n   діє до: {fmt_dt(promo['expires_at'], tz)}"
            elif status == "used" and promo["used_at"]:
                line += f"\n   використано: {fmt_dt(promo['used_at'], tz)}"
            else:
                line += f"\n   закінчився: {fmt_dt(promo['expires_at'], tz)}"
            lines.append(line)

    return "\n".join(lines), use_codes_keyboard(promos)


# --------------------------------------------------------------------- команди й кнопки
@router.message(CommandStart())
@router.message(F.text == BTN_BACK)
async def admin_to_client_menu(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    await state.clear()
    await show_start(message, db, settings)


@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN_PANEL)
async def open_panel(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await message.answer(
        "🛠 <b>Панель касира</b>",
        reply_markup=admin_menu(is_owner=message.from_user.id in settings.owner_ids),
    )


@router.message(Command("reset"))
async def reset_spins(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    """/reset — скинути сьогоднішні прокрутки собі; /reset +380... — клієнту."""
    arg = (command.args or "").strip()
    if arg:
        phone = normalize_phone(arg)
        if phone is None:
            await message.answer("Невірний формат номера. Приклад: <code>/reset +380661234567</code>")
            return
        user = await db.get_user_by_phone(phone)
        if user is None:
            await message.answer(f"Клієнта з номером <code>{phone}</code> не знайдено.")
            return
    else:
        user = await db.get_user(message.from_user.id)
        if user is None:
            await message.answer(
                "Ви ще не зареєстровані як клієнт. Натисніть /start і поділіться номером, "
                "потім повторіть /reset."
            )
            return

    day = local_day(datetime.now(settings.tz))
    deleted = await db.reset_spins(user["telegram_id"], day)
    log.info("Адмін %s скинув прокрутки клієнта %s (видалено %s)", message.from_user.id, user["telegram_id"], deleted)
    await message.answer(
        f"🔄 Прокрутки за сьогодні скинуто для <code>{user['phone']}</code> "
        f"(видалено записів: {deleted}). Тепер можна крутити рулетку знову.\n"
        "Уже виграні промокоди залишились."
    )


@router.message(F.text == BTN_CANCEL)
async def cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await message.answer(
        "Скасовано.",
        reply_markup=admin_menu(is_owner=message.from_user.id in settings.owner_ids),
    )


@router.message(F.text == BTN_CHECK_CLIENT)
async def ask_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_phone)
    await message.answer(
        "Введіть номер телефону клієнта у форматі <b>+380XXXXXXXXX</b>\n"
        "(або сам промокод, наприклад <code>sushityt10-5</code>).",
        reply_markup=cancel_menu(),
    )


@router.message(AdminStates.waiting_phone, F.text, ~F.text.startswith("/"))
async def on_phone(message: Message, db: Database, settings: Settings) -> None:
    text = message.text.strip()

    phone = normalize_phone(text)
    if phone is None and CODE_RE.match(text):
        # Схоже на промокод (будь-який префікс — власник може додавати свої призи).
        promo = await db.get_promo_by_code(text)
        user = await db.get_user(promo["telegram_id"]) if promo else None
        if user is None:
            await message.answer("Такого промокоду не знайдено. Спробуйте ще раз.")
            return
    else:
        if phone is None:
            await message.answer("Невірний формат. Потрібно: <b>+380XXXXXXXXX</b>. Спробуйте ще раз.")
            return
        user = await db.get_user_by_phone(phone)
        if user is None:
            await message.answer(
                f"Клієнта з номером <code>{phone}</code> не знайдено — він ще не реєструвався в боті."
            )
            return

    body, keyboard = await render_client(db, settings, user)
    await message.answer(body, reply_markup=keyboard)
    await message.answer("Для наступного клієнта введіть номер або натисніть «❌ Скасувати».")


# --------------------------------------------------------------------- погашення коду
@router.callback_query(F.data.startswith("use:"))
async def on_mark_used(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        promo_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некоректні дані кнопки.", show_alert=True)
        return

    promo = await db.get_promo(promo_id)
    if promo is None:
        await callback.answer("Код не знайдено.", show_alert=True)
        return

    if await db.mark_used(promo_id, int(time.time())):
        await callback.answer("Код позначено як використаний ✅")
    else:
        await callback.answer(
            "Цей код уже не активний: він використаний або прострочений.", show_alert=True
        )

    # Оновлюємо повідомлення зі списком кодів клієнта.
    user = await db.get_user(promo["telegram_id"])
    if user is None or callback.message is None:
        return
    body, keyboard = await render_client(db, settings, user)
    try:
        await callback.message.edit_text(body, reply_markup=keyboard)
    except TelegramBadRequest:
        # «message is not modified» та подібні безпечні помилки ігноруємо.
        pass

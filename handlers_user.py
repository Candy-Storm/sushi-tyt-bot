"""Хендлери для клієнтів: реєстрація за контактом, рулетка, «Мої промокоди»."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, FSInputFile, Message

import wheel
from config import Settings
from database import Database, SpinResult
from keyboards import (
    BTN_MY_CODES,
    BTN_NOTIFY,
    BTN_SPIN,
    contact_keyboard,
    main_menu,
    notify_keyboard,
)
from prizes import PRIZES_BY_KEY
from utils import fmt_dt, fmt_duration, local_day, normalize_phone, seconds_until_next_day

log = logging.getLogger(__name__)

router = Router(name="user")
router.message.filter(F.chat.type == "private")

# Після першої відправки GIF Telegram дає file_id — далі надсилаємо без повторного
# завантаження файлу (швидше і дешевше).
_wheel_file_ids: dict[str, str] = {}


# --------------------------------------------------------------------- допоміжне
async def ask_contact(message: Message, retry: bool = False) -> None:
    """Просить поділитися номером через кнопку RequestContact."""
    if retry:
        text = (
            "📱 Номер потрібно надіслати кнопкою <b>«Поділитися номером»</b> внизу екрана.\n"
            "Номер, введений вручну, не приймається — так ми переконуємось, що він справжній."
        )
    else:
        text = (
            "Привіт! 👋 Це бот суші-бару <b>«Sushi tyt»</b> 🍣\n\n"
            "Тут можна щодня крутити рулетку й вигравати знижки, ролли та напої "
            "для самовиносу.\n\n"
            "Щоб почати, поділіться номером телефону — натисніть кнопку "
            "<b>«Поділитися номером»</b> внизу екрана."
        )
    await message.answer(text, reply_markup=contact_keyboard())


async def show_start(message: Message, db: Database, settings: Settings) -> None:
    """Головне меню клієнта або запит контакту, якщо він ще не зареєстрований."""
    user = await db.get_user(message.from_user.id)
    if user is None:
        await ask_contact(message)
        return
    await message.answer(
        "🍣 <b>Sushi tyt</b> — головне меню.\nОберіть дію:",
        reply_markup=main_menu(is_admin=message.from_user.id in settings.admin_ids),
    )


async def _send_wheel(message: Message, prize_key: str) -> Message | None:
    """Надсилає анімацію колеса, що зупиняється на виграному призі.

    Повертає надіслане повідомлення або None, якщо анімацію показати не вдалося
    (тоді викликач покаже простий текстовий варіант).
    """
    # Ім'я файлу містить відбиток налаштувань призів, тож після зміни призів
    # старий file_id із кешу не використовується.
    path = wheel.gif_path(prize_key)
    cache_key = path.name
    cached = _wheel_file_ids.get(cache_key)
    if cached is None and not path.exists():
        return None
    media = cached or FSInputFile(path)
    try:
        sent = await message.answer_animation(media, caption="🎡 Крутимо колесо...")
    except TelegramAPIError:
        log.warning("Не вдалося надіслати анімацію колеса", exc_info=True)
        _wheel_file_ids.pop(cache_key, None)
        return None
    media_obj = sent.animation or sent.document
    if media_obj is not None:
        _wheel_file_ids[cache_key] = media_obj.file_id
    return sent


def _result_text(result: SpinResult, tz: ZoneInfo) -> str:
    prize = result.prize
    if result.code and result.expires_at:
        return (
            f"🎉 <b>Вітаємо! Ви виграли:</b> {prize.title}\n\n"
            f"🎟 Ваш промокод: <code>{result.code}</code>\n"
            f"⏳ Діє 24 години — до {fmt_dt(result.expires_at, tz)}\n\n"
            "Назвіть цей код касиру при самовиносі."
        )
    if prize.grants_extra_spin:
        return (
            "🔁 <b>Прокрутити ще раз!</b>\n\n"
            "У вас є додаткова спроба — натисніть «🎡 Крутити рулетку» ще раз."
        )
    return (
        "🍀 <b>Спробуй завтра!</b>\n\n"
        "Цього разу без призу, але завтра рулетка знову чекатиме на вас."
    )


# --------------------------------------------------------------------- хендлери
@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, settings: Settings) -> None:
    await show_start(message, db, settings)


@router.message(F.contact)
async def on_contact(message: Message, db: Database, settings: Settings) -> None:
    contact = message.contact

    # Приймаємо лише власний контакт, а не чужий, пересланий зі списку.
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Будь ласка, надішліть <b>свій</b> номер кнопкою «Поділитися номером».",
            reply_markup=contact_keyboard(),
        )
        return

    phone = normalize_phone(contact.phone_number)
    if phone is None:
        await message.answer(
            "Бот працює з українськими номерами у форматі <b>+380XXXXXXXXX</b>. "
            "Ваш номер не підходить. Якщо це помилка, зверніться до адміністратора.",
            reply_markup=contact_keyboard(),
        )
        return

    saved = await db.save_user(message.from_user.id, phone, int(time.time()))
    if not saved:
        await message.answer(
            "Цей номер уже прив'язаний до іншого акаунта Telegram. "
            "Зверніться до адміністратора, якщо це помилка."
        )
        return

    await message.answer(
        f"✅ Номер <code>{phone}</code> збережено!\nТепер можна крутити рулетку 🎡",
        reply_markup=main_menu(is_admin=message.from_user.id in settings.admin_ids),
    )


@router.message(F.text == BTN_SPIN)
async def on_spin(message: Message, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    if await db.get_user(user_id) is None:
        await ask_contact(message)
        return

    now = datetime.now(settings.tz)
    try:
        result = await db.try_spin(user_id, local_day(now), int(now.timestamp()))
    except Exception:
        log.exception("Помилка під час прокрутки рулетки (user_id=%s)", user_id)
        await message.answer("😔 Сталася помилка. Спробуйте, будь ласка, трохи пізніше.")
        return

    if result is None:
        wait = fmt_duration(seconds_until_next_day(now))
        await message.answer(
            "🎡 Сьогодні ви вже крутили рулетку.\n"
            f"Наступна спроба — завтра о 00:00, тобто через <b>{wait}</b>."
        )
        return

    text = _result_text(result, settings.tz)

    # Колесо крутиться й зупиняється на виграному призі; після цього
    # підпис під анімацією замінюється результатом.
    spinning = await _send_wheel(message, result.prize.key)
    if spinning is not None:
        await asyncio.sleep(wheel.SPIN_SECONDS + 0.7)
        try:
            await spinning.edit_caption(caption=text)
        except TelegramBadRequest:
            await message.answer(text)
        return

    # Запасний варіант без анімації (немає GIF або Telegram не прийняв файл).
    spinning = await message.answer("🎡 Крутимо колесо...")
    await asyncio.sleep(1.5)
    try:
        await spinning.edit_text(text)
    except TelegramBadRequest:
        await message.answer(text)


@router.message(F.text == BTN_MY_CODES)
async def on_my_codes(message: Message, db: Database, settings: Settings) -> None:
    user_id = message.from_user.id
    if await db.get_user(user_id) is None:
        await ask_contact(message)
        return

    now = int(time.time())
    promos = await db.get_active_promos(user_id, now)
    if not promos:
        await message.answer("У вас немає активних промокодів. Крутіть рулетку 🎡")
        return

    lines = ["🎟 <b>Ваші активні промокоди:</b>", ""]
    for promo in promos:
        prize = PRIZES_BY_KEY.get(promo["prize_key"])
        title = prize.title if prize else promo["prize_key"]
        left = fmt_duration(promo["expires_at"] - now)
        lines.append(
            f"• <code>{promo['code']}</code> — {title}\n"
            f"   діє до {fmt_dt(promo['expires_at'], settings.tz)} (ще {left})"
        )
    await message.answer("\n".join(lines))


def _notify_text(enabled: bool) -> str:
    state = "увімкнено 🔔" if enabled else "вимкнено 🔕"
    return (
        "🔔 <b>Сповіщення</b>\n\n"
        "Це нагадування про промокоди, що скоро згорять, про рулетку та новини закладу. "
        "Ми пишемо нечасто.\n\n"
        f"Зараз: <b>{state}</b>"
    )


@router.message(F.text == BTN_NOTIFY)
async def on_notify(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)
    if user is None:
        await ask_contact(message)
        return
    enabled = bool(user["notify"])
    await message.answer(_notify_text(enabled), reply_markup=notify_keyboard(enabled))


@router.callback_query(F.data.in_({"notify:on", "notify:off"}))
async def on_notify_toggle(callback: CallbackQuery, db: Database) -> None:
    """Кнопка вмикання/вимкнення сповіщень (є і в меню, і під кожним нагадуванням)."""
    if await db.get_user(callback.from_user.id) is None:
        await callback.answer("Спершу поділіться номером: натисніть /start.", show_alert=True)
        return
    enabled = callback.data == "notify:on"
    await db.set_notify(callback.from_user.id, enabled)
    await callback.answer("Сповіщення увімкнено 🔔" if enabled else "Сповіщення вимкнено 🔕")

    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        if message.text and message.text.startswith("🔔 Сповіщення"):
            # Це екран налаштувань: оновлюємо і текст зі станом, і кнопку.
            await message.edit_text(_notify_text(enabled), reply_markup=notify_keyboard(enabled))
        else:
            # Нагадування чи розсилка: лишаємо текст, міняємо тільки кнопку.
            await message.edit_reply_markup(reply_markup=notify_keyboard(enabled))
    except TelegramBadRequest:
        pass


@router.my_chat_member(F.chat.type == "private")
async def on_my_chat_member(event: ChatMemberUpdated, db: Database) -> None:
    """Telegram повідомляє, коли клієнт блокує бота або знову його розблоковує."""
    status = event.new_chat_member.status
    if status in ("kicked", "left"):
        await db.set_blocked(event.from_user.id, True)
    elif status == "member":
        await db.set_blocked(event.from_user.id, False)


@router.message()
async def fallback(message: Message, db: Database, settings: Settings) -> None:
    """Усе інше: нагадуємо про контакт або показуємо меню."""
    if await db.get_user(message.from_user.id) is None:
        await ask_contact(message, retry=True)
    else:
        await message.answer(
            "Скористайтеся кнопками меню 👇",
            reply_markup=main_menu(is_admin=message.from_user.id in settings.admin_ids),
        )

"""Ручна розсилка: доступна лише власнику (OWNER_IDS у .env).

Сценарій: «📢 Розсилка» → вибір групи → текст або фото з підписом →
попередній перегляд → «✅ Надіслати» → звіт про доставку.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import IDLE_DAYS_CHOICES, ROULETTE_IDLE_DAYS, Settings
from database import SEGMENTS, Database
from keyboards import (
    BTN_BROADCAST,
    admin_menu,
    cancel_menu,
    confirm_keyboard,
    idle_days_keyboard,
    notify_keyboard,
    segment_keyboard,
    segment_title,
)
from messaging import MAX_CAPTION_LEN, MAX_TEXT_LEN, Content, broadcast, send_content

log = logging.getLogger(__name__)


class IsOwner(BaseFilter):
    """Пропускає лише користувачів зі списку OWNER_IDS (.env)."""

    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return event.from_user is not None and event.from_user.id in settings.owner_ids


class BroadcastStates(StatesGroup):
    choosing_segment = State()
    choosing_idle_days = State()
    waiting_content = State()
    confirming = State()


router = Router(name="broadcast")
router.message.filter(F.chat.type == "private", IsOwner())
router.callback_query.filter(IsOwner())

# Одночасно може йти лише одна розсилка.
_running = False
# Посилання на фонові завдання, щоб їх не прибрав збирач сміття.
_tasks: set[asyncio.Task] = set()

_SEGMENT_PROMPT = (
    "📢 <b>Розсилка</b>\n\nКому надіслати? У дужках — скільки отримувачів. "
    "Тих, хто вимкнув сповіщення або заблокував бота, у списку немає."
)


# --------------------------------------------------------------------- початок і вибір групи
@router.message(Command("broadcast"))
@router.message(F.text == BTN_BROADCAST)
async def start_broadcast(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    now = int(time.time())
    counts = {segment: len(await db.get_recipients(segment, now)) for segment in SEGMENTS}
    if counts["all"] == 0:
        await message.answer(
            "📢 Поки немає кому надсилати: немає клієнтів із увімкненими сповіщеннями.",
            reply_markup=admin_menu(is_owner=True),
        )
        return
    await state.set_state(BroadcastStates.choosing_segment)
    await message.answer(_SEGMENT_PROMPT, reply_markup=segment_keyboard(counts))


async def _ask_for_content(
    callback: CallbackQuery, state: FSMContext, segment: str, days: int | None, count: int
) -> None:
    """Група вибрана: просимо надіслати текст або фото."""
    await state.update_data(segment=segment, days=days)
    await state.set_state(BroadcastStates.waiting_content)
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                f"📢 Група: <b>{segment_title(segment, days)}</b> ({count})"
            )
        except TelegramAPIError:
            pass
        await callback.message.answer(
            "✍️ Надішліть текст оголошення або фото з підписом.\n\n"
            "Можна користуватися форматуванням Telegram (жирний, курсив, посилання). "
            "Щоб передумати, натисніть «❌ Скасувати».",
            reply_markup=cancel_menu(),
        )


@router.callback_query(BroadcastStates.choosing_segment, F.data.startswith("bc:seg:"))
async def choose_segment(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    segment = callback.data.split(":", 2)[2]
    if segment not in SEGMENTS:
        await callback.answer("Невідома група.", show_alert=True)
        return

    now = int(time.time())
    if segment == "idle":
        # Для цієї групи власник ще обирає, скільки днів без прокруток.
        counts = {
            days: len(await db.get_recipients("idle", now, idle_days=days))
            for days in IDLE_DAYS_CHOICES
        }
        if not any(counts.values()):
            await callback.answer("У цій групі зараз нікого немає.", show_alert=True)
            return
        await state.set_state(BroadcastStates.choosing_idle_days)
        await callback.answer()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "😴 <b>Не крутили кілька днів</b>\n\n"
                    "Скільки днів без прокруток? У дужках — скільки отримувачів.",
                    reply_markup=idle_days_keyboard(counts),
                )
            except TelegramAPIError:
                pass
        return

    count = len(await db.get_recipients(segment, now))
    if count == 0:
        await callback.answer("У цій групі зараз нікого немає.", show_alert=True)
        return
    await _ask_for_content(callback, state, segment, None, count)


@router.callback_query(BroadcastStates.choosing_idle_days, F.data.startswith("bc:days:"))
async def choose_idle_days(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    try:
        days = int(callback.data.split(":", 2)[2])
    except ValueError:
        days = 0
    if days not in IDLE_DAYS_CHOICES:
        await callback.answer("Невідомий варіант.", show_alert=True)
        return
    count = len(await db.get_recipients("idle", int(time.time()), idle_days=days))
    if count == 0:
        await callback.answer("Для цього терміну нікого немає. Оберіть інший.", show_alert=True)
        return
    await _ask_for_content(callback, state, "idle", days, count)


@router.callback_query(BroadcastStates.choosing_idle_days, F.data == "bc:back")
async def back_to_segments(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    now = int(time.time())
    counts = {segment: len(await db.get_recipients(segment, now)) for segment in SEGMENTS}
    await state.set_state(BroadcastStates.choosing_segment)
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(_SEGMENT_PROMPT, reply_markup=segment_keyboard(counts))
        except TelegramAPIError:
            pass


# --------------------------------------------------------------------- вміст і перегляд
@router.message(BroadcastStates.waiting_content, F.photo)
async def got_photo(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    caption = message.caption or ""
    if len(caption) > MAX_CAPTION_LEN:
        await message.answer(
            f"Підпис до фото задовгий: {len(caption)} із {MAX_CAPTION_LEN} символів. "
            "Скоротіть і надішліть ще раз."
        )
        return
    content = Content("photo", message.html_text if caption else "", message.photo[-1].file_id)
    await _preview(message, state, bot, db, content)


@router.message(BroadcastStates.waiting_content, F.text, ~F.text.startswith("/"))
async def got_text(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    if len(message.text) > MAX_TEXT_LEN:
        await message.answer(
            f"Текст задовгий: {len(message.text)} із {MAX_TEXT_LEN} символів. "
            "Скоротіть і надішліть ще раз."
        )
        return
    await _preview(message, state, bot, db, Content("text", message.html_text))


async def _preview(
    message: Message, state: FSMContext, bot: Bot, db: Database, content: Content
) -> None:
    """Показує власнику точно те, що побачать клієнти, і просить підтвердити."""
    data = await state.get_data()
    segment = data["segment"]
    days = data.get("days")
    count = len(
        await db.get_recipients(segment, int(time.time()), idle_days=days or ROULETTE_IDLE_DAYS)
    )
    try:
        await send_content(bot, message.chat.id, content, reply_markup=notify_keyboard(True))
    except TelegramAPIError as exc:
        log.warning("Telegram не прийняв повідомлення розсилки: %s", exc.message)
        await message.answer(
            f"⚠️ Telegram не прийняв таке повідомлення: {html.escape(exc.message)}\n"
            "Перевірте текст і надішліть ще раз."
        )
        return

    await state.update_data(kind=content.kind, text=content.text, photo_id=content.photo_id)
    await state.set_state(BroadcastStates.confirming)
    await message.answer(
        "👆 Так повідомлення побачать клієнти.\n\n"
        f"Група: <b>{segment_title(segment, days)}</b>\n"
        f"Отримувачів зараз: <b>{count}</b>\n\n"
        "Надсилаємо?",
        reply_markup=confirm_keyboard(),
    )


# --------------------------------------------------------------------- відправка й звіт
async def _run_broadcast(
    bot: Bot, db: Database, owner_id: int, recipients: list[int], content: Content
) -> None:
    """Фонове завдання: розсилає повідомлення й надсилає власнику звіт."""
    global _running
    try:
        report = await broadcast(bot, db, recipients, content)
        text = (
            "✅ <b>Розсилку завершено</b>\n\n"
            f"👥 Отримувачів: {report.total}\n"
            f"✅ Доставлено: {report.delivered}\n"
            f"🚫 Заблокували бота: {report.blocked}\n"
            f"⚠️ Інші помилки: {report.failed}"
        )
    except Exception:
        log.exception("Розсилка зупинилась через помилку")
        text = "⚠️ Розсилка зупинилась через помилку. Подробиці — у журналі бота."
    finally:
        _running = False
    try:
        await bot.send_message(owner_id, text, reply_markup=admin_menu(is_owner=True))
    except TelegramAPIError:
        log.warning("Не вдалося надіслати власнику звіт про розсилку", exc_info=True)


@router.callback_query(BroadcastStates.confirming, F.data == "bc:send")
async def confirm_send(
    callback: CallbackQuery, state: FSMContext, bot: Bot, db: Database
) -> None:
    global _running
    if _running:
        await callback.answer("Інша розсилка ще триває. Дочекайтеся звіту.", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()
    content = Content(data["kind"], data["text"], data.get("photo_id"))
    recipients = await db.get_recipients(
        data["segment"], int(time.time()), idle_days=data.get("days") or ROULETTE_IDLE_DAYS
    )
    if not recipients:
        await callback.answer("Отримувачів більше немає.", show_alert=True)
        return

    _running = True
    task = asyncio.create_task(
        _run_broadcast(bot, db, callback.from_user.id, recipients, content)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    await callback.answer("Розсилку запущено ✅")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                f"📤 Розсилку запущено: {len(recipients)} отримувачів. "
                "Я напишу сюди, коли закінчу."
            )
        except TelegramAPIError:
            pass


# --------------------------------------------------------------------- скасування
@router.callback_query(F.data == "bc:cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Скасовано")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("Розсилку скасовано.")
        except TelegramAPIError:
            pass
        await callback.message.answer("Меню власника 👇", reply_markup=admin_menu(is_owner=True))


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Скасовано.", reply_markup=admin_menu(is_owner=True))


@router.message(BroadcastStates.waiting_content)
async def unsupported_content(message: Message) -> None:
    """У розсилці підтримуються лише текст і фото з підписом."""
    await message.answer(
        "Тут підтримується лише текст або фото з підписом. "
        "Надішліть оголошення або натисніть «❌ Скасувати»."
    )


@router.message(
    StateFilter(
        BroadcastStates.choosing_segment,
        BroadcastStates.choosing_idle_days,
        BroadcastStates.confirming,
    )
)
async def use_buttons(message: Message) -> None:
    await message.answer("Скористайтеся кнопками під повідомленням вище або натисніть «❌ Скасувати».")


@router.callback_query(F.data.startswith("bc:"))
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка зі старого повідомлення (розсилку вже скасовано чи завершено)."""
    await callback.answer("Ця кнопка вже неактивна. Почніть розсилку заново.", show_alert=True)

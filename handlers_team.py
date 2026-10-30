"""Команда для власника: кнопка «👥 Команда» або /team.

Власник бачить список власників і касирів, додає нових за номером телефону
клієнта (людина вже має зареєструватися в боті), змінює роль або видаляє.
Себе змінити не можна, останнього власника зняти теж. Людина одразу отримує
повідомлення й оновлене меню. ID з .env лише переносяться в базу при першому
запуску (див. Database.sync_staff), далі ролями керують тут.
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from database import Database
from handlers_broadcast import IsOwner
from keyboards import BTN_TEAM, main_menu
from utils import normalize_phone

log = logging.getLogger(__name__)

router = Router(name="team")
router.message.filter(F.chat.type == "private", IsOwner())
router.callback_query.filter(IsOwner())


class TeamStates(StatesGroup):
    waiting_phone = State()


ROLE_TITLES = {"owner": "👑 Власник", "admin": "🧾 Касир"}
ROLE_ABLATIVE = {"owner": "власником", "admin": "касиром"}
ROLE_ACCUSATIVE = {"owner": "власника", "admin": "касира"}

OWNER_RIGHTS = (
    "Власник має повний доступ: розсилки, призи, статистика, таблиця клієнтів, "
    "резервні копії й керування командою."
)

NOTICES = {
    "admin": (
        "🧾 Вам надано доступ <b>касира</b> в боті «Sushi tyt». "
        "У головному меню з'явилась кнопка «🛠 Панель касира»: там можна перевіряти й погашати промокоди."
    ),
    "owner": (
        "👑 Вас призначено <b>власником</b> бота «Sushi tyt». "
        "У «🛠 Панель касира» тепер є розсилка, призи, статистика, клієнти, команда й резервна копія."
    ),
    "none": "Ваш доступ до панелі керування в боті «Sushi tyt» скасовано.",
}


# --------------------------------------------------------------------- відображення
def _who(uid: int, phone: str | None) -> str:
    return f"<code>{phone}</code>" if phone else f"ID <code>{uid}</code> (без номера)"


def render_team(rows: list[tuple], me: int, note: str | None = None) -> str:
    lines = []
    if note:
        lines += [note, ""]
    lines.append("👥 <b>Команда</b>")
    for role in ("owner", "admin"):
        group = [r for r in rows if r[1] == role]
        lines += ["", "<b>👑 Власники</b>" if role == "owner" else "<b>🧾 Касири</b>"]
        if not group:
            lines.append("—")
        for uid, _, phone in group:
            lines.append(f"• {_who(uid, phone)}" + (" — це ви" if uid == me else ""))
    lines += ["", "Щоб змінити роль або видалити людину, натисніть на її номер."]
    return "\n".join(lines)


def team_keyboard(rows: list[tuple], me: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for uid, role, phone in rows:
        if uid != me:
            icon = "👑" if role == "owner" else "🧾"
            builder.button(text=f"{icon} {phone or uid}", callback_data=f"tm:open:{uid}")
    builder.button(text="➕ Додати касира", callback_data="tm:add:admin")
    builder.button(text="➕ Додати власника", callback_data="tm:add:owner")
    builder.adjust(1)
    return builder.as_markup()


def card_keyboard(uid: int, role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if role == "admin":
        builder.button(text="👑 Зробити власником", callback_data=f"tm:ask:owner:{uid}")
    else:
        builder.button(text="🧾 Зробити касиром", callback_data=f"tm:ask:admin:{uid}")
    builder.button(text="🗑 Видалити з команди", callback_data=f"tm:ask:none:{uid}")
    builder.button(text="⬅️ Назад", callback_data="tm:list")
    builder.adjust(1)
    return builder.as_markup()


def confirm_keyboard(uid: int, role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Так", callback_data=f"tm:do:{role}:{uid}")
    builder.button(text="❌ Ні", callback_data="tm:list")
    builder.adjust(2)
    return builder.as_markup()


def confirm_text(current: str, role: str, who: str) -> str:
    if role == "none":
        return f"Видалити {who} з команди?\nЛюдина втратить доступ до панелі керування."
    if role == "owner":
        return f"Зробити {who} <b>власником</b>?\n{OWNER_RIGHTS}"
    text = f"Зробити {who} <b>касиром</b>?\n"
    if current == "owner":
        return text + "Людина втратить доступ до розсилок, призів, статистики й решти функцій власника."
    return text + "Касир може перевіряти й погашати промокоди клієнтів."


# --------------------------------------------------------------------- допоміжне
async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in exc.message.lower():
            log.warning("Не вдалося оновити екран команди: %s", exc.message)


async def _show_list(db: Database, me: int, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.list_staff()
    return render_team(rows, me, note), team_keyboard(rows, me)


async def _label_for(db: Database, uid: int) -> str:
    user = await db.get_user(uid)
    return _who(uid, user["phone"] if user else None)


def _parse(data: str, parts: int) -> list[str] | None:
    chunks = data.split(":")
    return chunks if len(chunks) == parts else None


async def _ask_confirm(db: Database, uid: int, role: str, me: int) -> tuple[str, InlineKeyboardMarkup] | str:
    """Екран підтвердження або текст відмови."""
    if uid == me:
        return "Себе змінити не можна."
    current = await db.get_role(uid)
    if current == role or (current == "none" and role == "none"):
        return "Ця людина вже має таку роль." if role != "none" else "Цієї людини немає в команді."
    who = await _label_for(db, uid)
    return confirm_text(current, role, who), confirm_keyboard(uid, role)


# --------------------------------------------------------------------- список і картка
@router.message(Command("team"))
@router.message(F.text == BTN_TEAM)
async def open_team(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    text, markup = await _show_list(db, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "tm:list")
async def back_to_list(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await callback.answer()
    text, markup = await _show_list(db, callback.from_user.id)
    await _edit(callback, text, markup)


@router.callback_query(F.data.startswith("tm:open:"))
async def open_card(callback: CallbackQuery, db: Database) -> None:
    parts = _parse(callback.data, 3)
    if not parts or not parts[2].isdigit():
        await callback.answer("Невідома дія.", show_alert=True)
        return
    uid = int(parts[2])
    role = await db.get_role(uid)
    if uid == callback.from_user.id or role not in ("owner", "admin"):
        await callback.answer("Цієї людини вже немає в команді." if role == "none" else "Себе змінити не можна.",
                              show_alert=True)
        return
    await callback.answer()
    who = await _label_for(db, uid)
    await _edit(callback, f"{ROLE_TITLES[role]}: {who}\n\nЩо зробити?", card_keyboard(uid, role))


# --------------------------------------------------------------------- додавання
@router.callback_query(F.data.startswith("tm:add:"))
async def start_add(callback: CallbackQuery, state: FSMContext) -> None:
    parts = _parse(callback.data, 3)
    if not parts or parts[2] not in ROLE_ABLATIVE:
        await callback.answer("Невідома дія.", show_alert=True)
        return
    role = parts[2]
    await state.set_state(TeamStates.waiting_phone)
    await state.update_data(role=role)
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="tm:list")
    await _edit(
        callback,
        f"➕ Додати {ROLE_ACCUSATIVE[role]}\n\n"
        "Введіть номер телефону людини у форматі <b>+380XXXXXXXXX</b>.\n"
        "Вона вже має відкрити бота й поділитися номером (/start), інакше бот її не знайде.",
        builder.as_markup(),
    )


@router.message(StateFilter(TeamStates.waiting_phone), F.text, ~F.text.startswith("/"))
async def got_phone(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    role = data.get("role")
    if role not in ROLE_ABLATIVE:
        await state.clear()
        await message.answer("Щось пішло не так. Відкрийте «👥 Команда» ще раз.")
        return
    phone = normalize_phone(message.text)
    if phone is None:
        await message.answer("Невірний формат номера. Приклад: <code>+380661234567</code>")
        return
    user = await db.get_user_by_phone(phone)
    if user is None:
        await message.answer(
            f"Номер <code>{phone}</code> не зареєстрований у боті.\n"
            "Нехай людина відкриє бота, натисне /start і поділиться номером, потім введіть номер ще раз."
        )
        return
    result = await _ask_confirm(db, user["telegram_id"], role, message.from_user.id)
    if isinstance(result, str):
        await message.answer(result)
        return
    await state.clear()
    text, markup = result
    await message.answer(text, reply_markup=markup)


# --------------------------------------------------------------------- зміна ролі
@router.callback_query(F.data.startswith("tm:ask:"))
async def ask_change(callback: CallbackQuery, db: Database) -> None:
    parts = _parse(callback.data, 4)
    if not parts or parts[2] not in ("owner", "admin", "none") or not parts[3].isdigit():
        await callback.answer("Невідома дія.", show_alert=True)
        return
    result = await _ask_confirm(db, int(parts[3]), parts[2], callback.from_user.id)
    if isinstance(result, str):
        await callback.answer(result, show_alert=True)
        return
    await callback.answer()
    text, markup = result
    await _edit(callback, text, markup)


@router.callback_query(F.data.startswith("tm:do:"))
async def apply_change(
    callback: CallbackQuery, state: FSMContext, bot: Bot, db: Database, settings: Settings
) -> None:
    parts = _parse(callback.data, 4)
    if not parts or parts[2] not in ("owner", "admin", "none") or not parts[3].isdigit():
        await callback.answer("Невідома дія.", show_alert=True)
        return
    role, uid, me = parts[2], int(parts[3]), callback.from_user.id
    if uid == me:
        await callback.answer("Себе змінити не можна.", show_alert=True)
        return
    await state.clear()
    previous = await db.get_role(uid)
    status = await db.set_role(uid, role, me, int(time.time()))
    if status == "last_owner":
        await callback.answer("Останнього власника зняти не можна.", show_alert=True)
        return
    if status == "same":
        await callback.answer("Нічого не змінилось: роль уже така.", show_alert=True)
        return
    await callback.answer()

    owners, admins = await db.load_staff()
    settings.set_roles(owners, admins)
    log.info("Власник %s змінив роль %s: %s → %s", me, uid, previous, role)

    who = await _label_for(db, uid)
    if role == "none":
        note = f"✅ {who} видалено з команди."
    else:
        note = f"✅ {who} тепер {ROLE_ABLATIVE[role]}."
    try:
        await bot.send_message(uid, NOTICES[role], reply_markup=main_menu(is_admin=uid in settings.admin_ids))
    except TelegramAPIError:
        log.warning("Не вдалося повідомити %s про зміну ролі", uid, exc_info=True)
        note += "\n⚠️ Повідомити людину не вдалося (можливо, вона заблокувала бота)."

    text, markup = await _show_list(db, me, note)
    await _edit(callback, text, markup)

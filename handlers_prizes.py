"""Меню «🎰 Призи» для власника (OWNER_IDS): зміна шансів, назв, додавання й видалення призів.

Власник працює з чернеткою: нічого не застосовується, доки він не натисне
«✅ Зберегти» і сума відсотків не стане рівно 100. Після збереження призи
записуються в базу, бот одразу починає крутити за новими шансами (без
перезапуску), а колесо перемальовується у фоні.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import prize_editor as ed
import prizes
import wheel
from config import MAX_PRIZES
from database import Database
from handlers_broadcast import IsOwner
from keyboards import (
    BTN_PRIZES,
    prize_card_keyboard,
    prize_input_keyboard,
    prize_kind_keyboard,
    prizes_list_keyboard,
)
from prizes import DEFAULT_PRIZES

log = logging.getLogger(__name__)


class PrizeStates(StatesGroup):
    viewing = State()      # на екрані список або картка призу
    edit_pct = State()     # чекаємо новий відсоток
    edit_title = State()   # чекаємо нову назву
    edit_label = State()   # чекаємо новий підпис на колесі
    add_title = State()    # новий приз: назва
    add_kind = State()     # новий приз: з промокодом чи без (кнопки)
    add_prefix = State()   # новий приз: префікс промокоду
    add_pct = State()      # новий приз: відсоток


router = Router(name="prizes")
router.message.filter(F.chat.type == "private", IsOwner())
router.callback_query.filter(IsOwner())

# Перемальовувати колесо може лише одне завдання за раз: інакше одне прибрало б
# файли, які щойно намалювало інше.
_wheel_lock = asyncio.Lock()
# Посилання на фонові завдання, щоб їх не прибрав збирач сміття.
_tasks: set[asyncio.Task] = set()

_STALE = "Сесія застаріла. Відкрийте «🎰 Призи» знову."


# --------------------------------------------------------------------- допоміжне
def _msg(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


async def _send(
    target: Message, text: str, markup: InlineKeyboardMarkup | None, edit: bool
) -> None:
    """Редагує повідомлення (edit=True) або надсилає нове."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "not modified" in exc.message.lower():
                return
    await target.answer(text, reply_markup=markup)


async def _load(callback: CallbackQuery, state: FSMContext, key: str | None = None):
    """Чернетка (і приз за ключем) зі стану; якщо їх немає — показує попередження.

    Повертає (draft, item); draft = None означає, що далі працювати не можна.
    """
    draft = (await state.get_data()).get("draft")
    if draft is None:
        await callback.answer(_STALE, show_alert=True)
        return None, None
    if key is None:
        return draft, None
    item = ed.find(draft, key)
    if item is None:
        await callback.answer("Цього призу вже немає в списку.", show_alert=True)
        return None, None
    return draft, item


async def _show_list(target: Message, state: FSMContext, edit: bool) -> None:
    draft = (await state.get_data())["draft"]
    dirty = ed.is_dirty(draft, prizes.ALL_PRIZES)
    await state.set_state(PrizeStates.viewing)
    await _send(
        target,
        ed.render_list(draft, dirty),
        prizes_list_keyboard(draft, dirty, can_add=len(draft) < MAX_PRIZES),
        edit,
    )


async def _show_card(target: Message, state: FSMContext, item: dict, edit: bool, note: str = "") -> None:
    draft = (await state.get_data())["draft"]
    await state.set_state(PrizeStates.viewing)
    text = ed.render_card(item) + "\n\n" + ed.sum_line(draft)
    if note:
        text += "\n\n" + note
    await _send(target, text, prize_card_keyboard(item["key"]), edit)


def _pct_prompt(title: str, current: int | None, rest: int) -> str:
    lines = [f"📊 <b>{html.escape(title)}</b>", ""]
    if current is not None:
        lines.append(f"Зараз шанс: <b>{current}%</b>.")
    lines.append("Надішліть новий шанс цілим числом від 0 до 100. З 0% приз не випадає, але лишається в списку.")
    if 0 <= rest <= 100:
        lines.append(f"Щоб разом вийшло 100%, потрібно: <b>{rest}%</b>.")
    return "\n".join(lines)


def _rest_button(rest: int, data: str) -> tuple[str | None, str | None]:
    if 0 <= rest <= 100:
        return f"📊 Поставити {rest}% (до суми 100%)", data
    return None, None


# --------------------------------------------------------------------- вхід
@router.message(Command("prizes"))
@router.message(F.text == BTN_PRIZES)
async def open_editor(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(draft=ed.draft_from_prizes(prizes.ALL_PRIZES))
    await _show_list(message, state, edit=False)


@router.callback_query(F.data == "pz:back")
async def back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    draft, _ = await _load(callback, state)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    await _show_list(msg, state, edit=True)


@router.callback_query(F.data.startswith("pz:open:"))
async def open_card(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    await _show_card(msg, state, item, edit=True)


# --------------------------------------------------------------------- шанс
@router.callback_query(F.data.startswith("pz:pct:"))
async def ask_pct(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    await state.set_state(PrizeStates.edit_pct)
    await state.update_data(key=key)
    rest = ed.rest_for(draft, key)
    extra_text, extra_data = _rest_button(rest, f"pz:rest:{key}")
    await _send(
        msg,
        _pct_prompt(item["title"], int(item["weight"]), rest),
        prize_input_keyboard(extra_text, extra_data),
        edit=True,
    )


@router.callback_query(F.data.startswith("pz:rest:"))
async def set_rest(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    rest = ed.rest_for(draft, key)
    if not 0 <= rest <= 100:
        await callback.answer("Цей варіант зараз недоступний.", show_alert=True)
        return
    item["weight"] = rest
    await state.update_data(draft=draft)
    await callback.answer(f"Шанс: {rest}%")
    await _show_card(msg, state, item, edit=True)


@router.message(PrizeStates.edit_pct, F.text, ~F.text.startswith("/"))
async def got_pct(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft, key = data.get("draft"), data.get("key")
    item = ed.find(draft, key) if draft is not None else None
    if item is None:
        await state.clear()
        await message.answer(_STALE)
        return
    value, error = ed.parse_percent(message.text)
    if error:
        await message.answer(error)
        return
    item["weight"] = value
    await state.update_data(draft=draft)
    await _show_card(message, state, item, edit=False)


# --------------------------------------------------------------------- назва
@router.callback_query(F.data.startswith("pz:title:"))
async def ask_title(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    await state.set_state(PrizeStates.edit_title)
    await state.update_data(key=key)
    await _send(
        msg,
        f"✏️ Зараз назва: <b>{html.escape(item['title'])}</b>\n\n"
        f"Надішліть нову назву (до {ed.TITLE_MAX} символів, можна з емодзі). "
        "Її бачать клієнти, коли виграють приз.",
        prize_input_keyboard(),
        edit=True,
    )


@router.message(PrizeStates.edit_title, F.text, ~F.text.startswith("/"))
async def got_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft, key = data.get("draft"), data.get("key")
    item = ed.find(draft, key) if draft is not None else None
    if item is None:
        await state.clear()
        await message.answer(_STALE)
        return
    title, error = ed.validate_title(message.text)
    if error:
        await message.answer(error)
        return
    item["title"] = title
    await state.update_data(draft=draft)
    await _show_card(message, state, item, edit=False)


# --------------------------------------------------------------------- підпис на колесі
@router.callback_query(F.data.startswith("pz:label:"))
async def ask_label(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    await state.set_state(PrizeStates.edit_label)
    await state.update_data(key=key)
    auto = ed.suggest_label(item["title"])
    await _send(
        msg,
        f"🏷 Зараз підпис: <b>{html.escape(item['label'] or '—')}</b>\n\n"
        f"Надішліть короткий підпис для сектора колеса (до {ed.LABEL_MAX} символів, без емодзі), "
        "наприклад <code>-20%</code> або <code>ДЕСЕРТ</code>.",
        prize_input_keyboard(f"✨ Підібрати автоматично ({auto})", f"pz:autolabel:{key}"),
        edit=True,
    )


@router.callback_query(F.data.startswith("pz:autolabel:"))
async def auto_label(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    item["label"] = ed.suggest_label(item["title"])
    await state.update_data(draft=draft)
    await callback.answer("Підпис оновлено")
    await _show_card(msg, state, item, edit=True)


@router.message(PrizeStates.edit_label, F.text, ~F.text.startswith("/"))
async def got_label(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft, key = data.get("draft"), data.get("key")
    item = ed.find(draft, key) if draft is not None else None
    if item is None:
        await state.clear()
        await message.answer(_STALE)
        return
    label, error = ed.validate_label(message.text)
    if error:
        await message.answer(error)
        return
    item["label"] = label
    await state.update_data(draft=draft)
    await _show_card(message, state, item, edit=False)


# --------------------------------------------------------------------- видалення
@router.callback_query(F.data.startswith("pz:del:"))
async def delete_prize(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    draft, item = await _load(callback, state, key)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    if len(draft) <= 1:
        await callback.answer("Потрібен хоча б один приз.", show_alert=True)
        return
    draft.remove(item)
    await state.update_data(draft=draft)
    await callback.answer("Приз прибрано з чернетки")
    await _show_list(msg, state, edit=True)


@router.callback_query(F.data == "pz:reset")
async def reset_defaults(callback: CallbackQuery, state: FSMContext) -> None:
    draft, _ = await _load(callback, state)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await state.update_data(draft=ed.draft_from_prizes(DEFAULT_PRIZES))
    await callback.answer("Початкові призи підставлено. Щоб застосувати, натисніть «Зберегти».", show_alert=True)
    await _show_list(msg, state, edit=True)


# --------------------------------------------------------------------- новий приз
@router.callback_query(F.data == "pz:add")
async def add_start(callback: CallbackQuery, state: FSMContext) -> None:
    draft, _ = await _load(callback, state)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    if len(draft) >= MAX_PRIZES:
        await callback.answer(f"Максимум {MAX_PRIZES} призів на колесі.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(PrizeStates.add_title)
    await state.update_data(new_title=None, new_prefix=None)
    await _send(
        msg,
        "➕ <b>Новий приз</b> (крок 1 із 3)\n\n"
        f"Надішліть назву призу (до {ed.TITLE_MAX} символів, можна з емодзі), "
        "наприклад <code>🍰 Десерт у подарунок</code>. Її побачить клієнт, коли виграє.",
        prize_input_keyboard(),
        edit=True,
    )


@router.message(PrizeStates.add_title, F.text, ~F.text.startswith("/"))
async def got_new_title(message: Message, state: FSMContext) -> None:
    title, error = ed.validate_title(message.text)
    if error:
        await message.answer(error)
        return
    await state.update_data(new_title=title)
    await state.set_state(PrizeStates.add_kind)
    await message.answer(
        f"Приз: <b>{html.escape(title)}</b>\n\n"
        "Чи видавати за ним промокод? Для знижок і подарунків потрібен промокод, "
        "який клієнт назве касиру. Без промокоду приз — лише повідомлення.",
        reply_markup=prize_kind_keyboard(),
    )


async def _ask_new_pct(target: Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    draft = data["draft"]
    await state.set_state(PrizeStates.add_pct)
    rest = ed.rest_for(draft)
    extra_text, extra_data = _rest_button(rest, "pz:newrest")
    await _send(
        target,
        "➕ <b>Новий приз</b> (крок 3 із 3)\n\n"
        + _pct_prompt(data["new_title"], None, rest),
        prize_input_keyboard(extra_text, extra_data),
        edit,
    )


@router.callback_query(PrizeStates.add_kind, F.data.in_({"pz:kind:code", "pz:kind:plain"}))
async def got_kind(callback: CallbackQuery, state: FSMContext) -> None:
    draft, _ = await _load(callback, state)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    await callback.answer()
    if callback.data == "pz:kind:code":
        await state.set_state(PrizeStates.add_prefix)
        await _send(
            msg,
            "➕ <b>Новий приз</b> (крок 2 із 3)\n\n"
            "Придумайте префікс промокоду: латинські літери та цифри, "
            f"від {ed.PREFIX_MIN} до {ed.PREFIX_MAX} символів, без пробілів і дефісів, "
            "наприклад <code>sushitytset</code>.\n"
            "Коди матимуть вигляд <code>sushitytset-1</code>, <code>sushitytset-2</code> тощо.",
            prize_input_keyboard(),
            edit=True,
        )
    else:
        await state.update_data(new_prefix=None)
        await _ask_new_pct(msg, state, edit=True)


@router.message(PrizeStates.add_prefix, F.text, ~F.text.startswith("/"))
async def got_prefix(message: Message, state: FSMContext) -> None:
    draft = (await state.get_data()).get("draft")
    if draft is None:
        await state.clear()
        await message.answer(_STALE)
        return
    prefix, error = ed.validate_prefix(message.text, draft)
    if error:
        await message.answer(error)
        return
    await state.update_data(new_prefix=prefix)
    await _ask_new_pct(message, state, edit=False)


async def _create_prize(target: Message, state: FSMContext, weight: int, edit: bool) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if draft is None or not data.get("new_title"):
        await state.clear()
        await target.answer(_STALE)
        return
    if len(draft) >= MAX_PRIZES:
        await target.answer(f"Максимум {MAX_PRIZES} призів на колесі.")
        await _show_list(target, state, edit=False)
        return
    item = {
        "key": ed.new_key(draft, taken=prizes.PRIZES_BY_KEY),
        "title": data["new_title"],
        "label": ed.suggest_label(data["new_title"]),
        "code_prefix": data.get("new_prefix"),
        "weight": weight,
    }
    draft.append(item)
    await state.update_data(draft=draft, new_title=None, new_prefix=None)
    await _show_card(
        target, state, item, edit=edit,
        note=f"✅ Приз додано до чернетки. Підпис на колесі підібрано автоматично ({html.escape(item['label'])}), "
             "його можна змінити. Щоб приз запрацював, натисніть «Зберегти» у списку.",
    )


@router.message(PrizeStates.add_pct, F.text, ~F.text.startswith("/"))
async def got_new_pct(message: Message, state: FSMContext) -> None:
    value, error = ed.parse_percent(message.text)
    if error:
        await message.answer(error)
        return
    await _create_prize(message, state, value, edit=False)


@router.callback_query(PrizeStates.add_pct, F.data == "pz:newrest")
async def new_rest(callback: CallbackQuery, state: FSMContext) -> None:
    draft, _ = await _load(callback, state)
    msg = _msg(callback)
    if draft is None or msg is None:
        return
    rest = ed.rest_for(draft)
    if not 0 <= rest <= 100:
        await callback.answer("Цей варіант зараз недоступний.", show_alert=True)
        return
    await callback.answer()
    await _create_prize(msg, state, rest, edit=True)


# --------------------------------------------------------------------- збереження
async def redraw_wheel() -> tuple[int | None, str | None]:
    """Перемальовує колесо за поточними призами. Повертає (створено GIF, помилка)."""
    async with _wheel_lock:
        snapshot = list(prizes.PRIZES)      # знімок після того, як дочекались своєї черги
        try:
            created = await asyncio.to_thread(wheel.ensure_wheels, snapshot)
            return created, None
        except Exception as exc:
            log.exception("Не вдалося перемалювати колесо")
            return None, str(exc)


async def _redraw_and_report(bot: Bot, chat_id: int) -> None:
    _, error = await redraw_wheel()
    if error:
        text = (
            "⚠️ Призи збережено й уже діють, але колесо перемалювати не вдалося, тому клієнти "
            "поки бачать звичайну текстову рулетку. Подробиці — у журналі бота."
        )
    else:
        text = "🎡 Колесо перемальовано й готове."
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        log.warning("Не вдалося повідомити власника про колесо", exc_info=True)


@router.callback_query(F.data == "pz:save")
async def save(callback: CallbackQuery, state: FSMContext, bot: Bot, db: Database) -> None:
    draft, _ = await _load(callback, state)
    if draft is None:
        return
    error = ed.check_ready(draft)
    if error:
        await callback.answer(error, show_alert=True)
        return
    if not ed.is_dirty(draft, prizes.ALL_PRIZES):
        await callback.answer("Змін немає.", show_alert=True)
        return

    try:
        await db.save_prizes(ed.draft_to_prizes(draft))
        active, archived = await db.load_prizes()
        prizes.configure(active, archived)
    except Exception:
        log.exception("Не вдалося зберегти призи")
        await callback.answer("Не вдалося зберегти призи. Спробуйте ще раз.", show_alert=True)
        return

    await state.clear()
    await callback.answer("Збережено ✅")
    summary = "\n".join(
        f"• {html.escape(p.title)} — {p.weight}%" for p in prizes.ALL_PRIZES
    )
    msg = _msg(callback)
    if msg is not None:
        await _send(
            msg,
            "✅ <b>Призи збережено</b>\n\n" + summary + "\n\n"
            "Нові шанси діють уже зараз. Колесо перемальовується, це займає близько хвилини; "
            "доки воно не готове, клієнти бачать звичайну текстову рулетку.",
            None,
            edit=True,
        )
    task = asyncio.create_task(_redraw_and_report(bot, callback.from_user.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(F.data == "pz:cancel")
async def close_editor(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    dirty = draft is not None and ed.is_dirty(draft, prizes.ALL_PRIZES)
    await state.clear()
    await callback.answer("Зміни скасовано" if dirty else "Закрито")
    msg = _msg(callback)
    if msg is not None:
        await _send(msg, "Зміни скасовано, призи лишились як були." if dirty else "Закрито.", None, edit=True)


# --------------------------------------------------------------------- підказки й застарілі кнопки
@router.message(
    StateFilter(
        PrizeStates.edit_pct,
        PrizeStates.edit_title,
        PrizeStates.edit_label,
        PrizeStates.add_title,
        PrizeStates.add_prefix,
        PrizeStates.add_pct,
    )
)
async def expect_text(message: Message) -> None:
    await message.answer("Очікую відповідь текстом. Щоб повернутися, натисніть «⬅️ Назад» під повідомленням вище.")


@router.message(StateFilter(PrizeStates.viewing, PrizeStates.add_kind))
async def use_buttons(message: Message) -> None:
    await message.answer("Скористайтеся кнопками під повідомленням вище.")


@router.callback_query(F.data.startswith("pz:"))
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка зі старого повідомлення або натиснута не на тому кроці."""
    await callback.answer("Ця кнопка вже неактивна. Відкрийте «🎰 Призи» знову.", show_alert=True)

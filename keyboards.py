"""Клавіатури бота та тексти кнопок."""
from __future__ import annotations

from typing import Iterable, Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import IDLE_DAYS_CHOICES, NEW_USER_DAYS

# Тексти кнопок (за ними хендлери розпізнають натискання).
BTN_SHARE_CONTACT = "📱 Поділитися номером"
BTN_SPIN = "🎡 Крутити рулетку"
BTN_MY_CODES = "🎟 Мої промокоди"
BTN_NOTIFY = "🔔 Сповіщення"
BTN_ADMIN_PANEL = "🛠 Панель касира"
BTN_CHECK_CLIENT = "🔍 Перевірити код клієнта"
BTN_BROADCAST = "📢 Розсилка"
BTN_PRIZES = "🎰 Призи"
BTN_BACKUP = "💾 Резервна копія"
BTN_STATS = "📊 Статистика"
BTN_CLIENTS = "📥 Клієнти (Excel)"
BTN_TEAM = "👥 Команда"
BTN_BACK = "⬅️ Меню клієнта"
BTN_CANCEL = "❌ Скасувати"

# Групи отримувачів ручної розсилки (ключі збігаються з database.SEGMENTS).
SEGMENT_TITLES = {
    "all": "👥 Усім клієнтам",
    "promo": "🎟 З активним промокодом",
    "idle": "😴 Не крутили кілька днів",
    "new": f"🆕 Нові (останні {NEW_USER_DAYS} днів)",
    "never": "🙈 Ніколи не крутили",
}


def segment_title(segment: str, days: int | None = None) -> str:
    """Назва групи для повідомлень; для «не крутили» додає вибрану кількість днів."""
    if segment == "idle" and days:
        return f"😴 Не крутили {days}+ дн."
    return SEGMENT_TITLES[segment]


def contact_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка, яка надсилає контакт користувача (RequestContact)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SHARE_CONTACT, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SPIN)],
        [KeyboardButton(text=BTN_MY_CODES), KeyboardButton(text=BTN_NOTIFY)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN_PANEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu(is_owner: bool = False) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_CHECK_CLIENT)]]
    if is_owner:
        rows.append([KeyboardButton(text=BTN_BROADCAST), KeyboardButton(text=BTN_PRIZES)])
        rows.append([KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_CLIENTS)])
        rows.append([KeyboardButton(text=BTN_TEAM), KeyboardButton(text=BTN_BACKUP)])
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def use_codes_keyboard(promos: Iterable[Mapping]) -> InlineKeyboardMarkup | None:
    """Під кожним активним кодом — кнопка «Позначити як використаний»."""
    builder = InlineKeyboardBuilder()
    count = 0
    for promo in promos:
        if promo["status"] == "active":
            builder.button(
                text=f"✅ Позначити як використаний · {promo['code']}",
                callback_data=f"use:{promo['id']}",
            )
            count += 1
    if not count:
        return None
    builder.adjust(1)
    return builder.as_markup()


def notify_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Кнопка вмикання/вимкнення сповіщень (додається до кожної розсилки й нагадування)."""
    builder = InlineKeyboardBuilder()
    if enabled:
        builder.button(text="🔕 Вимкнути сповіщення", callback_data="notify:off")
    else:
        builder.button(text="🔔 Увімкнути сповіщення", callback_data="notify:on")
    return builder.as_markup()


def segment_keyboard(counts: Mapping[str, int]) -> InlineKeyboardMarkup:
    """Вибір групи отримувачів; у дужках — скільки людей у групі."""
    builder = InlineKeyboardBuilder()
    for key, title in SEGMENT_TITLES.items():
        # Для «не крутили» кількість залежить від терміну, його вибирають на наступному кроці.
        label = f"{title} ›" if key == "idle" else f"{title} ({counts.get(key, 0)})"
        builder.button(text=label, callback_data=f"bc:seg:{key}")
    builder.button(text=BTN_CANCEL, callback_data="bc:cancel")
    builder.adjust(1)
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Надіслати", callback_data="bc:send")
    builder.button(text=BTN_CANCEL, callback_data="bc:cancel")
    builder.adjust(2)
    return builder.as_markup()


# --------------------------------------------------------------------- редактор призів
def _short(text: str, limit: int = 34) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def prizes_list_keyboard(draft: Iterable[Mapping], dirty: bool, can_add: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in draft:
        builder.button(text=f"{_short(d['title'])} · {d['weight']}%", callback_data=f"pz:open:{d['key']}")
    builder.adjust(1)
    extra = []
    if can_add:
        extra.append(("➕ Додати приз", "pz:add"))
    extra.append(("🔄 Початкові призи", "pz:reset"))
    for text, data in extra:
        builder.row(InlineKeyboardButton(text=text, callback_data=data))
    if dirty:
        builder.row(
            InlineKeyboardButton(text="✅ Зберегти", callback_data="pz:save"),
            InlineKeyboardButton(text="❌ Скасувати зміни", callback_data="pz:cancel"),
        )
    else:
        builder.row(InlineKeyboardButton(text="⬅️ Закрити", callback_data="pz:cancel"))
    return builder.as_markup()


def prize_card_keyboard(key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Змінити шанс", callback_data=f"pz:pct:{key}")
    builder.button(text="✏️ Змінити назву", callback_data=f"pz:title:{key}")
    builder.button(text="🏷 Підпис на колесі", callback_data=f"pz:label:{key}")
    builder.button(text="🗑 Видалити приз", callback_data=f"pz:del:{key}")
    builder.button(text="⬅️ До списку", callback_data="pz:back")
    builder.adjust(1)
    return builder.as_markup()


def prize_input_keyboard(extra_text: str | None = None, extra_data: str | None = None) -> InlineKeyboardMarkup:
    """Під запитом на введення: необов'язкова кнопка-підказка й «Назад»."""
    builder = InlineKeyboardBuilder()
    if extra_text and extra_data:
        builder.button(text=extra_text, callback_data=extra_data)
    builder.button(text="⬅️ Назад", callback_data="pz:back")
    builder.adjust(1)
    return builder.as_markup()


def prize_kind_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎟 З промокодом (знижка, подарунок)", callback_data="pz:kind:code")
    builder.button(text="💬 Без промокоду (лише повідомлення)", callback_data="pz:kind:plain")
    builder.button(text="⬅️ Назад", callback_data="pz:back")
    builder.adjust(1)
    return builder.as_markup()


def idle_days_keyboard(counts: Mapping[int, int]) -> InlineKeyboardMarkup:
    """Скільки днів без прокруток; у дужках — скільки людей підходить."""
    builder = InlineKeyboardBuilder()
    for days in IDLE_DAYS_CHOICES:
        builder.button(text=f"{days} дн. ({counts.get(days, 0)})", callback_data=f"bc:days:{days}")
    builder.button(text="⬅️ Назад", callback_data="bc:back")
    builder.button(text=BTN_CANCEL, callback_data="bc:cancel")
    builder.adjust(3, 2, 2)
    return builder.as_markup()

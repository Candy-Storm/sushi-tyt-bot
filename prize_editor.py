"""Логіка редактора призів (без Telegram): чернетка, перевірки, тексти.

Чернетка — це звичайний список словників (зберігається в стані FSM). Нічого не
застосовується, доки власник не натисне «Зберегти» і сума відсотків не стане 100.
"""
from __future__ import annotations

import html
import re
import secrets
from typing import Iterable, Sequence

from config import MAX_PRIZES
from prizes import SPIN_AGAIN_KEY, Prize

TITLE_MAX = 60
LABEL_MAX = 8
PREFIX_MIN, PREFIX_MAX = 3, 24

_LABEL_RE = re.compile(rf"^[\w%+\-.!/ ]{{1,{LABEL_MAX}}}$")
_PREFIX_RE = re.compile(r"^[a-z0-9]{%d,%d}$" % (PREFIX_MIN, PREFIX_MAX))

Draft = list[dict]


# --------------------------------------------------------------------- чернетка
def draft_from_prizes(prizes: Iterable[Prize]) -> Draft:
    return [
        {"key": p.key, "title": p.title, "label": p.label, "code_prefix": p.code_prefix, "weight": p.weight}
        for p in prizes
    ]


def draft_to_prizes(draft: Sequence[dict]) -> list[Prize]:
    return [
        Prize(
            key=d["key"],
            title=d["title"],
            weight=int(d["weight"]),
            code_prefix=d["code_prefix"],
            grants_extra_spin=d["key"] == SPIN_AGAIN_KEY,
            label=d["label"],
        )
        for d in draft
    ]


def find(draft: Sequence[dict], key: str) -> dict | None:
    return next((d for d in draft if d["key"] == key), None)


def total(draft: Sequence[dict]) -> int:
    return sum(int(d["weight"]) for d in draft)


def rest_for(draft: Sequence[dict], key: str | None = None) -> int:
    """Скільки відсотків лишається до 100, якщо не рахувати приз key (або нового призу)."""
    return 100 - sum(int(d["weight"]) for d in draft if d["key"] != key)


def is_dirty(draft: Sequence[dict], live: Iterable[Prize]) -> bool:
    return draft != draft_from_prizes(live)


def new_key(draft: Sequence[dict], taken: Iterable[str] = ()) -> str:
    used = {d["key"] for d in draft} | set(taken)
    while True:
        key = "c" + secrets.token_hex(3)
        if key not in used:
            return key


def check_ready(draft: Sequence[dict]) -> str | None:
    """Повертає текст помилки, якщо чернетку не можна зберегти, інакше None."""
    if not draft:
        return "Потрібен хоча б один приз."
    if len(draft) > MAX_PRIZES:
        return f"Призів забагато: максимум {MAX_PRIZES}."
    diff = 100 - total(draft)
    if diff > 0:
        return f"Сума відсотків {total(draft)}% — бракує {diff}%. Має бути рівно 100%."
    if diff < 0:
        return f"Сума відсотків {total(draft)}% — забагато на {-diff}%. Має бути рівно 100%."
    return None


# --------------------------------------------------------------------- перевірка введення
def parse_percent(text: str) -> tuple[int | None, str | None]:
    """«10», «10%», «10 %» → 10. Повертає (значення, None) або (None, помилка)."""
    cleaned = text.strip().replace("%", "").strip()
    if not cleaned.isdigit():
        return None, "Потрібне ціле число від 0 до 100, наприклад <code>15</code>."
    value = int(cleaned)
    if value > 100:
        return None, "Відсоток не може бути більшим за 100."
    return value, None


def validate_title(text: str) -> tuple[str | None, str | None]:
    title = " ".join(text.split())
    if not title:
        return None, "Назва не може бути порожньою."
    if len(title) > TITLE_MAX:
        return None, f"Назва задовга: {len(title)} із {TITLE_MAX} символів."
    if any(ch in title for ch in "<>&"):
        return None, "У назві не можна використовувати символи &lt; &gt; &amp;."
    return title, None


def validate_label(text: str) -> tuple[str | None, str | None]:
    label = " ".join(text.split())
    if not _LABEL_RE.match(label):
        return None, (
            f"Підпис — до {LABEL_MAX} символів: літери, цифри та знаки <code>% + - . ! /</code>. "
            "Емодзі на колесі не відображаються."
        )
    return label, None


def validate_prefix(text: str, draft: Sequence[dict]) -> tuple[str | None, str | None]:
    prefix = text.strip().lower()
    if not _PREFIX_RE.match(prefix):
        return None, (
            f"Префікс — від {PREFIX_MIN} до {PREFIX_MAX} символів: латинські літери та цифри, "
            "без пробілів і дефісів. Наприклад <code>sushitytset</code>."
        )
    if any(d["code_prefix"] == prefix for d in draft):
        return None, "Такий префікс уже використовує інший приз. Придумайте інший."
    return prefix, None


def suggest_label(title: str) -> str:
    """Підпис для колеса за назвою: перше слово великими літерами (без емодзі), до 8 символів."""
    for word in title.split():
        letters = "".join(ch for ch in word if ch.isalnum() or ch in "%+-.!/")
        if letters:
            return letters.upper()[:LABEL_MAX]
    return "ПРИЗ"


# --------------------------------------------------------------------- тексти
def sum_line(draft: Sequence[dict]) -> str:
    diff = 100 - total(draft)
    if diff == 0:
        return "Сума: <b>100%</b> ✅"
    if diff > 0:
        return f"Сума: <b>{total(draft)}%</b> — бракує <b>{diff}%</b> ⚠️"
    return f"Сума: <b>{total(draft)}%</b> — забагато на <b>{-diff}%</b> ⚠️"


def code_text(prefix: str | None) -> str:
    return f"промокод <code>{html.escape(prefix)}-№</code>" if prefix else "без промокоду"


def render_list(draft: Sequence[dict], dirty: bool) -> str:
    lines = ["🎰 <b>Призи рулетки</b>", ""]
    for i, d in enumerate(draft, 1):
        weight = int(d["weight"])
        chance = f"<b>{weight}%</b>" if weight else "<b>0%</b> (не випадає)"
        lines.append(f"{i}. {html.escape(d['title'])} — {chance}\n    {code_text(d['code_prefix'])}")
    lines += ["", sum_line(draft), ""]
    if dirty:
        lines.append("✏️ Є незбережені зміни. Вони діятимуть лише після «✅ Зберегти».")
    else:
        lines.append("Оберіть приз, щоб змінити шанс, назву чи підпис, або додайте новий.")
    lines.append("Зміни стосуються лише наступних прокруток; уже видані промокоди не змінюються.")
    return "\n".join(lines)


def render_card(item: dict) -> str:
    weight = int(item["weight"])
    return (
        f"<b>{html.escape(item['title'])}</b>\n\n"
        f"📊 Шанс: <b>{weight}%</b>{' (не випадає)' if weight == 0 else ''}\n"
        f"🎟 {code_text(item['code_prefix']).capitalize()}\n"
        f"🏷 Підпис на колесі: <b>{html.escape(item['label'] or '—')}</b>"
    )

"""Призи рулетки та їхні ймовірності.

Початкові призи (DEFAULT_PRIZES) при першому запуску потрапляють у базу даних.
Далі власник змінює їх у Telegram («🎰 Призи»), а бот підхоплює зміни без
перезапуску через configure().
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

SPIN_AGAIN_KEY = "spin_again"


@dataclass(frozen=True)
class Prize:
    key: str                       # ключ, який зберігається в БД
    title: str                     # назва для клієнта
    weight: int                    # ймовірність у відсотках
    code_prefix: str | None        # префікс промокоду; None — промокод не створюється
    grants_extra_spin: bool = False
    label: str | None = None       # короткий підпис на колесі (None — візьметься зі стилю за замовчуванням)


# Початкові призи й ймовірності відповідають ТЗ. Сума ваг — рівно 100.
DEFAULT_PRIZES: tuple[Prize, ...] = (
    Prize("roll", "🍣 Рол у подарунок до замовлення", 10, "sushitytroll", label="РОЛ"),
    Prize("discount15", "🔥 Знижка 15%", 10, "sushityt15", label="-15%"),
    Prize("discount10", "💸 Знижка 10%", 20, "sushityt10", label="-10%"),
    Prize("drink", "🥤 Напій у подарунок", 20, "sushitytdrink", label="НАПІЙ"),
    Prize("try_tomorrow", "🍀 Спробуй завтра", 20, None, label="ЗАВТРА"),
    Prize(SPIN_AGAIN_KEY, "🔁 Прокрутити ще раз", 20, None, grants_extra_spin=True, label="ЩЕ РАЗ"),
)

# «Живі» списки. Їх змінюють лише «на місці» (у configure), тому всі модулі, які
# імпортували ці об'єкти, одразу бачать оновлення.
PRIZES: list[Prize] = []              # беруть участь у розіграші й на колесі (відсоток > 0)
ALL_PRIZES: list[Prize] = []          # усі не видалені призи (для редактора), разом із 0%
PRIZES_BY_KEY: dict[str, Prize] = {}  # усі відомі призи, зокрема видалені (назви старих промокодів)
_WEIGHTS: list[int] = []

# SystemRandom бере випадковість від ОС — передбачити результат неможливо.
_rng = random.SystemRandom()


def configure(prizes: Sequence[Prize], archived: Sequence[Prize] = ()) -> None:
    """Застосовує нову конфігурацію призів (без перезапуску бота).

    prizes: чинні призи в порядку показу (можуть мати 0%, тоді в розіграші не беруть участі);
    archived: видалені призи, потрібні лише щоб показувати назви раніше виданих промокодів.
    Сума відсотків чинних призів має бути рівно 100.
    """
    prizes = list(prizes)
    if not prizes:
        raise ValueError("Потрібен хоча б один приз.")
    if any(not 0 <= p.weight <= 100 for p in prizes):
        raise ValueError("Відсоток кожного призу має бути від 0 до 100.")
    total = sum(p.weight for p in prizes)
    if total != 100:
        raise ValueError(f"Сума ймовірностей призів має дорівнювати 100%, зараз {total}%.")

    ALL_PRIZES[:] = prizes
    PRIZES[:] = [p for p in prizes if p.weight > 0]
    _WEIGHTS[:] = [p.weight for p in PRIZES]
    PRIZES_BY_KEY.clear()
    PRIZES_BY_KEY.update({p.key: p for p in archived})
    PRIZES_BY_KEY.update({p.key: p for p in prizes})


def pick_prize() -> Prize:
    """Випадково обирає приз згідно з ймовірностями."""
    return _rng.choices(PRIZES, weights=_WEIGHTS, k=1)[0]


configure(DEFAULT_PRIZES)

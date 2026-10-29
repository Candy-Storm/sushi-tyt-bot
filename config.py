"""Налаштування бота: змінні читаються з файлу .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

# Скільки живе промокод після виграшу: рівно 24 години.
PROMO_TTL_SECONDS = 24 * 60 * 60

# --- сповіщення та розсилки ----------------------------------------------------
# За скільки до кінця промокоду нагадати про нього.
PROMO_REMINDER_BEFORE_SECONDS = 6 * 60 * 60
# Скільки днів без прокруток вважається «давно не крутив».
ROULETTE_IDLE_DAYS = 3
# Група «Нові клієнти» в ручній розсилці: зареєструвалися не раніше ніж стільки днів тому.
NEW_USER_DAYS = 7
# Варіанти «Не крутили N днів» у ручній розсилці (власник обирає один із них).
IDLE_DAYS_CHOICES = (2, 3, 5, 7, 14)
# Скільки призів максимум може бути на колесі (більше — сектори стають нерозбірливими).
MAX_PRIZES = 10
# Нагадування «рулетка чекає»: не частіше ніж раз на стільки днів...
ROULETTE_REMINDER_REPEAT_DAYS = 7
# ...і не більше стількох поспіль без жодної прокрутки у відповідь.
ROULETTE_REMINDER_MAX_UNANSWERED = 2
# Одному клієнту не більше одного автоматичного нагадування за цей час.
AUTO_REMINDER_COOLDOWN_SECONDS = 24 * 60 * 60
# Тихі години: автоматичні нагадування не надсилаються з 22:00 до 09:00.
QUIET_START_HOUR = 22
QUIET_END_HOUR = 9

# --- резервні копії бази ----------------------------------------------------------
# О котрій годині (за місцевим часом) робити щоденну копію.
BACKUP_HOUR = 4
# Скільки останніх копій зберігати на диску (старіші видаляються).
BACKUP_KEEP = 14
# Як часто бот перевіряє, чи пора робити копію.
BACKUP_CHECK_INTERVAL = 600
# Telegram дозволяє надсилати боту файли до 50 МБ; з запасом.
BACKUP_MAX_SEND_BYTES = 49 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]      # усі, хто бачить панель касира (ADMIN_IDS + OWNER_IDS)
    db_path: str
    tz: ZoneInfo
    owner_ids: frozenset[int] = frozenset()   # лише ці люди можуть робити розсилки
    backup_dir: str = "backups"               # куди складати резервні копії бази


def _load_tz(name: str) -> ZoneInfo:
    """Пробуємо вказаний часовий пояс, а потім обидві назви київського."""
    for candidate in (name, "Europe/Kyiv", "Europe/Kiev"):
        try:
            return ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
    raise RuntimeError(
        f"Не вдалося знайти часовий пояс «{name}». "
        "Встановіть пакет tzdata: pip install tzdata"
    )


def _parse_ids(raw: str, var_name: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RuntimeError(
                f"{var_name} містить некоректне значення «{part}». "
                "Вкажіть числові Telegram ID через кому."
            )
        ids.add(int(part))
    return frozenset(ids)


def load_settings() -> Settings:
    token = os.getenv("TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задано TOKEN. Скопіюйте .env.example у .env і впишіть токен від @BotFather.")

    admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""), "ADMIN_IDS")
    owner_ids = _parse_ids(os.getenv("OWNER_IDS", ""), "OWNER_IDS")
    if not admin_ids and not owner_ids:
        # Бот працюватиме, але касир не зможе перевіряти коди.
        print("⚠️  ADMIN_IDS порожній: панель касира буде недоступна.")
    if not owner_ids:
        print("⚠️  OWNER_IDS порожній: розсилки будуть недоступні.")

    return Settings(
        token=token,
        # Власник теж бачить панель касира.
        admin_ids=admin_ids | owner_ids,
        db_path=os.getenv("DB_PATH", "sushi_bot.db").strip() or "sushi_bot.db",
        tz=_load_tz(os.getenv("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"),
        owner_ids=owner_ids,
        backup_dir=os.getenv("BACKUP_DIR", "backups").strip() or "backups",
    )

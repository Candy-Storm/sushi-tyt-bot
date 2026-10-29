"""Допоміжні функції: телефони, дата й час, форматування."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

from config import PROMO_REMINDER_BEFORE_SECONDS, QUIET_END_HOUR, QUIET_START_HOUR

_PHONE_RE = re.compile(r"^\+380\d{9}$")


def normalize_phone(raw: str | None) -> str | None:
    """Приводить номер до формату +380XXXXXXXXX.

    Приймає «+380661234567», «380661234567», «0661234567» (з пробілами,
    дужками, дефісами). Якщо номер не український або некоректний — None.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    candidate = "+" + digits
    return candidate if _PHONE_RE.fullmatch(candidate) else None


def fmt_dt(ts: int, tz: tzinfo) -> str:
    """Unix-час → '18.10.2026 15:32' у локальному часовому поясі."""
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M")


def fmt_duration(seconds: int) -> str:
    """Секунди → '5 год 12 хв' або '14 хв'."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} год {minutes} хв"
    return f"{max(minutes, 1)} хв"


def local_day(now: datetime) -> str:
    """Календарний день за місцевим часом ('2026-10-18')."""
    return now.date().isoformat()


def seconds_until_next_day(now: datetime) -> int:
    """Скільки секунд лишилось до найближчої місцевої опівночі."""
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


def in_quiet_hours(dt: datetime) -> bool:
    """Чи припадає час на «тихі години» (за замовчуванням 22:00-09:00)."""
    return dt.hour >= QUIET_START_HOUR or dt.hour < QUIET_END_HOUR


def promo_reminder_due_ts(expires_at: int, tz: tzinfo) -> int:
    """З якого моменту можна нагадувати про промокод, що спливає в expires_at.

    Зазвичай це «за N годин до кінця». Якщо цей момент випадає на тиху ніч,
    нагадування переноситься на 21:00 попереднього вечора, інакше код міг би
    згоріти, так і не отримавши нагадування.
    """
    due = datetime.fromtimestamp(expires_at - PROMO_REMINDER_BEFORE_SECONDS, tz)
    if in_quiet_hours(due):
        base = due if due.hour >= QUIET_START_HOUR else due - timedelta(days=1)
        due = base.replace(hour=QUIET_START_HOUR - 1, minute=0, second=0, microsecond=0)
    return int(due.timestamp())

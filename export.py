"""Вивантаження клієнтів в Excel (.xlsx) для власника."""
from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

HEADERS = [
    ("Телефон", 17),
    ("Telegram ID", 14),
    ("Дата реєстрації", 18),
    ("Прокруток", 11),
    ("Остання прокрутка", 18),
    ("Промокодів видано", 18),
    ("Використано", 13),
    ("Активних зараз", 15),
    ("Сповіщення", 13),
    ("Заблокував бота", 17),
]


def _fmt(ts: int | None, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M") if ts else ""


def build_clients_xlsx(rows: list[tuple], tz: ZoneInfo) -> bytes:
    """Таблиця клієнтів у вигляді байтів xlsx; телефони зберігаються як текст."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Клієнти"
    ws.append([title for title, _ in HEADERS])
    for col, (_, width) in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F6B4F")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[cell.column_letter].width = width

    for (tg_id, phone, created, notify, blocked, spins, last_spin,
         issued, used, active) in rows:
        ws.append([
            str(phone), tg_id, _fmt(created, tz), spins, _fmt(last_spin, tz),
            issued, used, active, "так" if notify else "вимкнено", "так" if blocked else "",
        ])
        ws.cell(row=ws.max_row, column=1).number_format = "@"

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

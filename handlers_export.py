"""Вивантаження клієнтів для власника: кнопка «📥 Клієнти (Excel)» або команда /clients."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from config import Settings
from database import Database
from export import build_clients_xlsx
from handlers_broadcast import IsOwner
from keyboards import BTN_CLIENTS

log = logging.getLogger(__name__)

router = Router(name="export")
router.message.filter(F.chat.type == "private", IsOwner())

MAX_FILE_BYTES = 49 * 1024 * 1024  # ліміт Telegram для файлів ботів — 50 МБ


@router.message(Command("clients"))
@router.message(F.text == BTN_CLIENTS)
async def export_clients(message: Message, db: Database, settings: Settings) -> None:
    progress = await message.answer("📥 Збираю таблицю клієнтів...")
    try:
        rows = await db.get_clients_export(int(time.time()))
        if not rows:
            await progress.edit_text("Клієнтів поки немає: ніхто не зареєструвався.")
            return
        data = await asyncio.to_thread(build_clients_xlsx, rows, settings.tz)
        if len(data) > MAX_FILE_BYTES:
            await progress.edit_text("⚠️ Таблиця завелика для Telegram. Використайте резервну копію бази.")
            return
        stamp = datetime.now(settings.tz).strftime("%Y-%m-%d_%H%M")
        await message.answer_document(
            BufferedInputFile(data, filename=f"klienty_{stamp}.xlsx"),
            caption=(
                f"👥 Клієнтів: {len(rows)}\n"
                "У таблиці є номери телефонів, не пересилайте її стороннім."
            ),
        )
        await progress.delete()
    except Exception:
        log.exception("Не вдалося вивантажити клієнтів")
        await progress.edit_text("⚠️ Не вдалося сформувати таблицю. Подробиці — у журналі бота.")

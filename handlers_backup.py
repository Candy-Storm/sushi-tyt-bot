"""Ручна резервна копія для власника: кнопка «💾 Резервна копія» або команда /backup."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

import backup
from config import BACKUP_KEEP, Settings
from handlers_broadcast import IsOwner
from keyboards import BTN_BACKUP

log = logging.getLogger(__name__)

router = Router(name="backup")
router.message.filter(F.chat.type == "private", IsOwner())


@router.message(Command("backup"))
@router.message(F.text == BTN_BACKUP)
async def manual_backup(message: Message, bot: Bot, settings: Settings) -> None:
    progress = await message.answer("💾 Роблю резервну копію...")
    try:
        path = await backup.create_backup(settings)
    except Exception:
        log.exception("Не вдалося зробити резервну копію за запитом власника")
        await progress.edit_text("⚠️ Не вдалося зробити копію. Подробиці — у журналі бота.")
        return

    delivered = await backup.send_backup(bot, [message.from_user.id], path, "за запитом")
    if delivered:
        text = (
            "✅ Копію створено й надіслано вище.\n"
            f"На сервері в папці «{settings.backup_dir}» зберігаються останні {BACKUP_KEEP} копій; "
            "щодня о 04:00 бот робить нову й надсилає її власникам."
        )
    else:
        text = f"✅ Копію створено: {path.name} (в папці «{settings.backup_dir}»), але надіслати її в Telegram не вдалося."
    await progress.edit_text(text)

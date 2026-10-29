"""Резервні копії бази даних.

Раз на добу (о BACKUP_HOUR за місцевим часом) бот робить копію файлу бази, пакує її
в zip, зберігає в папці BACKUP_DIR (останні BACKUP_KEEP штук) і надсилає файл
власникам (OWNER_IDS) у Telegram. Копію можна зробити й вручну: кнопка
«💾 Резервна копія» або команда /backup.

Копія робиться через вбудований механізм SQLite (backup), тому вона цілісна, навіть
якщо в цей момент хтось крутить рулетку, і перевіряється перед пакуванням.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from config import (
    BACKUP_CHECK_INTERVAL,
    BACKUP_HOUR,
    BACKUP_KEEP,
    BACKUP_MAX_SEND_BYTES,
    Settings,
)

log = logging.getLogger(__name__)

PREFIX = "sushi_bot_"
_TIME_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Дві копії одночасно (за розкладом і за кнопкою) не мають мішатися.
_lock = asyncio.Lock()


# --------------------------------------------------------------------- файли
def list_backups(backup_dir: str | Path) -> list[Path]:
    """Усі копії в папці, від найстарішої до найновішої (час зашито в назві файлу)."""
    folder = Path(backup_dir)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{PREFIX}*.zip"), key=lambda p: p.name)


def backup_time(path: Path) -> datetime | None:
    """Час створення копії за назвою файлу (місцевий час, без часового поясу)."""
    stem = path.stem
    if not stem.startswith(PREFIX):
        return None
    try:
        return datetime.strptime(stem[len(PREFIX):], _TIME_FORMAT)
    except ValueError:
        return None


def latest_backup_time(backup_dir: str | Path) -> datetime | None:
    for path in reversed(list_backups(backup_dir)):
        moment = backup_time(path)
        if moment is not None:
            return moment
    return None


def prune_backups(backup_dir: str | Path, keep: int = BACKUP_KEEP) -> int:
    """Лишає keep найновіших копій, решту видаляє. Повертає, скільки видалено."""
    removed = 0
    for path in list_backups(backup_dir)[: max(0, len(list_backups(backup_dir)) - keep)]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.warning("Не вдалося видалити стару копію %s", path.name, exc_info=True)
    return removed


def is_due(now: datetime, last: datetime | None, hour: int = BACKUP_HOUR) -> bool:
    """Чи пора робити щоденну копію (now і last — місцевий час без часового поясу).

    Копія потрібна, якщо їх ще не було зовсім, або якщо вже настав сьогоднішній
    час копіювання, а остання копія старша за нього. Тож якщо бот був вимкнений
    о 04:00, копія зробиться одразу після запуску.
    """
    if last is None:
        return True
    today_slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return now >= today_slot and last < today_slot


def make_backup(
    db_path: str | Path,
    backup_dir: str | Path,
    now: datetime,
    keep: int = BACKUP_KEEP,
) -> Path:
    """Робить копію бази: цілісний знімок → перевірка → zip у backup_dir.

    Синхронна функція (її запускають у окремому потоці). now — місцевий час без
    часового поясу, він іде в назву файлу. Повертає шлях до створеного zip.
    """
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"Файл бази не знайдено: {source}")
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{now.strftime(_TIME_FORMAT)}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / source.name
        src = sqlite3.connect(str(source))
        dst = sqlite3.connect(str(snapshot))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        check = sqlite3.connect(str(snapshot))
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if verdict != "ok":
            raise RuntimeError(f"Копія бази не пройшла перевірку: {verdict}")

        partial = target.with_name(target.name + ".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, arcname=source.name)
        partial.replace(target)

    prune_backups(folder, keep)
    return target


# --------------------------------------------------------------------- Telegram
def _size_text(size: int) -> str:
    return f"{size / 1024:.0f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"


async def send_backup(bot: Bot, user_ids: Sequence[int], path: Path, kind: str) -> int:
    """Надсилає файл копії користувачам. Повертає, скільки отримали.

    Якщо файл завеликий для Telegram, він лишається лише на диску, а власник
    отримує повідомлення про це.
    """
    size = path.stat().st_size
    moment = backup_time(path)
    when = moment.strftime("%d.%m.%Y %H:%M") if moment else path.stem
    if size > BACKUP_MAX_SEND_BYTES:
        text = (
            f"⚠️ Копія бази створена ({_size_text(size)}), але завелика для Telegram. "
            f"Вона лежить на сервері в папці з копіями: {path.name}"
        )
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, text)
            except TelegramAPIError:
                log.warning("Не вдалося повідомити %s про велику копію", user_id, exc_info=True)
        return 0

    caption = (
        f"💾 <b>Резервна копія бази</b> ({kind})\n"
        f"🕒 {when} · {_size_text(size)}\n\n"
        "У файлі номери телефонів клієнтів, тож не пересилайте його стороннім. "
        "Як відновити базу з копії, описано в README."
    )
    delivered = 0
    for user_id in user_ids:
        try:
            await bot.send_document(user_id, FSInputFile(path), caption=caption)
            delivered += 1
        except TelegramAPIError:
            log.warning("Не вдалося надіслати копію користувачу %s", user_id, exc_info=True)
    return delivered


def local_now(settings: Settings) -> datetime:
    """Поточний місцевий час без часового поясу (як у назвах файлів копій)."""
    return datetime.now(settings.tz).replace(tzinfo=None)


async def create_backup(settings: Settings, now: datetime | None = None) -> Path:
    """Робить копію в окремому потоці, не блокуючи бота."""
    async with _lock:
        return await asyncio.to_thread(
            make_backup, settings.db_path, settings.backup_dir, now or local_now(settings)
        )


async def backup_loop(bot: Bot, settings: Settings) -> None:
    """Фонове завдання: раз на добу робить копію й надсилає її власникам."""
    failure_reported_on: date | None = None
    while True:
        try:
            now = local_now(settings)
            if is_due(now, latest_backup_time(settings.backup_dir)):
                path = await create_backup(settings, now)
                delivered = await send_backup(bot, sorted(settings.owner_ids), path, "за розкладом")
                log.info("Резервну копію створено: %s (надіслано власникам: %s)", path.name, delivered)
                failure_reported_on = None
        except Exception:
            log.exception("Не вдалося зробити резервну копію")
            today = local_now(settings).date()
            if failure_reported_on != today:
                failure_reported_on = today      # про збій пишемо один раз на добу
                for user_id in sorted(settings.owner_ids):
                    try:
                        await bot.send_message(
                            user_id,
                            "⚠️ Не вдалося зробити резервну копію бази. Подробиці — у журналі бота. "
                            "Спробую ще раз за кілька хвилин.",
                        )
                    except TelegramAPIError:
                        log.warning("Не вдалося повідомити власника про збій копіювання", exc_info=True)
        await asyncio.sleep(BACKUP_CHECK_INTERVAL)

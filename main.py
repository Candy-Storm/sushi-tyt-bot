"""Точка входу: Telegram-бот суші-бару «Sushi tyt» (самовиніс, рулетка призів)."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

import backup
import handlers_admin
import handlers_backup
import handlers_broadcast
import handlers_export
import handlers_prizes
import handlers_stats
import handlers_team
import handlers_user
import prizes
import wheel
from config import load_settings
from database import Database
from messaging import reminder_loop

log = logging.getLogger("sushi_bot")

EXPIRE_CHECK_INTERVAL = 300  # раз на 5 хвилин переводимо прострочені коди в expired


async def expire_loop(db: Database) -> None:
    """Фонове завдання: періодично закриває прострочені промокоди."""
    while True:
        try:
            await db.expire_old(int(time.time()))
        except Exception:
            log.exception("Не вдалося оновити статуси промокодів")
        await asyncio.sleep(EXPIRE_CHECK_INTERVAL)


async def on_error(event: ErrorEvent) -> bool:
    """Єдине місце, де логуються неперехоплені помилки хендлерів."""
    log.error("Помилка під час обробки оновлення", exc_info=event.exception)
    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()

    db = Database(settings.db_path)
    await db.connect()

    # Ролі: ID з .env переносяться в базу (один раз), далі власник керує командою в боті.
    owners, admins = await db.sync_staff(
        settings.owner_ids, settings.admin_ids - settings.owner_ids, int(time.time())
    )
    settings.set_roles(owners, admins)
    if not owners:
        log.warning("Немає жодного власника: розсилки, призи й статистика будуть недоступні. Вкажіть OWNER_IDS у .env.")
    if not settings.admin_ids:
        log.warning("Немає жодного касира чи власника: панель касира буде недоступна.")
    log.info("Команда: власників %s, касирів %s", len(owners), len(admins))

    # Призи й шанси зберігаються в базі (їх змінює власник у Telegram).
    active, archived = await db.load_prizes()
    prizes.configure(active, archived)
    log.info("Призів на колесі: %s", len(prizes.PRIZES))

    # Анімації колеса (GIF) уже лежать у assets/wheel; якщо їх немає або змінилися
    # призи — перемальовуємо (кілька десятків секунд, лише один раз).
    try:
        created = await asyncio.to_thread(wheel.ensure_wheels, list(prizes.PRIZES))
        if created:
            log.info("Намальовано анімацій колеса: %s", created)
    except Exception:
        log.warning("Анімація колеса недоступна, бот покаже текстову рулетку", exc_info=True)

    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # db і settings автоматично потрапляють у хендлери за назвою параметрів.
    dp = Dispatcher(storage=MemoryStorage(), db=db, settings=settings)
    dp.errors.register(on_error)

    # Порядок важливий: спочатку адмін-роутер (працює лише для ADMIN_IDS),
    # потім розсилка (лише для OWNER_IDS), а клієнтський із «універсальним»
    # хендлером лишається в кінці.
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_broadcast.router)
    dp.include_router(handlers_prizes.router)
    dp.include_router(handlers_backup.router)
    dp.include_router(handlers_stats.router)
    dp.include_router(handlers_export.router)
    dp.include_router(handlers_team.router)
    dp.include_router(handlers_user.router)

    expire_task = asyncio.create_task(expire_loop(db))
    reminder_task = asyncio.create_task(reminder_loop(bot, db, settings))
    backup_task = asyncio.create_task(backup.backup_loop(bot, settings))
    try:
        me = await bot.get_me()
        log.info("Бот @%s запущено", me.username)
        await dp.start_polling(bot)
    finally:
        expire_task.cancel()
        reminder_task.cancel()
        backup_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот зупинено")

"""Робота з SQLite (через aiosqlite): користувачі, прокрутки, промокоди."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

import aiosqlite

from config import NEW_USER_DAYS, PROMO_TTL_SECONDS, ROULETTE_IDLE_DAYS
from prizes import DEFAULT_PRIZES, SPIN_AGAIN_KEY, Prize, pick_prize

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    phone       TEXT NOT NULL UNIQUE,          -- +380XXXXXXXXX, один номер = один акаунт
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    day         TEXT NOT NULL,                 -- календарний день за місцевим часом
    prize_key   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spins_user_day ON spins(telegram_id, day);

CREATE TABLE IF NOT EXISTS promo_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    prize_key   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'used', 'expired')),
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_promo_user ON promo_codes(telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_promo_status ON promo_codes(status, expires_at);

-- Лічильники для послідовних кодів: sushityt10-1, sushityt10-2, ...
CREATE TABLE IF NOT EXISTS code_counters (
    prefix      TEXT PRIMARY KEY,
    last_number INTEGER NOT NULL
);

-- Журнал автоматичних нагадувань: дає ліміт частоти й захист від дублів.
CREATE TABLE IF NOT EXISTS reminder_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
    kind        TEXT NOT NULL CHECK (kind IN ('promo', 'roulette')),
    ref         INTEGER,                       -- id промокоду для kind = 'promo'
    sent_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_user ON reminder_log(telegram_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_reminder_ref ON reminder_log(kind, ref);

-- Призи рулетки (змінюються власником у Telegram). Видалені призи не стираються,
-- а позначаються archived = 1, щоб старі промокоди й надалі мали назву.
CREATE TABLE IF NOT EXISTS prize_config (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    label       TEXT,                          -- підпис на колесі
    code_prefix TEXT,                          -- NULL — приз без промокоду
    weight      INTEGER NOT NULL,              -- відсоток шансу
    position    INTEGER NOT NULL,              -- порядок на колесі й у списку
    archived    INTEGER NOT NULL DEFAULT 0
);
"""

# Групи отримувачів ручної розсилки.
SEGMENTS = ("all", "promo", "idle", "new", "never")


@dataclass(frozen=True)
class SpinResult:
    prize: Prize
    code: str | None          # None, якщо приз без промокоду
    expires_at: int | None


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Усі записи йдуть через один замок, щоб дві одночасні прокрутки
        # не могли обійти ліміт і не змішували транзакції.
        self._lock: asyncio.Lock | None = None

    # ---------------------------------------------------------------- життєвий цикл
    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._seed_prizes()
        self._lock = asyncio.Lock()

    async def _migrate(self) -> None:
        """Додає нові колонки до бази, створеної попередньою версією бота.

        Наявні користувачі й промокоди зберігаються; сповіщення для всіх
        за замовчуванням увімкнені.
        """
        async with self._db.execute("PRAGMA table_info(users)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "notify" not in columns:
            await self._db.execute("ALTER TABLE users ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
        if "blocked" not in columns:
            await self._db.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        await self._db.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База даних не підключена. Спочатку викличте connect().")
        return self._conn

    @property
    def _write_lock(self) -> asyncio.Lock:
        if self._lock is None:
            raise RuntimeError("База даних не підключена. Спочатку викличте connect().")
        return self._lock

    # ---------------------------------------------------------------- користувачі
    async def get_user(self, telegram_id: int) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_user_by_phone(self, phone: str) -> aiosqlite.Row | None:
        async with self._db.execute("SELECT * FROM users WHERE phone = ?", (phone,)) as cur:
            return await cur.fetchone()

    async def save_user(self, telegram_id: int, phone: str, now: int) -> bool:
        """Прив'язує номер до Telegram ID.

        Повертає False, якщо цей номер уже закріплений за іншим акаунтом.
        """
        async with self._write_lock:
            try:
                await self._db.execute(
                    "INSERT INTO users (telegram_id, phone, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(telegram_id) DO UPDATE SET phone = excluded.phone",
                    (telegram_id, phone, now),
                )
                await self._db.commit()
                return True
            except aiosqlite.IntegrityError:
                await self._db.rollback()
                return False

    # ---------------------------------------------------------------- рулетка
    async def _spin_counts(self, telegram_id: int, day: str) -> tuple[int, int]:
        """Повертає (скільки прокруток зроблено сьогодні, скільки дозволено).

        Дозволено = 1 щоденна + по одній за кожне випадіння «Прокрутити ще раз».
        """
        async with self._db.execute(
            "SELECT COUNT(*) AS total, "
            "       COALESCE(SUM(prize_key = ?), 0) AS again "
            "FROM spins WHERE telegram_id = ? AND day = ?",
            (SPIN_AGAIN_KEY, telegram_id, day),
        ) as cur:
            row = await cur.fetchone()
        return row["total"], 1 + row["again"]

    async def reset_spins(self, telegram_id: int, day: str) -> int:
        """Видаляє прокрутки клієнта за вказаний день (для тестування).

        Промокоди, які вже виграні, не чіпає. Повертає кількість видалених записів.
        """
        async with self._write_lock:
            async with self._db.execute(
                "DELETE FROM spins WHERE telegram_id = ? AND day = ?", (telegram_id, day)
            ) as cur:
                deleted = cur.rowcount
            await self._db.commit()
        return deleted

    async def try_spin(self, telegram_id: int, day: str, now: int) -> SpinResult | None:
        """Робить прокрутку, якщо вона сьогодні ще доступна.

        Повертає None, якщо ліміт вичерпано. Запис прокрутки і створення
        промокоду відбуваються в одній транзакції.
        """
        async with self._write_lock:
            used, allowed = await self._spin_counts(telegram_id, day)
            if used >= allowed:
                return None

            prize = pick_prize()
            try:
                await self._db.execute(
                    "INSERT INTO spins (telegram_id, day, prize_key, created_at) VALUES (?, ?, ?, ?)",
                    (telegram_id, day, prize.key, now),
                )
                code = expires_at = None
                if prize.code_prefix:
                    code, expires_at = await self._create_promo(
                        telegram_id, prize, now
                    )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
            return SpinResult(prize=prize, code=code, expires_at=expires_at)

    async def _create_promo(self, telegram_id: int, prize: Prize, now: int) -> tuple[str, int]:
        """Створює промокод із наступним номером для свого префікса.

        Викликається всередині try_spin (замок і транзакція вже є).
        """
        prefix = prize.code_prefix
        await self._db.execute(
            "INSERT OR IGNORE INTO code_counters (prefix, last_number) VALUES (?, 0)", (prefix,)
        )
        await self._db.execute(
            "UPDATE code_counters SET last_number = last_number + 1 WHERE prefix = ?", (prefix,)
        )
        async with self._db.execute(
            "SELECT last_number FROM code_counters WHERE prefix = ?", (prefix,)
        ) as cur:
            number = (await cur.fetchone())["last_number"]

        code = f"{prefix}-{number}"
        expires_at = now + PROMO_TTL_SECONDS
        await self._db.execute(
            "INSERT INTO promo_codes (code, telegram_id, prize_key, status, created_at, expires_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (code, telegram_id, prize.key, now, expires_at),
        )
        return code, expires_at

    # ---------------------------------------------------------------- промокоди
    async def expire_old(self, now: int) -> int:
        """Активні коди, термін яких вийшов, переводить у статус expired."""
        async with self._write_lock:
            async with self._db.execute(
                "UPDATE promo_codes SET status = 'expired' "
                "WHERE status = 'active' AND expires_at <= ?",
                (now,),
            ) as cur:
                changed = cur.rowcount
            await self._db.commit()
        if changed:
            log.info("Прострочено промокодів: %s", changed)
        return changed

    async def get_promos_for_user(
        self, telegram_id: int, now: int, limit: int = 25
    ) -> list[aiosqlite.Row]:
        """Коди клієнта (найновіші першими). Перед вибіркою оновлює прострочені."""
        await self.expire_old(now)
        async with self._db.execute(
            "SELECT * FROM promo_codes WHERE telegram_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (telegram_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def get_active_promos(self, telegram_id: int, now: int) -> list[aiosqlite.Row]:
        await self.expire_old(now)
        async with self._db.execute(
            "SELECT * FROM promo_codes WHERE telegram_id = ? AND status = 'active' "
            "ORDER BY expires_at ASC",
            (telegram_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def get_promo(self, promo_id: int) -> aiosqlite.Row | None:
        async with self._db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,)) as cur:
            return await cur.fetchone()

    async def get_promo_by_code(self, code: str) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM promo_codes WHERE code = ?", (code.strip().lower(),)
        ) as cur:
            return await cur.fetchone()

    async def mark_used(self, promo_id: int, now: int) -> bool:
        """Позначає код використаним. True — вдалося; False — код уже не активний.

        Перевірка статусу і зміна відбуваються одним UPDATE, тому
        подвійне натискання не дозволить застосувати код двічі.
        """
        async with self._write_lock:
            async with self._db.execute(
                "UPDATE promo_codes SET status = 'used', used_at = ? "
                "WHERE id = ? AND status = 'active' AND expires_at > ?",
                (now, promo_id, now),
            ) as cur:
                changed = cur.rowcount
            await self._db.commit()
        return changed == 1

    # ---------------------------------------------------------------- сповіщення і розсилки
    async def set_notify(self, telegram_id: int, enabled: bool) -> None:
        """Вмикає чи вимикає сповіщення (розсилки й нагадування) для клієнта."""
        async with self._write_lock:
            await self._db.execute(
                "UPDATE users SET notify = ? WHERE telegram_id = ?", (int(enabled), telegram_id)
            )
            await self._db.commit()

    async def set_blocked(self, telegram_id: int, blocked: bool) -> None:
        """Позначає, що клієнт заблокував бота (або розблокував його)."""
        async with self._write_lock:
            await self._db.execute(
                "UPDATE users SET blocked = ? WHERE telegram_id = ?", (int(blocked), telegram_id)
            )
            await self._db.commit()

    async def get_recipients(
        self, segment: str, now: int, idle_days: int = ROULETTE_IDLE_DAYS
    ) -> list[int]:
        """Telegram ID отримувачів ручної розсилки для вибраної групи.

        Групи: all (усі), promo (з активним промокодом), idle (не крутили
        idle_days днів), new (зареєструвалися за останні NEW_USER_DAYS днів),
        never (жодного разу не крутили). У будь-якій групі немає тих, хто
        вимкнув сповіщення або заблокував бота.
        """
        sql = "SELECT u.telegram_id FROM users u WHERE u.notify = 1 AND u.blocked = 0"
        params: tuple = ()
        if segment == "all":
            pass
        elif segment == "promo":
            sql += (
                " AND EXISTS (SELECT 1 FROM promo_codes p WHERE p.telegram_id = u.telegram_id"
                " AND p.status = 'active' AND p.expires_at > ?)"
            )
            params = (now,)
        elif segment == "idle":
            sql += (
                " AND COALESCE((SELECT MAX(s.created_at) FROM spins s"
                " WHERE s.telegram_id = u.telegram_id), u.created_at) <= ?"
            )
            params = (now - idle_days * 86400,)
        elif segment == "new":
            sql += " AND u.created_at >= ?"
            params = (now - NEW_USER_DAYS * 86400,)
        elif segment == "never":
            sql += " AND NOT EXISTS (SELECT 1 FROM spins s WHERE s.telegram_id = u.telegram_id)"
        else:
            raise ValueError(f"Невідома група розсилки: {segment}")
        async with self._db.execute(sql + " ORDER BY u.telegram_id", params) as cur:
            return [row["telegram_id"] for row in await cur.fetchall()]

    async def get_remindable_promos(self, now: int) -> list[aiosqlite.Row]:
        """Активні коди, про які ще не нагадували, власники яких приймають сповіщення."""
        async with self._db.execute(
            "SELECT p.id, p.code, p.prize_key, p.expires_at, p.telegram_id "
            "FROM promo_codes p JOIN users u ON u.telegram_id = p.telegram_id "
            "WHERE p.status = 'active' AND p.expires_at > ? AND u.notify = 1 AND u.blocked = 0 "
            "AND NOT EXISTS (SELECT 1 FROM reminder_log r WHERE r.kind = 'promo' AND r.ref = p.id) "
            "ORDER BY p.expires_at",
            (now,),
        ) as cur:
            return list(await cur.fetchall())

    async def users_reminded_since(self, since: int) -> set[int]:
        """Кому вже надсилали автоматичне нагадування після моменту since."""
        async with self._db.execute(
            "SELECT DISTINCT telegram_id FROM reminder_log WHERE sent_at > ?", (since,)
        ) as cur:
            return {row["telegram_id"] for row in await cur.fetchall()}

    async def get_idle_users(self, now: int) -> list[aiosqlite.Row]:
        """Клієнти, які давно не крутили рулетку (telegram_id, last_activity)."""
        async with self._db.execute(
            "SELECT u.telegram_id AS telegram_id, "
            "COALESCE((SELECT MAX(s.created_at) FROM spins s WHERE s.telegram_id = u.telegram_id), "
            "         u.created_at) AS last_activity "
            "FROM users u WHERE u.notify = 1 AND u.blocked = 0 "
            "AND COALESCE((SELECT MAX(s.created_at) FROM spins s WHERE s.telegram_id = u.telegram_id), "
            "             u.created_at) <= ? "
            "ORDER BY u.telegram_id",
            (now - ROULETTE_IDLE_DAYS * 86400,),
        ) as cur:
            return list(await cur.fetchall())

    async def roulette_reminder_stats(
        self, telegram_id: int, since: int
    ) -> tuple[int, int | None]:
        """(скільки нагадувань «рулетка чекає» вже було після since, час останнього)."""
        async with self._db.execute(
            "SELECT COUNT(*) AS n, MAX(sent_at) AS last FROM reminder_log "
            "WHERE telegram_id = ? AND kind = 'roulette' AND sent_at > ?",
            (telegram_id, since),
        ) as cur:
            row = await cur.fetchone()
        return row["n"], row["last"]

    async def log_reminder(self, telegram_id: int, kind: str, ref: int | None, now: int) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO reminder_log (telegram_id, kind, ref, sent_at) VALUES (?, ?, ?, ?)",
                (telegram_id, kind, ref, now),
            )
            await self._db.commit()

    # ---------------------------------------------------------------- призи рулетки
    async def _seed_prizes(self) -> None:
        """Якщо призів у базі ще немає (перший запуск чи оновлення), заносить початкові."""
        async with self._db.execute("SELECT COUNT(*) AS n FROM prize_config") as cur:
            if (await cur.fetchone())["n"]:
                return
        for position, p in enumerate(DEFAULT_PRIZES):
            await self._db.execute(
                "INSERT INTO prize_config (key, title, label, code_prefix, weight, position, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (p.key, p.title, p.label, p.code_prefix, p.weight, position),
            )
        await self._db.commit()

    async def load_prizes(self) -> tuple[list[Prize], list[Prize]]:
        """Повертає (чинні призи в порядку показу, видалені призи)."""
        async with self._db.execute(
            "SELECT * FROM prize_config ORDER BY archived, position, rowid"
        ) as cur:
            rows = await cur.fetchall()
        active: list[Prize] = []
        archived: list[Prize] = []
        for row in rows:
            prize = Prize(
                key=row["key"],
                title=row["title"],
                weight=row["weight"],
                code_prefix=row["code_prefix"],
                grants_extra_spin=row["key"] == SPIN_AGAIN_KEY,
                label=row["label"],
            )
            (archived if row["archived"] else active).append(prize)
        return active, archived

    async def save_prizes(self, prizes: Sequence[Prize]) -> None:
        """Зберігає новий список призів; призи, яких у ньому немає, архівуються.

        Усе відбувається однією транзакцією: або збережеться все, або нічого.
        """
        if not prizes:
            raise ValueError("Потрібен хоча б один приз.")
        async with self._write_lock:
            try:
                for position, p in enumerate(prizes):
                    await self._db.execute(
                        "INSERT INTO prize_config (key, title, label, code_prefix, weight, position, archived) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(key) DO UPDATE SET title = excluded.title, label = excluded.label, "
                        "code_prefix = excluded.code_prefix, weight = excluded.weight, "
                        "position = excluded.position, archived = 0",
                        (p.key, p.title, p.label, p.code_prefix, p.weight, position),
                    )
                marks = ",".join("?" * len(prizes))
                await self._db.execute(
                    f"UPDATE prize_config SET archived = 1 WHERE key NOT IN ({marks})",
                    [p.key for p in prizes],
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    # ---------------------------------------------------------------- статистика
    async def get_stats(self, since: int, until: int, now: int) -> dict:
        """Зведення для власника за період [since, until) (unix-час).

        Кількість клієнтів загалом і тих, хто вимкнув сповіщення чи заблокував бота, —
        це стан на зараз; решта показників стосується лише періоду. Прокрутки, які
        стерла команда /reset, у статистику не потрапляють.
        """
        await self.expire_old(now)
        period = (since, until)

        async def one(sql: str, params: tuple = ()) -> int:
            async with self._db.execute(sql, params) as cur:
                return (await cur.fetchone())[0] or 0

        async def rows(sql: str, params: tuple = ()) -> list[tuple]:
            async with self._db.execute(sql, params) as cur:
                return [tuple(row) for row in await cur.fetchall()]

        in_spins = "created_at >= ? AND created_at < ?"
        return {
            "users_total": await one("SELECT COUNT(*) FROM users"),
            "users_new": await one(f"SELECT COUNT(*) FROM users WHERE {in_spins}", period),
            "users_muted": await one("SELECT COUNT(*) FROM users WHERE notify = 0"),
            "users_blocked": await one("SELECT COUNT(*) FROM users WHERE blocked = 1"),
            "spins": await one(f"SELECT COUNT(*) FROM spins WHERE {in_spins}", period),
            "spinners": await one(
                f"SELECT COUNT(DISTINCT telegram_id) FROM spins WHERE {in_spins}", period
            ),
            # Крутили принаймні у два різні дні — тобто повернулися.
            "returning": await one(
                f"SELECT COUNT(*) FROM (SELECT telegram_id FROM spins WHERE {in_spins} "
                "GROUP BY telegram_id HAVING COUNT(DISTINCT day) >= 2)",
                period,
            ),
            "spins_by_prize": await rows(
                f"SELECT prize_key, COUNT(*) FROM spins WHERE {in_spins} "
                "GROUP BY prize_key ORDER BY COUNT(*) DESC, prize_key",
                period,
            ),
            "promo_by_status": dict(
                await rows(
                    f"SELECT status, COUNT(*) FROM promo_codes WHERE {in_spins} GROUP BY status",
                    period,
                )
            ),
            "promo_by_prize": await rows(
                "SELECT prize_key, COUNT(*), COALESCE(SUM(status = 'used'), 0) FROM promo_codes "
                f"WHERE {in_spins} GROUP BY prize_key ORDER BY COUNT(*) DESC, prize_key",
                period,
            ),
            "redeemed": await one(
                "SELECT COUNT(*) FROM promo_codes WHERE used_at >= ? AND used_at < ?", period
            ),
        }

    # ---------------------------------------------------------------- експорт клієнтів
    async def get_clients_export(self, now: int) -> list[tuple]:
        """Усі клієнти з підсумками для таблиці (нові зверху).

        Кортеж: telegram_id, phone, created_at, notify, blocked, прокруток,
        остання прокрутка (unix або None), кодів видано, використано, активних зараз.
        """
        await self.expire_old(now)
        sql = """
            SELECT u.telegram_id, u.phone, u.created_at, u.notify, u.blocked,
                   (SELECT COUNT(*) FROM spins s WHERE s.telegram_id = u.telegram_id),
                   (SELECT MAX(s.created_at) FROM spins s WHERE s.telegram_id = u.telegram_id),
                   (SELECT COUNT(*) FROM promo_codes p WHERE p.telegram_id = u.telegram_id),
                   (SELECT COUNT(*) FROM promo_codes p
                     WHERE p.telegram_id = u.telegram_id AND p.status = 'used'),
                   (SELECT COUNT(*) FROM promo_codes p
                     WHERE p.telegram_id = u.telegram_id AND p.status = 'active')
            FROM users u
            ORDER BY u.created_at DESC, u.telegram_id DESC
        """
        async with self._db.execute(sql) as cur:
            return [tuple(row) for row in await cur.fetchall()]

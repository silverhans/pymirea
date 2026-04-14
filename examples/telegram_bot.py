"""Пример: минимальный Telegram-бот на aiogram 3.x с командами /login
и /schedule. Сессии МИРЭА хранятся зашифрованными через SessionCrypto
в маленькой SQLite-БД.

Для продакшена:
- Замените SQLite на свою реальную БД
- Корректно обработайте 2FA (сейчас бот ждёт OTP следующим сообщением)
- Добавьте фоновый job обновления токенов (см. pymirea.try_refresh_tokens)

Запуск::

    pip install aiogram aiosqlite
    export TELEGRAM_BOT_TOKEN="..."
    export MIREA_SESSION_KEY="..."
    python examples/telegram_bot.py
"""

import asyncio
import os

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from pymirea import Config, MireaAPI, MireaAuth, configure
from pymirea.crypto import get_crypto

# ── однократная настройка ──────────────────────────────────────────────────────
configure(Config(session_keys=os.environ["MIREA_SESSION_KEY"]))
crypto = get_crypto()
bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
dp = Dispatcher()
DB_PATH = "bot_sessions.db"

# Состояние многошагового логина по юзеру (in-memory; в проде — Redis)
pending_login: dict[int, dict] = {}


# ── маленькое хранилище сессий ──────────────────────────────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS sessions(tg_id INTEGER PRIMARY KEY, mirea_session TEXT NOT NULL)"
        )
        await db.commit()


async def save_session(tg_id: int, cookies: dict) -> None:
    encrypted = crypto.encrypt_session(cookies)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?)",
            (tg_id, encrypted),
        )
        await db.commit()


async def load_session(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT mirea_session FROM sessions WHERE tg_id = ?",
            (tg_id,),
        )
        row = await cur.fetchone()
        return crypto.decrypt_session(row[0]) if row else None


# ── обработчики ────────────────────────────────────────────────────────────
@dp.message(Command("login"))
async def cmd_login(msg: Message) -> None:
    parts = msg.text.split(maxsplit=2)
    if len(parts) != 3:
        await msg.answer("Использование: /login ваш_логин@edu.mirea.ru пароль")
        return
    _, login, password = parts

    auth = MireaAuth()
    result = await auth.login(login, password)

    if result.challenge:
        pending_login[msg.from_user.id] = {"auth": auth, "challenge": result.challenge}
        await msg.answer("Мирэа просит OTP. Пришлите код одним сообщением.")
        return

    if not result.tokens:
        await msg.answer("Не удалось войти. Проверьте логин и пароль.")
        return

    await save_session(msg.from_user.id, result.tokens)
    await msg.answer("Вошли! /schedule покажет расписание.")


@dp.message(lambda m: m.from_user.id in pending_login)
async def handle_otp(msg: Message) -> None:
    state = pending_login.pop(msg.from_user.id)
    auth = state["auth"]
    result = await auth.complete_2fa(state["challenge"], msg.text.strip())
    if result.tokens:
        await save_session(msg.from_user.id, result.tokens)
        await msg.answer("OTP принят. Теперь /schedule.")
    else:
        await msg.answer("Неверный код.")


@dp.message(Command("schedule"))
async def cmd_schedule(msg: Message) -> None:
    cookies = await load_session(msg.from_user.id)
    if not cookies:
        await msg.answer("Сначала /login.")
        return

    api = MireaAPI(session_cookies=cookies)
    sched = await api.get_schedule()

    lines = []
    for day in sched.days[:3]:  # первые 3 дня
        lines.append(f"📅 *{day.date}*")
        for lesson in day.lessons:
            lines.append(f"  {lesson.start} {lesson.subject}")
    await msg.answer("\n".join(lines) or "Расписание пустое.")


# ── точка входа ─────────────────────────────────────────────────────────
async def main() -> None:
    await init_db()
    print("Бот запускается…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

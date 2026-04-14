"""Example: minimal Telegram bot on aiogram 3.x exposing /login and
/schedule commands. Stores Мирэа sessions encrypted via SessionCrypto
in a tiny SQLite store.

For production:
- Replace the SQLite store with your real DB
- Handle 2FA properly (currently asks user to send OTP as a follow-up message)
- Add token-refresh background job (see pymirea.try_refresh_tokens)

Run::

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

# ── one-time setup ──────────────────────────────────────────────────────
configure(Config(session_keys=os.environ["MIREA_SESSION_KEY"]))
crypto = get_crypto()
bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
dp = Dispatcher()
DB_PATH = "bot_sessions.db"

# Per-user state for multi-step login (in-memory; use Redis in production)
pending_login: dict[int, dict] = {}


# ── tiny session store ──────────────────────────────────────────────────
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


# ── handlers ────────────────────────────────────────────────────────────
@dp.message(Command("login"))
async def cmd_login(msg: Message) -> None:
    parts = msg.text.split(maxsplit=2)
    if len(parts) != 3:
        await msg.answer("Usage: /login your_login@edu.mirea.ru password")
        return
    _, login, password = parts

    auth = MireaAuth()
    result = await auth.login(login, password)

    if result.challenge:
        pending_login[msg.from_user.id] = {"auth": auth, "challenge": result.challenge}
        await msg.answer("Мирэа просит OTP. Пришлите код одним сообщением.")
        return

    if not result.tokens:
        await msg.answer("Login failed.")
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
    for day in sched.days[:3]:  # first 3 days
        lines.append(f"📅 *{day.date}*")
        for lesson in day.lessons:
            lines.append(f"  {lesson.start} {lesson.subject}")
    await msg.answer("\n".join(lines) or "Расписание пустое.")


# ── entry point ─────────────────────────────────────────────────────────
async def main() -> None:
    await init_db()
    print("Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

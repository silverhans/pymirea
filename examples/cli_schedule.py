"""Пример: CLI-скрипт, который входит в Пульс МИРЭА и печатает расписание
на ближайшие 7 дней.

Запуск::

    export MIREA_LOGIN="s12345@edu.mirea.ru"
    export MIREA_PASSWORD="..."
    export MIREA_SESSION_KEY="$(python -c 'import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    python examples/cli_schedule.py
"""

import asyncio
import os
from datetime import datetime

from pymirea import Config, MireaAuth, configure
from pymirea.grades import MireaGrades


async def main() -> None:
    login = os.environ["MIREA_LOGIN"]
    password = os.environ["MIREA_PASSWORD"]

    configure(Config(session_keys=os.environ["MIREA_SESSION_KEY"]))

    auth = MireaAuth()
    result = await auth.login(login, password)

    if result.challenge:
        otp = input("Введите код из email: ").strip()
        result = await auth.complete_2fa(result.challenge, otp)

    if not result.success or not result.tokens:
        print("Не удалось войти:", result.message)
        return

    api = MireaGrades(session_cookies=result.tokens)
    schedule = await api.get_schedule(days=7)
    if not schedule.success or not schedule.lessons:
        print("Нет пар:", schedule.message)
        return

    print(f"\nРасписание ({len(schedule.lessons)} пар):\n")
    current_day = ""
    for lesson in schedule.lessons:
        when = datetime.fromtimestamp(lesson.start_epoch or 0)
        day = when.strftime("%d.%m (%a)")
        if day != current_day:
            print(f"\n=== {day} ===")
            current_day = day
        end = datetime.fromtimestamp(lesson.end_epoch or 0).strftime("%H:%M") if lesson.end_epoch else "?"
        print(
            f"  {when.strftime('%H:%M')}-{end}  "
            f"{lesson.name:<40}  {lesson.teacher or '?':<25}  "
            f"{lesson.room or ''}"
        )


if __name__ == "__main__":
    asyncio.run(main())

"""Пример: CLI-скрипт, который входит в МИРЭА LKS и печатает расписание
на текущую неделю.

Запуск::

    export MIREA_LOGIN="s12345@edu.mirea.ru"
    export MIREA_PASSWORD="..."
    export MIREA_SESSION_KEY="$(python -c 'import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    python examples/cli_schedule.py
"""

import asyncio
import os

from pymirea import Config, MireaAPI, MireaAuth, configure


async def main() -> None:
    login = os.environ["MIREA_LOGIN"]
    password = os.environ["MIREA_PASSWORD"]

    configure(Config(session_keys=os.environ["MIREA_SESSION_KEY"]))

    auth = MireaAuth()
    result = await auth.login(login, password)

    if result.challenge:
        # 2FA: Мирэа отправит OTP на университетскую почту.
        otp = input("Введите код из email: ").strip()
        result = await auth.complete_2fa(result.challenge, otp)

    if not result.tokens:
        print("Не удалось войти. Проверьте логин/пароль.")
        return

    api = MireaAPI(session_cookies=result.tokens)
    schedule = await api.get_schedule()

    print(f"\nРасписание ({len(schedule.days)} дней):\n")
    for day in schedule.days:
        print(f"=== {day.date} ===")
        for lesson in day.lessons:
            print(
                f"  {lesson.start}-{lesson.end}  "
                f"{lesson.subject:<40}  {lesson.teacher or '?':<25}  "
                f"{lesson.classroom or ''}"
            )
        print()


if __name__ == "__main__":
    asyncio.run(main())

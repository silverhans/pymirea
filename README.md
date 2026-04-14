# pymirea

[![PyPI](https://img.shields.io/pypi/v/pymirea.svg)](https://pypi.org/project/pymirea/)
[![CI](https://github.com/silverhans/pymirea/actions/workflows/ci.yml/badge.svg)](https://github.com/silverhans/pymirea/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Асинхронный Python-клиент для [Пульса МИРЭА](https://pulse.mirea.ru).

Поддерживает: вход с 2FA через Keycloak SSO, расписание занятий, оценки, посещаемость (отметка + детали), события турникетов (ACS), регистрацию в e-sports, шифрование сессий (Fernet + HKDF).

## Установка

```bash
pip install pymirea
```

## Первый запрос за 30 секунд

```python
import asyncio
from pymirea import Config, configure, MireaAuth

# Сгенерируйте свой ключ один раз и сохраните:
#   python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
configure(Config(session_keys="ВАША_BASE64_СТРОКА_32_БАЙТА"))

async def main():
    auth = MireaAuth()
    result = await auth.login("ваш_логин@edu.mirea.ru", "пароль")

    if result.challenge:
        # МИРЭА просит OTP (приходит на университетскую почту)
        code = input("Код из email: ")
        result = await auth.complete_2fa(result.challenge, code)

    print("Вошли! Токены:", result.tokens)

asyncio.run(main())
```

## Примеры использования

### 1. Простой скрипт — получить расписание

```python
import asyncio
from pymirea import Config, configure, MireaAuth, MireaAPI

configure(Config(session_keys="..."))

async def main():
    auth = MireaAuth()
    result = await auth.login("login@edu.mirea.ru", "пароль")
    # ... обработка 2FA если потребуется ...

    api = MireaAPI(session_cookies=result.tokens)
    schedule = await api.get_schedule()
    for day in schedule.days:
        print(f"{day.date}:")
        for lesson in day.lessons:
            print(f"  {lesson.start} {lesson.subject} — {lesson.teacher}")

asyncio.run(main())
```

Полная версия: [`examples/cli_schedule.py`](examples/cli_schedule.py).

### 2. Telegram-бот на aiogram

```python
from aiogram import Bot, Dispatcher, types
from pymirea import Config, configure, MireaAuth, MireaAPI

configure(Config(session_keys="..."))

bot = Bot(token="...")
dp = Dispatcher()

# user_id -> session cookies (в продакшене — в БД, зашифрованно через SessionCrypto)
sessions: dict[int, dict] = {}

@dp.message(commands=["schedule"])
async def cmd_schedule(msg: types.Message):
    cookies = sessions.get(msg.from_user.id)
    if not cookies:
        await msg.answer("Сначала /login")
        return
    api = MireaAPI(session_cookies=cookies)
    sched = await api.get_schedule()
    text = "\n".join(f"{l.start} — {l.subject}" for d in sched.days for l in d.lessons)
    await msg.answer(text)
```

Полная версия: [`examples/telegram_bot.py`](examples/telegram_bot.py).

### 3. Отметить посещаемость себе и друзьям одним QR-сканом

Классический MireaScanner-флоу — сделайте своё приложение за 20 минут:

```python
from pymirea import MireaAPI, SessionCrypto
from pymirea.crypto import get_crypto

crypto = get_crypto()

# Загрузите из своей БД зашифрованные сессии друзей
friends = [
    (123, "Саша", crypto.decrypt_session(row_sasha)),
    (456, "Петя", crypto.decrypt_session(row_petya)),
]

api = MireaAPI(session_cookies=my_session)
results = await api.mark_attendance_for_group(qr_data, friends)

for r in results:
    print(f"{r.user_name}: {'✓' if r.success else '✗ ' + r.message}")
```

`mark_attendance_for_group` сам извлекает токен из QR-URL, проходит по всем переданным сессиям и возвращает список `AttendanceResult` — готово к отображению в UI.

### 4. FastAPI-веб-сервис

```python
from fastapi import FastAPI, Depends, HTTPException
from pymirea import Config, configure, MireaAPI
from pymirea.crypto import get_crypto

configure(Config(session_keys="..."))
crypto = get_crypto()
app = FastAPI()

def current_session(token: str = Depends(...)) -> dict:
    """Расшифровать сессию пользователя из вашего хранилища."""
    encrypted = your_db.get_mirea_session(user_id_from(token))
    if not encrypted:
        raise HTTPException(401)
    return crypto.decrypt_session(encrypted)

@app.get("/api/schedule")
async def get_schedule(session: dict = Depends(current_session)):
    api = MireaAPI(session_cookies=session)
    return await api.get_schedule()
```

Полная версия: [`examples/fastapi_app.py`](examples/fastapi_app.py).

> Примеров для QR-сканирования в папке `examples/` пока нет — PR приветствуется.

## Шифрование сессий

Хранить cookies МИРЭА в БД в открытом виде нельзя — `SessionCrypto` оборачивает их в Fernet-токен с ключом, выведенным через HKDF из вашего `session_keys`:

```python
from pymirea.crypto import get_crypto

crypto = get_crypto()
encrypted: str = crypto.encrypt_session(cookies_dict)   # сохраните в БД
decrypted: dict = crypto.decrypt_session(encrypted)     # прочтите из БД
```

При смене `session_keys` старые токены становятся нечитаемыми — храните старый ключ в `legacy_bot_token` для grace-period миграции.

## Public API

| Имя | Назначение |
|---|---|
| `Config` / `configure(Config)` | Конфигурация runtime (DI-style, вызывается один раз на старте) |
| `MireaAuth` | `login()`, `complete_2fa()`, refresh-token flow |
| `MireaAPI` | `get_schedule()`, `get_grades()`, `get_attendance()`, `mark_attendance()`, `mark_attendance_for_group()` |
| `MireaACS` | События турникетов через pulse.mirea.ru |
| `MireaEsports` | Регистрация в e-sports |
| `SessionCrypto` | Шифрование/расшифровка cookies (Fernet + HKDF) |
| `AuthChallenge` / `AuthResult` | Результаты login-флоу |
| `get_authorization_header(cookies)` | Bearer-токен из dict сессии |
| `try_refresh_tokens(cookies)` | Best-effort обновление access-токена |
| `get_token_age_seconds(cookies)` | Сколько секунд токен живёт |

Подробнее в исходниках: [`pymirea/`](pymirea/).

## Конфигурация

```python
from pymirea import Config

Config(
    session_keys="base64-32-bytes",     # обязательно
    mirea_proxy="http://proxy:8080",    # опционально, для pulse.mirea.ru (датацентры заблокированы)
    legacy_bot_token=None,              # старый ключ для миграции при ротации
    request_timeout_s=15.0,
    breaker_failure_threshold=5,
    breaker_recovery_s=30.0,
)
```

## Совместимость

- **Python** 3.11+
- **Только async** (нет sync-обёртки; для скриптов используйте `asyncio.run`)
- **Любой web-фреймворк**: FastAPI, aiohttp, Sanic, Litestar, Quart — pymirea не привязан ни к одному
- **Любая БД** для хранения сессий — pymirea даёт только примитивы шифрования

## Что pymirea НЕ делает

- ❌ Не хранит сессии — это задача вашего приложения (БД/Redis/файл)
- ❌ Не управляет пользователями — у вас своя auth-система
- ❌ Не предоставляет HTTP-handlers — пишите свои под свой фреймворк
- ❌ Не парсит сложные нестандартные формы МИРЭА (профили, дипломы — TBD)

Если хочется готовый микросервис — соберите Telegram-бот / FastAPI-сервис на основе примеров.

## Внести вклад

Issues и PR приветствуются. Особенно полезно:
- Тесты для новых сценариев (logout, edge-cases в 2FA, etc.)
- Поддержка дополнительных эндпоинтов МИРЭА
- Примеры для других фреймворков (Discord-боты, CLI-tools, ...)

## Лицензия

MIT — см. [LICENSE](LICENSE).

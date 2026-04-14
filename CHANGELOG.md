# История изменений

Заметные изменения **pymirea** перечислены здесь. Формат —
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — по
[SemVer](https://semver.org/lang/ru/).

## [Unreleased]

### Исправлено
- `AuthResult.tokens` добавлен как property-алиас для `cookies` — README/examples ссылались на `result.tokens`, которого не существовало
- `MireaAuth.complete_2fa()` добавлен как алиас для `submit_otp()` — README/examples звали `complete_2fa`, которого не существовало

### Добавлено
- 2 smoke-теста, фиксирующих контракт обоих алиасов на будущее

## [0.1.2] — 2026-04-14

### Добавлено
- README расширен тремя готовыми примерами (CLI-скрипт, Telegram-бот, FastAPI-сервис)
- Папка `examples/` с runnable-скриптами
- GitHub Actions CI: ruff lint + pytest на каждый push и PR
- GitHub Actions release workflow: автопубликация на PyPI на push тега `v*` через trusted publishing (OIDC, без API-токенов)
- Smoke-тесты в `tests/test_smoke.py` (6 offline-проверок API без живого аккаунта)

### Изменено
- Документация переведена на русский (целевая аудитория — студенты МИРЭА)
- `LKS` → `Пульс` в README/CHANGELOG/docstring'ах: lks.mirea.ru как название портала устарело, актуальное — pulse.mirea.ru

### Исправлено
- `SessionCrypto.get_crypto()` обращался к отсутствующему `settings.jwt_secret` (переименован в `legacy_bot_token` при extraction). Заменено на `getattr` с fallback'ом, чтобы приложения без legacy-секрета не падали.

## [0.1.1] — 2026-04-14

### Изменено
- Зависимость `httpx` ослаблена до `>=0.25` (было `>=0.27`) — для совместимости с downstream-приложениями, ещё запиненными на старые версии.

## [0.1.0] — 2026-04-14

Первый релиз. Async-клиент для Пульса МИРЭА извлечён из двух внутренних
проектов (`silverhans/versiti-project` MireaScanner Web и `Oplexx`
backend мессенджера) в единый канонический пакет.

### Добавлено
- `MireaAuth` — login + 2FA (Keycloak SSO + OTP) + refresh токенов
- `MireaAPI` — расписание, оценки, посещаемость, детали посещаемости
- `MireaACS` — события турникетов через pulse.mirea.ru
- `MireaEsports` — регистрация в e-sports
- `SessionCrypto` — Fernet+HKDF шифрование cookies
- `Config` + `configure()` — конфигурация runtime через DI
- Public-хелперы: `get_authorization_header`, `try_refresh_tokens`, `get_token_age_seconds`
- Лицензия MIT

[Unreleased]: https://github.com/silverhans/pymirea/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/silverhans/pymirea/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/silverhans/pymirea/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/silverhans/pymirea/releases/tag/v0.1.0

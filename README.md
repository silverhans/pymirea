# pymirea

Async Python client for [Мирэа LKS](https://lks.mirea.ru) (Личный Кабинет Студента).

Covers login + 2FA via Keycloak SSO, class schedule, grades, attendance (mark & detail), ACS (турникеты) entry/exit events, e-sports registration, and session-token encryption (Fernet + HKDF).

## Install

```bash
pip install git+https://github.com/silverhans/pymirea.git@v0.1.0
```

## Quick start

```python
import asyncio
from pymirea import Config, configure, MireaAuth

configure(Config(
    session_keys="base64-hkdf-seed",   # required; see below
    mirea_proxy=None,                  # optional HTTP/SOCKS proxy for pulse.mirea.ru
))

async def main():
    auth = MireaAuth()
    result = await auth.login("s12345@edu.mirea.ru", "password")
    if result.challenge:
        # 2FA required — prompt user for OTP, then:
        result = await auth.complete_2fa(result.challenge, "123456")
    tokens = result.tokens  # access / refresh / id tokens

asyncio.run(main())
```

## Configuration

`Config.session_keys` is the only required field. Generate a 32-byte HKDF seed:

```bash
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Full config options: see [`pymirea/config.py`](pymirea/config.py).

## Architecture

Pure async, no global state besides an opt-in `settings` shim used internally so the ported code can reference `settings.foo` without each module knowing about dependency injection. Your app calls `configure()` once at startup; everything else follows.

Public API (`pymirea.*`):

| Name | Purpose |
|---|---|
| `Config` | Runtime configuration dataclass |
| `configure(Config)` | Wire the library to your config |
| `MireaAuth` | Login, 2FA, token refresh |
| `MireaAPI` | Schedule / grades / attendance wrapper |
| `MireaACS` | Турникеты (entry/exit events) |
| `MireaEsports` | E-sports registration endpoints |
| `SessionCrypto` | Fernet+HKDF encryption of session cookies |
| `get_authorization_header()` | Pull Bearer header from session dict |
| `try_refresh_tokens()` | Best-effort refresh before expiry |

## License

MIT — see [LICENSE](LICENSE).

## Upstream

Extracted from [Oplexx](https://github.com/oplexx) (закрытый) и [versiti-project](https://github.com/silverhans/versiti-project) (MireaScanner Web). Обе кодовые базы теперь потребляют `pymirea` как зависимость.

# Changelog

All notable changes to **pymirea** are listed here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- README expanded with three runnable examples (CLI script, Telegram bot, FastAPI service)
- `examples/` directory with copy-pasteable code
- GitHub Actions CI: ruff lint + pytest on every push and PR

## [0.1.1] — 2026-04-14

### Changed
- Relax `httpx` dependency to `>=0.25` (was `>=0.27`) for compatibility with downstream apps still pinned to older versions.

## [0.1.0] — 2026-04-14

Initial release. Core async client for Мирэа LKS extracted from two
internal projects (`silverhans/versiti-project` MireaScanner Web and
`Oplexx` messenger backend) into a single canonical package.

### Added
- `MireaAuth` — login + 2FA (Keycloak SSO + OTP) + token refresh
- `MireaAPI` — schedule, grades, attendance, attendance-detail
- `MireaACS` — pulse.mirea.ru entry/exit events
- `MireaEsports` — registration endpoints
- `SessionCrypto` — Fernet+HKDF encryption of session cookies
- `Config` + `configure()` — runtime configuration via DI
- Public helpers: `get_authorization_header`, `try_refresh_tokens`,
  `get_token_age_seconds`
- MIT license

[Unreleased]: https://github.com/silverhans/pymirea/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/silverhans/pymirea/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/silverhans/pymirea/releases/tag/v0.1.0

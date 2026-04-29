"""
Сервис бронирования киберзоны МИРЭА (esports.mirea.ru).

Авторизация через login.mirea.ru OAuth2 (Django, НЕ Keycloak).
API — обычный REST с JWT Bearer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

ESPORTS_API = "https://esports.mirea.ru/api/v1"
LOGIN_MIREA_URL = "https://login.mirea.ru"
ESPORTS_LOGIN_REDIRECT = f"{ESPORTS_API}/login/mirea"
ESPORTS_OAUTH_START = f"{ESPORTS_API}/login?service=mirea"


@dataclass
class EsportsTokens:
    access_token: str
    refresh_token: str


@dataclass
class EsportsAuthResult:
    success: bool
    message: str
    tokens: EsportsTokens | None = None


class MireaEsports:
    """Клиент для esports.mirea.ru."""

    def __init__(self) -> None:
        from ._http import make_async_client
        from ._settings import settings
        self._client = make_async_client(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── Auth ─────────────────────────────────────────────────────────

    async def login(self, email: str, password: str) -> EsportsAuthResult:
        """
        Полный OAuth2 flow:
        1. GET esports → 307 → login.mirea.ru/oauth2/authorize
        2. GET login page → CSRF token
        3. POST login (email + password + CSRF)
        4. Follow redirects → esports callback с ?code= → JWT
        """
        try:
            # Шаг 1: Получаем OAuth2 authorize URL
            resp = await self._client.get(ESPORTS_OAUTH_START)
            if resp.status_code not in (301, 302, 307, 308):
                return EsportsAuthResult(False, f"Esports OAuth start failed: {resp.status_code}")

            authorize_url = resp.headers.get("location", "")
            if not authorize_url:
                return EsportsAuthResult(False, "No redirect from esports OAuth")

            logger.info("Esports OAuth → %s", authorize_url[:100])

            # Шаг 2: Открываем login.mirea.ru (получаем форму + CSRF)
            login_page = await self._client.get(authorize_url, follow_redirects=True)
            if login_page.status_code != 200:
                return EsportsAuthResult(False, f"login.mirea.ru недоступен: {login_page.status_code}")

            csrf_token = self._extract_csrf(login_page.text)
            if not csrf_token:
                return EsportsAuthResult(False, "Не удалось получить CSRF токен")

            # Извлекаем next URL из формы
            next_url = self._extract_next(login_page.text)
            login_post_url = str(login_page.url)
            # Убираем query string для POST
            if "?" in login_post_url:
                login_post_url = login_post_url.split("?")[0]
            if not login_post_url.endswith("/"):
                login_post_url += "/"

            logger.info("Login form URL: %s, next: %s", login_post_url[:80], (next_url or "")[:80])

            # Шаг 3: POST логин
            form_data = {
                "csrfmiddlewaretoken": csrf_token,
                "login": email,
                "password": password,
            }
            if next_url:
                form_data["next"] = next_url

            login_resp = await self._client.post(
                login_post_url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": str(login_page.url),
                },
            )

            # Шаг 4: Следуем по redirect-цепочке до esports callback
            tokens = await self._follow_redirects_to_tokens(login_resp)
            if tokens:
                return EsportsAuthResult(True, "Авторизация в киберзоне успешна", tokens)

            # Если остались на login.mirea.ru — неверные данные
            final_url = str(login_resp.url) if login_resp.status_code == 200 else ""
            if "login.mirea.ru" in final_url or login_resp.status_code == 200:
                error = self._extract_login_error(login_resp.text)
                return EsportsAuthResult(False, error or "Неверный логин или пароль")

            return EsportsAuthResult(False, "Не удалось получить токены киберзоны")

        except httpx.TimeoutException:
            return EsportsAuthResult(False, "Сервер login.mirea.ru не отвечает")
        except Exception as e:
            logger.error("Esports login error: %s", e)
            return EsportsAuthResult(False, f"Ошибка: {e}")

    async def refresh_tokens(self, refresh_token: str) -> EsportsTokens | None:
        """Обновить JWT через /user/refresh."""
        try:
            resp = await self._client.post(
                f"{ESPORTS_API}/user/refresh",
                json={"refresh": refresh_token},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning("Esports token refresh failed: %s", resp.status_code)
                return None

            data = resp.json()
            access = data.get("access_token") or data.get("access")
            refresh = data.get("refresh_token") or data.get("refresh")
            if access and refresh:
                return EsportsTokens(access_token=access, refresh_token=refresh)
            return None
        except Exception as e:
            logger.error("Esports refresh error: %s", e)
            return None

    # ── Booking API ──────────────────────────────────────────────────

    async def get_configuration(self, access_token: str) -> dict | None:
        """Получить категории устройств."""
        return await self._api_get("/bookings/configuration", access_token)

    async def get_slots(
        self,
        access_token: str,
        *,
        date: str,
        duration: int,
        start_time: str,
        category: str = "all",
    ) -> dict | None:
        """Получить свободные слоты."""
        return await self._api_get(
            "/bookings/slots",
            access_token,
            params={"date": date, "duration": duration, "start_time": start_time, "category": category},
        )

    async def create_booking(
        self,
        access_token: str,
        *,
        device_id: str,
        booking_datetime: str,
        booking_duration: int,
    ) -> dict | None:
        """Создать бронирование."""
        return await self._api_post(
            "/bookings/@me/create",
            access_token,
            json={
                "device_id": device_id,
                "booking_datetime": booking_datetime,
                "booking_duration": booking_duration,
            },
        )

    async def get_my_bookings(self, access_token: str, *, limit: int = 20, offset: int = 0) -> dict | None:
        """Мои бронирования."""
        return await self._api_get(
            "/bookings/@me",
            access_token,
            params={"limit": limit, "offset": offset},
        )

    async def cancel_booking(self, access_token: str, *, booking_id: int | str) -> dict | None:
        """Отменить бронирование."""
        return await self._api_patch(f"/bookings/@me/cancel/{booking_id}", access_token)

    # ── Internal helpers ─────────────────────────────────────────────

    async def _api_get(self, path: str, token: str, *, params: dict | None = None) -> dict | None:
        try:
            resp = await self._client.get(
                f"{ESPORTS_API}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                return {"_unauthorized": True}
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception as e:
            logger.error("Esports API GET %s: %s", path, e)
            return None

    async def _api_post(self, path: str, token: str, *, json: dict | None = None) -> dict | None:
        try:
            logger.info("Esports API POST %s body=%s", path, json)
            resp = await self._client.post(
                f"{ESPORTS_API}{path}",
                json=json,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                return {"_unauthorized": True}
            if resp.status_code not in (200, 201):
                logger.warning("Esports API POST %s → %s: %s", path, resp.status_code, resp.text[:500])
                try:
                    return resp.json()
                except Exception:
                    return None
            return resp.json()
        except Exception as e:
            logger.error("Esports API POST %s: %s", path, e)
            return None

    async def _api_patch(self, path: str, token: str, *, json: dict | None = None) -> dict | None:
        try:
            resp = await self._client.patch(
                f"{ESPORTS_API}{path}",
                json=json or {},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                return {"_unauthorized": True}
            if resp.status_code not in (200, 201):
                try:
                    return resp.json()
                except Exception:
                    return None
            return resp.json()
        except Exception as e:
            logger.error("Esports API PATCH %s: %s", path, e)
            return None

    async def _follow_redirects_to_tokens(self, resp: httpx.Response) -> EsportsTokens | None:
        """Следуем по redirect-цепочке, ищем JWT в финальном ответе от esports."""
        max_redirects = 10
        for _ in range(max_redirects):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break

            location = resp.headers.get("location", "")
            if not location:
                break

            redirect_url = urljoin(str(resp.url), location)
            logger.info("Esports redirect → %s", redirect_url[:120])

            # Если redirect ведёт на esports callback — достаём токены
            if "esports.mirea.ru" in redirect_url and "/login/mirea" in redirect_url:
                return await self._fetch_tokens_following_redirects(redirect_url)

            resp = await self._client.get(
                redirect_url,
                headers={"Referer": str(resp.url)},
            )

        # Authorize может вернуть 200 — это consent/authorization page.
        # Автоматически подтверждаем, если есть форма с allow/authorize.
        if resp.status_code == 200 and "login.mirea.ru" in str(resp.url):
            consent_result = await self._handle_consent_page(resp)
            if consent_result:
                return consent_result

        # Может быть, мы уже на esports и ответ содержит токены
        return self._parse_tokens(resp)

    async def _fetch_tokens_following_redirects(self, url: str) -> EsportsTokens | None:
        """GET esports callback and follow any redirects (307, etc.) until we get tokens."""
        resp = await self._client.get(url)
        for _ in range(10):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("location", "")
            if not location:
                break
            redirect_url = urljoin(str(resp.url), location)
            logger.info("Esports callback redirect → %s", redirect_url[:120])
            # Tokens may be passed as URL query params in the redirect
            tokens = self._parse_tokens_from_url(redirect_url)
            if tokens:
                return tokens
            resp = await self._client.get(redirect_url)
        # Try URL params of the final response URL, then JSON body
        return self._parse_tokens_from_url(str(resp.url)) or self._parse_tokens(resp)

    async def _handle_consent_page(self, resp: httpx.Response) -> EsportsTokens | None:
        """Обработать страницу согласия OAuth2 (consent/authorize)."""
        html = resp.text
        page_url = str(resp.url)

        # Ищем форму с action
        form_action = self._extract_form_action(html)
        csrf = self._extract_csrf(html)

        logger.info(
            "Consent page: url=%s, has_form=%s, has_csrf=%s, body_len=%d",
            page_url[:80], bool(form_action), bool(csrf), len(html),
        )

        # Форма может не иметь action (POST на тот же URL).
        if not form_action and "authorize" in page_url:
            form_action = page_url

        if not form_action:
            logger.warning("Consent page: no form action found")
            return None

        # Собираем все hidden inputs
        hidden_inputs = dict(re.findall(
            r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']',
            html,
        ))
        # Также ищем reversed order (value before name)
        hidden_inputs.update(dict(re.findall(
            r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\'][^>]+type=["\']hidden["\']',
            html,
        )))

        # Добавляем кнопку «Разрешить» (submit с name="allow")
        allow_match = re.search(
            r'<input[^>]+name=["\']allow["\'][^>]+value=["\']([^"\']*)["\']', html, re.I
        )
        if not allow_match:
            allow_match = re.search(
                r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']allow["\']', html, re.I
            )
        if allow_match:
            hidden_inputs["allow"] = allow_match.group(1)
        elif re.search(r'name=["\']allow["\']', html, re.I):
            hidden_inputs["allow"] = "Разрешить"

        if csrf and "csrfmiddlewaretoken" not in hidden_inputs:
            hidden_inputs["csrfmiddlewaretoken"] = csrf

        logger.info("Consent page: submitting form to %s with %d fields", form_action[:80], len(hidden_inputs))

        consent_resp = await self._client.post(
            form_action if form_action.startswith("http") else urljoin(page_url, form_action),
            data=hidden_inputs,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": page_url,
            },
        )

        # Follow remaining redirects to get tokens
        return await self._follow_consent_redirects(consent_resp)

    async def _follow_consent_redirects(self, resp: httpx.Response) -> EsportsTokens | None:
        """Follow redirects after consent form submission."""
        for _ in range(10):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break

            location = resp.headers.get("location", "")
            if not location:
                break

            redirect_url = urljoin(str(resp.url), location)
            logger.info("Consent redirect → %s", redirect_url[:120])

            if "esports.mirea.ru" in redirect_url and "/login/mirea" in redirect_url:
                return await self._fetch_tokens_following_redirects(redirect_url)

            resp = await self._client.get(
                redirect_url,
                headers={"Referer": str(resp.url)},
            )

        return self._parse_tokens(resp)

    @staticmethod
    def _extract_form_action(html: str) -> str | None:
        m = re.search(r'<form[^>]+action=["\']([^"\']+)', html, re.I)
        return m.group(1).replace("&amp;", "&") if m else None

    @staticmethod
    def _parse_tokens_from_url(url: str) -> EsportsTokens | None:
        """Извлечь access_token/refresh_token из query params URL."""
        try:
            qs = parse_qs(urlparse(url).query)
            access = (qs.get("access_token") or [None])[0]
            refresh = (qs.get("refresh_token") or [None])[0]
            if access and refresh:
                return EsportsTokens(access_token=access, refresh_token=refresh)
            return None
        except Exception:
            return None

    @staticmethod
    def _parse_tokens(resp: httpx.Response) -> EsportsTokens | None:
        """Извлечь access_token/refresh_token из JSON-ответа esports."""
        try:
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            access = data.get("access_token") or data.get("access")
            refresh = data.get("refresh_token") or data.get("refresh")
            if access and refresh:
                return EsportsTokens(access_token=access, refresh_token=refresh)
            return None
        except Exception:
            return None

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        m = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)', html)
        if m:
            return m.group(1)
        m = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrfmiddlewaretoken', html)
        return m.group(1) if m else None

    @staticmethod
    def _extract_next(html: str) -> str | None:
        m = re.search(r'name=["\']next["\']\s+value=["\']([^"\']+)', html)
        if m:
            return m.group(1).replace("&amp;", "&")
        m = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']next', html)
        return m.group(1).replace("&amp;", "&") if m else None

    @staticmethod
    def _extract_login_error(html: str) -> str | None:
        m = re.search(r'class="[^"]*error[^"]*"[^>]*>\s*([^<]+)', html, re.IGNORECASE)
        return m.group(1).strip() if m else None

"""
Сервис авторизации через SSO МИРЭА

Авторизация происходит через login.mirea.ru с учётными данными студента.
После успешного входа сохраняются cookies для дальнейших запросов.
"""
import base64
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ._settings import settings
from .upstreams import get_breaker

logger = logging.getLogger(__name__)

@dataclass
class AuthChallenge:
    kind: str  # "otp"
    action_url: str
    field_name: str
    hidden_fields: dict[str, str]
    referer: str | None = None
    # Used for OAuth2 code -> token exchange (PKCE).
    pkce_verifier: str | None = None
    redirect_uri: str | None = None


@dataclass
class AuthResult:
    success: bool
    message: str
    cookies: dict | None = None
    user_info: dict | None = None
    challenge: AuthChallenge | None = None

    @property
    def tokens(self) -> dict | None:
        """Alias for ``cookies`` — name used in README/examples."""
        return self.cookies


class MireaAuth:
    """Авторизация через SSO МИРЭА"""

    LOGIN_URL = "https://login.mirea.ru"
    SSO_URL = "https://sso.mirea.ru"
    ATTENDANCE_URL = "https://pulse.mirea.ru"
    ATTENDANCE_API_URL = "https://pulse.mirea.ru"
    TOKEN_URL = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/token"
    CLIENT_ID = "attendance-app"

    def __init__(self):
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=50, keepalive_expiry=10.0)
        timeout = httpx.Timeout(30.0, connect=10.0)
        # Основной клиент для SSO
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=self._headers,
            transport=httpx.AsyncHTTPTransport(retries=2, limits=limits),
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        )
        # Клиент с прокси для pulse.mirea.ru (блокирует датацентры)
        self.proxy_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=self._headers,
            transport=httpx.AsyncHTTPTransport(retries=2, limits=limits),
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        ) if settings.mirea_proxy else None
        if self.proxy_client:
            # Держим куки в синхроне, чтобы можно было повторить запросы через прокси
            self.proxy_client.cookies.update(self.client.cookies)

    @staticmethod
    def _generate_pkce_verifier() -> str:
        # RFC 7636: verifier length 43..128 chars. token_urlsafe(64) is usually ~86 chars.
        return secrets.token_urlsafe(64)

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @staticmethod
    def _extract_code_from_url(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            return str(code) if code else None
        except (ValueError, AttributeError):
            return None

    async def _exchange_code_for_token(self, code: str, *, code_verifier: str | None, redirect_uri: str) -> dict | None:
        """
        Exchange OAuth2 authorization code for Keycloak tokens.

        Returns dict with access_token/refresh_token/token_type/expires_in on success, else None.
        """
        if not code:
            return None
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self.CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier

        try:
            breaker = get_breaker("mirea_sso")
            decision = await breaker.allow()
            if not decision.allowed:
                return None
            resp = await self.client.post(
                self.TOKEN_URL,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                if 500 <= int(resp.status_code) <= 599:
                    await breaker.record_failure()
                else:
                    await breaker.record_success()
                try:
                    err_body = resp.text[:500]
                except (UnicodeDecodeError, AttributeError):
                    err_body = "<unreadable>"
                logger.warning("Token exchange failed: %s body=%s verifier=%s redirect=%s",
                               resp.status_code, err_body, bool(code_verifier), redirect_uri[:80] if redirect_uri else None)
                return None
            body = resp.json()
            if not isinstance(body, dict):
                await breaker.record_success()
                return None
            if not body.get("access_token"):
                await breaker.record_success()
                return None
            await breaker.record_success()
            return {
                "access_token": body.get("access_token"),
                "token_type": body.get("token_type", "Bearer"),
                "refresh_token": body.get("refresh_token"),
                "expires_in": body.get("expires_in"),
            }
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.warning("Token exchange network error: %s", type(e).__name__)
            try:
                await get_breaker("mirea_sso").record_failure()
            except Exception:
                pass
            return None
        except (ValueError, KeyError) as e:
            logger.warning("Token exchange response error: %s", type(e).__name__)
            return None

    async def refresh_tokens(self, refresh_token: str) -> dict | None:
        """
        Refresh Keycloak tokens using refresh_token grant.

        Returns dict with access_token/refresh_token/token_type/expires_in on success, else None.
        """
        token = (refresh_token or "").strip()
        if not token:
            return None

        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "client_id": self.CLIENT_ID,
            "refresh_token": token,
        }

        breaker = get_breaker("mirea_sso")
        decision = await breaker.allow()
        if not decision.allowed:
            return None

        try:
            resp = await self.client.post(
                self.TOKEN_URL,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                if 500 <= int(resp.status_code) <= 599:
                    await breaker.record_failure()
                else:
                    await breaker.record_success()
                logger.warning("Token refresh failed: %s", resp.status_code)
                return None

            body = resp.json()
            if not isinstance(body, dict):
                await breaker.record_success()
                return None

            access = body.get("access_token")
            if not access:
                await breaker.record_success()
                return None

            await breaker.record_success()
            return {
                "access_token": access,
                "token_type": body.get("token_type", "Bearer"),
                # Some Keycloak setups may omit refresh_token on refresh.
                "refresh_token": body.get("refresh_token") or token,
                "expires_in": body.get("expires_in"),
            }
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.warning("Token refresh network error: %s", type(e).__name__)
            try:
                await breaker.record_failure()
            except Exception as e2:
                logger.debug("Breaker record_failure failed: %s", e2)
            return None
        except (ValueError, KeyError) as e:
            # JSON decode or unexpected response shape
            logger.warning("Token refresh response error: %s", type(e).__name__)
            try:
                await breaker.record_success()  # Server responded, just bad data
            except Exception:
                pass
            return None

    async def login(self, username: str, password: str) -> AuthResult:
        """
        Авторизация с учётными данными МИРЭА

        Args:
            username: Логин (обычно формата name@edu.mirea.ru или студ. билет)
            password: Пароль

        Returns:
            AuthResult с cookies при успехе
        """
        try:
            logger.info("=== Keycloak SSO login ===")

            redirect_uri = f"{self.ATTENDANCE_URL}/"
            pkce_verifier = self._generate_pkce_verifier()
            pkce_challenge = self._pkce_challenge(pkce_verifier)

            breaker = get_breaker("mirea_sso")
            decision = await breaker.allow()
            if not decision.allowed:
                retry_after = decision.retry_after_s or 5
                return AuthResult(success=False, message=f"МИРЭА временно недоступна. Попробуйте через {retry_after} сек.")

            # Шаг 1: Открываем Keycloak auth endpoint (SPA страница)
            auth_page = await self.client.get(
                f"{self.SSO_URL}/realms/mirea/protocol/openid-connect/auth",
                params={
                    "client_id": self.CLIENT_ID,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "openid",
                    # PKCE: some Keycloak setups require it for public clients.
                    "code_challenge": pkce_challenge,
                    "code_challenge_method": "S256",
                }
            )

            if auth_page.status_code != 200:
                if 500 <= int(auth_page.status_code) <= 599:
                    await breaker.record_failure()
                else:
                    await breaker.record_success()
                return AuthResult(success=False, message=f"SSO недоступен: {auth_page.status_code}")
            await breaker.record_success()

            # Шаг 2: Извлекаем loginAction URL из kcContext в JavaScript
            login_action = self._extract_login_action(auth_page.text)
            if not login_action:
                return AuthResult(success=False, message="Не удалось получить форму входа")

            logger.info(f"loginAction: {login_action[:80]}...")

            # Шаг 3: POST username/password
            login_response = await self.client.post(
                login_action,
                data={"username": username, "password": password},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": str(auth_page.url),
                },
                follow_redirects=False,
            )

            logger.info(f"Cookies: {list(self.client.cookies.keys())}")

            if login_response.status_code in (302, 303, 307, 308) and login_response.headers.get("location"):
                location = login_response.headers.get("location") or ""
                redirect_url = urljoin(str(login_response.url), location)
                code = self._extract_code_from_url(redirect_url)

                # Keycloak may redirect to a required-action page before issuing the code.
                if not code and "required-action" in redirect_url:
                    ra_result = await self._handle_required_actions(
                        redirect_url,
                        referer=login_action,
                        pkce_verifier=pkce_verifier,
                        redirect_uri=redirect_uri,
                    )
                    if ra_result:
                        return ra_result

                # Exchange authorization code BEFORE following redirect.
                # Keycloak codes are single-use; the redirect would consume it.
                all_cookies = dict(self.client.cookies)
                if code:
                    tokens = await self._exchange_code_for_token(
                        code,
                        code_verifier=pkce_verifier,
                        redirect_uri=redirect_uri,
                    )
                    if tokens:
                        all_cookies.update(tokens)

                # Now follow redirect to establish server-side session cookie.
                try:
                    await self.client.get(
                        redirect_url,
                        headers={"Referer": login_action},
                        follow_redirects=True,
                    )
                    all_cookies.update(dict(self.client.cookies))
                except Exception:
                    pass

                if all_cookies.get("access_token"):
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                has_session = any(k in all_cookies for k in ('KEYCLOAK_IDENTITY', 'KEYCLOAK_SESSION'))
                if has_session:
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                if all_cookies:
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                return AuthResult(success=False, message="Не удалось получить сессию")

            final_url = str(login_response.url)
            logger.info(f"Final URL: {final_url}")

            # Если остались на Keycloak - ошибка авторизации (проверяем хост, не параметры)
            final_host = urlparse(final_url).hostname or ""
            if "login-actions" in final_url or final_host == "sso.mirea.ru":
                # Частый кейс: включена 2FA (OTP), Keycloak покажет вторую форму вместо редиректа.
                self._log_kc_context(login_response.text)
                challenge = self._extract_otp_challenge(login_response.text, base_url=final_url)
                if challenge:
                    challenge.referer = final_url
                    challenge.pkce_verifier = pkce_verifier
                    challenge.redirect_uri = redirect_uri
                    return AuthResult(
                        success=False,
                        message="Требуется код подтверждения (2FA)",
                        cookies=dict(self.client.cookies),
                        challenge=challenge,
                    )

                # Пробуем найти ошибку в kcContext
                error_msg = self._extract_keycloak_error(login_response.text)
                return AuthResult(
                    success=False,
                    message=error_msg or "Неверный логин или пароль"
                )

            # Собираем cookies
            all_cookies = dict(self.client.cookies)

            # Some setups might not redirect (or the final URL keeps the code).
            code = self._extract_code_from_url(final_url)
            if code and "access_token" not in all_cookies:
                tokens = await self._exchange_code_for_token(
                    code,
                    code_verifier=pkce_verifier,
                    redirect_uri=redirect_uri,
                )
                if tokens:
                    all_cookies.update(tokens)

            has_session = any(k in all_cookies for k in ['KEYCLOAK_SESSION', 'KEYCLOAK_IDENTITY', 'sessionid'])
            if has_session:
                return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)

            # Даже без известных cookies - если нас редиректнуло с Keycloak, считаем успехом
            if all_cookies:
                return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)

            return AuthResult(success=False, message="Не удалось получить сессию")

        except httpx.TimeoutException:
            try:
                await get_breaker("mirea_sso").record_failure()
            except Exception:
                pass
            return AuthResult(success=False, message="Сервер МИРЭА не отвечает")
        except Exception as e:
            logger.error(f"Login error: {e}")
            return AuthResult(success=False, message="Внутренняя ошибка")

    @staticmethod
    def _log_kc_context(html: str) -> None:
        """Log kcContext details for debugging OTP issues."""
        try:
            page_id = re.search(r'"pageId"\s*:\s*"([^"]+)"', html)
            if page_id:
                logger.info("kcContext pageId: %s", page_id.group(1))

            # Dump raw HTML around kcContext (first <script> block with pageId).
            # The kcContext is embedded as a JS object in a <script> tag.
            script_idx = html.find('"pageId"')
            if script_idx >= 0:
                # Go back to find the start of the script content
                start = max(0, script_idx - 200)
                chunk = html[start:start+5000].replace('\n', ' ').replace('\r', ' ')
                chunk = re.sub(r'\s+', ' ', chunk)
                logger.info("kcContext raw: %s", chunk)

            # Log any message/error
            msg = re.search(r'"message"\s*:\s*"([^"]*)"', html)
            if msg:
                logger.info("kcContext message: %s", msg.group(1))
        except Exception:
            pass

    def _extract_login_action(self, html: str) -> str | None:
        """Извлечь loginAction URL из kcContext JavaScript"""
        m = re.search(r'"loginAction"\s*:\s*"(https?://[^"]+)"', html)
        if m:
            # URL может содержать escaped символы
            return m.group(1).replace("\\u0026", "&").replace("\\/", "/")
        return None

    def _extract_keycloak_error(self, html: str) -> str | None:
        """Извлечь ошибку из kcContext"""
        m = re.search(r'"message"\s*:\s*"([^"]+)"', html)
        if m:
            msg = m.group(1)
            msg_l = msg.lower()
            # Keycloak can return generic "invalid" messages for both password and OTP.
            if "invalid" in msg_l:
                if any(x in msg_l for x in ("otp", "code", "authenticator", "однораз", "кода")):
                    return "Неверный код подтверждения"
                if any(x in msg_l for x in ("credential", "password", "парол")):
                    return "Неверный логин или пароль"
            return msg
        return None

    def _extract_otp_challenge(self, html: str, *, base_url: str | None = None) -> AuthChallenge | None:
        """
        Detect Keycloak OTP step and extract form action + hidden fields.

        Supports both SPA (kcContext JavaScript) and classic HTML form pages.
        """
        try:
            # SPA detection: Keycloak SPA pages have "otpLogin" or "login-max-otp" in kcContext
            if '"otpLogin"' in html:
                action_url = self._extract_login_action(html)
                if action_url:
                    logger.info("OTP challenge detected from kcContext SPA")
                    return AuthChallenge(
                        kind="otp",
                        action_url=action_url,
                        field_name="otp",
                        hidden_fields={},
                    )

            # MAX OTP (SMS-based 2FA via MAX messenger)
            if '"login-max-otp"' in html:
                action_url = self._extract_login_action(html)
                if action_url:
                    logger.info("MAX OTP challenge detected from kcContext SPA")
                    return AuthChallenge(
                        kind="otp",
                        action_url=action_url,
                        field_name="code",
                        hidden_fields={"login": "true"},
                    )

            # Email code 2FA (code sent to user's @edu.mirea.ru email)
            if '"email-code-form"' in html:
                action_url = self._extract_login_action(html)
                if action_url:
                    logger.info("Email code challenge detected from kcContext SPA")
                    return AuthChallenge(
                        kind="email_code",
                        action_url=action_url,
                        field_name="emailCode",
                        hidden_fields={},
                    )

            # Fallback: classic HTML form parsing
            soup = BeautifulSoup(html, "html.parser")

            # Find the OTP form first (different Keycloak themes can rename the field).
            form = (
                soup.find("form", {"id": "kc-otp-login-form"})
                or soup.find("form", {"name": "kc-otp-login-form"})
            )
            if not form:
                # Heuristics: any form that contains an "one-time-code" field.
                for f in soup.find_all("form"):
                    if f.find("input", {"autocomplete": "one-time-code"}):
                        form = f
                        break

            # Fallback: look for an input that likely represents a 2FA/OTP code, then take its parent form.
            otp_input = None
            if not form:
                candidates = []
                for inp in soup.find_all("input"):
                    itype = (inp.get("type") or "").lower().strip()
                    name = (inp.get("name") or "").lower().strip()
                    iid = (inp.get("id") or "").lower().strip()
                    ac = (inp.get("autocomplete") or "").lower().strip()
                    im = (inp.get("inputmode") or "").lower().strip()

                    if itype in {"hidden", "submit", "button"}:
                        continue
                    if name in {"username", "password"} or iid in {"username", "password"}:
                        continue

                    # Strong indicators first.
                    score = 0
                    if ac == "one-time-code":
                        score += 10
                    if "otp" in name or "otp" in iid:
                        score += 8
                    if "totp" in name or "totp" in iid:
                        score += 8
                    if "code" in name or "code" in iid:
                        score += 6
                    if "token" in name or "token" in iid:
                        score += 4
                    if im in {"numeric", "tel"}:
                        score += 2
                    if itype in {"tel", "number"}:
                        score += 2
                    if score > 0:
                        candidates.append((score, inp))

                if candidates:
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    otp_input = candidates[0][1]
                    form = otp_input.find_parent("form")

            if not form:
                return None

            # Determine the field name to POST.
            otp_field_name = None
            if not otp_input:
                for name in ("otp", "totp", "otpCode"):
                    otp_input = form.find("input", {"name": name}) or form.find("input", {"id": name})
                    if otp_input:
                        otp_field_name = otp_input.get("name") or name
                        break

            if not otp_field_name:
                # Pick first non-hidden input that isn't username/password.
                for inp in form.find_all("input"):
                    itype = (inp.get("type") or "").lower().strip()
                    if itype in {"hidden", "submit", "button"}:
                        continue
                    name = (inp.get("name") or "").strip()
                    if not name:
                        continue
                    if name.lower() in {"username", "password"}:
                        continue
                    otp_field_name = name
                    otp_input = inp
                    break

            if not otp_input or not otp_field_name:
                return None

            # Keycloak usually exposes a "loginAction" in kcContext as a full URL.
            action = self._extract_login_action(html) or (form.get("action") or "").strip()
            if not action:
                return None
            if base_url and action.startswith("/"):
                action = urljoin(base_url, action)
            if base_url and not action.startswith("http"):
                action = urljoin(base_url, action)

            hidden_fields: dict[str, str] = {}
            for inp in form.find_all("input"):
                try:
                    if (inp.get("type") or "").lower() != "hidden":
                        continue
                    name = (inp.get("name") or "").strip()
                    if not name:
                        continue
                    hidden_fields[name] = inp.get("value") or ""
                except Exception:
                    continue

            return AuthChallenge(
                kind="otp",
                action_url=action,
                field_name=otp_field_name,
                hidden_fields=hidden_fields,
            )
        except Exception:
            return None

    async def _handle_required_actions(self, start_url: str, *, referer: str, pkce_verifier: str | None, redirect_uri: str) -> AuthResult | None:
        """
        Handle Keycloak required-action pages (e.g. max-account-config).

        If the page contains an OTP/TOTP challenge, returns it as a new AuthChallenge
        for the user to complete. Otherwise attempts to skip or complete the action.

        Returns AuthResult on success/failure, or None if the URL is not a required-action.
        """
        url = start_url
        for attempt in range(5):  # max 5 required actions
            if "required-action" not in url:
                return None

            logger.info("Keycloak required-action detected: %s", url[:120])

            try:
                page_resp = await self.client.get(url, headers={"Referer": referer}, follow_redirects=False)
            except Exception as e:
                logger.warning("required-action GET failed: %s", e)
                return None

            # If the required-action itself redirects (e.g., to the app with code), follow it.
            if page_resp.status_code in (302, 303, 307, 308):
                location = page_resp.headers.get("location", "")
                next_url = urljoin(str(page_resp.url), location)
                code = self._extract_code_from_url(next_url)
                if code:
                    all_cookies = dict(self.client.cookies)
                    tokens = await self._exchange_code_for_token(code, code_verifier=pkce_verifier, redirect_uri=redirect_uri)
                    if tokens:
                        all_cookies.update(tokens)
                    try:
                        await self.client.get(next_url, headers={"Referer": url}, follow_redirects=True)
                        all_cookies.update(dict(self.client.cookies))
                    except Exception:
                        pass
                    if all_cookies.get("access_token"):
                        return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                    # Token exchange failed but we may have session cookies (.AspNetCore.Cookies etc.)
                    has_session = any(k in all_cookies for k in ('.AspNetCore.Cookies', 'KEYCLOAK_IDENTITY', 'KEYCLOAK_SESSION'))
                    if has_session or all_cookies:
                        logger.info("required-action: token exchange failed but have session cookies, declaring success")
                        return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                # No code — keep following
                url = next_url
                referer = url
                continue

            # Got 200 — this is the required-action page HTML.
            html = page_resp.text
            self._log_kc_context(html)

            action_url = self._extract_login_action(html)
            if not action_url:
                logger.warning("required-action: no loginAction found, cannot skip")
                return None

            # Try to skip/cancel the required action.
            # MIREA theme uses hidden input name="skip" value="true" in kc-max-otp-skip-form.
            skip_payloads = [
                {"skip": "true"},
                {"cancel-aia": "true"},
            ]
            for payload in skip_payloads:
                logger.info("Attempting to skip required-action with %s to %s", payload or "{}", action_url[:100])
                try:
                    skip_resp = await self.client.post(
                        action_url,
                        data=payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": str(page_resp.url)},
                        follow_redirects=False,
                    )
                except Exception as e:
                    logger.warning("required-action POST failed: %s", e)
                    continue

                if skip_resp.status_code in (302, 303, 307, 308):
                    location = skip_resp.headers.get("location", "")
                    next_url = urljoin(str(skip_resp.url), location)
                    logger.info("required-action skip redirected to: %s", next_url[:120])
                    code = self._extract_code_from_url(next_url)
                    if code:
                        all_cookies = dict(self.client.cookies)
                        tokens = await self._exchange_code_for_token(code, code_verifier=pkce_verifier, redirect_uri=redirect_uri)
                        if tokens:
                            all_cookies.update(tokens)
                        try:
                            await self.client.get(next_url, headers={"Referer": action_url}, follow_redirects=True)
                            all_cookies.update(dict(self.client.cookies))
                        except Exception:
                            pass
                        if all_cookies.get("access_token"):
                            return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                        # Token exchange failed (confidential client) — try to bootstrap
                        # .AspNetCore.Cookies via pulse auth endpoint using the KC session.
                        # Without this cookie, downstream Pulse API calls return 401.
                        has_kc = any(k in all_cookies for k in ("KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION"))
                        if has_kc:
                            logger.info("required-action: token exchange failed but have KC cookies, trying aspnet bootstrap")
                            try:
                                kc_cookie_names = ("KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION")
                                cookie_header_parts = []
                                for cn in kc_cookie_names:
                                    cv = all_cookies.get(cn)
                                    if cv:
                                        cookie_header_parts.append(f"{cn}={cv}")
                                cookie_header = "; ".join(cookie_header_parts)
                                async with httpx.AsyncClient(
                                    follow_redirects=True,
                                    timeout=httpx.Timeout(15.0, connect=8.0),
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
                                        "Referer": "https://pulse.mirea.ru/",
                                        "Cookie": cookie_header,
                                    },
                                    transport=httpx.AsyncHTTPTransport(retries=2),
                                ) as bootstrap_client:
                                    bootstrap_resp = await bootstrap_client.get(
                                        "https://pulse.mirea.ru/api/auth/login",
                                        params={"redirectUri": "/api/baseinfo"},
                                    )
                                    for name, value in bootstrap_client.cookies.items():
                                        all_cookies[name] = value
                                    aspnet = bootstrap_client.cookies.get(".AspNetCore.Cookies")
                                    if aspnet:
                                        all_cookies[".AspNetCore.Cookies"] = aspnet
                                        logger.info("required-action: got .AspNetCore.Cookies via bootstrap")
                                    else:
                                        logger.warning(
                                            "required-action: bootstrap did not yield .AspNetCore.Cookies, final_url=%s",
                                            str(bootstrap_resp.url)[:150],
                                        )
                            except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
                                logger.warning("required-action: aspnet bootstrap failed: %s", type(e).__name__)
                            return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                        if all_cookies:
                            logger.info("required-action skip: token exchange failed but have cookies, declaring success")
                            return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                    # Another redirect — might be another required-action or next step
                    url = next_url
                    referer = action_url
                    break  # break skip_payloads loop, continue outer loop
                else:
                    logger.info("required-action skip returned %s, trying next approach", skip_resp.status_code)
                    continue
            else:
                # All skip approaches returned non-redirect — fall back to OTP challenge.
                otp_challenge = self._extract_otp_challenge(html, base_url=str(page_resp.url))
                if otp_challenge:
                    otp_challenge.referer = str(page_resp.url)
                    otp_challenge.pkce_verifier = pkce_verifier
                    otp_challenge.redirect_uri = redirect_uri
                    logger.info("required-action: skip failed, returning OTP challenge (kind=%s)", otp_challenge.kind)
                    return AuthResult(
                        success=False,
                        message="Требуется код подтверждения (2FA)",
                        cookies=dict(self.client.cookies),
                        challenge=otp_challenge,
                    )
                logger.warning("required-action: all skip attempts failed, no OTP fallback")
                return None
            return None

        return None

    async def submit_otp(self, challenge: AuthChallenge, otp_code: str, *, cookies: dict | None = None) -> AuthResult:
        """
        Continue Keycloak login flow with an OTP code.

        `challenge` should be returned by `_extract_otp_challenge`.
        """
        try:
            if cookies:
                self.client.cookies.update(cookies)
                if self.proxy_client:
                    self.proxy_client.cookies.update(self.client.cookies)

            payload = dict(challenge.hidden_fields or {})
            payload[challenge.field_name] = (otp_code or "").strip()

            if not payload[challenge.field_name]:
                return AuthResult(success=False, message="Введите код подтверждения")

            resp = await self.client.post(
                challenge.action_url,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": challenge.referer or challenge.action_url,
                },
                follow_redirects=False,
            )

            if resp.status_code in (302, 303, 307, 308) and resp.headers.get("location"):
                location = resp.headers.get("location") or ""
                redirect_url = urljoin(str(resp.url), location)
                code = self._extract_code_from_url(redirect_url)
                redirect_uri = challenge.redirect_uri or f"{self.ATTENDANCE_URL}/"

                # Keycloak may redirect to a required-action page before issuing the code.
                if not code and "required-action" in redirect_url:
                    ra_result = await self._handle_required_actions(
                        redirect_url,
                        referer=challenge.action_url,
                        pkce_verifier=challenge.pkce_verifier,
                        redirect_uri=redirect_uri,
                    )
                    if ra_result:
                        return ra_result
                    # Could not skip — fall through to legacy handling.

                # Exchange authorization code BEFORE following redirect.
                # Keycloak codes are single-use; the redirect would consume it.
                all_cookies = dict(self.client.cookies)
                if code:
                    tokens = await self._exchange_code_for_token(
                        code,
                        code_verifier=challenge.pkce_verifier,
                        redirect_uri=redirect_uri,
                    )
                    if tokens:
                        all_cookies.update(tokens)

                # Now follow redirect to establish server-side session cookie.
                try:
                    await self.client.get(
                        redirect_url,
                        headers={"Referer": challenge.action_url},
                        follow_redirects=True,
                    )
                    all_cookies.update(dict(self.client.cookies))
                except Exception:
                    pass

                if all_cookies.get("access_token"):
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                has_session = any(k in all_cookies for k in ('.AspNetCore.Cookies', 'KEYCLOAK_IDENTITY', 'KEYCLOAK_SESSION'))
                if has_session:
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                if all_cookies:
                    return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)
                return AuthResult(success=False, message="Не удалось получить сессию")

            final_url = str(resp.url)
            final_host = urlparse(final_url).hostname or ""
            if "login-actions" in final_url or final_host == "sso.mirea.ru":
                self._log_kc_context(resp.text)
                error_msg = self._extract_keycloak_error(resp.text)
                next_challenge = self._extract_otp_challenge(resp.text, base_url=final_url)
                if next_challenge:
                    next_challenge.referer = final_url
                    next_challenge.pkce_verifier = challenge.pkce_verifier
                    next_challenge.redirect_uri = challenge.redirect_uri
                    return AuthResult(
                        success=False,
                        message=error_msg or "Не удалось подтвердить вход (2FA)",
                        cookies=dict(self.client.cookies),
                        challenge=next_challenge,
                    )

                return AuthResult(success=False, message=error_msg or "Не удалось подтвердить вход (2FA)")

            all_cookies = dict(self.client.cookies)
            code = self._extract_code_from_url(final_url)
            if code and "access_token" not in all_cookies:
                redirect_uri = challenge.redirect_uri or f"{self.ATTENDANCE_URL}/"
                tokens = await self._exchange_code_for_token(
                    code,
                    code_verifier=challenge.pkce_verifier,
                    redirect_uri=redirect_uri,
                )
                if tokens:
                    all_cookies.update(tokens)
            if all_cookies:
                return AuthResult(success=True, message="Авторизация успешна", cookies=all_cookies)

            return AuthResult(success=False, message="Не удалось получить сессию")
        except httpx.TimeoutException:
            try:
                await get_breaker("mirea_sso").record_failure()
            except Exception:
                pass
            return AuthResult(success=False, message="Сервер МИРЭА не отвечает")
        except Exception as e:
            logger.error(f"OTP submit error: {e}")
            return AuthResult(success=False, message="Внутренняя ошибка")

    async def complete_2fa(self, challenge: AuthChallenge, code: str, *, cookies: dict | None = None) -> AuthResult:
        """Alias for :meth:`submit_otp` — name used in README/examples."""
        return await self.submit_otp(challenge, code, cookies=cookies)

    async def verify_session(self, cookies: dict) -> bool:
        """
        Проверить, активна ли сессия

        Args:
            cookies: Сохранённые cookies

        Returns:
            True если сессия активна
        """
        try:
            filtered_cookies = {}
            for name, value in (cookies or {}).items():
                if name in {"access_token", "token_type", "refresh_token", "expires_in"}:
                    continue
                if str(name).startswith("__"):
                    continue
                filtered_cookies[name] = value
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(15.0, connect=8.0),
                cookies=filtered_cookies,
                transport=httpx.AsyncHTTPTransport(retries=2),
                proxy=settings.mirea_proxy if settings.mirea_proxy else None,
            ) as client:
                response = await client.get(self.ATTENDANCE_URL)

            # Если нас не редиректит на логин — сессия активна
            return "/login" not in str(response.url).lower()
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.debug("verify_session network error: %s", type(e).__name__)
            return False

    async def close(self):
        await self.client.aclose()
        if self.proxy_client:
            await self.proxy_client.aclose()

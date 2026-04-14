"""
Сервис для работы с API МИРЭА (pulse.mirea.ru)

QR-код содержит URL вида:
https://pulse.mirea.ru/selfapprove?token=UUID

Для отметки нужна авторизованная сессия через SSO МИРЭА.
"""
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from ._settings import settings
from .tokens import try_refresh_tokens
from .upstreams import get_breaker


@dataclass
class AttendanceResult:
    success: bool
    message: str
    user_name: str | None = None


class MireaAPI:
    BASE_URL = "https://pulse.mirea.ru"
    SSO_URL = "https://sso.mirea.ru"
    SELFAPPROVE_ENDPOINT = "/selfapprove"

    def __init__(self, session_cookies: dict | None = None):
        """
        Args:
            session_cookies: Сохранённые cookies для авторизации
        """
        self.session_cookies = session_cookies or {}
        # Do not send internal session fields (tokens, local state) as HTTP cookies.
        cookies = httpx.Cookies()
        for name, value in self.session_cookies.items():
            if name in {"access_token", "token_type", "refresh_token", "expires_in"}:
                continue
            if name.startswith("__"):
                continue
            cookies.set(name, value, domain=".mirea.ru")
        limits = httpx.Limits(max_connections=30, max_keepalive_connections=15, keepalive_expiry=10.0)
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            cookies=cookies,
            transport=httpx.AsyncHTTPTransport(retries=2, limits=limits),
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        )

    # Допустимые домены для QR-кодов посещаемости
    ALLOWED_DOMAINS = [
        "pulse.mirea.ru",
        "attendance-app.mirea.ru",
        "attendance.mirea.ru",
        "att.mirea.ru",
    ]

    @staticmethod
    def extract_token_from_qr(qr_data: str) -> tuple[str | None, str | None]:
        """
        Извлечь токен из данных QR-кода

        Args:
            qr_data: URL из QR-кода (https://pulse.mirea.ru/selfapprove?token=...)

        Returns:
            Кортеж (токен, ошибка) - токен если успешно, иначе ошибка
        """
        try:
            qr_data = qr_data.strip()

            # Если это просто токен (UUID)
            if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', qr_data, re.I):
                return qr_data, None

            # Если это URL (схема может отсутствовать)
            candidate_url = None
            if qr_data.startswith("http"):
                candidate_url = qr_data
            else:
                # Часто QR может содержать URL без схемы
                lower = qr_data.lower()
                if any(host in lower for host in MireaAPI.ALLOWED_DOMAINS):
                    candidate_url = "https://" + qr_data.lstrip("/")

            if candidate_url:
                parsed = urlparse(candidate_url)
                host = (parsed.hostname or "").lower()

                # Проверяем что домен принадлежит MIREA (учитываем возможный порт)
                if host not in MireaAPI.ALLOWED_DOMAINS:
                    return None, "Неверный QR-код. Ожидается код посещаемости МИРЭА."

                params = parse_qs(parsed.query)
                token = params.get("token", [None])[0]
                if not token:
                    return None, "QR-код не содержит токен посещаемости"
                return token, None

            return None, "Неверный формат QR-кода"
        except Exception:
            return None, "Ошибка обработки QR-кода"

    async def get_attendance_score(self) -> float | None:
        """
        Получить текущий балл посещаемости через grades API.
        Возвращает сумму баллов attendance по всем предметам или None при ошибке.
        """
        try:
            from .grades import MireaGrades
            grades_api = MireaGrades(self.session_cookies)
            result = await grades_api.get_grades()
            await grades_api.close()

            if result.success and result.subjects:
                # Суммируем баллы посещаемости по всем предметам
                total_attendance = sum(s.attendance for s in result.subjects)
                return total_attendance
            return None
        except Exception:
            return None

    async def _mark_attendance_via_grpc(
        self,
        token: str,
    ) -> AttendanceResult | None:
        """
        Try gRPC self-approve flow used by pulse.mirea.ru.
        Returns AttendanceResult when gRPC gives a clear answer, else None.
        """
        import logging
        logger = logging.getLogger(__name__)
        try:
            from .grades import MireaGrades
            grades_api = MireaGrades(self.session_cookies)
            try:
                approved, message = await grades_api.self_approve_attendance(token)
                # Persist any refreshed cookies (e.g., .AspNetCore.Cookies)
                self.session_cookies.update(grades_api.session_cookies)
            finally:
                await grades_api.close()
        except Exception as e:
            logger.warning(f"gRPC self-approve failed: {e}")
            return None

        if approved is True:
            return AttendanceResult(
                success=True,
                message="Посещаемость отмечена",
            )

        if approved is False:
            return AttendanceResult(
                success=False,
                message=message or "Отметка пока недоступна",
            )

        # approved is None -> gRPC error or unknown response
        if message and ("Перелогиньтесь" in message or "Сессия истекла" in message):
            return AttendanceResult(
                success=False,
                message=message,
            )
        logger.info(f"gRPC self-approve unclear: {message}")
        return None

    async def mark_attendance(self, qr_data: str, *, _refreshed: bool = False) -> AttendanceResult:
        """
        Отметить посещаемость по данным из QR-кода
        """
        token, error = self.extract_token_from_qr(qr_data)
        if not token:
            return AttendanceResult(
                success=False,
                message=error or "Не удалось извлечь токен из QR-кода"
            )

        # Prefer gRPC self-approve (official webapp flow)
        grpc_result = await self._mark_attendance_via_grpc(token)
        if grpc_result is not None:
            # If upstream says session expired, try refresh-token flow once.
            msg = (grpc_result.message or "").lower()
            looks_expired = ("перелог" in msg) or ("сессия истек" in msg) or ("needs_auth" in msg)
            if (not grpc_result.success) and (not _refreshed) and looks_expired:
                refreshed = await try_refresh_tokens(self.session_cookies)
                if refreshed:
                    grpc_retry = await self._mark_attendance_via_grpc(token)
                    if grpc_retry is not None:
                        return grpc_retry
            return grpc_result

        try:
            breaker = get_breaker("mirea_attendance_app")
            decision = await breaker.allow()
            if not decision.allowed:
                retry_after = decision.retry_after_s or 5
                return AttendanceResult(
                    success=False,
                    message=f"МИРЭА временно недоступна. Попробуйте через {retry_after} сек.",
                )

            # Делаем GET запрос на selfapprove с токеном
            url = f"{self.BASE_URL}{self.SELFAPPROVE_ENDPOINT}"
            response = await self.client.get(url, params={"token": token})

            # Анализируем ответ
            text = response.text
            text_lower = text.lower()

            # Логируем для отладки
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"MIREA selfapprove [{response.status_code}] URL: {response.url}")
            logger.info(f"MIREA Response headers: {dict(response.headers)}")
            logger.info(f"MIREA Response body ({len(text)} chars): {text[:1000]}")

            # Проверяем, не вернул ли сервер JSON вместо HTML
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    json_data = response.json()
                    logger.info(f"MIREA JSON response: {json_data}")
                    # Если есть поле success/status в JSON
                    if json_data.get("success") or json_data.get("status") == "ok":
                        return AttendanceResult(
                            success=True,
                            message="Посещаемость отмечена",
                        )
                    elif "error" in json_data or json_data.get("success") is False:
                        return AttendanceResult(
                            success=False,
                            message=json_data.get("message", json_data.get("error", "Ошибка сервера"))
                        )
                except Exception:
                    pass

            # Проверяем финальный URL после редиректов
            final_url = str(response.url)
            logger.info(f"MIREA final URL after redirects: {final_url}")

            # Если нас редиректнуло на страницу логина - сессия истекла
            from urllib.parse import urlparse as _urlparse
            _final_host = _urlparse(final_url).hostname or ""
            if "login" in _final_host or _final_host == "sso.mirea.ru":
                return AttendanceResult(
                    success=False,
                    message="Сессия истекла. Перелогиньтесь в МИРЭА."
                )

            if response.status_code == 200:
                await breaker.record_success()
                # Проверяем, не пустой ли ответ или SPA-заглушка
                is_spa_shell = (
                    len(text) < 500 and "<div id=" in text_lower and "script" in text_lower
                ) or (
                    "<!doctype html>" in text_lower and len(text) < 2000 and text.count("<script") > 2
                )

                if is_spa_shell:
                    logger.warning("MIREA returned SPA shell - cannot parse content")
                    return AttendanceResult(
                        success=False,
                        message="Сервер вернул SPA без результата. Попробуйте позже или откройте ссылку вручную.",
                    )

                # Проверяем явные индикаторы УСПЕХА
                success_markers = [
                    "успешно отмечен",
                    "посещение зафиксировано",
                    "вы отмечены",
                    "отметка принята",
                    "attendance recorded",
                    "successfully marked",
                    "ваше присутствие",
                    "присутствие подтверждено",
                ]
                is_success = any(marker in text_lower for marker in success_markers)

                # Проверяем явные индикаторы ОШИБКИ
                auth_markers = ["авторизац", "войти", "login", "sign in", "unauthorized"]
                expired_markers = ["истек", "expired", "недействителен", "invalid token", "not found"]
                already_markers = ["уже отмечен", "already marked", "повторн"]

                is_auth_error = any(marker in text_lower for marker in auth_markers)
                is_expired = any(marker in text_lower for marker in expired_markers)
                is_already = any(marker in text_lower for marker in already_markers)

                if is_success:
                    return AttendanceResult(
                        success=True,
                        message="Посещаемость отмечена",
                    )
                elif is_already:
                    return AttendanceResult(
                        success=True,
                        message="Вы уже отмечены на этом занятии",
                    )
                elif is_auth_error:
                    return AttendanceResult(
                        success=False,
                        message="Сессия истекла. Перелогиньтесь в МИРЭА."
                    )
                elif is_expired:
                    return AttendanceResult(
                        success=False,
                        message="QR-код недействителен или истёк"
                    )
                else:
                    logger.warning("Unknown MIREA response")
                    return AttendanceResult(
                        success=False,
                        message="Неизвестный ответ сервера. Проверьте вручную.",
                    )

            elif response.status_code == 401 or response.status_code == 403:
                await breaker.record_success()
                return AttendanceResult(
                    success=False,
                    message="Сессия истекла. Перелогиньтесь в МИРЭА."
                )
            elif response.status_code == 404:
                await breaker.record_success()
                return AttendanceResult(
                    success=False,
                    message="QR-код не найден или недействителен"
                )
            else:
                if 500 <= int(response.status_code) <= 599:
                    await breaker.record_failure()
                else:
                    await breaker.record_success()
                return AttendanceResult(
                    success=False,
                    message=f"Ошибка сервера МИРЭА: {response.status_code}"
                )

        except httpx.TimeoutException:
            try:
                await get_breaker("mirea_attendance_app").record_failure()
            except Exception:
                pass
            return AttendanceResult(
                success=False,
                message="Сервер МИРЭА не отвечает (timeout)"
            )
        except Exception:
            try:
                await get_breaker("mirea_attendance_app").record_failure()
            except Exception:
                pass
            return AttendanceResult(
                success=False,
                message="Ошибка отметки посещаемости"
            )

    async def mark_attendance_for_group(
        self,
        qr_data: str,
        users_sessions: list[tuple[int, str, dict]]  # [(user_id, name, cookies), ...]
    ) -> list[AttendanceResult]:
        """
        Отметить посещаемость для группы пользователей

        Args:
            qr_data: URL или токен из QR-кода
            users_sessions: Список кортежей (user_id, user_name, session_cookies)

        Returns:
            Список результатов для каждого пользователя
        """
        results = []
        token, error = self.extract_token_from_qr(qr_data)

        if not token:
            return [AttendanceResult(
                success=False,
                message=error or "Не удалось извлечь токен из QR-кода"
            )]

        for user_id, user_name, cookies in users_sessions:
            api = MireaAPI(session_cookies=cookies)
            try:
                result = await api.mark_attendance(qr_data)
                result.user_name = user_name
                results.append(result)
            except Exception:
                results.append(AttendanceResult(
                    success=False,
                    message="Ошибка отметки",
                    user_name=user_name
                ))
            finally:
                await api.close()

        return results

    def export_cookies(self) -> str:
        """Экспортировать cookies в JSON для сохранения в БД"""
        return json.dumps(dict(self.client.cookies))

    @staticmethod
    def import_cookies(cookies_json: str) -> dict:
        """Импортировать cookies из JSON"""
        try:
            return json.loads(cookies_json)
        except:
            return {}

    async def close(self):
        await self.client.aclose()

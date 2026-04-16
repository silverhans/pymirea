"""
Сервис для получения баллов БРС (learn-rating-system) через gRPC-Web API.

Ключевой момент: браузер/Pulse не ходит в эти gRPC методы с Bearer токеном.
Он использует cookie `.AspNetCore.Cookies` на `attendance.mirea.ru`.
Эта cookie появляется после перехода на:

  https://pulse.mirea.ru/api/auth/login?redirectUri=%2Fapi%2Fbaseinfo

при наличии активной Keycloak-сессии (cookies `KEYCLOAK_IDENTITY/KEYCLOAK_SESSION` и др).

Мы повторяем это поведение, затем вызываем gRPC-Web методы и парсим protobuf.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ._settings import settings
from .tokens import get_authorization_header, try_refresh_tokens
from .upstreams import get_breaker

logger = logging.getLogger(__name__)


@dataclass
class Subject:
    name: str
    discipline_id: str | None = None
    current_control: float = 0.0  # Текущий контроль (Макс. 40)
    semester_control: float = 0.0  # Семестровый контроль (Макс. 30)
    attendance: float = 0.0  # Посещения (Макс. 30)
    attendance_max_possible: float | None = None  # Достижимый максимум посещаемости (если возвращается API)
    achievements: float = 0.0  # Достижения (Макс. 10)
    additional: float = 0.0  # Дополнительные (Макс. 10)
    total: float = 0.0  # Всего баллов (Макс. 120)


@dataclass
class GradesResult:
    success: bool
    message: str
    subjects: list[Subject] | None = None
    semester: str | None = None


@dataclass
class ScheduleLesson:
    name: str
    lesson_type: str = ""  # ПР, ЛЕК, ЛАБ
    room: str = ""
    teacher: str = ""
    start_epoch: float | None = None
    end_epoch: float | None = None
    subgroup: str = ""


@dataclass
class ScheduleResult:
    success: bool
    message: str
    lessons: list[ScheduleLesson] | None = None


@dataclass
class AttendanceEntry:
    lesson_start: float | None = None  # epoch timestamp
    attend_type: int | None = None  # 0=unknown, 1=absent, 2=excused, 3=present


@dataclass
class AttendanceSummary:
    total_lessons: int = 0
    present: int = 0
    excused: int = 0
    absent: int = 0


@dataclass
class AttendanceDetailResult:
    success: bool
    message: str
    summary: AttendanceSummary | None = None
    entries: list[AttendanceEntry] | None = None


@dataclass
class _CategoryDef:
    id: str
    title: str | None = None
    max_value: float | None = None


@dataclass
class _ParsedDiscipline:
    name: str
    discipline_id: str | None
    components: dict[str, float]
    component_caps: dict[str, float]
    total: float | None


class MireaGrades:
    """
    Получение оценок из БРС МИРЭА через gRPC-Web (как в Pulse).

    Важное: перед gRPC вызовами нужно убедиться, что есть `.AspNetCore.Cookies`.
    """

    APP_URL = "https://pulse.mirea.ru"
    ATTENDANCE_URL = "https://pulse.mirea.ru"
    AUTH_LOGIN_URL = "https://pulse.mirea.ru/api/auth/login"

    GRPC_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.LearnRatingScoreService/GetLearnRatingScoreReportForStudentInVisitingLogV2"
    )
    VISITING_LOGS_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.VisitingLogService/GetAvailableVisitingLogsOfStudent"
    )
    ATTENDANCE_PRIMARY_INFO_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.AttendanceService/GetStudentAttendancesPrimaryInfoOfDiscipline"
    )
    ATTENDANCE_DISCIPLINE_INFO_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.AttendanceService/GetStudentAttendancesOfDisciplineInVisitingLog"
    )
    SELF_APPROVE_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.AttendanceService/SelfApproveAttendanceThroughQRCode"
    )
    LESSONS_URL = (
        "https://pulse.mirea.ru/"
        "rtu_tc.attendance.api.LessonService/GetAvailableLessonsOfVisitingLogs"
    )

    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    # Stable category UUIDs observed in Pulse learn-rating-system.
    _CAT_CURRENT = "e8e1272c-63b2-5a17-b60d-4b7aac78169e"
    _CAT_SEMESTER = "b05e81e2-8c9b-5050-97d5-0d0c7c232468"
    _CAT_ATTENDANCE = "fc76ecc6-3ac7-5a50-9694-72e0bf301976"
    _CAT_ACHIEVEMENTS = "5bd0a6c7-53e5-5c7d-aa6b-0dd8356a366c"
    _CAT_ADDITIONAL = "d88ec1f5-9400-5bae-9b58-b4c2f64a9f1f"

    def __init__(self, session_cookies: dict):
        self.session_cookies = session_cookies or {}
        self._cache_key = "__brs_visiting_log_id"
        self._student_id_cache_key = "__brs_student_id"

        cookies = httpx.Cookies()
        for name, value in (self.session_cookies or {}).items():
            if not value:
                continue
            if name in {"access_token", "token_type", "refresh_token", "expires_in"}:
                continue
            if str(name).startswith("__"):
                continue
            # Make cookies available for all *.mirea.ru hosts to survive redirects between subdomains.
            cookies.set(str(name), str(value), domain=".mirea.ru")

        limits = httpx.Limits(max_connections=30, max_keepalive_connections=15, keepalive_expiry=10.0)
        timeout = httpx.Timeout(30.0, connect=10.0)
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={
                # Safari-ish UA to look like a real client (helps with WAF heuristics).
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            cookies=cookies,
            transport=httpx.AsyncHTTPTransport(retries=2, limits=limits),
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        )

    # --- Auth bootstrap ---

    async def _ensure_aspnet_cookie(self) -> tuple[bool, str | None]:
        """
        Ensure `.AspNetCore.Cookies` exists in the session.

        Returns (ok, error_message).
        """
        existing = self.client.cookies.get(".AspNetCore.Cookies")
        if existing:
            self.session_cookies[".AspNetCore.Cookies"] = existing
            return True, None

        breaker = get_breaker("mirea_attendance_grpc")
        decision = await breaker.allow()
        if not decision.allowed:
            retry_after = decision.retry_after_s or 5
            return False, f"МИРЭА временно недоступна. Попробуйте через {retry_after} сек."

        base_headers = {
            # Mirror the web app.
            "Origin": self.APP_URL,
            "Referer": f"{self.APP_URL}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        authz = get_authorization_header(self.session_cookies)
        if authz:
            base_headers["Authorization"] = authz

        try:
            resp = await self.client.get(
                self.AUTH_LOGIN_URL,
                params={"redirectUri": "/api/baseinfo"},
                headers=base_headers,
            )
            final_url = str(resp.url)
            logger.info(f"BRS auth bootstrap: {resp.status_code} final={final_url}")

            # If we ended up on SSO, the session is not authorized anymore.
            final_host = (httpx.URL(final_url).host or "").lower()
            if "sso.mirea.ru" in final_host or "login.mirea.ru" in final_host:
                # Try refresh-token flow first (best-effort). Some upstreams accept Bearer auth
                # for issuing `.AspNetCore.Cookies`, or gRPC may accept Bearer directly.
                refreshed = await try_refresh_tokens(self.session_cookies)
                if refreshed:
                    authz2 = get_authorization_header(self.session_cookies)
                    headers2 = dict(base_headers)
                    if authz2:
                        headers2["Authorization"] = authz2
                    try:
                        resp2 = await self.client.get(
                            self.AUTH_LOGIN_URL,
                            params={"redirectUri": "/api/baseinfo"},
                            headers=headers2,
                        )
                        final_url2 = str(resp2.url)
                        final_host2 = (httpx.URL(final_url2).host or "").lower()
                        logger.info(f"BRS auth bootstrap retry: {resp2.status_code} final={final_url2}")
                        aspnet2 = self.client.cookies.get(".AspNetCore.Cookies")
                        if aspnet2:
                            self.session_cookies[".AspNetCore.Cookies"] = aspnet2
                            await breaker.record_success()
                            return True, None
                        if "sso.mirea.ru" not in final_host2 and "login.mirea.ru" not in final_host2:
                            # Upstream responded but didn't set cookie. We'll fall back to Bearer in gRPC headers.
                            await breaker.record_success()
                            if authz2:
                                return True, None
                        # Still unauthorized after refresh attempt.
                    except Exception:
                        pass

                await breaker.record_success()
                # If we have an access token, allow caller to proceed: gRPC may accept Bearer.
                if get_authorization_header(self.session_cookies):
                    return True, None
                return False, "Сессия истекла. Перелогиньтесь в МИРЭА."

            aspnet = self.client.cookies.get(".AspNetCore.Cookies")
            if aspnet:
                self.session_cookies[".AspNetCore.Cookies"] = aspnet
                await breaker.record_success()
                return True, None

            # Upstream responded, but didn't give us the cookie (usually auth/session issue).
            await breaker.record_success()
            return False, "Не удалось получить cookie (.AspNetCore.Cookies). Перелогиньтесь."
        except Exception as e:
            logger.warning(f"BRS auth bootstrap failed: {e}")
            await breaker.record_failure()
            return False, "МИРЭА не отвечает. Попробуйте позже."

    # --- gRPC-Web helpers ---

    def _grpc_headers(self) -> dict:
        headers = {
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-requested-with": "XMLHttpRequest",
            "Origin": self.APP_URL,
            "Referer": f"{self.APP_URL}/",
            "pulse-app-type": "pulse-app",
            "pulse-app-version": "1.5.9+5157",
            "Accept": "*/*",
        }
        authz = get_authorization_header(self.session_cookies)
        if authz:
            headers["Authorization"] = authz
        return headers

    @staticmethod
    def _grpc_web_frame(payload: bytes) -> bytes:
        return struct.pack(">BI", 0, len(payload)) + payload

    @staticmethod
    def _parse_grpc_web_frames(data: bytes) -> tuple[list[bytes], dict[str, str]]:
        """
        Parse gRPC-Web response frames.

        Returns (messages, trailers).
        """
        msgs: list[bytes] = []
        trailers: dict[str, str] = {}
        pos = 0
        while pos + 5 <= len(data):
            flag = data[pos]
            length = struct.unpack(">I", data[pos + 1 : pos + 5])[0]
            pos += 5
            if pos + length > len(data):
                break
            payload = data[pos : pos + length]
            pos += length

            if flag == 0x00:
                msgs.append(payload)
                continue
            if flag == 0x80:
                try:
                    text = payload.decode("utf-8", errors="ignore")
                    for line in text.split("\r\n"):
                        if not line or ":" not in line:
                            continue
                        k, v = line.split(":", 1)
                        trailers[k.strip().lower()] = v.strip()
                except Exception:
                    pass
                continue
            # Unknown flag: ignore.
        return msgs, trailers

    @staticmethod
    def _try_decode_grpc_web_text(data: bytes) -> bytes | None:
        """
        grpc-web-text returns base64 instead of binary frames.
        Best-effort decode when payload looks like base64.
        """
        if not data:
            return None
        text = data.strip()
        if not text:
            return None
        if not re.fullmatch(rb"[A-Za-z0-9+/=\r\n]+", text):
            return None
        compact = re.sub(rb"[\r\n\s]+", b"", text)
        if not compact or (len(compact) % 4) != 0:
            return None
        try:
            return base64.b64decode(compact, validate=False)
        except Exception:
            return None

    async def _grpc_unary(
        self,
        url: str,
        proto_payload: bytes,
        *,
        refresh_retry: bool = True,
    ) -> tuple[bytes | None, str | None]:
        """
        Perform unary gRPC-Web call and return the protobuf message bytes.
        """
        breaker = get_breaker("mirea_attendance_grpc")
        decision = await breaker.allow()
        if not decision.allowed:
            retry_after = decision.retry_after_s or 5
            return None, f"МИРЭА временно недоступна. Попробуйте через {retry_after} сек."

        try:
            resp = await self.client.post(
                url,
                content=self._grpc_web_frame(proto_payload),
                headers=self._grpc_headers(),
            )
        except httpx.TimeoutException:
            await breaker.record_failure()
            return None, "Сервер не отвечает"
        except Exception as e:
            await breaker.record_failure()
            return None, f"Ошибка сети: {e}"

        if resp.status_code != 200:
            if 500 <= int(resp.status_code) <= 599:
                await breaker.record_failure()
            else:
                await breaker.record_success()
            return None, f"Ошибка сервера: {resp.status_code}"

        raw_content = resp.content or b""
        msgs, trailers = self._parse_grpc_web_frames(raw_content)
        if not msgs and not trailers:
            decoded = self._try_decode_grpc_web_text(raw_content)
            if decoded:
                msgs, trailers = self._parse_grpc_web_frames(decoded)
        grpc_status = trailers.get("grpc-status")
        if grpc_status and grpc_status != "0":
            grpc_message = trailers.get("grpc-message") or "gRPC ошибка"
            if refresh_retry and grpc_status in {"7", "16"}:
                refreshed = await try_refresh_tokens(self.session_cookies)
                if refreshed:
                    return await self._grpc_unary(url, proto_payload, refresh_retry=False)
            await breaker.record_success()
            return None, grpc_message

        await breaker.record_success()
        if not msgs:
            preview = (raw_content[:300] or b"").decode("utf-8", errors="ignore").lower()
            looks_like_auth = ("<html" in preview or "keycloak" in preview or "login" in preview or "signin" in preview)
            if looks_like_auth and refresh_retry:
                refreshed = await try_refresh_tokens(self.session_cookies)
                if refreshed:
                    return await self._grpc_unary(url, proto_payload, refresh_retry=False)
            if looks_like_auth:
                return None, "Сессия МИРЭА истекла. Перелогиньтесь."
            return None, "Пустой ответ gRPC"
        return msgs[0], None

    # --- Protobuf (wire format) ---

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        out = bytearray()
        v = int(value)
        while v > 0x7F:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v & 0x7F)
        return bytes(out)

    @staticmethod
    def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
        result = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result, pos
            shift += 7
            if shift > 70:
                break
        return result, pos

    def _skip_field(self, data: bytes, pos: int, wire_type: int) -> int:
        if wire_type == 0:  # varint
            _, pos = self._decode_varint(data, pos)
            return pos
        if wire_type == 1:  # 64-bit
            return pos + 8
        if wire_type == 5:  # 32-bit
            return pos + 4
        if wire_type == 2:  # length-delimited
            length, pos = self._decode_varint(data, pos)
            return pos + int(length)
        return pos

    def _read_length_delimited(self, data: bytes, pos: int) -> tuple[bytes, int]:
        length, pos = self._decode_varint(data, pos)
        length_i = int(length)
        if pos + length_i > len(data):
            return b"", len(data)
        value = data[pos : pos + length_i]
        return value, pos + length_i

    def _parse_string_field(self, data: bytes, field_no: int) -> str | None:
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == field_no and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                try:
                    return raw.decode("utf-8", errors="ignore").strip() or None
                except Exception:
                    return None
            p = self._skip_field(data, p, wt)
        return None

    # --- Visiting logs ---

    def _extract_uuid_strings(self, data: bytes, *, max_depth: int = 6) -> list[str]:
        """
        Recursively walk protobuf length-delimited fields and collect UUID strings.
        """
        found: list[str] = []

        def _walk(buf: bytes, depth: int) -> None:
            if depth > max_depth:
                return
            p = 0
            while p < len(buf):
                key, p = self._decode_varint(buf, p)
                if p >= len(buf):
                    break
                wt = key & 0x7
                if wt == 2:
                    raw, p2 = self._read_length_delimited(buf, p)
                    p = p2
                    try:
                        s = raw.decode("utf-8").strip()
                        if self._UUID_RE.match(s):
                            found.append(s)
                    except Exception:
                        pass
                    _walk(raw, depth + 1)
                    continue
                p = self._skip_field(buf, p, wt)

        _walk(data, 0)

        # Deduplicate preserving order
        out: list[str] = []
        seen = set()
        for x in found:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    async def _get_visiting_logs(self) -> tuple[list[str], str | None]:
        payload, err = await self._grpc_unary(self.VISITING_LOGS_URL, b"")
        if err or not payload:
            return [], err or "Не удалось получить список журналов"

        logs, student_id = self._parse_available_visiting_logs(payload)

        # Some responses are wrapped into field #1 with the real payload inside.
        unwrapped = self._unwrap_field_1(payload)
        if unwrapped:
            logs_unwrapped, student_id_unwrapped = self._parse_available_visiting_logs(unwrapped)
            if len(logs_unwrapped) > len(logs):
                logs = logs_unwrapped
            if not student_id and student_id_unwrapped:
                student_id = student_id_unwrapped

        if not logs:
            # Fallback for old/unknown response formats.
            logs = self._extract_uuid_strings(payload)
            if unwrapped:
                uu = self._extract_uuid_strings(unwrapped)
                if len(uu) > len(logs):
                    logs = uu
        if student_id and self._UUID_RE.match(student_id):
            self.session_cookies[self._student_id_cache_key] = student_id
        return logs, None

    def _parse_available_visiting_logs(self, data: bytes) -> tuple[list[str], str | None]:
        """
        Parse VisitingLogService.GetAvailableVisitingLogsOfStudent response:
        message { repeated visitingLogs (field 1), repeated studentMemberships (field 2) }.
        """
        log_ids: list[str] = []
        student_id: str | None = None

        def _add_log(value: str | None) -> None:
            if not value or not self._UUID_RE.match(value):
                return
            if value not in log_ids:
                log_ids.append(value)

        def _parse_visiting_log_from_base_info(raw: bytes) -> str | None:
            # pi: field 1 = visitingLogId
            return self._parse_string_field(raw, 1)

        def _parse_visiting_log_entry(raw: bytes) -> tuple[str | None, str | None]:
            # Gv:
            # field 1 = baseLogInfo (pi), field 4 = studentId
            entry_log_id: str | None = None
            entry_student_id: str | None = None
            p = 0
            while p < len(raw):
                key, p = self._decode_varint(raw, p)
                field = key >> 3
                wt = key & 0x7
                if field == 1 and wt == 2:
                    base_info, p = self._read_length_delimited(raw, p)
                    entry_log_id = _parse_visiting_log_from_base_info(base_info) or entry_log_id
                    continue
                if field == 4 and wt == 2:
                    sid, p = self._read_length_delimited(raw, p)
                    try:
                        entry_student_id = sid.decode("utf-8", errors="ignore").strip() or entry_student_id
                    except Exception:
                        pass
                    continue
                p = self._skip_field(raw, p, wt)
            return entry_log_id, entry_student_id

        def _parse_student_membership_entry(raw: bytes) -> tuple[str | None, str | None]:
            # Na:
            # field 1 = student (Vu), field 2 = visitingLog (pi)
            sid: str | None = None
            log_id: str | None = None
            p = 0
            while p < len(raw):
                key, p = self._decode_varint(raw, p)
                field = key >> 3
                wt = key & 0x7
                if field == 1 and wt == 2:
                    student_raw, p = self._read_length_delimited(raw, p)
                    # Vu field 2 = id
                    sid = self._parse_string_field(student_raw, 2) or sid
                    continue
                if field == 2 and wt == 2:
                    log_raw, p = self._read_length_delimited(raw, p)
                    log_id = _parse_visiting_log_from_base_info(log_raw) or log_id
                    continue
                p = self._skip_field(raw, p, wt)
            return sid, log_id

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                entry, p = self._read_length_delimited(data, p)
                log_id, sid = _parse_visiting_log_entry(entry)
                _add_log(log_id)
                if not student_id and sid and self._UUID_RE.match(sid):
                    student_id = sid
                continue
            if field == 2 and wt == 2:
                entry, p = self._read_length_delimited(data, p)
                sid, log_id = _parse_student_membership_entry(entry)
                _add_log(log_id)
                if not student_id and sid and self._UUID_RE.match(sid):
                    student_id = sid
                continue
            p = self._skip_field(data, p, wt)

        return log_ids, student_id

    # --- Attendance self-approve ---

    async def self_approve_attendance(self, token: str) -> tuple[bool | None, str | None]:
        """
        Call AttendanceService.SelfApproveAttendanceThroughQRCode via gRPC-Web.

        Returns (approved, message).
        """
        if not token:
            return None, "Пустой токен"

        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return None, err or "Сессия истекла. Перелогиньтесь в МИРЭА."

        payload, err = await self._grpc_unary(self.SELF_APPROVE_URL, self._encode_selfapprove_request(token))
        if err or not payload:
            return None, err or "Пустой ответ gRPC"

        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.debug("selfapprove raw payload hex: %s", payload.hex())

        approved, reason, _lesson_id = self._parse_selfapprove_response(payload)
        _log.debug("selfapprove parsed: approved=%s reason=%r lesson_id=%r", approved, reason, _lesson_id)
        if approved is True:
            return True, None
        if approved is False:
            return False, reason or "Отметка пока недоступна"
        return None, "Неизвестный ответ gRPC"

    # --- Grades report ---

    def _encode_grades_request(self, visiting_log_id: str) -> bytes:
        # field 1, wire type 2 (string)
        tag = (1 << 3) | 2
        b = visiting_log_id.encode("utf-8")
        return bytes([tag]) + self._encode_varint(len(b)) + b

    def _encode_student_discipline_visiting_log_request(
        self,
        student_id: str,
        discipline_id: str,
        visiting_log_id: str,
    ) -> bytes:
        # field 1: studentId, field 2: disciplineId, field 3: visitingLogId
        out = bytearray()
        for idx, value in enumerate((student_id, discipline_id, visiting_log_id), start=1):
            tag = (idx << 3) | 2
            b = value.encode("utf-8")
            out.append(tag)
            out.extend(self._encode_varint(len(b)))
            out.extend(b)
        return bytes(out)

    def _encode_selfapprove_request(self, token: str) -> bytes:
        # field 1, wire type 2 (string)
        tag = (1 << 3) | 2
        b = token.encode("utf-8")
        return bytes([tag]) + self._encode_varint(len(b)) + b

    def _parse_selfapprove_response(self, data: bytes) -> tuple[bool | None, str | None, str | None]:
        """
        Parse AttendanceService.SelfApproveAttendanceThroughQRCode response.

        Returns (approved, reason, lesson_id).
        """
        # Some deployments return a BoolValue wrapped in field 1:
        # 0a 02 08 01  => { success: { value: true } }
        def _parse_bool_wrapper(buf: bytes) -> bool | None:
            p = 0
            while p < len(buf):
                key, p = self._decode_varint(buf, p)
                field = key >> 3
                wt = key & 0x7
                if field == 1 and wt == 0:
                    val, p = self._decode_varint(buf, p)
                    return bool(val)
                if field == 1 and wt == 2:
                    raw, p = self._read_length_delimited(buf, p)
                    # Nested BoolValue: field 1, varint
                    p2 = 0
                    key2, p2 = self._decode_varint(raw, p2)
                    if (key2 >> 3) == 1 and (key2 & 0x7) == 0:
                        val, _ = self._decode_varint(raw, p2)
                        return bool(val)
                    return None
                p = self._skip_field(buf, p, wt)
            return None

        bool_value = _parse_bool_wrapper(data)
        if bool_value is not None:
            return bool_value, None, None

        not_yet_reason: str | None = None
        approved_lesson: str | None = None
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(data, p)
                if field == 1:  # notYet
                    not_yet_reason = self._parse_string_field(raw, 1) or not_yet_reason
                    continue
                if field == 2:  # approved
                    approved_lesson = self._parse_string_field(raw, 1) or approved_lesson
                    continue
                continue
            p = self._skip_field(data, p, wt)

        if approved_lesson is not None:
            return True, None, approved_lesson
        if not_yet_reason is not None:
            return False, not_yet_reason, None
        return None, None, None

    def _parse_category(self, data: bytes) -> _CategoryDef | None:
        cid: str | None = None
        title: str | None = None
        max_value: float | None = None

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7

            if field in (1, 2, 3) and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                try:
                    s = raw.decode("utf-8", errors="ignore")
                except Exception:
                    s = ""
                if field == 1:
                    cid = s.strip() or cid
                elif field == 2:
                    title = s.strip() or title
                # field 3 is description - ignore
                continue

            if field == 4 and wt == 1 and p + 8 <= len(data):
                max_value = float(struct.unpack("<d", data[p : p + 8])[0])
                p += 8
                continue

            p = self._skip_field(data, p, wt)

        if not cid:
            return None
        return _CategoryDef(id=cid, title=title, max_value=max_value)

    def _parse_category_group(self, data: bytes, categories: dict[str, _CategoryDef]) -> None:
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                cat = self._parse_category(raw)
                if cat:
                    categories[cat.id] = cat
                continue
            p = self._skip_field(data, p, wt)

    def _parse_discipline_info(self, data: bytes) -> tuple[str | None, str | None]:
        title: str | None = None
        discipline_id: str | None = None
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field in (1, 2) and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                try:
                    value = raw.decode("utf-8", errors="ignore").strip()
                    if not value:
                        continue
                    if field == 1:
                        title = value
                    elif field == 2:
                        discipline_id = value
                except Exception:
                    pass
                continue
            p = self._skip_field(data, p, wt)
        return title, discipline_id

    def _parse_component(self, data: bytes) -> tuple[str | None, float | None, float | None]:
        cat_id: str | None = None
        score: float | None = None
        caps: list[float] = []
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                try:
                    cat_id = raw.decode("utf-8", errors="ignore").strip() or cat_id
                except Exception:
                    pass
                continue
            if field == 2 and wt == 1 and p + 8 <= len(data):
                score = float(struct.unpack("<d", data[p : p + 8])[0])
                p += 8
                continue
            if wt == 1 and p + 8 <= len(data):
                val = float(struct.unpack("<d", data[p : p + 8])[0])
                # Some API versions provide component max/cap in additional double fields.
                if field != 2:
                    caps.append(val)
                p += 8
                continue
            p = self._skip_field(data, p, wt)
        cap: float | None = None
        if caps:
            valid = [x for x in caps if x > 0]
            if valid:
                cap = max(valid)
        return cat_id, score, cap

    def _parse_discipline(self, data: bytes) -> _ParsedDiscipline | None:
        name: str | None = None
        discipline_id: str | None = None
        total: float | None = None
        components: dict[str, float] = {}
        component_caps: dict[str, float] = {}

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                parsed_name, parsed_id = self._parse_discipline_info(raw)
                name = parsed_name or name
                discipline_id = parsed_id or discipline_id
                continue

            if field == 2 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                cat_id, score, cap = self._parse_component(raw)
                if cat_id:
                    components[cat_id] = float(score) if score is not None else 0.0
                    if cap is not None:
                        component_caps[cat_id] = float(cap)
                continue

            if field == 3 and wt == 1 and p + 8 <= len(data):
                total = float(struct.unpack("<d", data[p : p + 8])[0])
                p += 8
                continue

            p = self._skip_field(data, p, wt)

        if not name:
            return None
        return _ParsedDiscipline(
            name=name,
            discipline_id=discipline_id,
            components=components,
            component_caps=component_caps,
            total=total,
        )

    def _parse_report(self, data: bytes) -> list[Subject]:
        categories: dict[str, _CategoryDef] = {}
        disciplines: list[_ParsedDiscipline] = []

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                disc = self._parse_discipline(raw)
                if disc:
                    disciplines.append(disc)
                continue
            if field == 2 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                self._parse_category_group(raw, categories)
                continue
            p = self._skip_field(data, p, wt)

        subjects: list[Subject] = []
        for d in disciplines:
            subj = Subject(name=d.name, discipline_id=d.discipline_id)
            for cat_id, score in d.components.items():
                cap = d.component_caps.get(cat_id)
                if cap is None:
                    cap = categories.get(cat_id).max_value if categories.get(cat_id) else None
                if cat_id == self._CAT_CURRENT:
                    subj.current_control = float(score)
                elif cat_id == self._CAT_SEMESTER:
                    subj.semester_control = float(score)
                elif cat_id == self._CAT_ATTENDANCE:
                    subj.attendance = float(score)
                elif cat_id == self._CAT_ACHIEVEMENTS:
                    subj.achievements = float(score)
                elif cat_id == self._CAT_ADDITIONAL:
                    subj.additional = float(score)
                else:
                    # Fallback: map by title if UUIDs changed.
                    title = (categories.get(cat_id).title if categories.get(cat_id) else "") or ""
                    tl = title.lower()
                    if "текущ" in tl:
                        subj.current_control = float(score)
                    elif "семестр" in tl:
                        subj.semester_control = float(score)
                    elif "посещ" in tl:
                        subj.attendance = float(score)
                    elif "достижен" in tl:
                        subj.achievements = float(score)
                    elif "дополн" in tl:
                        subj.additional = float(score)

            total = d.total
            if total is None:
                total = (
                    subj.current_control
                    + subj.semester_control
                    + subj.attendance
                    + subj.achievements
                    + subj.additional
                )
            subj.total = float(total or 0.0)
            subjects.append(subj)

        return subjects

    def _parse_attendance_primary_info_stats(self, data: bytes) -> dict[int, int]:
        """
        Parse AttendanceService.GetStudentAttendancesPrimaryInfoOfDiscipline response.

        Возвращает varint-поля вложенного блока field #2 как словарь {field_no: value}.
        """
        stats: dict[int, int] = {}

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                p2 = 0
                while p2 < len(raw):
                    key2, p2 = self._decode_varint(raw, p2)
                    field2 = key2 >> 3
                    wt2 = key2 & 0x7
                    if wt2 == 0:
                        value, p2 = self._decode_varint(raw, p2)
                        stats[int(field2)] = int(value)
                        continue
                    p2 = self._skip_field(raw, p2, wt2)
                continue
            p = self._skip_field(data, p, wt)

        return stats

    def _estimate_attendance_cap_from_primary_stats(
        self,
        stats: dict[int, int],
        current_attendance: float,
    ) -> float | None:
        """
        Estimate ideal attendance cap from aggregated counters.
        """
        total_count = max(int(stats.get(1, 0)), 0)
        present_attendances = max(int(stats.get(2, 0)), 0)
        excused_absences = max(int(stats.get(3, 0)), 0)
        if total_count <= 0:
            return None

        current = max(float(current_attendance), 0.0)
        expected_from_total = (30.0 * min(present_attendances + excused_absences, total_count)) / float(total_count)
        # If API denominator matches current attendance closely, `total_count` is likely full-plan size.
        total_looks_planned = abs(expected_from_total - current) <= 1.5

        absent_count: int | None = None
        if 4 in stats:
            absent_count = max(0, min(total_count, int(stats[4])))
        else:
            # Fallback heuristic for API variations: look for a plausible "absent" counter.
            candidates: list[int] = []
            for field_no, raw_value in stats.items():
                if field_no in (1, 2, 3):
                    continue
                value = int(raw_value)
                if value < 0 or value > total_count:
                    continue
                if present_attendances + excused_absences + value <= total_count:
                    candidates.append(value)
            if candidates:
                absent_count = max(candidates)

        if absent_count is not None:
            cap = (30.0 * max(total_count - absent_count, 0)) / float(total_count)
            return round(cap, 2)

        # Без явного счетчика пропусков не делаем оптимистичный прогноз.
        # Иначе получаем ложные 30/30 на части дисциплин.
        _ = total_looks_planned
        _ = current

        return None

    def _extract_attendance_type(self, data: bytes, *, max_depth: int = 4) -> int | None:
        """
        Best-effort extraction of ATTEND_TYPE enum value from nested protobuf blobs.
        Expected values: 0 unknown, 1 absent, 2 excused_absence, 3 present.
        """
        def _walk(buf: bytes, depth: int) -> int | None:
            if depth > max_depth:
                return None
            p = 0
            while p < len(buf):
                key, p = self._decode_varint(buf, p)
                field = key >> 3
                wt = key & 0x7
                if wt == 0:
                    value, p = self._decode_varint(buf, p)
                    iv = int(value)
                    if field in (2, 3, 4) and iv in (0, 1, 2, 3):
                        return iv
                    continue
                if wt == 2:
                    raw, p = self._read_length_delimited(buf, p)
                    found = _walk(raw, depth + 1)
                    if found is not None:
                        return found
                    continue
                p = self._skip_field(buf, p, wt)
            return None

        return _walk(data, 0)

    def _collect_small_enums(self, data: bytes, *, max_depth: int = 6) -> dict[tuple[int, ...], int]:
        """
        Collect varint fields with small enum-like values (0..3) from nested protobuf message.
        Returns map: path(tuple of field numbers) -> value.
        """
        found: dict[tuple[int, ...], int] = {}

        def _walk(buf: bytes, depth: int, path: tuple[int, ...]) -> None:
            if depth > max_depth:
                return
            p = 0
            while p < len(buf):
                key, p = self._decode_varint(buf, p)
                field = key >> 3
                wt = key & 0x7
                cur_path = path + (int(field),)
                if wt == 0:
                    value, p = self._decode_varint(buf, p)
                    iv = int(value)
                    if 0 <= iv <= 3:
                        found[cur_path] = iv
                    continue
                if wt == 2:
                    raw, p = self._read_length_delimited(buf, p)
                    _walk(raw, depth + 1, cur_path)
                    continue
                p = self._skip_field(buf, p, wt)

        _walk(data, 0, tuple())
        return found

    def _pick_attendance_enum_path(self, per_entry: list[dict[tuple[int, ...], int]]) -> tuple[int, ...] | None:
        """
        Choose the most likely protobuf path that represents ATTEND_TYPE enum.
        """
        if not per_entry:
            return None
        total = len(per_entry)
        stats: dict[tuple[int, ...], dict[str, object]] = {}

        for entry in per_entry:
            for path, value in entry.items():
                s = stats.setdefault(path, {"entries": 0, "values": {0: 0, 1: 0, 2: 0, 3: 0}})
                s["entries"] = int(s["entries"]) + 1
                values = s["values"]
                values[int(value)] = int(values.get(int(value), 0)) + 1

        candidates: list[tuple[tuple[int, ...], int, int, int]] = []
        # tuple: (path, entries, diversity, score_for_absence_signal)
        min_entries = max(2, int(total * 0.6))
        for path, s in stats.items():
            entries = int(s["entries"])
            if entries < min_entries:
                continue
            values = s["values"]
            diversity = sum(1 for v in values.values() if int(v) > 0)
            if diversity == 0:
                continue
            absent_signal = int(values.get(1, 0))
            candidates.append((path, entries, diversity, absent_signal))

        if not candidates:
            return None

        # Prefer paths present in most entries, then with richer value diversity,
        # then with explicit absences, then shorter path.
        candidates.sort(key=lambda x: (x[1], x[2], x[3], -len(x[0])), reverse=True)
        return candidates[0][0]

    def _parse_timestamp_epoch(self, data: bytes) -> float | None:
        """
        Parse protobuf Timestamp (google.protobuf.Timestamp):
        field 1 = seconds, field 2 = nanos.
        """
        seconds: int | None = None
        nanos = 0

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 0:
                value, p = self._decode_varint(data, p)
                seconds = int(value)
                continue
            if field == 2 and wt == 0:
                value, p = self._decode_varint(data, p)
                nanos = int(value)
                continue
            p = self._skip_field(data, p, wt)

        if seconds is None:
            return None
        return float(seconds) + float(nanos) / 1_000_000_000.0

    def _parse_lesson_start_epoch(self, data: bytes) -> float | None:
        """
        Parse lesson message (`Zv`) and extract `start` timestamp.
        In Pulse schema: field 2 = start Timestamp.
        """
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 2:
                ts_raw, p = self._read_length_delimited(data, p)
                return self._parse_timestamp_epoch(ts_raw)
            p = self._skip_field(data, p, wt)
        return None

    def _parse_attend_type(self, data: bytes) -> int | None:
        """
        Parse attendance message (`No`) and extract `attendType`.
        In Pulse schema: field 2 = attendType enum.
        """
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 0:
                value, p = self._decode_varint(data, p)
                return int(value)
            p = self._skip_field(data, p, wt)
        return None

    def _parse_detailed_attendance_entries(self, data: bytes) -> list[tuple[int | None, float | None]]:
        """
        Parse `vNe` response:
          field 1 = attendancesInfo[] (`Wv`)
          `Wv` field 1 = attendance (`No`), field 2 = lesson (`Zv`)
        Returns list of tuples: (attend_type, lesson_start_epoch)
        """
        entries: list[tuple[int | None, float | None]] = []
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                entry_raw, p = self._read_length_delimited(data, p)
                attend_type: int | None = None
                lesson_start: float | None = None

                p2 = 0
                while p2 < len(entry_raw):
                    key2, p2 = self._decode_varint(entry_raw, p2)
                    field2 = key2 >> 3
                    wt2 = key2 & 0x7
                    if field2 == 1 and wt2 == 2:
                        attendance_raw, p2 = self._read_length_delimited(entry_raw, p2)
                        attend_type = self._parse_attend_type(attendance_raw)
                        continue
                    if field2 == 2 and wt2 == 2:
                        lesson_raw, p2 = self._read_length_delimited(entry_raw, p2)
                        lesson_start = self._parse_lesson_start_epoch(lesson_raw)
                        continue
                    p2 = self._skip_field(entry_raw, p2, wt2)

                entries.append((attend_type, lesson_start))
                continue
            p = self._skip_field(data, p, wt)
        return entries

    def _estimate_attendance_cap_from_detailed_response(
        self,
        data: bytes,
        current_attendance: float,
    ) -> float | None:
        """
        Compute achievable attendance cap (0..30) from per-lesson attendance.

        Logic:
        - explicit ABSENT (attendType=1) in past lessons are irreversible misses
        - future lessons are treated as potentially attended
        - cap = 30 * (total_lessons - missed_past) / total_lessons
        """
        entries = self._parse_detailed_attendance_entries(data)
        return self._estimate_attendance_cap_from_entries(
            entries,
            current_attendance=float(current_attendance),
            now_epoch=time.time(),
        )

    def _estimate_attendance_cap_from_entries(
        self,
        entries: list[tuple[int | None, float | None]],
        current_attendance: float,
        now_epoch: float | None = None,
    ) -> float | None:
        """
        Compute achievable attendance cap (0..30) from per-lesson attendance entries.

        Logic:
        - explicit ABSENT (attendType=1) in past lessons are irreversible misses
        - future lessons are treated as potentially attended
        - cap = 30 * (total_lessons - missed_past) / total_lessons
        """
        total_lessons = len(entries)
        if total_lessons <= 0:
            return None

        now_epoch = float(now_epoch if now_epoch is not None else time.time())
        missed_past = 0
        recognized = 0

        for attend_type, lesson_start in entries:
            if attend_type is None:
                continue
            recognized += 1
            if attend_type != 1:
                continue
            # Count only missed lessons that have already started.
            if lesson_start is None:
                continue
            if lesson_start <= now_epoch + 120.0:
                missed_past += 1

        # If parser returned almost no typed entries, this payload is likely not the expected one.
        if recognized < max(1, int(total_lessons * 0.2)):
            return None

        cap_est = 30.0 * float(max(total_lessons - missed_past, 0)) / float(total_lessons)
        cap_est = max(float(current_attendance), cap_est)
        return round(min(30.0, cap_est), 2)

    async def _estimate_attendance_cap_from_core(
        self,
        entries: list[tuple[int | None, float | None]],
        current_attendance: float,
        *,
        now_epoch: float,
    ) -> float | None:
        """
        Optional Rust acceleration path. Returns None if core is disabled/unavailable.
        """
        if not (bool(settings.attendance_core_enabled) or bool(settings.attendance_core_shadow)):
            return None

        bin_path = Path(str(settings.attendance_core_bin)).expanduser()
        if not bin_path.exists():
            return None

        payload = {
            "entries": [
                {
                    "attend_type": int(attend_type) if attend_type is not None else None,
                    "lesson_start_epoch": float(lesson_start) if lesson_start is not None else None,
                }
                for attend_type, lesson_start in entries
            ],
            "current_attendance": float(current_attendance),
            "now_epoch": float(now_epoch),
            "missing_attend_type": 1,
            "future_skew_seconds": 120.0,
            "max_points": 30.0,
        }

        timeout_s = max(0.2, float(settings.attendance_core_timeout_s))
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(bin_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(stdin_payload), timeout=timeout_s)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning("attendance_core timeout (%.2fs)", timeout_s)
                return None

            if process.returncode != 0:
                err_text = (stderr or b"").decode("utf-8", errors="replace").strip()
                logger.warning("attendance_core exited with code=%s err=%s", process.returncode, err_text[:200])
                return None

            response = json.loads((stdout or b"{}").decode("utf-8", errors="replace"))
            if not isinstance(response, dict) or not bool(response.get("ok")):
                return None
            cap = response.get("cap")
            if cap is None:
                return None
            cap_value = float(cap)
            cap_value = min(30.0, max(float(current_attendance), cap_value))
            return round(cap_value, 2)
        except Exception:
            logger.exception("attendance_core execution failed")
            return None

    def _parse_attendance_log_totals(self, data: bytes) -> tuple[int, int, int]:
        """
        Parse AttendanceService.GetStudentAttendancesOfDisciplineInVisitingLog response.
        Returns total entries, explicit absences and number of entries with recognized enum.
        """
        total = 0
        absent = 0
        recognized = 0
        per_entry_candidates: list[dict[tuple[int, ...], int]] = []

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                info_raw, p = self._read_length_delimited(data, p)
                total += 1
                candidates = self._collect_small_enums(info_raw)
                if candidates:
                    per_entry_candidates.append(candidates)
                continue
            p = self._skip_field(data, p, wt)

        enum_path = self._pick_attendance_enum_path(per_entry_candidates)
        if enum_path is None:
            return total, 0, 0

        for entry in per_entry_candidates:
            if enum_path not in entry:
                continue
            recognized += 1
            if int(entry[enum_path]) == 1:
                absent += 1

        return total, absent, recognized

    async def _fetch_attendance_max_possible(
        self,
        student_id: str,
        discipline_id: str,
        visiting_log_id: str,
        current_attendance: float,
    ) -> float | None:
        req = self._encode_student_discipline_visiting_log_request(
            student_id=student_id,
            discipline_id=discipline_id,
            visiting_log_id=visiting_log_id,
        )

        cap: float | None = None

        # Prefer per-lesson attendance stream and calibrate enum path by current attendance.
        payload, err = await self._grpc_unary(self.ATTENDANCE_DISCIPLINE_INFO_URL, req)
        if not err and payload:
            for candidate_payload in (payload, self._unwrap_field_1(payload)):
                if not candidate_payload:
                    continue
                entries = self._parse_detailed_attendance_entries(candidate_payload)
                now_epoch = time.time()
                py_est = self._estimate_attendance_cap_from_entries(
                    entries,
                    current_attendance=float(current_attendance),
                    now_epoch=now_epoch,
                )
                if py_est is not None:
                    cap_candidate = py_est
                    rust_est = await self._estimate_attendance_cap_from_core(
                        entries,
                        current_attendance=float(current_attendance),
                        now_epoch=now_epoch,
                    )
                    if rust_est is not None:
                        delta = abs(float(rust_est) - float(py_est))
                        if bool(settings.attendance_core_enabled) and not bool(settings.attendance_core_shadow):
                            cap_candidate = rust_est
                            logger.info(
                                "attendance_core active: cap_py=%.2f cap_rust=%.2f delta=%.2f",
                                float(py_est), float(rust_est), float(delta)
                            )
                        else:
                            logger.info(
                                "attendance_core shadow: cap_py=%.2f cap_rust=%.2f delta=%.2f",
                                float(py_est), float(rust_est), float(delta)
                            )
                    cap = cap_candidate
                    break

        if cap is None:
            return None

        # Safety bounds against parser/API drift.
        cap = min(30.0, max(float(current_attendance), cap))
        return round(cap, 2)

    async def _enrich_attendance_caps(
        self,
        subjects: list[Subject],
        visiting_log_id: str,
    ) -> None:
        student_id = (self.session_cookies.get(self._student_id_cache_key) or "").strip()
        if not student_id or not self._UUID_RE.match(student_id):
            return
        if not visiting_log_id or not self._UUID_RE.match(visiting_log_id):
            return

        semaphore = asyncio.Semaphore(8)

        async def _enrich_subject(subject: Subject) -> None:
            discipline_id = (subject.discipline_id or "").strip()
            if not discipline_id or not self._UUID_RE.match(discipline_id):
                return
            async with semaphore:
                cap = await self._fetch_attendance_max_possible(
                    student_id=student_id,
                    discipline_id=discipline_id,
                    visiting_log_id=visiting_log_id,
                    current_attendance=subject.attendance,
                )
            if cap is not None:
                subject.attendance_max_possible = cap

        await asyncio.gather(*(_enrich_subject(s) for s in subjects))

    async def _fetch_grades_for_log(self, visiting_log_id: str) -> tuple[list[Subject] | None, str | None]:
        payload, err = await self._grpc_unary(self.GRPC_URL, self._encode_grades_request(visiting_log_id))
        if err:
            return None, err
        if not payload:
            return None, "Пустой ответ"
        try:
            # gRPC response is wrapped: field 1 contains the actual report message.
            report = self._unwrap_field_1(payload) or payload
            subjects = self._parse_report(report)
            return subjects, None
        except Exception as e:
            logger.debug(f"Failed to parse BRS report: {e}", exc_info=True)
            return None, "Не удалось распарсить ответ сервера"

    def _unwrap_field_1(self, data: bytes) -> bytes | None:
        """
        Many Pulse gRPC responses wrap the actual message into the outer response message:
        field 1 (length-delimited) contains the real payload.
        """
        try:
            p = 0
            while p < len(data):
                key, p = self._decode_varint(data, p)
                field = key >> 3
                wt = key & 0x7
                if field == 1 and wt == 2:
                    raw, p = self._read_length_delimited(data, p)
                    return raw
                p = self._skip_field(data, p, wt)
            return None
        except Exception:
            return None

    async def get_grades(self, visiting_log_id: str | None = None) -> GradesResult:
        """
        Получить оценки.

        Если visiting_log_id не указан, берём кешированный, иначе выбираем из списка журналов
        тот, на котором действительно возвращаются данные.
        """
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return GradesResult(
                success=False,
                message=err
                or "БРС недоступен: не удалось получить cookie (.AspNetCore.Cookies). Перелогиньтесь.",
            )

        cached = None
        if not visiting_log_id:
            cached = (self.session_cookies.get(self._cache_key) or "").strip() or None

        candidates: list[str] = []
        if visiting_log_id:
            candidates = [visiting_log_id]
        elif cached:
            candidates = [cached]

        logs: list[str] = []
        if not candidates:
            logs, err = await self._get_visiting_logs()
            if err or not logs:
                return GradesResult(success=False, message=err or "Не удалось получить список журналов")
            candidates = logs
        else:
            # Expand candidates with logs list to auto-recover from stale cached ID.
            logs, _ = await self._get_visiting_logs()
            if logs:
                tail = [x for x in logs if x not in candidates]
                candidates.extend(tail)

        last_err = "Не удалось получить оценки"
        for cand in candidates:
            subjects, err = await self._fetch_grades_for_log(cand)
            if subjects:
                if not visiting_log_id:
                    # Cache the working visiting log id in the session (internal key).
                    self.session_cookies[self._cache_key] = cand
                    self.session_cookies["__brs_visiting_log_cached_at"] = int(time.time())
                await self._enrich_attendance_caps(subjects, cand)
                return GradesResult(success=True, message="Оценки получены", subjects=subjects, semester=cand)
            if err:
                last_err = err

        return GradesResult(success=False, message=last_err)

    async def get_attendance_detail(
        self,
        discipline_id: str,
        visiting_log_id: str | None = None,
    ) -> AttendanceDetailResult:
        """Fetch per-lesson attendance for one discipline."""
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return AttendanceDetailResult(success=False, message=err or "Сессия истекла.")

        # Resolve visiting_log_id if not provided
        if not visiting_log_id:
            cached = (self.session_cookies.get(self._cache_key) or "").strip() or None
            if cached:
                visiting_log_id = cached
            else:
                logs, log_err = await self._get_visiting_logs()
                if not logs:
                    return AttendanceDetailResult(success=False, message=log_err or "Нет журналов")
                visiting_log_id = logs[0]

        # Resolve student_id
        student_id = (self.session_cookies.get(self._student_id_cache_key) or "").strip()
        if not student_id or not self._UUID_RE.match(student_id):
            logs, log_err = await self._get_visiting_logs()
            student_id = (self.session_cookies.get(self._student_id_cache_key) or "").strip()
            if not student_id or not self._UUID_RE.match(student_id):
                return AttendanceDetailResult(success=False, message="Не удалось получить student_id")

        req = self._encode_student_discipline_visiting_log_request(
            student_id=student_id,
            discipline_id=discipline_id,
            visiting_log_id=visiting_log_id,
        )

        # Fetch summary stats
        summary = AttendanceSummary()
        primary_payload, primary_err = await self._grpc_unary(self.ATTENDANCE_PRIMARY_INFO_URL, req)
        if not primary_err and primary_payload:
            for candidate in (primary_payload, self._unwrap_field_1(primary_payload)):
                if not candidate:
                    continue
                stats = self._parse_attendance_primary_info_stats(candidate)
                if stats:
                    summary.total_lessons = max(int(stats.get(1, 0)), 0)
                    summary.present = max(int(stats.get(2, 0)), 0)
                    summary.excused = max(int(stats.get(3, 0)), 0)
                    summary.absent = max(int(stats.get(4, 0)), 0)
                    break

        # Fetch per-lesson entries
        entries_list: list[AttendanceEntry] = []
        detail_payload, detail_err = await self._grpc_unary(self.ATTENDANCE_DISCIPLINE_INFO_URL, req)
        if not detail_err and detail_payload:
            for candidate in (detail_payload, self._unwrap_field_1(detail_payload)):
                if not candidate:
                    continue
                raw_entries = self._parse_detailed_attendance_entries(candidate)
                if raw_entries:
                    entries_list = [
                        AttendanceEntry(lesson_start=ls, attend_type=at)
                        for at, ls in raw_entries
                    ]
                    break

        if not entries_list and not summary.total_lessons:
            return AttendanceDetailResult(
                success=False,
                message=detail_err or primary_err or "Нет данных о посещаемости",
            )

        return AttendanceDetailResult(
            success=True,
            message="OK",
            summary=summary,
            entries=entries_list,
        )

    # --- Schedule (LessonService) ---

    def _encode_date_request(self, year: int, month: int, day: int) -> bytes:
        """Encode a date request for GetAvailableLessonsOfVisitingLogs.

        Wire format: field 2 (length-delimited) containing sub-message
        with field 1=year, field 2=month, field 3=day (all varints).
        """
        date_msg = (
            b"\x08" + self._encode_varint(year)
            + b"\x10" + self._encode_varint(month)
            + b"\x18" + self._encode_varint(day)
        )
        return b"\x12" + self._encode_varint(len(date_msg)) + date_msg

    def _parse_lessons_response(self, data: bytes) -> list[ScheduleLesson]:
        """Parse GetAvailableLessonsOfVisitingLogs response.

        Outer message: repeated field 1 = Lesson sub-messages.
        Each Lesson:
          field 2: start timestamp (sub-message with epoch-like encoding)
          field 4: discipline {field 1: uuid, field 2: name}
          field 5: lesson type {field 1: uuid, field 2: name}
          field 6: room {field 1: uuid, field 2: name}
          field 7: teacher {field 1: uuid, field 2: firstName, field 3: lastName, field 4: patronymic}
          field 8: end timestamp
          field 10: subgroup string
        """
        lessons: list[ScheduleLesson] = []
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                outer_raw, p = self._read_length_delimited(data, p)
                # Response wraps each lesson: outer field 2 → inner field 3 = actual lesson
                # Try to extract field 3 sub-message from the outer wrapper
                lesson_raw = self._extract_submessage_field(outer_raw, target_field=3)
                if lesson_raw is None:
                    # Fallback: try parsing outer_raw directly as a lesson
                    lesson_raw = outer_raw
                lesson = self._parse_single_lesson(lesson_raw)
                if lesson:
                    lessons.append(lesson)
            else:
                p = self._skip_field(data, p, wt)
        return lessons

    def _extract_submessage_field(self, data: bytes, target_field: int) -> bytes | None:
        """Extract a specific length-delimited field from a protobuf message."""
        p = 0
        while p < len(data):
            key, p2 = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p2 = self._read_length_delimited(data, p2)
                if field == target_field:
                    return raw
            elif wt == 0:
                _, p2 = self._decode_varint(data, p2)
            else:
                p2 = self._skip_field(data, p2, wt)
            p = p2
        return None

    def _parse_single_lesson(self, data: bytes) -> ScheduleLesson | None:
        name = ""
        lesson_type = ""
        room = ""
        teacher = ""
        subgroup = ""
        start_epoch: float | None = None
        end_epoch: float | None = None

        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(data, p)
                if field == 2:
                    start_epoch = self._parse_timestamp_msg(raw)
                elif field == 3:
                    end_epoch = self._parse_timestamp_msg(raw)
                elif field == 4:
                    name = self._parse_string_field(raw, 2) or ""
                elif field == 5:
                    lesson_type = self._parse_string_field(raw, 2) or ""
                elif field == 6:
                    room = self._parse_string_field(raw, 2) or ""
                elif field == 7:
                    teacher = self._parse_teacher_name(raw)
                elif field == 8:
                    # Could also be end timestamp in some responses
                    if not end_epoch:
                        end_epoch = self._parse_timestamp_msg(raw)
                elif field == 10:
                    try:
                        subgroup = raw.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        pass
            else:
                p = self._skip_field(data, p, wt)

        if not name:
            return None
        return ScheduleLesson(
            name=name,
            lesson_type=lesson_type,
            room=room,
            teacher=teacher,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            subgroup=subgroup,
        )

    def _parse_teacher_name(self, data: bytes) -> str:
        first = self._parse_string_field(data, 2) or ""
        last = self._parse_string_field(data, 3) or ""
        patronymic = self._parse_string_field(data, 4) or ""
        parts = [p for p in (last, first, patronymic) if p]
        return " ".join(parts)

    def _parse_timestamp_msg(self, data: bytes) -> float | None:
        """Parse a Timestamp sub-message.

        Tries two strategies:
        1. Google-style: field 1 = seconds (varint), field 2 = nanos (varint)
        2. Fallback: look for any varint that looks like a unix epoch (> 1e9)
        """
        seconds: int | None = None
        nanos: int = 0
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 0:
                val, p = self._decode_varint(data, p)
                if field == 1:
                    seconds = val
                elif field == 2:
                    nanos = val
            else:
                p = self._skip_field(data, p, wt)
        if seconds is not None and seconds > 1_000_000_000:
            return float(seconds) + float(nanos) / 1e9
        return None

    async def get_schedule(self, days: int = 7) -> ScheduleResult:
        """Fetch schedule for the next N days via Pulse LessonService."""
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return ScheduleResult(success=False, message=err or "Auth failed")

        all_lessons: list[ScheduleLesson] = []
        now = datetime.now()

        sem = asyncio.Semaphore(5)

        async def _fetch_day(offset: int) -> list[ScheduleLesson]:
            dt = now + timedelta(days=offset)
            payload = self._encode_date_request(dt.year, dt.month, dt.day)
            async with sem:
                raw, grpc_err = await self._grpc_unary(self.LESSONS_URL, payload)
            if grpc_err or not raw:
                logger.debug(f"Schedule fetch for {dt.date()}: {grpc_err}")
                return []
            return self._parse_lessons_response(raw)

        results = await asyncio.gather(*(_fetch_day(d) for d in range(days)))
        for day_lessons in results:
            all_lessons.extend(day_lessons)

        if not all_lessons:
            return ScheduleResult(success=False, message="Нет пар в ближайшие дни")

        all_lessons.sort(key=lambda l: l.start_epoch or 0)
        return ScheduleResult(success=True, message="OK", lessons=all_lessons)

    async def close(self) -> None:
        await self.client.aclose()

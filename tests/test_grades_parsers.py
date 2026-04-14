"""Tests for pymirea.grades parsers (encoders + response decoders).

These are unit tests on pure functions — no HTTP. We build protobuf-shaped
byte fixtures using the library's own encoders and feed them back through
the parsers to assert round-trip parity."""

from __future__ import annotations

import base64
import secrets
import struct

import pytest

from pymirea import Config, configure
from pymirea.grades import MireaGrades


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


@pytest.fixture
def g() -> MireaGrades:
    _setup()
    return MireaGrades(session_cookies={".AspNetCore.Cookies": "x"})


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _double_field(field_no: int, value: float) -> bytes:
    tag = (field_no << 3) | 1  # wire type 1 = fixed64
    return bytes([tag]) + struct.pack("<d", value)


# ---------------------------------------------------------------------------
# _encode_selfapprove_request — wraps token as field 1 string
# ---------------------------------------------------------------------------


def test_encode_selfapprove_request(g):
    msg = g._encode_selfapprove_request("my-token")
    # field 1, wire-type 2, length=8, "my-token"
    assert msg == bytes([0x0a, 0x08]) + b"my-token"


def test_encode_grades_request(g):
    msg = g._encode_grades_request("log-uuid")
    # field 1 = "log-uuid"
    assert msg == bytes([0x0a, 0x08]) + b"log-uuid"


def test_encode_student_discipline_visiting_log_request(g):
    msg = g._encode_student_discipline_visiting_log_request(
        student_id="stu", discipline_id="disc", visiting_log_id="log"
    )
    # field 1 = "stu", field 2 = "disc", field 3 = "log"
    # tags: 0x0a, 0x12, 0x1a
    assert msg == bytes([0x0a, 0x03]) + b"stu" + bytes([0x12, 0x04]) + b"disc" + bytes([0x1a, 0x03]) + b"log"


# ---------------------------------------------------------------------------
# _parse_selfapprove_response — 3 shapes
# ---------------------------------------------------------------------------


def test_parse_selfapprove_bool_true(g):
    # { success: { value: true } } → field 1 length-delim with nested field 1 varint=1
    payload = bytes.fromhex("0a020801")
    approved, reason, lesson_id = g._parse_selfapprove_response(payload)
    assert approved is True
    assert reason is None


def test_parse_selfapprove_bool_false(g):
    payload = bytes.fromhex("0a020800")
    approved, reason, lesson_id = g._parse_selfapprove_response(payload)
    assert approved is False


def test_parse_selfapprove_approved_with_lesson_id(g):
    # { approved: { lesson_id: "L-42" } }
    # inner: 0a 04 'L' '-' '4' '2'
    # outer: 12 06 ...
    inner = bytes([0x0a, 0x04]) + b"L-42"
    outer = bytes([0x12, len(inner)]) + inner

    approved, reason, lesson_id = g._parse_selfapprove_response(outer)
    assert approved is True
    assert lesson_id == "L-42"


def test_parse_selfapprove_not_yet_with_reason(g):
    reason_text = "Пара ещё не началась"
    reason_bytes = reason_text.encode()
    inner = bytes([0x0a, len(reason_bytes)]) + reason_bytes
    outer = bytes([0x0a, len(inner)]) + inner

    approved, reason, lesson_id = g._parse_selfapprove_response(outer)
    assert approved is False
    assert reason == reason_text


def test_parse_selfapprove_empty_returns_none(g):
    approved, reason, lesson_id = g._parse_selfapprove_response(b"")
    assert approved is None
    assert reason is None
    assert lesson_id is None


# ---------------------------------------------------------------------------
# _parse_available_visiting_logs — 2 response shapes
# ---------------------------------------------------------------------------


VLOG_A = "abcdef12-3456-7890-abcd-ef1234567890"
VLOG_B = "11111111-2222-3333-4444-555555555555"
STUDENT_ID = "99999999-8888-7777-6666-555555555555"


def _log_entry_bytes(log_id: str, student_id: str) -> bytes:
    # visitingLogEntry: field 1 = baseLogInfo { field 1 = log_id }, field 4 = student_id
    base_info = bytes([0x0a, len(log_id)]) + log_id.encode()
    entry = bytes([0x0a, len(base_info)]) + base_info
    if student_id:
        sid_b = student_id.encode()
        entry += bytes([0x22, len(sid_b)]) + sid_b  # field 4
    return bytes([0x0a, len(entry)]) + entry  # outer field 1 wraps it


def test_parse_visiting_logs_extracts_log_and_student(g):
    payload = _log_entry_bytes(VLOG_A, STUDENT_ID)
    log_ids, student_id = g._parse_available_visiting_logs(payload)
    assert log_ids == [VLOG_A]
    assert student_id == STUDENT_ID


def test_parse_visiting_logs_deduplicates(g):
    payload = _log_entry_bytes(VLOG_A, STUDENT_ID) + _log_entry_bytes(VLOG_A, STUDENT_ID)
    log_ids, _ = g._parse_available_visiting_logs(payload)
    assert log_ids == [VLOG_A]


def test_parse_visiting_logs_rejects_non_uuid(g):
    payload = _log_entry_bytes("not-a-uuid", "also-not")
    log_ids, student_id = g._parse_available_visiting_logs(payload)
    assert log_ids == []
    assert student_id is None


def test_parse_visiting_logs_empty_returns_empty(g):
    log_ids, student_id = g._parse_available_visiting_logs(b"")
    assert log_ids == []
    assert student_id is None


# ---------------------------------------------------------------------------
# _parse_report — discipline with scored categories
# ---------------------------------------------------------------------------


CAT_CURRENT = "e8e1272c-63b2-5a17-b60d-4b7aac78169e"
CAT_SEMESTER = "b05e81e2-8c9b-5050-97d5-0d0c7c232468"
CAT_ATTENDANCE = "fc76ecc6-3ac7-5a50-9694-72e0bf301976"


def _str_field(field_no: int, value: str) -> bytes:
    b = value.encode()
    return bytes([(field_no << 3) | 2]) + _varint(len(b)) + b


def _wrap(field_no: int, payload: bytes) -> bytes:
    return bytes([(field_no << 3) | 2]) + _varint(len(payload)) + payload


def _discipline_bytes(name: str, disc_id: str, components: dict[str, float], total: float | None = None) -> bytes:
    info = _str_field(1, name) + _str_field(2, disc_id)
    out = _wrap(1, info)  # discipline.info

    for cat_id, score in components.items():
        comp = _str_field(1, cat_id) + _double_field(2, score)
        out += _wrap(2, comp)  # discipline.component

    if total is not None:
        out += _double_field(3, total)
    return out


def test_parse_report_maps_categories_to_subject_fields(g):
    disc = _discipline_bytes("Математика", "disc-uuid", {
        CAT_CURRENT: 35.0,
        CAT_SEMESTER: 28.0,
        CAT_ATTENDANCE: 20.0,
    })
    report = _wrap(1, disc)

    subjects = g._parse_report(report)
    assert len(subjects) == 1
    s = subjects[0]
    assert s.name == "Математика"
    assert s.discipline_id == "disc-uuid"
    assert s.current_control == pytest.approx(35.0)
    assert s.semester_control == pytest.approx(28.0)
    assert s.attendance == pytest.approx(20.0)
    # total computed by sum since field 3 absent
    assert s.total == pytest.approx(83.0)


def test_parse_report_uses_server_total(g):
    disc = _discipline_bytes("Физика", "", {CAT_CURRENT: 10.0}, total=95.5)
    report = _wrap(1, disc)
    subjects = g._parse_report(report)
    assert subjects[0].total == pytest.approx(95.5)


def test_parse_report_skips_nameless_discipline(g):
    # Empty discipline message (no name field)
    report = _wrap(1, b"")
    assert g._parse_report(report) == []


def test_parse_report_empty_input(g):
    assert g._parse_report(b"") == []


def test_parse_report_category_title_fallback(g):
    # Unknown UUID but category dict maps it to "Текущий контроль"
    new_uuid = "00000000-0000-0000-0000-000000000001"

    # category group: field 2 = category{ field 1 = uuid, field 2 = title }
    cat_entry = _str_field(1, new_uuid) + _str_field(2, "Текущий контроль")
    cat_group = _wrap(2, cat_entry)

    disc = _discipline_bytes("Информатика", "", {new_uuid: 30.0})
    report = _wrap(1, disc) + _wrap(2, cat_group)

    subjects = g._parse_report(report)
    assert len(subjects) == 1
    assert subjects[0].current_control == pytest.approx(30.0)

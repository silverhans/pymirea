"""Tests for pymirea.acs parsers (zone classification + timestamp + event).

ACS response parsing is heuristic — this test suite locks down the current
behaviour so future edits don't regress zone recognition."""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timezone

import pytest

from pymirea import Config, configure
from pymirea.acs import MireaACS


def _setup() -> None:
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


@pytest.fixture
def acs() -> MireaACS:
    _setup()
    return MireaACS(session_cookies={})


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _field_varint(field_no: int, value: int) -> bytes:
    return bytes([(field_no << 3) | 0]) + _varint(value)


def _field_bytes(field_no: int, payload: bytes) -> bytes:
    return bytes([(field_no << 3) | 2]) + _varint(len(payload)) + payload


# ---------------------------------------------------------------------------
# _parse_timestamp_message — google.protobuf.Timestamp-like msg
# ---------------------------------------------------------------------------


def test_parse_timestamp_seconds_only(acs):
    now = int(datetime.now(timezone.utc).timestamp())
    msg = _field_varint(1, now)
    assert acs._parse_timestamp_message(msg) == pytest.approx(now, abs=1)


def test_parse_timestamp_with_nanos(acs):
    msg = _field_varint(1, 1_700_000_000) + _field_varint(2, 500_000_000)
    assert acs._parse_timestamp_message(msg) == pytest.approx(1_700_000_000.5, abs=0.001)


def test_parse_timestamp_rejects_implausible_past(acs):
    msg = _field_varint(1, 100_000_000)  # ~1973
    assert acs._parse_timestamp_message(msg) is None


def test_parse_timestamp_rejects_implausible_future(acs):
    msg = _field_varint(1, 3_000_000_000)  # ~2065, above the plausible cap
    assert acs._parse_timestamp_message(msg) is None


def test_parse_timestamp_missing_seconds_returns_none(acs):
    # Only nanos, no seconds
    msg = _field_varint(2, 100)
    assert acs._parse_timestamp_message(msg) is None


def test_parse_timestamp_empty_returns_none(acs):
    assert acs._parse_timestamp_message(b"") is None


# ---------------------------------------------------------------------------
# _is_technical_token
# ---------------------------------------------------------------------------


def test_is_technical_token_uuid_full():
    assert MireaACS._is_technical_token("abcdef12-3456-7890-abcd-ef1234567890") is True


def test_is_technical_token_uuid_embedded():
    assert MireaACS._is_technical_token("prefix-abcdef12-3456-7890-abcd-ef1234567890-suffix") is True


def test_is_technical_token_long_hex():
    assert MireaACS._is_technical_token("deadbeefcafebabe1234567890abcdef") is True


def test_is_technical_token_real_zone_name():
    assert MireaACS._is_technical_token("КПП Бирюзова") is False


def test_is_technical_token_building_code():
    assert MireaACS._is_technical_token("Корпус 78") is False


def test_is_technical_token_empty():
    assert MireaACS._is_technical_token("") is True
    assert MireaACS._is_technical_token("   ") is True


# ---------------------------------------------------------------------------
# _looks_text
# ---------------------------------------------------------------------------


def test_looks_text_accepts_russian_zone_name():
    assert MireaACS._looks_text("КПП Бирюзова".encode()) == "КПП Бирюзова"


def test_looks_text_rejects_url():
    assert MireaACS._looks_text(b"https://pulse.mirea.ru/x") is None


def test_looks_text_rejects_uuid():
    assert MireaACS._looks_text(b"abcdef12-3456-7890-abcd-ef1234567890") is None


def test_looks_text_rejects_binary_garbage():
    assert MireaACS._looks_text(b"\x00\x01\x02\x03") is None


def test_looks_text_rejects_too_long():
    assert MireaACS._looks_text(b"a" * 200) is None


# ---------------------------------------------------------------------------
# _zone_score
# ---------------------------------------------------------------------------


def test_zone_score_real_zone_higher_than_tech():
    real = MireaACS._zone_score("КПП Корпус 78")
    tech = MireaACS._zone_score("abcdef12-3456-7890-abcd-ef1234567890")
    assert real > tech
    assert real >= 5


def test_zone_score_keyword_bonus():
    assert MireaACS._zone_score("Вход центральный") >= 5  # has "вход"
    assert MireaACS._zone_score("Зона ресепшн") >= 5  # has "зона"


def test_zone_score_empty():
    assert MireaACS._zone_score("") == -100

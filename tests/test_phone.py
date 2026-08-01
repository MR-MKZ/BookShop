"""Unit tests for Iranian phone validation."""

from app.utils.phone import normalize_iran_phone, validate_iran_phone


def test_normalize_plus98():
    assert normalize_iran_phone("+989121112233") == "09121112233"


def test_valid_phones():
    for phone in ("09121112233", "09351234876", "09103334455"):
        ok, result = validate_iran_phone(phone)
        assert ok, result
        assert result == phone


def test_reject_fake_repeated():
    ok, msg = validate_iran_phone("09111111111")
    assert not ok


def test_reject_bad_prefix():
    ok, msg = validate_iran_phone("09501234567")
    assert not ok


def test_reject_short():
    ok, msg = validate_iran_phone("0912111")
    assert not ok


def test_reject_sequential():
    ok, msg = validate_iran_phone("09123456789")
    assert not ok

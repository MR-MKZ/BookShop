"""Iranian mobile phone validation."""

from __future__ import annotations

import re

# Common Iranian mobile prefixes (IR-MCI, Irancell, Rightel, etc.)
VALID_PREFIXES = {
    "0901",
    "0902",
    "0903",
    "0904",
    "0905",
    "0910",
    "0911",
    "0912",
    "0913",
    "0914",
    "0915",
    "0916",
    "0917",
    "0918",
    "0919",
    "0920",
    "0921",
    "0922",
    "0923",
    "0930",
    "0933",
    "0935",
    "0936",
    "0937",
    "0938",
    "0939",
    "0990",
    "0991",
    "0992",
    "0993",
    "0994",
    "0995",
    "0996",
    "0997",
    "0998",
    "0999",
}

_PHONE_RE = re.compile(r"^09\d{9}$")


def normalize_iran_phone(raw: str | None) -> str:
    """Normalize phone to 09xxxxxxxxx format."""
    if not raw:
        return ""
    phone = re.sub(r"[\s\-()]", "", str(raw).strip())
    if phone.startswith("+98"):
        phone = "0" + phone[3:]
    elif phone.startswith("0098"):
        phone = "0" + phone[4:]
    elif phone.startswith("98") and len(phone) == 12:
        phone = "0" + phone[2:]
    return phone


def validate_iran_phone(raw: str | None) -> tuple[bool, str]:
    """
    Validate Iranian mobile number.
    Returns (ok, error_message_or_normalized_phone).
    """
    phone = normalize_iran_phone(raw)
    if not phone:
        return False, "شماره تلفن الزامی است"

    if not _PHONE_RE.match(phone):
        return False, "فرمت شماره باید مانند 09153276607 باشد"

    prefix = phone[:4]
    if prefix not in VALID_PREFIXES:
        return False, "پیش‌شماره موبایل ایرانی معتبر نیست"

    # Reject obvious fake / sequential / repeated patterns
    digits = phone[2:]  # last 9 digits after 09
    if len(set(digits)) == 1:
        return False, "شماره تلفن معتبر نیست"

    if digits in {"123456789", "987654321", "012345678", "111111111", "000000000"}:
        return False, "شماره تلفن معتبر نیست"

    # Reject ascending/descending runs of 6+
    asc = "0123456789"
    desc = "9876543210"
    for i in range(len(digits) - 5):
        chunk = digits[i : i + 6]
        if chunk in asc or chunk in desc:
            return False, "شماره تلفن معتبر نیست"

    return True, phone

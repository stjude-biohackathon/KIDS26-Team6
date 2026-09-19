"""Score matched identifiers with checksum and range tests.

Validators increase confidence for plausible values. They keep low-confidence
matches because invalid or mistyped identifiers can still contain PHI. The
over-redaction limit in ``thresholds.json`` controls precision.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D+")


def digits_only(value: str) -> str:
    return _DIGITS.sub("", value)


def luhn_ok(value: str) -> bool:
    """Standard Luhn mod-10 check. Used for payment cards and (via NPI) for
    provider identifiers."""

    digits = [int(char) for char in digits_only(value)]
    if len(digits) < 2:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def npi_ok(value: str) -> bool:
    """US National Provider Identifier: 10 digits, Luhn over ``80840`` + first 9.

    The ``80840`` prefix is the NPI issuer identifier; it is prepended before the
    checksum rather than being part of the printed number.
    """

    digits = digits_only(value)
    if len(digits) != 10:
        return False
    return luhn_ok("80840" + digits)


def ssn_plausible(value: str) -> bool:
    """Whether a 9-digit string is an *issuable* SSN.

    ``False`` for the reserved-invalid ranges the eval corpus is built from, and
    for the all-zero group forms. Callers use this to set ``score``, **not** to
    drop the span -- see the module docstring.
    """

    digits = digits_only(value)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in {"000", "666"} or area.startswith("9"):
        return False
    return not (group == "00" or serial == "0000")


def ipv4_ok(value: str) -> bool:
    """Dotted quad with every octet in ``0..255`` and no leading zeros.

    Leading zeros are rejected because ``010.1.1.1`` is parsed as octal by some
    resolvers and as decimal by others; treating it as a well-formed address
    would be asserting a resolution we have not made.
    """

    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part[0] == "0"):
            return False
        if not 0 <= int(part) <= 255:
            return False
    return True


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def iso_date_ok(value: str) -> bool:
    """A calendar-plausible ``YYYY-MM-DD``.

    February is allowed 29 days unconditionally: rejecting ``2013-02-29`` would
    mean a typo'd date of birth escapes redaction, and the cost of keeping it is
    one extra generalization.
    """

    match = _ISO.match(value)
    if match is None:
        return False
    year, month, day = (int(group) for group in match.groups())
    if not 1900 <= year <= 2099 or not 1 <= month <= 12:
        return False
    return 1 <= day <= _DAYS_IN_MONTH[month - 1]


def nanp_plausible(value: str) -> bool:
    """A structurally valid North American number.

    Area code and exchange must start ``2-9``; an 11-digit form must start ``1``.
    ``555-01xx`` exchanges -- the fictitious range the corpus uses -- pass, since
    they are structurally valid and only conventionally reserved.
    """

    digits = digits_only(value)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return False
    return digits[0] in "23456789" and digits[3] in "23456789"


def zip_plausible(value: str) -> bool:
    digits = digits_only(value)
    return len(digits) in (5, 9)

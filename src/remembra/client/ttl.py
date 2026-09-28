"""TTL strings: the one format the server, the SDK and its shadow cache read.

A TTL is a number and a unit: ``30d``, ``1.5d``, ``36h``, ``90min``. The
number may have a decimal part. Case does not matter, except that a capital
``M`` on its own means months. Spaces around the TTL and between the number
and the unit are allowed (``"2 weeks"``).

=====================  ==================
Unit                   Means
=====================  ==================
s, sec, second(s)      seconds
min, minute(s)         minutes
h, hr, hour(s)         hours
d, day(s)              days
w, week(s)             weeks (7 days)
mo, month(s), M        months (30 days)
y, yr, year(s)         years (365 days)
=====================  ==================

A TTL runs from 1 second to 100 years. Anything else raises ``ValueError``
with a message that says what to write instead.

A bare ``m`` is refused, with "use 'min' for minutes or 'mo' for months".
Servers 0.16.1 and earlier read ``m`` as months, and this module's first
version read it as minutes; refusing it means the same string never quietly
changes meaning. Those servers also read only whole numbers and do not know
``min`` or ``mo``: for a TTL they must read, use whole ``h``, ``d``, ``w`` or
``y`` values.

Stdlib only: the SDK imports this without the server's dependencies.
"""

from __future__ import annotations

import re

MAX_TTL_SECONDS = 100 * 365 * 86400
MAX_TTL_CHARS = 32

_TTL_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)")

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
    "mo": 2592000,  # 30 days
    "month": 2592000,
    "months": 2592000,
    "y": 31536000,  # 365 days
    "yr": 31536000,
    "year": 31536000,
    "years": 31536000,
}

_HOW = "Use a number and a unit, e.g. '30d', '1.5d', '36h' or '90min'. Units: s, min, h, d, w, mo (months), y."
_BARE_M = "'m' could mean minutes or months: use 'min' for minutes or 'mo' for months."


def parse_ttl_seconds(ttl: str) -> float:
    """The TTL in seconds. Raises ``ValueError`` when it is not a TTL.

    >>> parse_ttl_seconds("1.5d")
    129600.0
    >>> parse_ttl_seconds("40min")
    2400.0
    """
    text = str(ttl).strip()
    if len(text) > MAX_TTL_CHARS:
        raise ValueError(f"Invalid TTL: at most {MAX_TTL_CHARS} characters. {_HOW}")
    match = _TTL_RE.fullmatch(text)
    if not match:
        raise ValueError(f"Invalid TTL {text!r}. {_HOW}")
    number, unit = match.groups()
    if unit == "m":
        raise ValueError(f"Invalid TTL {text!r}: {_BARE_M}")
    multiplier = _UNIT_SECONDS["mo"] if unit == "M" else _UNIT_SECONDS.get(unit.lower())
    if multiplier is None:
        raise ValueError(f"Invalid TTL {text!r}: unknown unit {unit!r}. {_HOW}")
    seconds = float(number) * multiplier
    if seconds < 1:
        raise ValueError(f"Invalid TTL {text!r}: a TTL must be at least 1 second. {_HOW}")
    if seconds > MAX_TTL_SECONDS:
        raise ValueError(f"Invalid TTL {text!r}: the longest TTL is 100 years.")
    return seconds

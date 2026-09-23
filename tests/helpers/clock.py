"""Reading the API's timestamps, which `fromisoformat` alone cannot."""

from __future__ import annotations

import re
from datetime import datetime, timezone


def parse_timestamp(value: str) -> datetime:
    """Parse an API timestamp, trimming the nanoseconds `fromisoformat` rejects.

    Some fields carry nine fractional digits and a trailing `Z`; the tail is cut
    to microseconds before parsing, and a naive result is read as UTC.
    """
    text = re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

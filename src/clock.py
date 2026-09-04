"""Injected clock (spec §2 + section 12).

`datetime.now()` must not appear anywhere else in this repo. Every time-dependent
decision reads an injected Clock so that eval runs are deterministic and replayable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")  # India default; policy windows resolve via tz_for()


def tz_for(market_code: str = "IN") -> ZoneInfo:
    """Policy windows are evaluated in the MARKET's timezone, not always IST.

    A Malaysian merchant's quiet hours are Asia/Kuala_Lumpur. Hardcoding IST would
    silently send a message at 03:00 local time and breach the local equivalent of the
    rule we were trying to honour.
    """
    from src.market import get_market
    return get_market(market_code).timezone


class Clock(Protocol):
    def now(self) -> datetime: ...


class VirtualClock:
    """Deterministic clock for eval and replay. Advances only when told to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("VirtualClock requires a timezone-aware start time")
        self._now = start.astimezone(IST)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        """Move forward. Returns the new time. Never moves backwards."""
        step = timedelta(**delta)
        if step < timedelta(0):
            raise ValueError("VirtualClock cannot move backwards")
        self._now = self._now + step
        return self._now

    def set_to(self, when: datetime) -> datetime:
        when = when.astimezone(IST)
        if when < self._now:
            raise ValueError("VirtualClock cannot move backwards")
        self._now = when
        return self._now


class SystemClock:
    """Real wall time. Permitted ONLY at process edges (API request logging)."""

    def now(self) -> datetime:
        return datetime.now(tz=IST)

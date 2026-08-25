"""Market profiles - everything locale-specific, in one place.

Razorpay operates in India, Malaysia (as Curlec) and Singapore, and its error
documentation also lists the United States. An agent that hardcodes rupees, IST and RBI
is an Indian script rather than a product: it cannot serve a Kuala Lumpur merchant
without a rewrite, and it silently mis-formats every amount it prints.

So currency, timezone, lawful contact window, payment rails and languages are all
DATA (`config/markets.yaml`), not code. The decision core is market-agnostic; it asks a
Market object for anything locale-shaped.

Money stays in INTEGER MINOR UNITS everywhere (paise, sen, cents). `minor_per_major`
happens to be 100 in all three markets, but currencies exist where it is not, so the
conversion is never assumed - it is read from the profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

MARKETS_PATH = Path(__file__).resolve().parents[1] / "config" / "markets.yaml"
DEFAULT_MARKET = "IN"


class UnknownMarket(Exception):
    """Raised rather than silently falling back to India."""


@dataclass(frozen=True)
class Currency:
    code: str
    symbol: str
    minor_per_major: int
    minor_name: str

    def format(self, minor_units: int) -> str:
        """The ONLY place minor units become a human-readable amount."""
        return f"{self.symbol}{minor_units / self.minor_per_major:,.2f}"


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    verified: bool
    currency: Currency
    timezone: ZoneInfo
    contact_window_start: time
    contact_window_end: time
    regulators: tuple[str, ...]
    pre_debit_notification_hours: int
    registered_template_channels: tuple[str, ...]
    rails: tuple[str, ...]
    rail_alternatives: dict[str, tuple[str, ...]]
    languages: tuple[str, ...]

    def money(self, minor_units: int) -> str:
        return self.currency.format(minor_units)

    def alternatives_to(self, rail: str | None) -> tuple[str, ...]:
        return self.rail_alternatives.get(rail or "", ())

    def supports(self, rail: str | None) -> bool:
        return rail in self.rails

    @property
    def quiet_hours(self) -> tuple[time, time]:
        """Quiet period is the complement of the lawful contact window."""
        return (self.contact_window_end, self.contact_window_start)

    def caveat(self) -> str | None:
        """Surface unverified profiles rather than letting them pass as checked."""
        if self.verified:
            return None
        return (f"{self.code} ({self.name}) contact window and notice period are NOT yet "
                f"verified against {', '.join(self.regulators)}. Treat as placeholder.")


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


@lru_cache(maxsize=1)
def _raw(path: str | None = None) -> dict:
    with Path(path or MARKETS_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=8)
def get_market(code: str = DEFAULT_MARKET) -> Market:
    data = _raw()
    key = (code or DEFAULT_MARKET).upper()
    if key not in data:
        raise UnknownMarket(f"no market profile for {key!r}; known: {sorted(data)}")
    m = data[key]
    c = m["currency"]
    return Market(
        code=key,
        name=m["name"],
        verified=bool(m.get("verified", False)),
        currency=Currency(c["code"], c["symbol"], int(c["minor_per_major"]), c["minor_name"]),
        timezone=ZoneInfo(m["timezone"]),
        contact_window_start=_hhmm(m["contact_window"]["start"]),
        contact_window_end=_hhmm(m["contact_window"]["end"]),
        regulators=tuple(m.get("regulators", ())),
        pre_debit_notification_hours=int(m.get("pre_debit_notification_hours", 24)),
        registered_template_channels=tuple(m.get("registered_template_channels", ())),
        rails=tuple(m.get("rails", ())),
        rail_alternatives={k: tuple(v) for k, v in (m.get("rail_alternatives") or {}).items()},
        languages=tuple(m.get("languages", ("en",))),
    )


def known_markets() -> tuple[str, ...]:
    return tuple(sorted(_raw()))


def market_for_currency(currency_code: str) -> Market:
    """Resolve a market from the currency on an inbound event."""
    for code in known_markets():
        market = get_market(code)
        if market.currency.code == currency_code.upper():
            return market
    raise UnknownMarket(f"no market profile uses currency {currency_code!r}")

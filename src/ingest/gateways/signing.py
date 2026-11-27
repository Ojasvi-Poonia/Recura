"""Timestamped HMAC-SHA256 signatures: `t=<unix seconds>,v1=<hex>[,v1=<hex>...]`.

Stripe's scheme (https://docs.stripe.com/webhooks, "Verify signatures manually",
read 2026-11-26): the signed string is the timestamp, a ".", and the unmodified body;
the key is the endpoint secret; HMAC-SHA256, hex. `v1` may appear more than once while
a secret is being rolled, and at least one must match. `v0` and the `secret`,
`destination` and `payload` debug entries are not signatures and are ignored. Stripe's
libraries allow five minutes between the timestamp and the current time.

The neutral `recura` format uses the same scheme, so any system that can sign a Stripe
webhook can sign one of ours. Unlike Razorpay's, the signature covers a timestamp, so a
captured delivery cannot be replayed after the tolerance has passed.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

TOLERANCE_SECONDS = 300


def sign(raw: bytes, secret: str, timestamp: int) -> str:
    """The header value for a body. For tests and for senders of the neutral format."""
    return f"t={timestamp},v1={_digest(raw, secret, timestamp)}"


def _digest(raw: bytes, secret: str, timestamp: int) -> str:
    message = str(timestamp).encode("ascii") + b"." + raw
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _parse(header: str) -> tuple[int | None, list[str]]:
    timestamp, signatures = None, []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    return timestamp, signatures


def verify(raw: bytes, header: str, secret: str, now: datetime,
           tolerance: int = TOLERANCE_SECONDS) -> bool:
    if not isinstance(raw, bytes):
        raise TypeError("raw body must be bytes - never parse before verifying")
    timestamp, signatures = _parse(header or "")
    if timestamp is None or not signatures:
        return False
    if abs(now.timestamp() - timestamp) > tolerance:
        return False
    expected = _digest(raw, secret, timestamp)
    # Every candidate is compared, in constant time, whichever matches.
    return any([hmac.compare_digest(expected, s) for s in signatures])  # noqa: C419

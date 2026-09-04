"""Razorpay webhook signature verification (spec §7).

Razorpay computes HMAC-SHA256 with the webhook secret as key and the RAW request
body as message, delivered in the `X-Razorpay-Signature` header.

The critical rule, in Razorpay's own words: *do not parse or cast the webhook
request body* before verifying. Re-serialising JSON reorders keys and changes
whitespace, which changes the digest, which fails the check. Everything in this
module therefore operates on `bytes`, never on a parsed dict.
Checked 2026-08-26.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "x-razorpay-event-id"  # unique per event; used for idempotency


class SignatureError(Exception):
    """Raised when a webhook body does not match its signature."""


def expected_signature(raw_body: bytes, secret: str) -> str:
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes - never parse before verifying")
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time comparison. Never short-circuits on the first differing byte."""
    if not signature:
        return False
    return hmac.compare_digest(expected_signature(raw_body, secret), signature)


def verify_or_raise(raw_body: bytes, signature: str, secret: str) -> None:
    if not verify(raw_body, signature, secret):
        raise SignatureError("webhook signature mismatch")

"""Razorpay: the reference adapter. The logic lives where it always has, in
src/ingest/signature.py and src/ingest/webhook.py; this only gives it the common shape."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from src.ingest.signature import EVENT_ID_HEADER, SIGNATURE_HEADER, verify
from src.ingest.webhook import IngestResult, normalise


class RazorpayGateway:
    name = "razorpay"

    def delivery_id(self, raw: bytes, headers: Mapping[str, str]) -> str | None:
        # Razorpay documents x-razorpay-event-id as unique per event and as the
        # intended dedupe key.
        return headers.get(EVENT_ID_HEADER) or None

    def verify(self, raw: bytes, headers: Mapping[str, str], secret: str,
               now: datetime) -> bool:
        # Razorpay's signature carries no timestamp, so `now` has nothing to check.
        return verify(raw, headers.get(SIGNATURE_HEADER.lower(), ""), secret)

    def normalise(self, raw: bytes, delivery_id: str | None) -> IngestResult:
        return normalise(raw, delivery_id)

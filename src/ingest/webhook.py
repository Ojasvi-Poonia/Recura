"""Razorpay webhook normalisation -> RiskEvent (spec §7).

Three behaviours here are directly driven by Razorpay's published webhook semantics
(checked 2026-08-26):

1. **Order is not guaranteed.** Razorpay states explicitly that `payment.authorized`
   then `payment.captured` "may not be followed at all times". Normalisation is
   therefore order-independent: every payload is classified on its own merits.

2. **Late authorisation is a STOP condition.** A success signal arriving after an
   apparent failure means the money already came in. Continuing to act would contact
   a customer who has already paid. spec §7 calls this a differentiator.

3. **Idempotency uses `x-razorpay-event-id`.** Razorpay documents this header as
   unique per event and as the intended dedupe key. We do not invent our own.

Inbound payload models deliberately ALLOW extra fields - Razorpay adds fields over
time and a strict model would reject live traffic. Only our internal `RiskEvent`
forbids extras (that strictness is what blocks latent smuggling, section 9.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from src.clock import IST
from src.models import (
    CustomerHistory,
    ErrorObject,
    MerchantContext,
    RiskEvent,
    SourceType,
)

# Events that mean money arrived. All are hard stops.
SUCCESS_EVENTS = {
    "payment.authorized": "late_authorisation",
    "payment.captured": "payment_captured",
    "order.paid": "order_paid",
    "subscription.charged": "subscription_charged",
    "invoice.paid": "invoice_paid",
}

# Events that open or continue a recovery episode.
RISK_EVENTS: dict[str, SourceType] = {
    "payment.failed": "payment",
    "subscription.halted": "mandate",
    "subscription.pending": "mandate",
    "invoice.expired": "invoice",
}

# Events that end an episode for a non-payment reason.
TERMINAL_EVENTS = {
    "subscription.cancelled": "subscription_cancelled",
    "payment.dispute.created": "dispute_opened",
}


class Outcome(StrEnum):
    RISK_EVENT = "risk_event"
    STOP = "stop"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    # A payload we recognised but could not normalise. Distinct from IGNORED, which
    # means "understood and deliberately not acted on". MALFORMED is never remembered
    # in the idempotency store, so Razorpay's retry gets a real second attempt.
    MALFORMED = "malformed"


class RazorpayWebhook(BaseModel):
    """Inbound envelope. Lenient by design - Razorpay may add fields."""

    model_config = ConfigDict(extra="allow")

    entity: str = "event"
    account_id: str | None = None
    event: str
    contains: list[str] = []
    payload: dict[str, Any] = {}
    created_at: int | None = None


@dataclass(frozen=True)
class IngestResult:
    outcome: Outcome
    risk_event: RiskEvent | None = None
    stop_reason: str | None = None
    razorpay_event_id: str | None = None
    note: str = ""


@dataclass
class IdempotencyStore:
    """In-memory dedupe on `x-razorpay-event-id`. SQLite-backed in production."""

    seen: set[str] = field(default_factory=set)

    def is_duplicate(self, event_id: str | None) -> bool:
        return bool(event_id) and event_id in self.seen

    def remember(self, event_id: str | None) -> None:
        if event_id:
            self.seen.add(event_id)


def _entity(hook: RazorpayWebhook, name: str) -> dict[str, Any]:
    return hook.payload.get(name, {}).get("entity", {}) or {}


def _first_entity(hook: RazorpayWebhook) -> dict[str, Any]:
    for name in ("payment", "subscription", "invoice", "order"):
        found = _entity(hook, name)
        if found:
            return found
    return {}


def _attempt_number(entity: dict) -> int:
    """Attempt count from merchant `notes`, which is untrusted free text."""
    raw = (entity.get("notes") or {}).get("attempt_number", 1)
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return 1
    return value if value >= 1 else 1


def _epoch_to_ist(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(int(value), tz=IST)


def _error_object(entity: dict[str, Any]) -> ErrorObject | None:
    """Razorpay flattens the error onto the payment entity as `error_*` fields."""
    if not any(k.startswith("error_") for k in entity):
        return None
    return ErrorObject(
        code=entity.get("error_code"),
        description=entity.get("error_description"),
        source=entity.get("error_source"),
        step=entity.get("error_step"),
        reason=entity.get("error_reason"),
        metadata=entity.get("error_metadata") or {},
    )


def _customer_id(entity: dict[str, Any]) -> str:
    notes = entity.get("notes") or {}
    for key in ("customer_id", "customer"):
        if entity.get(key):
            return str(entity[key])
        if notes.get(key):
            return str(notes[key])
    # Test mode often carries no customer_id. Fall back to a stable synthetic handle;
    # never to raw email/contact, which would put PII in the ledger (section 2).
    return f"anon_{entity.get('id', 'unknown')}"


def normalise(
    raw_body: bytes,
    razorpay_event_id: str | None = None,
    store: IdempotencyStore | None = None,
    merchant_id: str = "merchant_demo",
) -> IngestResult:
    """Classify one verified webhook body. Signature MUST already be verified."""
    if store is not None and store.is_duplicate(razorpay_event_id):
        return IngestResult(Outcome.DUPLICATE, razorpay_event_id=razorpay_event_id,
                            note="already processed")

    hook = RazorpayWebhook.model_validate(json.loads(raw_body))
    entity = _first_entity(hook)

    if hook.event in SUCCESS_EVENTS:
        return IngestResult(
            Outcome.STOP,
            stop_reason=SUCCESS_EVENTS[hook.event],
            razorpay_event_id=razorpay_event_id,
            note=f"money received via {hook.event}; episode must stop",
        )

    if hook.event in TERMINAL_EVENTS:
        return IngestResult(
            Outcome.STOP,
            stop_reason=TERMINAL_EVENTS[hook.event],
            razorpay_event_id=razorpay_event_id,
            note=f"episode terminated by {hook.event}",
        )

    source_type = RISK_EVENTS.get(hook.event)
    if source_type is None:
        return IngestResult(Outcome.IGNORED, razorpay_event_id=razorpay_event_id,
                            note=f"unhandled event {hook.event}")

    observed_at = _epoch_to_ist(entity.get("created_at")) or _epoch_to_ist(hook.created_at)
    if observed_at is None:
        # No wall clock fallback here - section 12 forbids datetime.now() outside clock.py.
        return IngestResult(Outcome.IGNORED, razorpay_event_id=razorpay_event_id,
                            note="payload carried no timestamp; cannot place on timeline")

    try:
        risk = RiskEvent(
            event_id=str(entity.get("id") or razorpay_event_id or "unknown"),
            merchant_id=merchant_id,
            customer_id=_customer_id(entity),
            source_type=source_type,
            amount_paise=int(entity.get("amount") or 0),  # Razorpay amounts are paise
            currency=str(entity.get("currency") or "INR"),
            observed_at=observed_at,
            razorpay_error=_error_object(entity),
            method=entity.get("method"),
            bank=entity.get("bank"),
            # `notes` is arbitrary merchant-supplied free text. A merchant writing
            # {"attempt_number": "3rd"} must not be able to raise here.
            attempt_number=_attempt_number(entity),
            customer_history=CustomerHistory(),
            merchant_context=MerchantContext(merchant_id=merchant_id),
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return IngestResult(Outcome.MALFORMED, razorpay_event_id=razorpay_event_id,
                            note=f"could not normalise payload: {exc}")

    # Remember ONLY now, once the event has actually been built.
    #
    # This used to run immediately after the JSON parse, so any normalisation failure
    # marked the id as processed on the way out. Razorpay's redelivery then hit the
    # duplicate branch and the risk event was swallowed permanently: first delivery 500,
    # every retry 200 "duplicate". An idempotency key must record work COMPLETED, never
    # work merely attempted.
    if store is not None:
        store.remember(razorpay_event_id)
    return IngestResult(Outcome.RISK_EVENT, risk_event=risk,
                        razorpay_event_id=razorpay_event_id)

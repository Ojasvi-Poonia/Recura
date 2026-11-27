"""Stripe webhooks -> RiskEvent.

Sources, read 2026-11-26: https://docs.stripe.com/webhooks (signatures),
https://docs.stripe.com/api/events/object, .../api/payment_intents/object,
.../api/invoices/object and .../api/events/types.

WHICH EVENTS OPEN AN EPISODE
One failed card payment produces both `payment_intent.payment_failed` and
`charge.failed`. Taking both would open two episodes for one failure, so the payment
intent is the failure and a charge only counts when it has no payment intent (the
legacy Charges API). `invoice.payment_failed` is the subscription failure: a mandate
when the invoice belongs to a subscription, an invoice otherwise.

TIME
Every object's `created` is when it was created, which for a payment intent or an
invoice can be long before it failed. The event's own `created` is when the failure
happened, and that is what the episode is timed from.

CODES
`last_payment_error.decline_code` (the issuer's reason) is preferred to `code` (Stripe's
own). Either is translated by the Stripe profile (config/psp/stripe.yaml), the same
table an export goes through, and kept untranslated in ErrorObject.code.

Like Razorpay, Stripe does not promise delivery order. Each event is classified on its
own, and the event `id` is the dedupe key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from src.ingest.gateways import signing
from src.ingest.psp import load_profile
from src.ingest.webhook import MAX_KEY_LENGTH, IngestResult, Outcome, _epoch_to_ist
from src.market import get_market
from src.models import CustomerHistory, ErrorObject, MerchantContext, RiskEvent

SIGNATURE_HEADER = "stripe-signature"

STOPS = {
    "payment_intent.succeeded": "payment_captured",
    "charge.succeeded": "payment_captured",
    "invoice.paid": "invoice_paid",
    "invoice.payment_succeeded": "invoice_paid",
}
TERMINAL = {
    "charge.dispute.created": "dispute_opened",
    "customer.subscription.deleted": "subscription_cancelled",
}
FAILURES = frozenset({"payment_intent.payment_failed", "charge.failed",
                      "invoice.payment_failed"})


class StripeGateway:
    name = "stripe"

    def delivery_id(self, raw: bytes, headers: Mapping[str, str]) -> str | None:
        try:
            event_id = json.loads(raw).get("id")
        except (ValueError, AttributeError):
            return None
        return event_id if isinstance(event_id, str) and event_id else None

    def verify(self, raw: bytes, headers: Mapping[str, str], secret: str,
               now: datetime) -> bool:
        return signing.verify(raw, headers.get(SIGNATURE_HEADER, ""), secret, now)

    def normalise(self, raw: bytes, delivery_id: str | None) -> IngestResult:
        try:
            event = json.loads(raw)
            kind, obj = str(event["type"]), event["data"]["object"]
            if not isinstance(obj, dict):
                raise TypeError("data.object is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            return IngestResult(Outcome.MALFORMED, razorpay_event_id=delivery_id,
                                note=f"not a Stripe event: {exc}")
        at = _epoch_to_ist(event.get("created"))
        keys = _keys(obj)

        if kind in STOPS:
            return IngestResult(Outcome.STOP, stop_reason=STOPS[kind],
                                razorpay_event_id=delivery_id, correlation_keys=keys,
                                occurred_at=at, note=f"money received via {kind}")
        if kind in TERMINAL:
            return IngestResult(Outcome.STOP, stop_reason=TERMINAL[kind],
                                razorpay_event_id=delivery_id, correlation_keys=keys,
                                occurred_at=at, note=f"episode terminated by {kind}")
        if kind not in FAILURES:
            return IngestResult(Outcome.IGNORED, razorpay_event_id=delivery_id,
                                note=f"unhandled event {kind}")
        if kind == "charge.failed" and obj.get("payment_intent"):
            return IngestResult(Outcome.IGNORED, razorpay_event_id=delivery_id,
                                note="counted through its payment_intent.payment_failed")
        if at is None:
            return IngestResult(Outcome.IGNORED, razorpay_event_id=delivery_id,
                                note="event carried no timestamp; cannot place on timeline")
        return _failure(kind, obj, at, keys, delivery_id)


def _failure(kind: str, obj: dict[str, Any], at: datetime, keys: tuple[str, ...],
             delivery_id: str | None) -> IngestResult:
    currency = str(obj.get("currency") or "").upper()
    market = get_market().currency.code
    if currency != market:
        return IngestResult(Outcome.IGNORED, razorpay_event_id=delivery_id,
                            note=f"currency {currency or 'missing'} is outside the verified "
                                 f"market ({market}); its contact rules are not checked")
    is_invoice = kind == "invoice.payment_failed"
    amount = obj.get("amount_remaining") if is_invoice else obj.get("amount")
    try:
        risk = RiskEvent(
            event_id=str(obj.get("id") or delivery_id or "unknown"),
            merchant_id="merchant_demo",
            customer_id=_customer(obj),
            source_type=("mandate" if _subscription(obj) else "invoice") if is_invoice
            else "payment",
            amount_paise=int(amount or 0),      # Stripe: the smallest currency unit
            currency=currency,
            observed_at=at,
            razorpay_error=_error(kind, obj),
            method=_method(obj),
            customer_history=CustomerHistory(),
            merchant_context=MerchantContext(merchant_id="merchant_demo"),
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return IngestResult(Outcome.MALFORMED, razorpay_event_id=delivery_id,
                            note=f"could not normalise payload: {exc}")
    return IngestResult(Outcome.RISK_EVENT, risk_event=risk, razorpay_event_id=delivery_id,
                        correlation_keys=keys, occurred_at=at)


def _error(kind: str, obj: dict[str, Any]) -> ErrorObject | None:
    if kind == "charge.failed":
        code = (obj.get("outcome") or {}).get("reason") or obj.get("failure_code")
        message = obj.get("failure_message")
    else:
        err = obj.get("last_payment_error") or {}
        code, message = err.get("decline_code") or err.get("code"), err.get("message")
    if not code:
        # An invoice that failed for want of a stored payment method has no code.
        return None
    code = str(code)
    return ErrorObject(code=code, reason=load_profile("stripe").reason_for(code),
                       description=message)


def _method(obj: dict[str, Any]) -> str | None:
    err_method = ((obj.get("last_payment_error") or {}).get("payment_method") or {})
    found = (err_method.get("type")
             or (obj.get("payment_method_details") or {}).get("type")
             or next(iter(obj.get("payment_method_types") or []), None))
    return str(found).lower() if found else None


def _subscription(obj: dict[str, Any]) -> str | None:
    details = ((obj.get("parent") or {}).get("subscription_details") or {})
    found = details.get("subscription") or obj.get("subscription")
    return found if isinstance(found, str) else None


def _customer(obj: dict[str, Any]) -> str:
    customer = obj.get("customer") or (obj.get("metadata") or {}).get("customer_id")
    # Never the email or phone: those would put PII in the ledger.
    return str(customer) if customer else f"anon_{obj.get('id', 'unknown')}"


def _keys(obj: dict[str, Any]) -> tuple[str, ...]:
    """The object's id and the ids it links to: a failed intent and the charge, invoice
    or subscription that later settles it share one of these."""
    found = [obj.get("id"), obj.get("payment_intent"), obj.get("latest_charge"),
             obj.get("invoice"), _subscription(obj)]
    return tuple(dict.fromkeys(
        k for k in found if isinstance(k, str) and 0 < len(k) <= MAX_KEY_LENGTH))

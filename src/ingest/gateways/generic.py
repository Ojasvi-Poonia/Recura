"""The neutral format: how a gateway without an adapter of its own sends failures.

    POST /webhooks/recura
    X-Recura-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">

    {"id": "dlv_0001",                       unique per delivery; the dedupe key
     "type": "payment.failed",               | payment.succeeded | episode.ended
     "occurred_at": "2026-03-10T11:00:00+05:30",
     "gateway": "cashfree",                  optional: the PSP profile for `code`
     "payment": {"id": "pay_1", "customer_id": "cust_1", "amount_minor": 249900,
                 "currency": "INR", "source_type": "payment", "method": "upi",
                 "code": "Z9", "reason": null, "description": "...",
                 "links": ["order_1"]},
     "end_reason": null}                     for episode.ended: why it ended

Exactly one of `payment.code` and `payment.reason` is needed for a failure:
`reason` when the sender already speaks the taxonomy's vocabulary, `code` plus
`gateway` when it forwards the gateway's own code and wants it translated by that
gateway's profile. A code the profile does not list passes through untranslated and is
handled as UNKNOWN, the conservative path.

Strict by design. This format is ours, so a misspelt field is a 422 with the field
named, never a silently ignored fact. Full description in docs/GATEWAYS.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.ingest.gateways import signing
from src.ingest.psp import ProfileError, load_profile
from src.ingest.webhook import MAX_KEY_LENGTH, IngestResult, Outcome
from src.market import get_market
from src.models import (
    CustomerHistory,
    ErrorObject,
    MerchantContext,
    RiskEvent,
    SourceType,
)

SIGNATURE_HEADER = "x-recura-signature"
END_REASONS = frozenset({"dispute_opened", "subscription_cancelled", "opted_out"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Payment(_Strict):
    id: str = Field(min_length=1, max_length=MAX_KEY_LENGTH)
    customer_id: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    amount_minor: int = Field(default=0, ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    source_type: SourceType = "payment"
    method: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    bank: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    code: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    reason: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    description: str | None = Field(default=None, max_length=500)
    links: tuple[str, ...] = Field(default=(), max_length=16)
    attempt_number: int = Field(default=1, ge=1)


class Delivery(_Strict):
    id: str = Field(min_length=1, max_length=128)
    type: Literal["payment.failed", "payment.succeeded", "episode.ended"]
    occurred_at: datetime
    gateway: str | None = Field(default=None, max_length=32)
    payment: Payment
    end_reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Delivery:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at needs a timezone offset")
        if self.type == "payment.failed" and not (self.payment.code or self.payment.reason):
            raise ValueError("a payment.failed needs payment.code or payment.reason")
        if self.payment.code and not self.payment.reason and not self.gateway:
            raise ValueError("payment.code needs `gateway`, the profile that translates it")
        if self.type == "episode.ended" and self.end_reason not in END_REASONS:
            raise ValueError(f"end_reason must be one of {sorted(END_REASONS)}")
        return self


class RecuraGateway:
    name = "recura"

    def delivery_id(self, raw: bytes, headers: Mapping[str, str]) -> str | None:
        try:
            return Delivery.model_validate_json(raw).id
        except ValidationError:
            return None

    def verify(self, raw: bytes, headers: Mapping[str, str], secret: str,
               now: datetime) -> bool:
        return signing.verify(raw, headers.get(SIGNATURE_HEADER, ""), secret, now)

    def normalise(self, raw: bytes, delivery_id: str | None) -> IngestResult:
        try:
            delivery = Delivery.model_validate_json(raw)
        except ValidationError as exc:
            return IngestResult(Outcome.MALFORMED, razorpay_event_id=delivery_id,
                                note=f"not a valid delivery: {_first_error(exc)}")
        payment = delivery.payment
        keys = tuple(dict.fromkeys((payment.id, *payment.links)))
        if delivery.type == "payment.succeeded":
            return IngestResult(Outcome.STOP, stop_reason="payment_captured",
                                razorpay_event_id=delivery_id, correlation_keys=keys,
                                occurred_at=delivery.occurred_at)
        if delivery.type == "episode.ended":
            return IngestResult(Outcome.STOP, stop_reason=delivery.end_reason,
                                razorpay_event_id=delivery_id, correlation_keys=keys,
                                occurred_at=delivery.occurred_at)
        return _failure(delivery, keys, delivery_id)


def _failure(delivery: Delivery, keys: tuple[str, ...],
             delivery_id: str | None) -> IngestResult:
    payment = delivery.payment
    market = get_market().currency.code
    if payment.currency.upper() != market:
        return IngestResult(Outcome.IGNORED, razorpay_event_id=delivery_id,
                            note=f"currency {payment.currency.upper()} is outside the "
                                 f"verified market ({market})")
    try:
        reason = payment.reason or _translate(delivery.gateway, payment.code)
    except ProfileError as exc:
        return IngestResult(Outcome.MALFORMED, razorpay_event_id=delivery_id, note=str(exc))
    risk = RiskEvent(
        event_id=payment.id, merchant_id="merchant_demo",
        customer_id=payment.customer_id or f"anon_{payment.id}",
        source_type=payment.source_type, amount_paise=payment.amount_minor,
        currency=market, observed_at=delivery.occurred_at,
        razorpay_error=ErrorObject(code=payment.code, reason=reason,
                                   description=payment.description),
        method=payment.method.lower() if payment.method else None,
        bank=payment.bank.upper() if payment.bank else None,
        attempt_number=payment.attempt_number,
        customer_history=CustomerHistory(),
        merchant_context=MerchantContext(merchant_id="merchant_demo"),
    )
    return IngestResult(Outcome.RISK_EVENT, risk_event=risk, razorpay_event_id=delivery_id,
                        correlation_keys=keys, occurred_at=delivery.occurred_at)


def _translate(gateway: str | None, code: str | None) -> str | None:
    if code is None or gateway is None:
        return code
    return load_profile(gateway).reasons.get(code, code)


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err["loc"]) or "body"
    return f"{where}: {err['msg']}"

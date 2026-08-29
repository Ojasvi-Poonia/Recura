"""Webhook ingest tests (CLAUDE.md sections 7 and 13)."""

import json

import pytest

from src.ingest.signature import SignatureError, expected_signature, verify, verify_or_raise
from src.ingest.webhook import IdempotencyStore, Outcome, normalise

SECRET = "whsec_test_recura"


def _body(event="payment.failed", **overrides):
    entity = {
        "id": "pay_TEST0001", "amount": 249900, "currency": "INR", "status": "failed",
        "method": "upi", "bank": "HDFC", "created_at": 1772000000,
        "error_code": "BAD_REQUEST_ERROR", "error_reason": "insufficient_funds",
        "error_source": "customer", "error_step": "payment_authorization",
        "notes": {"customer_id": "cust_007"},
    }
    entity.update(overrides)
    return json.dumps({
        "entity": "event", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": entity}}, "created_at": 1772000000,
    }).encode()


# --- signature -------------------------------------------------------------

def test_valid_signature_passes():
    b = _body()
    assert verify(b, expected_signature(b, SECRET), SECRET)


def test_tampered_body_fails():
    b = _body()
    sig = expected_signature(b, SECRET)
    assert not verify(b + b" ", sig, SECRET)


def test_wrong_secret_fails():
    b = _body()
    assert not verify(b, expected_signature(b, "other_secret"), SECRET)


def test_empty_signature_fails():
    assert not verify(_body(), "", SECRET)


def test_signature_requires_raw_bytes():
    """Re-serialising the body changes the digest - section 7's central gotcha."""
    with pytest.raises(TypeError):
        expected_signature({"not": "bytes"}, SECRET)  # type: ignore[arg-type]


def test_verify_or_raise():
    with pytest.raises(SignatureError):
        verify_or_raise(_body(), "deadbeef", SECRET)


# --- normalisation ---------------------------------------------------------

def test_payment_failed_produces_valid_risk_event():
    """section 13 definition of done for ingest."""
    r = normalise(_body(), "evt_A")
    assert r.outcome is Outcome.RISK_EVENT
    e = r.risk_event
    assert e.amount_paise == 249900 and isinstance(e.amount_paise, int)
    assert e.razorpay_error.reason == "insufficient_funds"
    assert e.customer_id == "cust_007"
    assert e.method == "upi" and e.source_type == "payment"


def test_late_authorisation_is_a_stop_condition():
    """section 7: payment.authorized can arrive AFTER an apparent failure."""
    r = normalise(_body(event="payment.authorized"), "evt_B")
    assert r.outcome is Outcome.STOP
    assert r.stop_reason == "late_authorisation"


@pytest.mark.parametrize("event,reason", [
    ("payment.captured", "payment_captured"),
    ("order.paid", "order_paid"),
    ("subscription.charged", "subscription_charged"),
    ("payment.dispute.created", "dispute_opened"),
])
def test_success_and_terminal_events_stop_the_episode(event, reason):
    r = normalise(_body(event=event), f"evt_{event}")
    assert r.outcome is Outcome.STOP and r.stop_reason == reason


def test_redelivered_webhook_is_deduped():
    """section 7: webhooks can be redelivered; dedupe on x-razorpay-event-id."""
    store = IdempotencyStore()
    assert normalise(_body(), "evt_dup", store).outcome is Outcome.RISK_EVENT
    assert normalise(_body(), "evt_dup", store).outcome is Outcome.DUPLICATE


def test_order_independence():
    """Razorpay states webhook order is not guaranteed. Classification must not care."""
    store = IdempotencyStore()
    first = normalise(_body(event="payment.authorized"), "evt_1", store)
    second = normalise(_body(event="payment.failed"), "evt_2", store)
    assert first.stop_reason == "late_authorisation"
    assert second.outcome is Outcome.RISK_EVENT  # classified on its own merits


def test_unhandled_event_is_ignored_not_crashed():
    assert normalise(_body(event="payment.pending.something"), "evt_X").outcome is Outcome.IGNORED


def test_missing_timestamp_is_ignored_rather_than_using_wall_clock():
    """section 12 forbids datetime.now() outside clock.py - including as a fallback."""
    body = json.loads(_body())
    body["payload"]["payment"]["entity"].pop("created_at")
    body.pop("created_at")
    r = normalise(json.dumps(body).encode(), "evt_NoTime")
    assert r.outcome is Outcome.IGNORED


def test_no_pii_in_customer_id_fallback():
    """section 2: no real PII. Never key a customer on their email or phone."""
    body = json.loads(_body())
    ent = body["payload"]["payment"]["entity"]
    ent.pop("notes")
    ent["email"] = "someone@example.com"
    ent["contact"] = "+919000000000"
    r = normalise(json.dumps(body).encode(), "evt_PII")
    assert "@" not in r.risk_event.customer_id
    assert "9000000000" not in r.risk_event.customer_id


def test_unknown_extra_fields_are_tolerated():
    """Razorpay adds fields over time; a strict inbound model would reject live traffic."""
    body = json.loads(_body())
    body["some_new_field_2027"] = {"x": 1}
    body["payload"]["payment"]["entity"]["another_new_field"] = "y"
    assert normalise(json.dumps(body).encode(), "evt_New").outcome is Outcome.RISK_EVENT


def test_a_payload_we_cannot_normalise_is_not_remembered_as_processed():
    """An idempotency key must record work COMPLETED, never work merely attempted.

    `store.remember()` used to run straight after the JSON parse, before the RiskEvent
    was built. Any normalisation failure therefore marked the id as processed on the way
    out, and Razorpay's redelivery hit the duplicate branch: first delivery 500, every
    retry 200 "duplicate", risk event swallowed permanently.
    """
    body = json.dumps({
        "event": "payment.failed", "created_at": 1756000000,
        "payload": {"payment": {"entity": {
            "id": "pay_2", "amount": "not-a-number", "currency": "INR",
            "created_at": 1756000000}}}}).encode()

    store = IdempotencyStore()
    first = normalise(body, razorpay_event_id="evt_bad", store=store)
    second = normalise(body, razorpay_event_id="evt_bad", store=store)

    assert first.outcome is Outcome.MALFORMED
    assert second.outcome is Outcome.MALFORMED, (
        "a failed normalisation was remembered, so the retry was swallowed as duplicate")


def test_merchant_notes_cannot_break_ingestion():
    """`notes` is arbitrary merchant free text and must never raise."""
    body = json.dumps({
        "event": "payment.failed", "created_at": 1756000000,
        "payload": {"payment": {"entity": {
            "id": "pay_1", "amount": 50000, "currency": "INR",
            "created_at": 1756000000,
            "notes": {"attempt_number": "3rd"}}}}}).encode()

    result = normalise(body, razorpay_event_id="evt_notes", store=IdempotencyStore())
    assert result.outcome is Outcome.RISK_EVENT
    assert result.risk_event.attempt_number == 1, "non-integer notes must fall back to 1"

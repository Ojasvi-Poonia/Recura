"""Gateway adapter tests.

The decision core never asks which gateway an event came from, so everything that
differs between gateways has to be right at the edge: each one's signature scheme, which
of its events is the failure, when the failure happened, and how its codes translate.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import src.api.main as api
from src.clock import IST, VirtualClock
from src.ingest.gateways import gateways, get_gateway, secret_env
from src.ingest.gateways.signing import sign, verify
from src.ingest.webhook import Outcome
from src.models import FailureClass
from src.taxonomy.mapping import classify

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)
T = int(NOW.timestamp())
SECRET = "whsec_gateway_test"
stripe = get_gateway("stripe")
recura = get_gateway("recura")


# -- timestamped signatures ---------------------------------------------------------


def test_a_signed_body_verifies():
    body = b'{"id": "evt_1"}'
    assert verify(body, sign(body, SECRET, T), SECRET, NOW)


@pytest.mark.parametrize("change", [
    lambda body, header: (body + b" ", header),                         # body edited
    lambda body, header: (body, header.replace(f"t={T}", f"t={T + 1}")),  # time edited
    lambda body, header: (body, ""),
    lambda body, header: (body, f"t={T}"),
])
def test_anything_but_the_signed_body_and_time_fails(change):
    body = b'{"id": "evt_1"}'
    raw, header = change(body, sign(body, SECRET, T))
    assert not verify(raw, header, SECRET, NOW)


def test_a_delivery_older_than_the_tolerance_is_refused_as_a_replay():
    body = b'{"id": "evt_1"}'
    header = sign(body, SECRET, T)

    assert verify(body, header, SECRET, NOW + timedelta(minutes=4))
    assert not verify(body, header, SECRET, NOW + timedelta(minutes=6))


def test_during_a_secret_roll_any_one_matching_v1_is_enough():
    body = b'{"id": "evt_1"}'
    good = sign(body, SECRET, T).split(",")[1]
    header = f"t={T},v1={'0' * 64},{good},v0=ignored,secret=whsec_debug"

    assert verify(body, header, SECRET, NOW)


def test_a_v0_signature_alone_is_not_a_signature():
    body = b'{"id": "evt_1"}'
    digest = sign(body, SECRET, T).split("v1=")[1]

    assert not verify(body, f"t={T},v0={digest}", SECRET, NOW)


# -- Stripe -------------------------------------------------------------------------


def stripe_event(kind="payment_intent.payment_failed", created=T, **obj):
    base = {"id": "pi_1", "object": "payment_intent", "amount": 249900, "currency": "inr",
            "customer": "cus_1", "created": created - 86_400, "latest_charge": "ch_1",
            "last_payment_error": {"type": "card_error", "code": "card_declined",
                                   "decline_code": "insufficient_funds",
                                   "message": "Your card has insufficient funds.",
                                   "payment_method": {"type": "card"}}}
    base.update(obj)
    return json.dumps({"id": "evt_1", "object": "event", "type": kind, "created": created,
                       "data": {"object": base}}).encode()


def test_a_failed_payment_intent_is_a_risk_event_in_the_taxonomys_terms():
    result = stripe.normalise(stripe_event(), "stripe:evt_1")

    risk = result.risk_event
    assert result.outcome is Outcome.RISK_EVENT
    assert risk.event_id == "pi_1" and risk.amount_paise == 249900 and risk.currency == "INR"
    assert risk.razorpay_error.reason == "insufficient_funds"
    assert risk.razorpay_error.code == "insufficient_funds"
    assert risk.method == "card" and risk.customer_id == "cus_1"
    assert classify(risk.razorpay_error).failure_class is FailureClass.FUNDS
    assert set(result.correlation_keys) == {"pi_1", "ch_1"}


def test_a_stripe_code_is_translated_by_the_same_profile_as_an_export():
    error = {"code": "expired_card", "decline_code": None, "payment_method": {"type": "card"}}

    risk = stripe.normalise(stripe_event(last_payment_error=error), None).risk_event

    assert risk.razorpay_error.reason == "card_expired"
    assert risk.razorpay_error.code == "expired_card"


def test_the_failure_is_timed_by_the_event_not_by_when_the_intent_was_created():
    risk = stripe.normalise(stripe_event(), None).risk_event

    assert risk.observed_at == NOW


def test_one_card_failure_is_one_episode_not_two():
    charge = stripe_event("charge.failed", id="ch_1", payment_intent="pi_1",
                          failure_code="card_declined")

    assert stripe.normalise(charge, None).outcome is Outcome.IGNORED


def test_a_legacy_charge_with_no_intent_is_the_failure_itself():
    charge = stripe_event("charge.failed", id="ch_9", object="charge", payment_intent=None,
                          failure_code="card_declined", outcome={"reason": "expired_card"})

    risk = stripe.normalise(charge, None).risk_event

    assert risk.event_id == "ch_9" and risk.razorpay_error.reason == "card_expired"


def test_a_failed_subscription_invoice_is_a_mandate_for_what_is_still_owed():
    invoice = stripe_event("invoice.payment_failed", id="in_1", object="invoice",
                           amount_due=99900, amount_remaining=49900, last_payment_error=None,
                           parent={"type": "subscription_details",
                                   "subscription_details": {"subscription": "sub_1"}})

    result = stripe.normalise(invoice, None)

    assert result.risk_event.source_type == "mandate"
    assert result.risk_event.amount_paise == 49900
    assert result.risk_event.razorpay_error is None
    assert "sub_1" in result.correlation_keys


@pytest.mark.parametrize("kind, reason", [
    ("payment_intent.succeeded", "payment_captured"),
    ("invoice.paid", "invoice_paid"),
    ("charge.dispute.created", "dispute_opened"),
    ("customer.subscription.deleted", "subscription_cancelled"),
])
def test_success_and_terminal_events_stop_the_episode(kind, reason):
    result = stripe.normalise(stripe_event(kind), None)

    assert result.outcome is Outcome.STOP and result.stop_reason == reason
    assert result.occurred_at == NOW


def test_a_payment_outside_the_verified_market_is_not_decided_on():
    result = stripe.normalise(stripe_event(currency="usd"), None)

    assert result.outcome is Outcome.IGNORED and "USD" in result.note


@pytest.mark.parametrize("body", [b"not json", b'{"type": "x"}', b'{"type": "x", "data": 1}'])
def test_a_body_that_is_not_a_stripe_event_is_malformed(body):
    assert stripe.normalise(body, None).outcome is Outcome.MALFORMED


# -- the neutral format -------------------------------------------------------------


def delivery(**kw):
    payment = {"id": "cf_1", "customer_id": "cust_1", "amount_minor": 249900,
               "method": "upi", "code": "Z9", **kw.pop("payment", {})}
    base = {"id": "dlv_1", "type": "payment.failed", "occurred_at": NOW.isoformat(),
            "gateway": "cashfree", "payment": payment, **kw}
    return json.dumps(base).encode()


def test_a_forwarded_gateway_code_is_translated_by_that_gateways_profile():
    result = recura.normalise(delivery(), "recura:dlv_1")

    risk = result.risk_event
    assert risk.razorpay_error.code == "Z9"
    assert risk.razorpay_error.reason == "insufficient_funds"
    assert risk.observed_at == NOW


def test_a_sender_that_speaks_the_taxonomy_needs_no_profile():
    body = delivery(gateway=None, payment={"code": None, "reason": "card_expired"})

    assert recura.normalise(body, None).risk_event.razorpay_error.reason == "card_expired"


@pytest.mark.parametrize("change, named", [
    ({"payment": {"amout_minor": 1}}, "amout_minor"),            # a typo is an error
    ({"occurred_at": "2026-03-10T11:00:00"}, "timezone"),
    ({"gateway": None}, "gateway"),
    ({"gateway": "no_such_gateway"}, "no PSP profile"),
    ({"type": "episode.ended", "end_reason": "bored"}, "end_reason"),
])
def test_a_delivery_that_does_not_say_what_it_means_is_refused_with_a_reason(change, named):
    result = recura.normalise(delivery(**change), None)

    assert result.outcome is Outcome.MALFORMED and named in result.note


def test_success_and_endings_in_the_neutral_format_stop_the_episode():
    paid = recura.normalise(delivery(type="payment.succeeded",
                                     payment={"links": ["order_1"]}), None)
    ended = recura.normalise(delivery(type="episode.ended", end_reason="dispute_opened"), None)

    assert paid.stop_reason == "payment_captured" and "order_1" in paid.correlation_keys
    assert ended.stop_reason == "dispute_opened"


# -- the receiver -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(api.SHADOW_DB_ENV, f"sqlite:///{tmp_path / 'gw.db'}")
    monkeypatch.setenv(api.API_KEY_ENV, "key_gw")
    for name in gateways():
        monkeypatch.setenv(secret_env(name), SECRET)
    monkeypatch.setattr(api, "_clock", VirtualClock(NOW))
    api._recorder_for.cache_clear()
    api._check_log_for.cache_clear()
    yield TestClient(api.app)
    api._recorder_for.cache_clear()
    api._check_log_for.cache_clear()


def post(client, gateway, body, header="stripe-signature"):
    return client.post(f"/webhooks/{gateway}", content=body,
                       headers={header: sign(body, SECRET, T)})


def test_a_stripe_failure_then_its_success_reads_as_paid_unaided(client):
    failed = post(client, "stripe", stripe_event())
    succeeded = stripe_event("payment_intent.succeeded", created=T + 600)
    paid = post(client, "stripe", succeeded.replace(b'"evt_1"', b'"evt_2"'))

    summary = client.get("/shadow/summary", headers={"Authorization": "Bearer key_gw"}).json()

    assert failed.json()["shadow"]["executed"] is False
    assert paid.json()["outcome"] == "stop"
    assert summary["paid_unaided"] == 1


def test_a_redelivered_stripe_event_is_a_duplicate(client):
    post(client, "stripe", stripe_event())

    assert post(client, "stripe", stripe_event()).json()["outcome"] == "duplicate"


def test_the_neutral_format_reaches_shadow_mode(client):
    r = post(client, "recura", delivery(), header="x-recura-signature")

    assert r.status_code == 200 and r.json()["outcome"] == "risk_event"


def test_an_unknown_gateway_is_named_not_guessed(client):
    r = client.post("/webhooks/paypal", content=b"{}")

    assert r.status_code == 404 and "stripe" in r.json()["detail"]


def test_a_gateway_with_no_secret_is_closed(client, monkeypatch):
    monkeypatch.delenv(secret_env("stripe"))

    r = post(client, "stripe", stripe_event())

    assert r.status_code == 503 and secret_env("stripe") in r.json()["detail"]


def test_a_stripe_body_signed_with_the_wrong_secret_is_refused(client):
    body = stripe_event()
    r = client.post("/webhooks/stripe", content=body,
                    headers={"stripe-signature": sign(body, "whsec_wrong", T)})

    assert r.status_code == 401


# -- one failure, every gateway -----------------------------------------------------


@pytest.mark.parametrize("gateway, code", [
    ("cashfree", "Z9"), ("phonepe", "INSUFFICIENT_BALANCE"), ("adyen", "12"),
    ("braintree", "2001"), ("checkout", "20051"), ("stripe", "insufficient_funds"),
])
def test_insufficient_funds_is_the_same_diagnosis_from_every_gateway(gateway, code):
    risk = recura.normalise(delivery(gateway=gateway, payment={"code": code}), None).risk_event

    assert classify(risk.razorpay_error).failure_class is FailureClass.FUNDS
    assert risk.razorpay_error.code == code


@pytest.mark.parametrize("gateway, code", [
    ("cashfree", "U16"), ("phonepe", "TXN_BLOCKED"), ("adyen", "20"),
    ("braintree", "2043"), ("checkout", "41301"), ("stripe", "fraudulent"),
])
def test_a_fraud_decline_is_never_retried_whichever_gateway_sent_it(gateway, code):
    risk = recura.normalise(delivery(gateway=gateway, payment={"code": code}), None).risk_event

    assert classify(risk.razorpay_error).failure_class is FailureClass.RISK_DECLINE

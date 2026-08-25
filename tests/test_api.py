"""API surface tests (CLAUDE.md sections 7, 10, 13)."""

import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import WEBHOOK_SECRET_ENV, app
from src.ingest.signature import expected_signature

SECRET = "whsec_api_test"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(WEBHOOK_SECRET_ENV, SECRET)
    import src.api.main as m
    m._store.seen.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _payload(event="payment.failed"):
    return json.dumps({
        "entity": "event", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_API0001", "amount": 150000, "currency": "INR", "status": "failed",
            "method": "card", "created_at": 1772000000,
            "error_reason": "card_expired", "error_source": "customer",
            "notes": {"customer_id": "cust_api"},
        }}}, "created_at": 1772000000,
    }).encode()


def _post(client, body, sig=None, event_id="evt_api_1"):
    return client.post(
        "/webhooks/razorpay", content=body,
        headers={
            "X-Razorpay-Signature": expected_signature(body, SECRET) if sig is None else sig,
            "x-razorpay-event-id": event_id,
            "Content-Type": "application/json",
        },
    )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["webhook_secret_configured"] is True


def test_valid_webhook_produces_a_risk_event(client):
    r = _post(client, _payload())
    assert r.status_code == 200
    assert r.json()["outcome"] == "risk_event"
    assert r.json()["amount_paise"] == 150000


def test_bad_signature_is_rejected(client):
    assert _post(client, _payload(), sig="deadbeef").status_code == 401


def test_tampered_body_is_rejected(client):
    """The signature is computed over the raw bytes we actually received."""
    body = _payload()
    good = expected_signature(body, SECRET)
    assert _post(client, body + b" ", sig=good).status_code == 401


def test_missing_secret_fails_closed(client, monkeypatch):
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
    assert _post(client, _payload()).status_code == 503


def test_late_authorisation_surfaces_as_a_stop(client):
    r = _post(client, _payload(event="payment.authorized"), event_id="evt_late")
    assert r.json()["outcome"] == "stop"
    assert r.json()["stop_reason"] == "late_authorisation"


def test_redelivery_is_idempotent(client):
    body = _payload()
    assert _post(client, body, event_id="evt_same").json()["outcome"] == "risk_event"
    assert _post(client, body, event_id="evt_same").json()["outcome"] == "duplicate"

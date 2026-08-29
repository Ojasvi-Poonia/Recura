"""FastAPI surface (CLAUDE.md section 10): /health plus the webhook receiver.

The one thing that matters here: the raw request body is read and verified BEFORE
any parsing. FastAPI's automatic JSON binding would re-serialise the payload and
break the HMAC, so the route takes a bare `Request` and reads bytes itself.

Secrets come from the environment, never from source (rules/common/security.md).
"""

from __future__ import annotations

import json
import logging
import os
import sys

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.ingest.signature import verify
from src.ingest.webhook import IdempotencyStore, Outcome, normalise

WEBHOOK_SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "msg": record.getMessage(), "logger": record.name}
        if isinstance(getattr(record, "extra_fields", None), dict):
            payload.update(record.extra_fields)
        return json.dumps(payload, sort_keys=True)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("recura")
log.setLevel(logging.INFO)
log.handlers = [handler]
log.propagate = False


def _log(msg: str, **fields) -> None:
    log.info(msg, extra={"extra_fields": fields})


app = FastAPI(title="Recura", version="0.1.0")
_store = IdempotencyStore()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "recura",
        "webhook_secret_configured": bool(os.getenv(WEBHOOK_SECRET_ENV)),
    }


@app.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
) -> dict:
    secret = os.getenv(WEBHOOK_SECRET_ENV)
    if not secret:
        # Fail closed. An unverifiable webhook is never processed.
        raise HTTPException(status_code=503, detail=f"{WEBHOOK_SECRET_ENV} not configured")

    raw = await request.body()  # RAW bytes, before any parsing (section 7)
    if not verify(raw, x_razorpay_signature, secret):
        _log("webhook.signature_rejected", event_id=x_razorpay_event_id or None)
        raise HTTPException(status_code=401, detail="signature mismatch")

    result = normalise(raw, x_razorpay_event_id or None, _store)
    _log(
        "webhook.received",
        outcome=result.outcome.value,
        event_id=x_razorpay_event_id or None,
        stop_reason=result.stop_reason,
        note=result.note or None,
    )

    # A stop signal is information, not an error: it is how late authorisation
    # and payment-received are surfaced to the episode loop (section 7).
    body: dict = {"outcome": result.outcome.value}
    if result.outcome is Outcome.RISK_EVENT and result.risk_event is not None:
        body["event_id"] = result.risk_event.event_id
        body["amount_paise"] = result.risk_event.amount_paise
    if result.stop_reason:
        body["stop_reason"] = result.stop_reason
    if result.note:
        body["note"] = result.note

    # A payload we could not normalise gets a 4xx, not a 200. Retrying will not change
    # the outcome - the body is the same body - and a 200 would tell Razorpay the event
    # was handled when it was not. The id is deliberately NOT in the idempotency store
    # (see webhook.normalise), so a corrected redelivery is still processed.
    if result.outcome is Outcome.MALFORMED:
        return JSONResponse(status_code=422, content=body)
    return body

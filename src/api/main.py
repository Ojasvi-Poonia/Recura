"""FastAPI surface (spec §10): /health, the webhook receiver, and shadow mode.

The one thing that matters here: the raw request body is read and verified BEFORE
any parsing. FastAPI's automatic JSON binding would re-serialise the payload and
break the HMAC, so the route takes a bare `Request` and reads bytes itself.

With RECURA_SHADOW_DB set, every verified failure is also decided on and written to
an append-only ledger, and nothing is executed (src/shadow.py). POST /policy/check puts
an action some other system wants to take to the same contract (src/policy/check.py).
Both need RECURA_API_KEY; with no key configured they are closed.

Secrets come from the environment, never from source (rules/common/security.md).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from src.agent import is_blocked
from src.clock import SystemClock
from src.ingest.signature import verify
from src.ingest.webhook import IdempotencyStore, IngestResult, Outcome, normalise
from src.ledger.store import Ledger
from src.policy.check import CheckResult, PolicyCheckLog, ProposedAction, check
from src.shadow import ShadowRecorder, ShadowSummary

WEBHOOK_SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"
SHADOW_DB_ENV = "RECURA_SHADOW_DB"
API_KEY_ENV = "RECURA_API_KEY"


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
_clock = SystemClock()   # the process edge: the one place real time is allowed in


@lru_cache(maxsize=4)
def _recorder_for(url: str) -> ShadowRecorder:
    return ShadowRecorder(Ledger(url=url))


def shadow_recorder() -> ShadowRecorder | None:
    """The shadow recorder, if shadow mode is on. One per database URL."""
    url = os.getenv(SHADOW_DB_ENV)
    return _recorder_for(url) if url else None


@lru_cache(maxsize=4)
def _check_log_for(url: str) -> PolicyCheckLog:
    return PolicyCheckLog(_recorder_for(url).ledger.engine)


def require_api_key(authorization: str = Header(default="")) -> None:
    """Bearer-token check for anything that reads recorded data. Fails closed."""
    expected = os.getenv(API_KEY_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail=f"{API_KEY_ENV} not configured")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid API key")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "recura",
        "webhook_secret_configured": bool(os.getenv(WEBHOOK_SECRET_ENV)),
        "shadow_mode": bool(os.getenv(SHADOW_DB_ENV)),
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

    event_id = x_razorpay_event_id or None
    shadow = shadow_recorder()
    # In shadow mode the dedupe record lives in the database, so a restart does not
    # turn Razorpay's redelivery into a second decision for the same failure.
    store = shadow if shadow is not None else _store

    if await run_in_threadpool(store.is_duplicate, event_id):
        result = IngestResult(Outcome.DUPLICATE, razorpay_event_id=event_id,
                              note="already processed")
    else:
        result = normalise(raw, event_id)

    body: dict = {"outcome": result.outcome.value}
    if shadow is not None:
        body.update(await run_in_threadpool(_shadow, shadow, result))

    # Remember the id LAST, after the shadow write as well as the normalisation. An
    # idempotency key records work completed: marking it earlier would let a failed
    # ledger write be answered with "duplicate" on every redelivery and lost for good.
    if result.outcome is Outcome.RISK_EVENT:
        await run_in_threadpool(store.remember, event_id)

    _log(
        "webhook.received",
        outcome=result.outcome.value,
        event_id=event_id,
        stop_reason=result.stop_reason,
        note=result.note or None,
    )

    # A stop signal is information, not an error: it is how late authorisation
    # and payment-received are surfaced to the episode loop (section 7).
    if result.outcome is Outcome.RISK_EVENT and result.risk_event is not None:
        body["event_id"] = result.risk_event.event_id
        body["amount_paise"] = result.risk_event.amount_paise
    if result.stop_reason:
        body["stop_reason"] = result.stop_reason
    if result.note:
        body["note"] = result.note

    # A payload we could not normalise gets a 4xx, not a 200. Retrying will not change
    # the outcome - the body is the same body - and a 200 would tell Razorpay the event
    # was handled when it was not. The id is deliberately NOT in the idempotency store,
    # so a corrected redelivery is still processed.
    if result.outcome is Outcome.MALFORMED:
        return JSONResponse(status_code=422, content=body)
    return body


def _shadow(shadow: ShadowRecorder, result: IngestResult) -> dict:
    """Record one verified webhook in shadow mode. Returns what to add to the reply."""
    if result.outcome is Outcome.RISK_EVENT and result.risk_event is not None:
        decision, verdict = shadow.record(result.risk_event, result.correlation_keys)
        blocked = is_blocked(decision, verdict)
        return {"shadow": {
            "would_do": decision.action.value,
            "blocked": blocked,
            "blocked_by": [r.rule_id for r in verdict.rules_blocked] if blocked else [],
            "executed": False,
        }}
    if result.outcome is Outcome.STOP and result.stop_reason:
        shadow.stop(result.correlation_keys, result.stop_reason, result.occurred_at)
    return {}


@app.get("/shadow/summary", response_model=ShadowSummary,
         dependencies=[Depends(require_api_key)])
def shadow_summary() -> ShadowSummary:
    """What shadow mode has seen so far. Needs `Authorization: Bearer $RECURA_API_KEY`."""
    shadow = shadow_recorder()
    if shadow is None:
        raise HTTPException(status_code=503, detail=f"shadow mode is off: set {SHADOW_DB_ENV}")
    return shadow.summary()


@app.post("/policy/check", response_model=CheckResult,
          dependencies=[Depends(require_api_key)])
def policy_check(proposal: ProposedAction) -> CheckResult:
    """May this action be taken? Answered by policy.yaml, never by a model.

    Recorded in the same database as shadow mode when RECURA_SHADOW_DB is set; the reply
    says whether it was (`logged`). Nothing is executed.
    """
    url = os.getenv(SHADOW_DB_ENV)
    return check(proposal, _clock.now(), _check_log_for(url) if url else None)

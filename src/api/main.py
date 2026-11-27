"""FastAPI surface (spec §10): /health, the webhook receiver, and shadow mode.

The one thing that matters here: the raw request body is read and verified BEFORE
any parsing. FastAPI's automatic JSON binding would re-serialise the payload and
break the HMAC, so the route takes a bare `Request` and reads bytes itself.

With RECURA_SHADOW_DB set, every verified failure is also decided on and written to
an append-only ledger, and nothing is executed (src/shadow.py). POST /policy/check puts
an action some other system wants to take to the same contract, and POST /policy/contacts
records the contacts it then makes (src/policy/check.py). All of these need
RECURA_API_KEY; with no key configured they are closed.

Secrets come from the environment, never from source (rules/common/security.md).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from src.agent import is_blocked
from src.clock import SystemClock
from src.ingest.gateways import gateways, get_gateway, secret_env, secret_for
from src.ingest.webhook import IdempotencyStore, IngestResult, Outcome
from src.ledger.store import Ledger
from src.policy.check import (
    MAX_ID_LENGTH,
    CheckResult,
    ConfirmedContact,
    ContactEntry,
    ContactRecorded,
    ContactRejected,
    PolicyCheckLog,
    ProposedAction,
    check,
)
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
def _engine_for(url: str) -> Engine:
    """One engine per database URL, shared by shadow mode and the contract check."""
    return create_engine(url, future=True)


@lru_cache(maxsize=4)
def _recorder_for(url: str) -> ShadowRecorder:
    return ShadowRecorder(Ledger(url=url, engine=_engine_for(url)))


def shadow_recorder() -> ShadowRecorder | None:
    """The shadow recorder, if shadow mode is on. One per database URL."""
    url = os.getenv(SHADOW_DB_ENV)
    return _recorder_for(url) if url else None


@lru_cache(maxsize=4)
def _check_log_for(url: str) -> PolicyCheckLog:
    # Not through the shadow recorder: that builds an agent, with its priors and LLM
    # provider, and the contract check must answer without either.
    return PolicyCheckLog(_engine_for(url))


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
        "gateways_configured": sorted(name for name, g in gateways().items()
                                      if secret_for(g)),
        "shadow_mode": bool(os.getenv(SHADOW_DB_ENV)),
    }


@app.post("/webhooks/{gateway_name}")
async def gateway_webhook(gateway_name: str, request: Request) -> dict:
    """One receiver for every gateway: razorpay, stripe, or the neutral `recura` format.

    Each gateway's own signature scheme is checked over the raw bytes before anything
    is parsed (src/ingest/gateways/). An unknown gateway is a 404, and a known one with
    no secret configured is a 503: an unverifiable webhook is never processed.
    """
    gateway = get_gateway(gateway_name)
    if gateway is None:
        raise HTTPException(status_code=404,
                            detail=f"no gateway named {gateway_name!r}; known: "
                                   f"{', '.join(sorted(gateways()))}")
    secret = secret_for(gateway)
    if not secret:
        env = WEBHOOK_SECRET_ENV if gateway.name == "razorpay" else secret_env(gateway.name)
        raise HTTPException(status_code=503, detail=f"{env} not configured")

    raw = await request.body()  # RAW bytes, before any parsing (section 7)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not gateway.verify(raw, headers, secret, _clock.now()):
        _log("webhook.signature_rejected", gateway=gateway.name)
        raise HTTPException(status_code=401, detail="signature mismatch")

    # Namespaced, so two gateways that happen to use the same id never collide.
    own_id = gateway.delivery_id(raw, headers)
    event_id = (own_id if gateway.name == "razorpay" or own_id is None
                else f"{gateway.name}:{own_id}")
    shadow = shadow_recorder()
    # In shadow mode the dedupe record lives in the database, so a restart does not
    # turn a redelivery into a second decision for the same failure.
    store = shadow if shadow is not None else _store

    if await run_in_threadpool(store.is_duplicate, event_id):
        result = IngestResult(Outcome.DUPLICATE, razorpay_event_id=event_id,
                              note="already processed")
    else:
        result = gateway.normalise(raw, event_id)

    shadowed = (await run_in_threadpool(_shadow, shadow, result, event_id)
                if shadow is not None else {})
    if shadowed is None:
        # Another delivery of this event was recorded while this one was being read.
        result = IngestResult(Outcome.DUPLICATE, razorpay_event_id=event_id,
                              note="already processed")
    body: dict = {"outcome": result.outcome.value, **(shadowed or {})}

    # Remember the id LAST, after the normalisation. An idempotency key records work
    # completed: marking it earlier would let a failed write be answered with
    # "duplicate" on every redelivery and lost for good. In shadow mode the receipt is
    # written with the decision, in the same transaction.
    if result.outcome is Outcome.RISK_EVENT and shadow is None:
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


def _shadow(shadow: ShadowRecorder, result: IngestResult,
            receipt_id: str | None) -> dict | None:
    """Record one verified webhook in shadow mode. Returns what to add to the reply,
    or None if this delivery turned out to be a duplicate."""
    if result.outcome is Outcome.RISK_EVENT and result.risk_event is not None:
        recorded = shadow.record(result.risk_event, result.correlation_keys, receipt_id)
        if recorded is None:
            return None
        decision, verdict = recorded
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


def check_log() -> PolicyCheckLog | None:
    url = os.getenv(SHADOW_DB_ENV)
    return _check_log_for(url) if url else None


def require_check_log() -> PolicyCheckLog:
    log = check_log()
    if log is None:
        raise HTTPException(status_code=503,
                            detail=f"no contact record is kept: set {SHADOW_DB_ENV}")
    return log


CheckLogDep = Annotated[PolicyCheckLog, Depends(require_check_log)]


@app.post("/policy/check", response_model=CheckResult,
          dependencies=[Depends(require_api_key)])
def policy_check(proposal: ProposedAction) -> CheckResult:
    """May this action be taken? Answered by policy.yaml, never by a model.

    Recorded in the same database as shadow mode when RECURA_SHADOW_DB is set; the reply
    says whether it was (`logged`), and counts contacts from that record as well as from
    what the caller reports. Nothing is executed.
    """
    return check(proposal, _clock.now(), check_log())


@app.post("/policy/contacts", response_model=ContactRecorded,
          dependencies=[Depends(require_api_key)])
def policy_contact(contact: ConfirmedContact, log: CheckLogDep) -> ContactRecorded:
    """Record a contact the caller has made, so later checks count it."""
    try:
        return log.confirm(contact, _clock.now())
    except ContactRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/policy/customers/{customer_id}/contacts", response_model=list[ContactEntry],
         dependencies=[Depends(require_api_key)])
def customer_contacts(
    customer_id: Annotated[str, Path(min_length=1, max_length=MAX_ID_LENGTH)],
    log: CheckLogDep,
) -> list[ContactEntry]:
    """Every recorded contact for one customer, each judged against the contract."""
    return log.contacts(customer_id)

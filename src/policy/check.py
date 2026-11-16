"""Ask the contract about an action another system wants to take.

Recura's own loop is not the only thing that contacts a customer about a failed payment.
A merchant's dunning tool does, their collections vendor does, a voice bot does. Each has
its own idea of the rules, and none of them can show afterwards that it kept to them.

This puts the same deterministic gate in front of any of them. The caller says what it
wants to do and what it knows about the customer; the contract in policy.yaml answers
allowed or blocked, with the rule and a reason a person can read, and the question and
the answer are written to a table the database refuses to update or delete.

    POST /policy/check      Authorization: Bearer $RECURA_API_KEY

THE GATE IS ONLY AS GOOD AS THE HISTORY IT IS GIVEN
This endpoint keeps no contact history of its own. If the caller says the customer has
been contacted once this week when it was four times, the answer will be "allowed" and
it will be wrong. The record shows what was asked, so the discrepancy is at least
visible later - but it is the caller's history that makes the answer true.

No model is involved anywhere on this path, and nothing here executes the action.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select
from sqlalchemy.engine import Engine

from src.act.costs import direct_cost_paise
from src.ledger.store import make_append_only
from src.market import get_market
from src.models import (
    ActionType,
    BlockedRule,
    Channel,
    Decision,
    ErrorObject,
    PolicyVerdict,
    SourceType,
)
from src.policy.engine import EpisodeState, evaluate
from src.taxonomy.mapping import classify

MAX_ID_LENGTH = 64

metadata = MetaData()

policy_checks = Table(
    "policy_checks", metadata,
    Column("check_id", String(32), primary_key=True),
    Column("checked_at", String(40), nullable=False),
    Column("event_id", String(MAX_ID_LENGTH), nullable=False, index=True),
    Column("customer_id", String(MAX_ID_LENGTH), nullable=False, index=True),
    Column("action", String(24), nullable=False),
    Column("allowed", Integer, nullable=False),
    Column("blocked_rules", Text, nullable=False),
    Column("request", Text, nullable=False),
)


class _Request(BaseModel):
    # A misspelt field must be an error, not a silently ignored fact about the customer.
    model_config = ConfigDict(extra="forbid")


class CustomerState(_Request):
    """What the caller knows about this customer and this episode. All of it observable."""

    contacts_last_7d: int = Field(default=0, ge=0)
    last_contact_at: datetime | None = None
    consented_channels: tuple[Channel, ...] = ()
    opted_out: bool = False
    attempts_made: int = Field(default=0, ge=0)
    attempts_this_mandate_cycle: int = Field(default=0, ge=0)
    pre_debit_notice_sent_at: datetime | None = None
    paid: bool = False
    disputed: bool = False
    late_authorised: bool = False
    broken_promise_to_pay: bool = False
    merchant_actions_today: int = Field(default=0, ge=0)
    merchant_spend_today_paise: int = Field(default=0, ge=0)
    escalations_today: int = Field(default=0, ge=0)


class ProposedAction(_Request):
    event_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    customer_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    action: ActionType
    amount_paise: int = Field(ge=0)
    episode_started_at: datetime
    source_type: SourceType = "payment"
    # The gateway's reason for the failure. It decides whether this is the customer's
    # problem at all, and whether a retry can ever succeed.
    failure_reason: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    failure_source: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    channel: Channel | None = None
    template_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    scheduled_at: datetime | None = None
    customer: CustomerState = Field(default_factory=CustomerState)


class CheckResult(BaseModel):
    check_id: str
    checked_at: datetime
    allowed: bool
    blocked_by: tuple[BlockedRule, ...]
    # Set when the contract permits the contact but not at the proposed time.
    send_at: datetime | None = None
    # Set when the contract requires a different action, such as human review.
    required_action: ActionType | None = None
    rules_evaluated: int
    logged: bool


def _aware(value: datetime | None) -> datetime | None:
    """A time with no zone is read in the market's zone, as the importer reads it."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=get_market().timezone)


def verdict_for(proposal: ProposedAction, now: datetime,
                policy: dict | None = None) -> PolicyVerdict:
    """Put a proposed action to the contract. Pure: no clock, no storage."""
    error = (ErrorObject(reason=proposal.failure_reason, source=proposal.failure_source)
             if proposal.failure_reason else None)
    mapping = classify(error, proposal.source_type)
    scheduled = _aware(proposal.scheduled_at)

    params: dict = {"amount_paise": proposal.amount_paise,
                    "source_type": proposal.source_type}
    if proposal.channel is not None:
        params["channel"] = proposal.channel.value
    if proposal.template_id:
        params["template_id"] = proposal.template_id
    if scheduled is not None:
        params["scheduled_at"] = scheduled.isoformat()

    decision = Decision(
        event_id=proposal.event_id, failure_class=mapping.failure_class,
        recoverability=mapping.recoverability,
        root_cause=(mapping.note or mapping.reason)[:200],
        action=proposal.action, params=params, expected_value_paise=0,
        p_recover=0.0, confidence=0.0, rationale="proposed by an external system",
        considered=(), decided_at=now,
    )
    known = proposal.customer
    # A nudge with no channel cannot be priced, and the consent and template rules will
    # already have nothing to check it against. Cost it at zero rather than raise.
    priceable = proposal.action is not ActionType.NUDGE or proposal.channel is not None
    state = EpisodeState(
        event_id=proposal.event_id,
        episode_started_at=_aware(proposal.episode_started_at),
        attempts_made=known.attempts_made,
        attempts_this_mandate_cycle=known.attempts_this_mandate_cycle,
        contacts_last_7d=known.contacts_last_7d,
        last_contact_at=_aware(known.last_contact_at),
        consented_channels=known.consented_channels,
        opted_out=known.opted_out, paid=known.paid, disputed=known.disputed,
        late_authorised=known.late_authorised,
        pre_debit_notice_sent_at=_aware(known.pre_debit_notice_sent_at),
        merchant_actions_today=known.merchant_actions_today,
        escalations_today=known.escalations_today,
        broken_promise_to_pay=known.broken_promise_to_pay,
        merchant_spend_today_paise=known.merchant_spend_today_paise,
        action_cost_paise=(direct_cost_paise(proposal.action, proposal.channel)
                           if priceable else 0),
    )
    return evaluate(decision, state, now, policy)


@dataclass
class PolicyCheckLog:
    """Where every question and answer is kept. Insert-only, enforced by triggers."""

    engine: Engine

    def __post_init__(self) -> None:
        metadata.create_all(self.engine)
        make_append_only(self.engine, policy_checks.name)

    def record(self, check_id: str, proposal: ProposedAction, verdict: PolicyVerdict,
               now: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(policy_checks.insert().values(
                check_id=check_id, checked_at=now.isoformat(),
                event_id=proposal.event_id, customer_id=proposal.customer_id,
                action=proposal.action.value, allowed=int(verdict.allowed),
                blocked_rules=json.dumps([r.rule_id for r in verdict.rules_blocked]),
                request=proposal.model_dump_json(),
            ))

    def for_customer(self, customer_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(policy_checks).where(policy_checks.c.customer_id == customer_id)
                .order_by(policy_checks.c.checked_at)).mappings().all()
        return [dict(r) for r in rows]


def check(proposal: ProposedAction, now: datetime, log: PolicyCheckLog | None = None,
          policy: dict | None = None) -> CheckResult:
    """Answer, and write the answer down if there is somewhere to write it.

    The record is written BEFORE the answer is returned. A caller that is told "allowed"
    and then acts must never be able to hold an answer the log does not contain.
    """
    verdict = verdict_for(proposal, now, policy)
    check_id = uuid.uuid4().hex
    if log is not None:
        log.record(check_id, proposal, verdict, now)

    # A new send time is only offered for an action that may be sent at all. Returning
    # one beside "blocked" reads as "try again at nine", which is not what was decided.
    shifted = (verdict.modified_params or {}).get("scheduled_at") if verdict.allowed else None
    if isinstance(shifted, str):
        shifted = datetime.fromisoformat(shifted)
    return CheckResult(
        check_id=check_id, checked_at=now, allowed=verdict.allowed,
        blocked_by=verdict.rules_blocked, send_at=shifted,
        required_action=verdict.required_action,
        rules_evaluated=len(verdict.rules_evaluated), logged=log is not None,
    )

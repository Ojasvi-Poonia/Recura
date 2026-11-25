"""Ask the contract about an action another system wants to take.

Recura's own loop is not the only thing that contacts a customer about a failed payment.
A merchant's dunning tool does, their collections vendor does, a voice bot does. Each has
its own idea of the rules, and none of them can show afterwards that it kept to them.

This puts the same deterministic gate in front of any of them. The caller says what it
wants to do and what it knows about the customer; the contract in policy.yaml answers
allowed or blocked, with the rule and a reason a person can read, and the question and
the answer are written to a table the database refuses to update or delete.

    POST /policy/check                               may this action be taken?
    POST /policy/contacts                            this contact was made
    GET  /policy/customers/{customer_id}/contacts    every recorded contact, checked
                                                     against the contract
    Authorization: Bearer $RECURA_API_KEY on all three

THE HISTORY IS RECORDED, NOT ONLY REPORTED
A gate that counts contacts from what the caller tells it is only as honest as the
caller. So a caller confirms each contact it actually made, and the check counts the
7-day window and the gap since the last contact from that record. When the caller's
figure and the record disagree, the stricter one is used and the reply says the caller
under-reported. A caller can make the answer more cautious; it cannot make it less.

A contact confirmed against an allowed check is "checked". One confirmed without one is
still recorded and still counted - a contact that skipped the gate is the one an auditor
most needs to see - and the contact record flags it.

AN ALLOWED CONTACT HOLDS ITS SLOT
Reading the record is not enough on its own: two senders that ask about the same
customer before either confirms would both be told yes. So a check that allows a contact
also holds that slot, from the time the contact was cleared for until HOLD_FOR after it.
Later checks count the hold as a contact, and a check refused only because of a hold
says so (`check.slot_held`) and names the check holding it. Confirming the contact
replaces the hold with the contact itself; a hold nobody confirms lapses. Checks for one
customer are answered one at a time, so the read and the hold cannot interleave.

WHAT THIS DOES NOT DO
That one-at-a-time ordering is a lock in the server process. It holds for `make serve`,
which runs one process; a deployment with several workers needs the lock in the database
instead, a per-customer row lock once this moves off SQLite. There is no early release:
a sender that is told yes and then decides not to send keeps the slot until it lapses.

No model is involved anywhere on this path, and nothing here executes the action.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

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
from src.policy.engine import EpisodeState, _hhmm, _in_quiet_hours, evaluate, load_policy
from src.taxonomy.mapping import classify

MAX_ID_LENGTH = 64
CONTACT_WINDOW = timedelta(days=7)
# A confirmation says a contact HAS happened. Allow for clock drift between the caller
# and us, and no more: a contact dated tomorrow is a plan, not a record.
MAX_CLOCK_SKEW = timedelta(minutes=5)
CONTACT_ACTIONS = frozenset({ActionType.NUDGE, ActionType.ESCALATE_HUMAN})
# How long an allowed contact keeps its slot past the time it was cleared for. Long
# enough to send one message and confirm it; short enough that a sender that asked and
# then never sent does not keep a customer from being contacted for the rest of the day.
HOLD_FOR = timedelta(minutes=15)
SLOT_HELD_RULE = "check.slot_held"
# Checks for one customer are serialised; checks for different customers mostly are not.
LOCK_STRIPES = 64

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
    Column("request", Text, nullable=False),        # what the caller said
    Column("history_used", Text, nullable=False),   # what the answer was based on
)

policy_contacts = Table(
    "policy_contacts", metadata,
    Column("contact_id", String(32), primary_key=True),
    Column("customer_id", String(MAX_ID_LENGTH), nullable=False, index=True),
    Column("event_id", String(MAX_ID_LENGTH), nullable=False),
    Column("channel", String(16), nullable=False),
    # UTC, so that the stored text sorts in time order whatever zone the caller used.
    Column("contacted_at", String(40), nullable=False),
    Column("recorded_at", String(40), nullable=False),
    # Unique: one allowed check is permission for one contact, not for a campaign.
    Column("check_id", String(32), unique=True),
    # The caller's own id for the message. Unique, so a retried confirmation is
    # recognised instead of counted twice.
    Column("contact_ref", String(MAX_ID_LENGTH), unique=True),
)

# A slot an allowed contact check holds. Never edited: a hold ends by being confirmed
# (a contact row cites its check_id) or by lapsing (expires_at passes).
policy_holds = Table(
    "policy_holds", metadata,
    Column("check_id", String(32), primary_key=True),
    Column("customer_id", String(MAX_ID_LENGTH), nullable=False, index=True),
    Column("due_at", String(40), nullable=False),       # UTC: when the contact may go
    Column("expires_at", String(40), nullable=False),   # UTC
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


class ConfirmedContact(_Request):
    """A contact the caller has made. Send it after the message goes, not before."""

    event_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    customer_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    channel: Channel
    contacted_at: datetime
    check_id: str | None = Field(default=None, max_length=32)
    contact_ref: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LENGTH)


class HistoryUsed(BaseModel):
    """Which contact history the answer rests on, and whether the caller's matched."""

    caller_contacts_last_7d: int
    recorded_contacts_last_7d: int | None    # None: no record is being kept
    contacts_last_7d: int                    # the one the contract was given
    last_contact_at: datetime | None     # including slots other checks hold
    caller_under_reported: bool          # from confirmed contacts only, never from holds
    held_contacts: int = 0               # allowed by another check, not yet confirmed
    held_until: datetime | None = None   # when the last of those holds lapses


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
    history: HistoryUsed


class ContactRecorded(BaseModel):
    contact_id: str
    checked: bool                  # confirmed against an allowed check
    already_recorded: bool         # a repeat of an earlier confirmation; not counted again
    recorded_contacts_last_7d: int


class ContactEntry(BaseModel):
    """One recorded contact, judged against the contract as it stands."""

    contact_id: str
    event_id: str
    channel: Channel
    contacted_at: datetime
    check_id: str | None
    checked: bool
    breaches: tuple[str, ...]      # rule ids this contact broke, from the record alone


@dataclass(frozen=True)
class Hold:
    check_id: str
    due_at: datetime
    expires_at: datetime


class ContactRejected(Exception):
    """A confirmation that would make the record untrue. The message says why."""


def _aware(value: datetime | None) -> datetime | None:
    """A time with no zone is read in the market's zone, as the importer reads it."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=get_market().timezone)


def _stored(value: datetime) -> str:
    return _aware(value).astimezone(UTC).isoformat()


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


def breaches_in(contacted: list[datetime], policy: dict | None = None
                ) -> list[tuple[str, ...]]:
    """For each contact, in time order, the contact rules it broke.

    Judged from the record alone, so only the rules a record can show: the contact
    window, the weekly cap, and the minimum gap. Consent and templates are not in it.
    """
    contact = (policy or load_policy())["contact"]
    quiet = contact["quiet_hours"]
    start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
    tz = get_market(quiet.get("tz_market", "IN")).timezone
    cap = contact["max_per_customer_per_7d"]
    min_gap = timedelta(hours=contact["min_hours_between"])

    times = sorted(contacted)
    out: list[tuple[str, ...]] = []
    for i, at in enumerate(times):
        broken: list[str] = []
        if _in_quiet_hours(at, start, end, tz):
            broken.append("contact.quiet_hours")
        # Including this one: the cap is broken by the contact that exceeds it.
        if sum(1 for t in times[: i + 1] if t > at - CONTACT_WINDOW) > cap:
            broken.append("contact.max_per_customer_per_7d")
        if i > 0 and at - times[i - 1] < min_gap:
            broken.append("contact.min_hours_between")
        out.append(tuple(broken))
    return out


@dataclass
class PolicyCheckLog:
    """Every question, every answer, every confirmed contact. Insert-only, by trigger."""

    engine: Engine
    _locks: tuple[threading.Lock, ...] = field(
        default_factory=lambda: tuple(threading.Lock() for _ in range(LOCK_STRIPES)),
        repr=False)

    def __post_init__(self) -> None:
        metadata.create_all(self.engine)
        for table in metadata.tables:
            make_append_only(self.engine, table)

    @contextmanager
    def turn(self, customer_id: str) -> Iterator[None]:
        """Answer one check for this customer at a time, from reading to holding."""
        with self._locks[zlib.crc32(customer_id.encode()) % len(self._locks)]:
            yield

    # -- checks ---------------------------------------------------------------------

    def record(self, check_id: str, proposal: ProposedAction, verdict: PolicyVerdict,
               history: HistoryUsed, now: datetime, hold: Hold | None = None) -> None:
        """Write the check, and the slot it holds if it allowed a contact, together."""
        with self.engine.begin() as conn:
            conn.execute(policy_checks.insert().values(
                check_id=check_id, checked_at=now.isoformat(),
                event_id=proposal.event_id, customer_id=proposal.customer_id,
                action=proposal.action.value, allowed=int(verdict.allowed),
                blocked_rules=json.dumps([r.rule_id for r in verdict.rules_blocked]),
                request=proposal.model_dump_json(),
                history_used=history.model_dump_json(),
            ))
            if hold is not None:
                conn.execute(policy_holds.insert().values(
                    check_id=check_id, customer_id=proposal.customer_id,
                    due_at=_stored(hold.due_at), expires_at=_stored(hold.expires_at)))

    def for_customer(self, customer_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(policy_checks).where(policy_checks.c.customer_id == customer_id)
                .order_by(policy_checks.c.checked_at)).mappings().all()
        return [dict(r) for r in rows]

    # -- contacts -------------------------------------------------------------------

    def _contact_times(self, customer_id: str) -> list[datetime]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(policy_contacts.c.contacted_at)
                .where(policy_contacts.c.customer_id == customer_id)).all()
        return sorted(datetime.fromisoformat(at) for (at,) in rows)

    def history(self, customer_id: str, now: datetime) -> tuple[int, datetime | None]:
        """(contacts in the 7 days to `now`, latest contact) from the record."""
        times = self._contact_times(customer_id)
        recent = sum(1 for t in times if now - CONTACT_WINDOW < t <= now + MAX_CLOCK_SKEW)
        # Stored in UTC for ordering; shown in the market's zone, like everything else.
        return recent, (times[-1].astimezone(get_market().timezone) if times else None)

    def holds(self, customer_id: str, now: datetime) -> list[Hold]:
        """Slots allowed for this customer that are neither confirmed nor lapsed."""
        confirmed = select(policy_contacts.c.check_id).where(
            policy_contacts.c.check_id.is_not(None))
        with self.engine.begin() as conn:
            rows = conn.execute(select(policy_holds).where(
                policy_holds.c.customer_id == customer_id,
                policy_holds.c.check_id.not_in(confirmed))).mappings().all()
        tz = get_market().timezone
        held = (Hold(check_id=r["check_id"],
                     due_at=datetime.fromisoformat(r["due_at"]).astimezone(tz),
                     expires_at=datetime.fromisoformat(r["expires_at"]).astimezone(tz))
                for r in rows)
        return sorted((h for h in held if h.expires_at > now), key=lambda h: h.due_at)

    def _check_row(self, check_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(policy_checks).where(
                policy_checks.c.check_id == check_id)).mappings().first()
        return dict(row) if row else None

    def _contact_by_ref(self, contact_ref: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(policy_contacts).where(
                policy_contacts.c.contact_ref == contact_ref)).mappings().first()
        return dict(row) if row else None

    def confirm(self, contact: ConfirmedContact, now: datetime) -> ContactRecorded:
        """Record a contact that happened. Raises ContactRejected if it cannot be true."""
        contacted_at = _aware(contact.contacted_at)
        if contacted_at > now + MAX_CLOCK_SKEW:
            raise ContactRejected(
                f"contacted_at {contacted_at.isoformat()} is in the future; confirm a "
                "contact after it is made, not when it is scheduled")

        if contact.contact_ref:
            earlier = self._contact_by_ref(contact.contact_ref)
            if earlier is not None:
                return self._repeat(earlier, contact, now)

        if contact.check_id:
            self._validate_check(contact)

        contact_id = uuid.uuid4().hex
        try:
            with self.engine.begin() as conn:
                conn.execute(policy_contacts.insert().values(
                    contact_id=contact_id, customer_id=contact.customer_id,
                    event_id=contact.event_id, channel=contact.channel.value,
                    contacted_at=_stored(contacted_at), recorded_at=now.isoformat(),
                    check_id=contact.check_id, contact_ref=contact.contact_ref,
                ))
        except IntegrityError as exc:
            # Lost a race with a concurrent confirmation of the same check or ref.
            if contact.contact_ref and (earlier := self._contact_by_ref(contact.contact_ref)):
                return self._repeat(earlier, contact, now)
            raise ContactRejected(
                f"check {contact.check_id} has already been used for a contact") from exc

        recent, _ = self.history(contact.customer_id, now)
        return ContactRecorded(contact_id=contact_id, checked=bool(contact.check_id),
                               already_recorded=False, recorded_contacts_last_7d=recent)

    def _repeat(self, earlier: dict, contact: ConfirmedContact,
                now: datetime) -> ContactRecorded:
        """A confirmation sent twice. Same answer, counted once - unless it is not the
        same contact, in which case reusing the ref is an error, not a retry."""
        same = (earlier["customer_id"] == contact.customer_id
                and earlier["event_id"] == contact.event_id
                and earlier["channel"] == contact.channel.value)
        if not same:
            raise ContactRejected(
                f"contact_ref {contact.contact_ref!r} already identifies a different contact")
        recent, _ = self.history(contact.customer_id, now)
        return ContactRecorded(contact_id=earlier["contact_id"],
                               checked=bool(earlier["check_id"]), already_recorded=True,
                               recorded_contacts_last_7d=recent)

    def _validate_check(self, contact: ConfirmedContact) -> None:
        row = self._check_row(contact.check_id)
        if row is None:
            raise ContactRejected(f"no check with id {contact.check_id}")
        if row["customer_id"] != contact.customer_id:
            raise ContactRejected(f"check {contact.check_id} was for a different customer")
        if ActionType(row["action"]) not in CONTACT_ACTIONS:
            raise ContactRejected(
                f"check {contact.check_id} was for {row['action']}, which is not a contact")
        if not row["allowed"]:
            raise ContactRejected(
                f"check {contact.check_id} was refused ({row['blocked_rules']}). Record "
                "the contact without a check_id if it was made anyway; it will be "
                "flagged as unchecked")

    def contacts(self, customer_id: str, policy: dict | None = None) -> list[ContactEntry]:
        """Every contact recorded for one customer, each judged against the contract."""
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(policy_contacts).where(policy_contacts.c.customer_id == customer_id)
                .order_by(policy_contacts.c.contacted_at)).mappings().all()
        return _judged([dict(r) for r in rows], policy)

    def all_contacts(self, policy: dict | None = None) -> list[tuple[str, ContactEntry]]:
        """(customer_id, entry) for every recorded contact, grouped by customer."""
        with self.engine.begin() as conn:
            rows = conn.execute(select(policy_contacts).order_by(
                policy_contacts.c.customer_id, policy_contacts.c.contacted_at)
            ).mappings().all()
        by_customer: dict[str, list[dict]] = {}
        for row in rows:
            by_customer.setdefault(row["customer_id"], []).append(dict(row))
        return [(customer, entry) for customer, group in by_customer.items()
                for entry in _judged(group, policy)]


def _judged(rows: list[dict], policy: dict | None) -> list[ContactEntry]:
    times = [datetime.fromisoformat(r["contacted_at"]) for r in rows]
    tz = get_market().timezone
    return [ContactEntry(
        contact_id=row["contact_id"], event_id=row["event_id"],
        channel=Channel(row["channel"]), contacted_at=at.astimezone(tz),
        check_id=row["check_id"], checked=bool(row["check_id"]), breaches=broken,
    ) for row, at, broken in zip(rows, times, breaches_in(times, policy), strict=True)]


def _history_used(proposal: ProposedAction, log: PolicyCheckLog | None,
                  now: datetime, holds: list[Hold]) -> HistoryUsed:
    known = proposal.customer
    caller_last = _aware(known.last_contact_at)
    if log is None:
        return HistoryUsed(caller_contacts_last_7d=known.contacts_last_7d,
                           recorded_contacts_last_7d=None,
                           contacts_last_7d=known.contacts_last_7d,
                           last_contact_at=caller_last, caller_under_reported=False)
    recorded, recorded_last = log.history(proposal.customer_id, now)
    held_last = holds[-1].due_at if holds else None
    latest = max((t for t in (caller_last, recorded_last, held_last) if t is not None),
                 default=None)
    return HistoryUsed(
        caller_contacts_last_7d=known.contacts_last_7d,
        recorded_contacts_last_7d=recorded,
        # The stricter of the two. A caller can make the answer more cautious, never less.
        # Holds are added to the record, not to the caller's figure: a sender that has
        # sent but not yet confirmed would count its own contact twice.
        contacts_last_7d=max(known.contacts_last_7d, recorded + len(holds)),
        last_contact_at=latest,
        caller_under_reported=(recorded > known.contacts_last_7d
                               or (recorded_last is not None
                                   and (caller_last is None or recorded_last > caller_last))),
        held_contacts=len(holds),
        held_until=max((h.expires_at for h in holds), default=None),
    )


def _given(proposal: ProposedAction, history: HistoryUsed) -> ProposedAction:
    """The proposal with the contact history the answer rests on in place of the caller's."""
    return proposal.model_copy(update={"customer": proposal.customer.model_copy(
        update={"contacts_last_7d": history.contacts_last_7d,
                "last_contact_at": history.last_contact_at})})


def _slot_held(holds: list[Hold]) -> BlockedRule:
    first, until = holds[0], max(h.expires_at for h in holds)
    return BlockedRule(
        rule_id=SLOT_HELD_RULE,
        reason=(f"Check {first.check_id} was allowed to contact this customer at "
                f"{first.due_at:%Y-%m-%d %H:%M} and has not confirmed it. The slot is held "
                f"until {until:%Y-%m-%d %H:%M}, or until that contact is confirmed."))


def _verdict(proposal: ProposedAction, log: PolicyCheckLog | None, now: datetime,
             policy: dict | None) -> tuple[PolicyVerdict, HistoryUsed]:
    holds = log.holds(proposal.customer_id, now) if log is not None else []
    history = _history_used(proposal, log, now, holds)
    verdict = verdict_for(_given(proposal, history), now, policy)
    if verdict.allowed or not holds:
        return verdict, history
    # Refused with holds counted. If the holds are the only reason, say that instead of
    # "last contact was 0.0h ago" about a contact that has not happened.
    unheld = _history_used(proposal, log, now, [])
    if verdict_for(_given(proposal, unheld), now, policy).allowed:
        verdict = verdict.model_copy(update={"rules_blocked": (_slot_held(holds),)})
    return verdict, history


def _hold(check_id: str, proposal: ProposedAction, verdict: PolicyVerdict,
          send_at: datetime | None, now: datetime) -> Hold | None:
    if not verdict.allowed or proposal.action not in CONTACT_ACTIONS:
        return None
    due = send_at or max(_aware(proposal.scheduled_at) or now, now)
    return Hold(check_id=check_id, due_at=due, expires_at=due + HOLD_FOR)


def check(proposal: ProposedAction, now: datetime, log: PolicyCheckLog | None = None,
          policy: dict | None = None) -> CheckResult:
    """Answer, and write the answer down if there is somewhere to write it.

    The record is written BEFORE the answer is returned. A caller that is told "allowed"
    and then acts must never be able to hold an answer the log does not contain. With a
    log, an allowed contact also holds its slot, written in the same transaction.
    """
    if log is None:
        return _answer(proposal, now, None, policy)
    with log.turn(proposal.customer_id):
        return _answer(proposal, now, log, policy)


def _answer(proposal: ProposedAction, now: datetime, log: PolicyCheckLog | None,
            policy: dict | None) -> CheckResult:
    verdict, history = _verdict(proposal, log, now, policy)
    # A new send time is only offered for an action that may be sent at all. Returning
    # one beside "blocked" reads as "try again at nine", which is not what was decided.
    shifted = (verdict.modified_params or {}).get("scheduled_at") if verdict.allowed else None
    if isinstance(shifted, str):
        shifted = datetime.fromisoformat(shifted)

    check_id = uuid.uuid4().hex
    if log is not None:
        log.record(check_id, proposal, verdict, history, now,
                   _hold(check_id, proposal, verdict, shifted, now))
    return CheckResult(
        check_id=check_id, checked_at=now, allowed=verdict.allowed,
        blocked_by=verdict.rules_blocked, send_at=shifted,
        required_action=verdict.required_action,
        rules_evaluated=len(verdict.rules_evaluated), logged=log is not None,
        history=history,
    )


EXPORT_COLUMNS = ("customer_id", "contact_id", "event_id", "channel", "contacted_at",
                  "checked", "check_id", "breaches")


def export_contacts(log: PolicyCheckLog, out: Path) -> tuple[int, int, int]:
    """Write every recorded contact to CSV. Returns (contacts, unchecked, with a breach)."""
    entries = log.all_contacts()
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(EXPORT_COLUMNS)
        for customer_id, e in entries:
            writer.writerow((customer_id, e.contact_id, e.event_id, e.channel.value,
                             e.contacted_at.isoformat(), "yes" if e.checked else "no",
                             e.check_id or "", ";".join(e.breaches)))
    return (len(entries), sum(1 for _, e in entries if not e.checked),
            sum(1 for _, e in entries if e.breaches))


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the recorded contact history")
    ap.add_argument("--db", required=True, help="SQLAlchemy URL of the policy-check database")
    ap.add_argument("--out", required=True, help="CSV file to write")
    args = ap.parse_args()
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        total, unchecked, breached = export_contacts(
            PolicyCheckLog(create_engine(args.db, future=True)), out)
    except OSError as exc:
        sys.exit(f"could not write {out}: {exc}")
    print(f"{total:,} contacts -> {out}  ({unchecked:,} made without an allowed check, "
          f"{breached:,} breaking a contact rule)")


if __name__ == "__main__":
    main()

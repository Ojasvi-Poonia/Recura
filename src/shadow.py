"""Shadow mode — decide on live traffic, send nothing, and see what comes back anyway.

A merchant will not let an untested system contact customers, and should not. Shadow mode
is the step before that. Point a webhook at Recura and for every failed payment it runs
the same decide and govern steps as the batch, writes the decision to the append-only
ledger, and stops there. No retry is attempted and no message is composed.

It also answers a question most merchants cannot answer today. Because nothing is sent,
every payment that arrives after a failure arrived under the merchant's EXISTING process.
That is the baseline any later recovery claim has to beat, measured on their own traffic
before a single customer has been treated.

    make serve SHADOW=1          receive webhooks, decide, log
    make shadow-report           what was seen, what would have been done, what came back

HOW A FAILURE IS JOINED TO THE PAYMENT THAT SETTLES IT
The retry that succeeds is a different payment from the one that failed. What they share
is the order, invoice or subscription they belong to, so every id on a payload is kept as
a correlation key. A stop counts for an episode only when it shares a key AND happened at
or after the failure - by the payload's own timestamps, never by arrival order, because
Razorpay does not guarantee delivery order. The time test is what stops last month's
`subscription.charged` from settling this month's halted subscription.

Everything here is insert-only. Closing an episode is a join, not an update.

WHAT IT DOES NOT DO
It does not learn: a decision that was never executed has no outcome to learn from. And
the recovery it reports is right-censored - an episode opened an hour ago has not had its
three weeks - so the summary gives the open count beside the rate rather than a rate alone.
"""

from __future__ import annotations

import argparse
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.exc import IntegrityError

from src.agent import Agent, is_blocked
from src.clock import Clock, SystemClock
from src.ingest.audit_report import BLOCKED_LABEL, Bucket, buckets
from src.ingest.webhook import SUCCESS_EVENTS
from src.ledger.store import Ledger, ledger_entries, make_entry
from src.market import get_market
from src.models import ActionResult, Decision, PolicyVerdict, Recoverability, RiskEvent

SHADOW_ARM = "shadow"
DEFAULT_DB_URL = "sqlite:///recura_shadow.db"
MONEY_ARRIVED = frozenset(SUCCESS_EVENTS.values())

metadata = MetaData()

episode_keys = Table(
    "shadow_episode_keys", metadata,
    Column("correlation_key", String(64), primary_key=True),
    Column("event_id", String(64), primary_key=True),
)

stops = Table(
    "shadow_stops", metadata,
    Column("correlation_key", String(64), primary_key=True),
    Column("stop_reason", String(40), primary_key=True),
    Column("occurred_at", String(40), primary_key=True),
)

receipts = Table(
    "webhook_receipts", metadata,
    Column("receipt_id", String(128), primary_key=True),
    Column("received_at", String(40), nullable=False),
)


@dataclass(frozen=True)
class ShadowSummary:
    """What shadow mode has seen. Amounts are minor units of `currency`."""

    currency: str
    episodes: int
    at_risk_minor: int
    # Paid with nothing sent: the merchant's baseline under their existing process.
    paid_unaided: int
    paid_unaided_minor: int
    ended_unpaid: int          # cancelled or disputed
    still_open: int            # no outcome yet - why the rate below is a floor
    actions: tuple[Bucket, ...]
    blocked_rules: tuple[Bucket, ...]
    merchant_bugs: tuple[Bucket, ...]
    stop_reasons: tuple[tuple[str, int], ...]
    executed: int = 0          # always 0; reported so the claim is checkable


@dataclass
class ShadowRecorder:
    """Decides, logs, and executes nothing. One instance per process."""

    ledger: Ledger
    clock: Clock = field(default_factory=SystemClock)
    agent: Agent = field(default_factory=Agent.for_preview)
    # The agent and the sequence counter are not safe to interleave, and webhooks are
    # handled on a thread pool.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        metadata.create_all(self.ledger.engine)

    # -- idempotency, same interface as ingest.webhook.IdempotencyStore -----------

    def is_duplicate(self, receipt_id: str | None) -> bool:
        if not receipt_id:
            return False
        with self.ledger.engine.begin() as conn:
            return conn.execute(select(receipts.c.receipt_id).where(
                receipts.c.receipt_id == receipt_id)).first() is not None

    def remember(self, receipt_id: str | None) -> None:
        if receipt_id:
            self._insert(receipts, receipt_id=receipt_id,
                         received_at=self.clock.now().isoformat())

    # -- the two things a webhook can mean ----------------------------------------

    def record(self, event: RiskEvent,
               keys: tuple[str, ...] = ()) -> tuple[Decision, PolicyVerdict]:
        """Decide on one failure and write the decision down. Nothing is executed."""
        now = self.clock.now()
        with self._lock:
            decision, verdict = self.agent.preview(event, now)
            sequence = len(self.ledger.for_event(event.event_id))
            self.ledger.append(make_entry(
                entry_id=uuid.uuid5(uuid.NAMESPACE_OID,
                                    f"{SHADOW_ARM}:{event.event_id}:{sequence}").hex,
                event_id=event.event_id, arm=SHADOW_ARM, sequence_number=sequence,
                observed_state=event, decision=decision, policy_verdict=verdict,
                action_result=ActionResult(executed=False, action=decision.action),
                clock_time=now,
            ))
        for key in dict.fromkeys((event.event_id, *keys)):
            self._insert(episode_keys, correlation_key=key, event_id=event.event_id)
        return decision, verdict

    def stop(self, keys: tuple[str, ...], reason: str,
             occurred_at: datetime | None = None) -> None:
        """Record that money arrived, or that an episode ended some other way.

        Recorded whether or not a failure with these keys has been seen. Most stops
        belong to payments that never failed, but the failure this one settles may
        simply not have been delivered yet.
        """
        at = (occurred_at or self.clock.now()).isoformat()
        for key in keys:
            self._insert(stops, correlation_key=key, stop_reason=reason, occurred_at=at)

    def _insert(self, table: Table, **row: str) -> None:
        try:
            with self.ledger.engine.begin() as conn:
                conn.execute(table.insert().values(**row))
        except IntegrityError:
            pass   # a redelivery of something already recorded; the first row stands

    # -- reading it back ----------------------------------------------------------

    def summary(self) -> ShadowSummary:
        with self.ledger.engine.begin() as conn:
            entries = conn.execute(
                select(ledger_entries).where(ledger_entries.c.arm == SHADOW_ARM)
                .order_by(ledger_entries.c.event_id, ledger_entries.c.sequence_number)
            ).mappings().all()
            key_rows = conn.execute(select(episode_keys)).all()
            stop_rows = conn.execute(select(stops)).all()

        # One episode per event id; a later entry for the same id is the newer decision.
        latest = {row["event_id"]: row for row in entries}
        episodes = [_Episode.from_row(row) for row in latest.values()]
        outcomes = _outcomes(episodes, key_rows, stop_rows)

        paid = [e for e in episodes if outcomes.get(e.event_id) in MONEY_ARRIVED]
        ended_unpaid = [e for e in episodes
                        if e.event_id in outcomes and outcomes[e.event_id] not in MONEY_ARRIVED]
        return ShadowSummary(
            currency=get_market().currency.code,
            episodes=len(episodes),
            at_risk_minor=sum(e.amount for e in episodes),
            paid_unaided=len(paid),
            paid_unaided_minor=sum(e.amount for e in paid),
            ended_unpaid=len(ended_unpaid),
            still_open=len(episodes) - len(paid) - len(ended_unpaid),
            actions=buckets((e.action_label, e.amount) for e in episodes),
            blocked_rules=buckets((e.blocked_by, e.amount) for e in episodes if e.blocked_by),
            merchant_bugs=buckets((e.reason or "unspecified", e.amount)
                                  for e in episodes if e.merchant_bug),
            stop_reasons=tuple(sorted(Counter(outcomes.values()).items())),
            executed=sum(1 for e in episodes if e.executed),
        )


@dataclass(frozen=True)
class _Episode:
    """The few fields the summary needs, read from one ledger row."""

    event_id: str
    opened_at: datetime
    amount: int
    reason: str | None
    merchant_bug: bool
    action_label: str
    blocked_by: str | None
    executed: bool

    @classmethod
    def from_row(cls, row) -> _Episode:
        state = RiskEvent.model_validate_json(row["observed_state"])
        decision = Decision.model_validate_json(row["decision"])
        verdict = PolicyVerdict.model_validate_json(row["policy_verdict"])
        result = ActionResult.model_validate_json(row["action_result"])
        blocked = is_blocked(decision, verdict)
        return cls(
            event_id=row["event_id"],
            opened_at=state.observed_at,
            amount=state.amount_paise,
            reason=state.razorpay_error.reason if state.razorpay_error else None,
            merchant_bug=decision.recoverability is Recoverability.MERCHANT_CONFIG,
            action_label=BLOCKED_LABEL if blocked else decision.action.value,
            blocked_by=verdict.rules_blocked[0].rule_id if blocked else None,
            executed=result.executed,
        )


def _outcomes(episodes: list[_Episode], key_rows, stop_rows) -> dict[str, str]:
    """event_id -> how its episode ended, for the episodes that have ended.

    The earliest qualifying stop wins. Money arriving and a later cancellation is a
    recovered episode; a cancellation and then money is not.
    """
    stops_by_key: dict[str, list[tuple[datetime, str]]] = {}
    for key, reason, at in stop_rows:
        stops_by_key.setdefault(key, []).append((datetime.fromisoformat(at), reason))
    keys_by_event: dict[str, list[str]] = {}
    for key, event_id in key_rows:
        keys_by_event.setdefault(event_id, []).append(key)

    outcomes: dict[str, str] = {}
    for episode in episodes:
        after = [(at, reason)
                 for key in keys_by_event.get(episode.event_id, ())
                 for at, reason in stops_by_key.get(key, ())
                 if at >= episode.opened_at]
        if after:
            outcomes[episode.event_id] = min(after)[1]
    return outcomes


def render_text(summary: ShadowSummary) -> str:
    market = get_market()
    rule = "─" * 74
    lines = [
        "", rule, "  RECURA — shadow mode: decided and logged, nothing sent", rule,
        f"  failures seen            {summary.episodes:,}",
        f"  at risk                  {market.money(summary.at_risk_minor)}",
        f"  actions executed         {summary.executed:,}",
        "",
        "  WHAT CAME BACK WITH NOTHING SENT  (your existing process, not Recura)",
        (f"    paid                   {summary.paid_unaided:,}   "
         f"{market.money(summary.paid_unaided_minor)}"),
        f"    ended unpaid           {summary.ended_unpaid:,}",
        f"    still open             {summary.still_open:,}",
    ]
    if summary.episodes:
        share = 1000 * summary.paid_unaided // summary.episodes
        lines.append(f"    paid so far            {share // 10}.{share % 10}% of failures "
                     "- a floor while episodes are still open")
    for heading, rows in (("WHAT THE AGENT WOULD HAVE DONE FIRST", summary.actions),
                          ("WHAT THE POLICY CONTRACT REFUSED", summary.blocked_rules),
                          ("FAILURES CAUSED BY THE INTEGRATION", summary.merchant_bugs)):
        if rows:
            lines += ["", f"  {heading}"]
            lines += [f"    {b.label:<38}{b.events:>7,}  {market.money(b.amount_minor):>16}"
                      for b in rows]
    lines += [
        "", rule,
        "  Proposed actions start from priors learned on the synthetic benchmark, not",
        "  on this traffic. The paid-unaided figure is real: it is the baseline a",
        "  live test with a holdout would have to beat.", "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Summarise a shadow-mode ledger")
    ap.add_argument("--db", default=DEFAULT_DB_URL, help="SQLAlchemy URL of the shadow ledger")
    args = ap.parse_args()
    print(render_text(ShadowRecorder(Ledger(url=args.db)).summary()))


if __name__ == "__main__":
    main()

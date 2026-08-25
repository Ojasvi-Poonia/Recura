"""Deterministic policy engine (CLAUDE.md section 6).

No LLM involvement anywhere in this module. It evaluates `policy.yaml` - a contract a
merchant could read and sign - and returns pass/block with a human-readable reason for
every rule considered.

A blocked action is EVIDENCE, not an error. Blocks are written to the ledger and the
count of refusals is a headline metric (section 6, section 8).

Two rule kinds:
  - blocking     : the action may not happen at all
  - modifying    : the action may happen, but not as proposed (quiet hours shift the
                   send time forward rather than cancelling the nudge outright)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

from src.clock import IST
from src.models import (
    ActionType,
    BlockedRule,
    Channel,
    Decision,
    FailureClass,
    PolicyVerdict,
    Recoverability,
)

POLICY_PATH = Path(__file__).resolve().parents[2] / "policy.yaml"

CONTACT_ACTIONS = {ActionType.NUDGE, ActionType.ESCALATE_HUMAN}
RETRY_ACTIONS = {ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED, ActionType.SWITCH_METHOD}


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> dict:
    with Path(path or POLICY_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class EpisodeState:
    """Everything the policy engine needs to know. All observable."""

    event_id: str
    episode_started_at: datetime
    attempts_made: int = 0
    attempts_this_mandate_cycle: int = 0
    contacts_last_7d: int = 0
    last_contact_at: datetime | None = None
    consented_channels: tuple[Channel, ...] = ()
    opted_out: bool = False
    paid: bool = False
    disputed: bool = False
    late_authorised: bool = False
    pre_debit_notice_sent_at: datetime | None = None
    merchant_actions_today: int = 0
    escalations_today: int = 0
    broken_promise_to_pay: bool = False
    merchant_spend_today_paise: int = 0
    action_cost_paise: int = 0


@dataclass
class RuleOutcome:
    blocked: BlockedRule | None = None
    modified: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ctx:
    decision: Decision
    state: EpisodeState
    policy: dict
    now: datetime


def _block(rule_id: str, reason: str) -> BlockedRule:
    return BlockedRule(rule_id=rule_id, reason=reason)


Rule = Callable[[Ctx], RuleOutcome]
_RULES: list[tuple[str, Rule]] = []


def rule(rule_id: str):
    def wrap(fn: Rule) -> Rule:
        _RULES.append((rule_id, fn))
        return fn
    return wrap


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _in_quiet_hours(at: datetime, start: time, end: time) -> bool:
    """Quiet window wraps midnight: 19:00 -> 09:00."""
    t = at.astimezone(IST).time()
    return t >= start or t < end


def _next_allowed(at: datetime, end: time) -> datetime:
    at = at.astimezone(IST)
    candidate = at.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= at:
        candidate += timedelta(days=1)
    return candidate


def _scheduled_at(ctx: Ctx) -> datetime:
    value = ctx.decision.params.get("scheduled_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return ctx.now


# --------------------------------------------------------------------------
# Episode stop conditions - checked first; nothing proceeds past a stop.
# --------------------------------------------------------------------------


@rule("episode.stop_on_payment")
def _stop_on_payment(ctx: Ctx) -> RuleOutcome:
    if ctx.policy["episode"]["stop_on_payment"] and ctx.state.paid:
        return RuleOutcome(_block("episode.stop_on_payment",
                                       "Customer has already paid; the episode is closed."))
    return RuleOutcome()


@rule("episode.stop_on_late_authorisation")
def _stop_on_late_auth(ctx: Ctx) -> RuleOutcome:
    if ctx.policy["episode"].get("stop_on_late_authorisation") and ctx.state.late_authorised:
        return RuleOutcome(_block(
            "episode.stop_on_late_authorisation",
            "Late authorisation: payment.authorized arrived after the failure, so the "
            "money is already in. Contacting this customer would be a false dunning."))
    return RuleOutcome()


@rule("episode.stop_on_opt_out")
def _stop_on_opt_out(ctx: Ctx) -> RuleOutcome:
    if ctx.policy["episode"]["stop_on_opt_out"] and ctx.state.opted_out:
        return RuleOutcome(_block("episode.stop_on_opt_out",
                                       "Customer has opted out of contact."))
    return RuleOutcome()


@rule("episode.stop_on_dispute")
def _stop_on_dispute(ctx: Ctx) -> RuleOutcome:
    if ctx.policy["episode"]["stop_on_dispute"] and ctx.state.disputed:
        return RuleOutcome(_block("episode.stop_on_dispute",
                                       "A dispute is open on this payment."))
    return RuleOutcome()


@rule("episode.max_days")
def _max_days(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["episode"]["max_days"]
    age = (ctx.now - ctx.state.episode_started_at).days
    if age > limit:
        return RuleOutcome(_block(
            "episode.max_days",
            f"Episode is {age} days old; the policy limit is {limit} days."))
    return RuleOutcome()


# --------------------------------------------------------------------------
# Triage - a merchant integration bug must never reach a customer.
# --------------------------------------------------------------------------


@rule("retry.forbidden_for_recoverability")
def _forbidden_recoverability(ctx: Ctx) -> RuleOutcome:
    forbidden = set(ctx.policy["retry"].get("forbidden_for_recoverability", []))
    if ctx.decision.action is ActionType.ESCALATE_HUMAN:
        return RuleOutcome()  # escalation is the CORRECT response to a config bug
    if ctx.decision.recoverability.value in forbidden:
        return RuleOutcome(_block(
            "retry.forbidden_for_recoverability",
            f"{ctx.decision.recoverability.value}: not a customer-recoverable failure. "
            "Only escalation to a human is permitted."))
    return RuleOutcome()


@rule("retry.forbidden_for_classes")
def _forbidden_classes(ctx: Ctx) -> RuleOutcome:
    forbidden = set(ctx.policy["retry"]["forbidden_for_classes"])
    if ctx.decision.action in RETRY_ACTIONS and ctx.decision.failure_class.value in forbidden:
        if (ctx.decision.action is ActionType.SWITCH_METHOD
                and ctx.decision.failure_class is FailureClass.INSTRUMENT_INVALID):
            return RuleOutcome()  # switching rails IS the remedy for a dead instrument
        return RuleOutcome(_block(
            "retry.forbidden_for_classes",
            f"Retrying a {ctx.decision.failure_class.value} cannot succeed and "
            "consumes an attempt."))
    return RuleOutcome()


# --------------------------------------------------------------------------
# Retry limits
# --------------------------------------------------------------------------


@rule("retry.max_attempts_per_episode")
def _max_attempts(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["retry"]["max_attempts_per_episode"]
    if ctx.decision.action in RETRY_ACTIONS and ctx.state.attempts_made >= limit:
        return RuleOutcome(_block(
            "retry.max_attempts_per_episode",
            f"Already made {ctx.state.attempts_made} of {limit} permitted attempts."))
    return RuleOutcome()


@rule("retry.max_attempts_per_mandate_cycle")
def _max_mandate_attempts(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["retry"]["max_attempts_per_mandate_cycle"]
    if ctx.decision.action in RETRY_ACTIONS and ctx.state.attempts_this_mandate_cycle >= limit:
        return RuleOutcome(_block(
            "retry.max_attempts_per_mandate_cycle",
            f"Mandate cycle already has {ctx.state.attempts_this_mandate_cycle} "
            f"of {limit} permitted debit attempts."))
    return RuleOutcome()


@rule("retry.pre_debit_notification_hours")
def _pre_debit_notice(ctx: Ctx) -> RuleOutcome:
    """RBI Digital Payments - E-Mandate Framework, 2026 (21 Apr 2026).

    A mandate debit requires a pre-debit notification at least 24h beforehand.
    Retrying without one is a regulatory breach, not merely bad practice.
    """
    hours = ctx.policy["retry"]["pre_debit_notification_hours"]
    is_mandate_debit = (
        ctx.decision.action in RETRY_ACTIONS
        and ctx.decision.params.get("source_type") == "mandate"
    )
    if not is_mandate_debit:
        return RuleOutcome()
    sent = ctx.state.pre_debit_notice_sent_at
    if sent is None:
        return RuleOutcome(_block(
            "retry.pre_debit_notification_hours",
            f"E-mandate debit requires a pre-debit notice {hours}h in advance "
            "(RBI E-Mandate Framework 2026); none has been sent."))
    elapsed = (ctx.now - sent).total_seconds() / 3600.0
    if elapsed < hours:
        return RuleOutcome(_block(
            "retry.pre_debit_notification_hours",
            f"Pre-debit notice sent {elapsed:.1f}h ago; {hours}h required."))
    return RuleOutcome()


# --------------------------------------------------------------------------
# Contact limits
# --------------------------------------------------------------------------


@rule("contact.max_per_customer_per_7d")
def _max_contacts(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["contact"]["max_per_customer_per_7d"]
    if ctx.decision.action in CONTACT_ACTIONS and ctx.state.contacts_last_7d >= limit:
        return RuleOutcome(_block(
            "contact.max_per_customer_per_7d",
            f"Customer already contacted {ctx.state.contacts_last_7d} times in 7 days "
            f"(limit {limit})."))
    return RuleOutcome()


@rule("contact.min_hours_between")
def _min_gap(ctx: Ctx) -> RuleOutcome:
    hours = ctx.policy["contact"]["min_hours_between"]
    if ctx.decision.action not in CONTACT_ACTIONS or ctx.state.last_contact_at is None:
        return RuleOutcome()
    elapsed = (ctx.now - ctx.state.last_contact_at).total_seconds() / 3600.0
    if elapsed < hours:
        return RuleOutcome(_block(
            "contact.min_hours_between",
            f"Last contact was {elapsed:.1f}h ago; {hours}h minimum between contacts."))
    return RuleOutcome()


@rule("contact.require_consent")
def _consent(ctx: Ctx) -> RuleOutcome:
    needs = set(ctx.policy["contact"]["require_consent"])
    if ctx.decision.action is not ActionType.NUDGE:
        return RuleOutcome()
    channel = ctx.decision.params.get("channel")
    if channel in needs and channel not in {c.value for c in ctx.state.consented_channels}:
        return RuleOutcome(_block(
            "contact.require_consent",
            f"No consent on record for {channel}."))
    return RuleOutcome()


@rule("contact.require_registered_template")
def _template(ctx: Ctx) -> RuleOutcome:
    """TRAI DLT: commercial messaging requires a registered template."""
    needs = set(ctx.policy["contact"].get("require_registered_template", []))
    if ctx.decision.action is not ActionType.NUDGE:
        return RuleOutcome()
    if ctx.decision.params.get("channel") in needs and not ctx.decision.params.get("template_id"):
        return RuleOutcome(_block(
            "contact.require_registered_template",
            "TRAI DLT requires a registered template id for this channel; "
            "free-form copy may not be sent."))
    return RuleOutcome()


@rule("contact.quiet_hours")
def _quiet_hours(ctx: Ctx) -> RuleOutcome:
    """MODIFYING rule: shift the send forward rather than cancelling it.

    Window is 09:00-19:00 IST (TRAI intersected with RBI Fair Practices Code).
    Silent retries are exempt - they do not disturb anyone.
    """
    if ctx.decision.action not in CONTACT_ACTIONS:
        return RuleOutcome()
    cfg = ctx.policy["contact"]["quiet_hours"]
    start, end = _hhmm(cfg["start"]), _hhmm(cfg["end"])
    at = _scheduled_at(ctx)
    if _in_quiet_hours(at, start, end):
        shifted = _next_allowed(at, end)
        return RuleOutcome(modified={
            "scheduled_at": shifted,
            "_quiet_hours_shift": f"{at.isoformat()} -> {shifted.isoformat()}",
        })
    return RuleOutcome()


# --------------------------------------------------------------------------
# Merchant budgets
# --------------------------------------------------------------------------


@rule("merchant.daily_action_budget")
def _action_budget(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["merchant"]["daily_action_budget"]
    if ctx.decision.action is ActionType.NO_ACTION:
        return RuleOutcome()
    if ctx.state.merchant_actions_today >= limit:
        return RuleOutcome(_block(
            "merchant.daily_action_budget",
            f"Merchant daily action budget exhausted ({ctx.state.merchant_actions_today}/{limit})."))
    return RuleOutcome()


@rule("merchant.daily_spend_cap_paise")
def _spend_cap(ctx: Ctx) -> RuleOutcome:
    limit = ctx.policy["merchant"]["daily_spend_cap_paise"]
    projected = ctx.state.merchant_spend_today_paise + ctx.state.action_cost_paise
    if ctx.decision.action is not ActionType.NO_ACTION and projected > limit:
        return RuleOutcome(_block(
            "merchant.daily_spend_cap_paise",
            f"Action would take today's spend to {projected} paise, over the "
            f"{limit} paise cap."))
    return RuleOutcome()


@rule("escalation.after_broken_promise_to_pay")
def _broken_promise(ctx: Ctx) -> RuleOutcome:
    """A broken promise-to-pay ends automated chasing.

    The customer engaged, was given a window, and it closed unpaid. Continuing to send
    automated messages at that point is the behaviour collections regulation exists to
    prevent; the case belongs with a human.
    """
    if not ctx.policy["escalation"].get("after_broken_promise_to_pay"):
        return RuleOutcome()
    if not ctx.state.broken_promise_to_pay:
        return RuleOutcome()
    if ctx.decision.action in (ActionType.ESCALATE_HUMAN, ActionType.NO_ACTION):
        return RuleOutcome()
    return RuleOutcome(_block(
        "escalation.after_broken_promise_to_pay",
        "Promise-to-pay window closed unpaid; this case must go to a human rather "
        "than receive further automated contact."))


@rule("escalation.max_per_day")
def _escalation_capacity(ctx: Ctx) -> RuleOutcome:
    """Human review is a finite resource. Exceeding it is not a policy nicety."""
    limit = ctx.policy["escalation"].get("max_per_day")
    if limit is None or ctx.decision.action is not ActionType.ESCALATE_HUMAN:
        return RuleOutcome()
    if ctx.state.escalations_today >= limit:
        return RuleOutcome(_block(
            "escalation.max_per_day",
            f"Human review capacity for today is spent "
            f"({ctx.state.escalations_today}/{limit} cases)."))
    return RuleOutcome()


@rule("escalation.to_human_above_paise")
def _escalation_threshold(ctx: Ctx) -> RuleOutcome:
    """High-value cases must go to a human rather than be handled automatically."""
    threshold = ctx.policy["escalation"]["to_human_above_paise"]
    amount = int(ctx.decision.params.get("amount_paise", 0))
    if amount > threshold and ctx.decision.action not in (
        ActionType.ESCALATE_HUMAN, ActionType.NO_ACTION
    ):
        return RuleOutcome(_block(
            "escalation.to_human_above_paise",
            f"Amount {amount} paise exceeds the {threshold} paise automation ceiling; "
            "must be escalated to a human."))
    return RuleOutcome()


# --------------------------------------------------------------------------


def evaluate(decision: Decision, state: EpisodeState, now: datetime,
             policy: dict | None = None) -> PolicyVerdict:
    """Evaluate every rule. Deterministic, total, and never raises on policy grounds."""
    ctx = Ctx(decision=decision, state=state, policy=policy or load_policy(), now=now)
    evaluated: list[str] = []
    blocked: list[BlockedRule] = []
    modified: dict[str, Any] = {}

    for rule_id, fn in _RULES:
        evaluated.append(rule_id)
        outcome = fn(ctx)
        if outcome.blocked is not None:
            blocked.append(outcome.blocked)
        if outcome.modified:
            modified.update(outcome.modified)

    return PolicyVerdict(
        allowed=not blocked,
        rules_evaluated=tuple(evaluated),
        rules_blocked=tuple(blocked),
        modified_params=modified or None,
    )


def rule_ids() -> tuple[str, ...]:
    return tuple(rid for rid, _ in _RULES)

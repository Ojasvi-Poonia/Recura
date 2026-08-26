"""Expected-value computation and candidate generation (CLAUDE.md section 5).

The intellectual spine. For every rupee at risk the agent enumerates its options,
prices each one, and takes the argmax.

REFINEMENT TO SECTION 5's FORMULA - incremental, not absolute EV
-----------------------------------------------------------------
Section 5 writes:

    EV(action) = p_recover(action) x amount x margin - direct_cost - attention_cost
    if max(EV) < 0 -> NO_ACTION

Taken literally that stopping rule can never fire, because doing nothing still has a
positive absolute EV (some customers pay unprompted) and every candidate inherits that
same positive base. So we price the INCREMENT over doing nothing:

    EV(action) = (p_recover(action) - p_recover(NO_ACTION)) x amount x margin
                 - direct_cost(action) - attention_cost(customer, action)

Now EV(NO_ACTION) is exactly 0, "max(EV) < 0" means every option destroys value, and
`NO_ACTION` falls out as arithmetic rather than as a special case - which is what
section 5 actually wants. The argmax ranking is unchanged; only the zero point moves.
Both the absolute and incremental figures are logged so the ledger shows the working.

Action space is (type, time, channel) - timing is a first-class dimension (section 5).

HORIZON DISCOUNTING - why a naive increment overvalues every action
-------------------------------------------------------------------
An episode has several decision points. If we score an action as if this were the
customer's last chance, we credit it with recoveries that would have happened anyway at
a later step, and the agent buys interventions it does not need.

Over `k` remaining opportunities, with baseline per-step probability `b`:

    P(recover eventually | never act)        = 1 - (1-b)^k
    P(recover eventually | act now, then not) = 1 - (1-p)(1-b)^(k-1)
    true increment                            = (1-b)^(k-1) * (p - b)

So the honest value of acting now is the naive increment discounted by (1-b)^(k-1) -
the chance the customer does NOT recover on their own in the meantime. At b=0.26 with
four further chances that factor is 0.30: a naive scorer overstates every intervention
by roughly 3x. This is what was driving 2,264 human escalations, 97% of all spend, for
a lift of under 5 points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from src.act.costs import attention_cost_paise, direct_cost_paise, margin_bps
from src.decide.bandit import PropensityModel
from src.decide.multipliers import adjust
from src.market import Market, get_market
from src.models import (
    ActionType,
    CandidateEV,
    Channel,
    FailureClass,
    Recoverability,
    RiskEvent,
)
from src.taxonomy.mapping import ReasonMapping

# Rail alternatives are MARKET data, not code: UPI does not exist in Malaysia and
# FPX does not exist in India. See config/markets.yaml.

# Candidate retry offsets in hours. Deliberately small and legible.
RETRY_OFFSETS_H = (6.0, 24.0, 72.0)
NUDGE_OFFSETS_H = (0.0, 24.0)
EPISODE_HORIZON_H = 21 * 24.0   # policy.yaml episode.max_days


@dataclass(frozen=True)
class DecisionContext:
    """Everything the EV layer needs. All observable - no latents anywhere."""

    event: RiskEvent
    failure_class: FailureClass          # argmax of `class_beliefs`; used for logging
    recoverability: Recoverability
    mapping: ReasonMapping
    now: datetime
    # Diagnosis as a DISTRIBUTION. Defaults to all-mass-on-failure_class, which makes
    # the rules-only path a special case of the same arithmetic.
    class_beliefs: tuple[tuple[FailureClass, float], ...] = ()
    downtime_active: bool = False
    downtime_clears_in_h: float = 0.0
    attempts_made: int = 0
    market: Market = field(default_factory=get_market)
    steps_remaining: int = 1   # decision points left in this episode, including now


def _next_salary_window(now: datetime) -> datetime:
    """First of next month, 10:00 IST - the population-level replenishment proxy."""
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return now.replace(year=year, month=month, day=1, hour=10, minute=0,
                       second=0, microsecond=0)


# B2B receivables ageing ladder. Collections practice worldwide escalates by bucket,
# and the buckets are the same everywhere because they follow net terms.
AGEING_BUCKETS = ((0, "current"), (30, "30d"), (60, "60d"), (90, "90d_plus"))


def ageing_bucket(days_overdue: int) -> str:
    label = "current"
    for threshold, name in AGEING_BUCKETS:
        if days_overdue >= threshold:
            label = name
    return label


def _receivable_candidates(ctx: DecisionContext, base: dict) -> list[tuple[ActionType, dict]]:
    """Actions available on an overdue invoice.

    Deliberately NOT the payment action space. There is no failed charge to retry and
    no instrument to switch - nobody attempted a payment. What exists is a reminder
    ladder that escalates with ageing, which is how receivables are actually collected.
    """
    ev = ctx.event
    days = ev.days_overdue(ctx.now)
    bucket = ageing_bucket(days)
    out: list[tuple[ActionType, dict]] = []

    for channel in ev.customer_history.consented_channels:
        for offset in NUDGE_OFFSETS_H:
            out.append((ActionType.NUDGE, {
                **base, "channel": channel.value,
                "template_id": f"tpl_receivable_{bucket}",
                "language": ev.customer_history.language,
                "scheduled_at": ctx.now + timedelta(hours=offset),
                "days_overdue": days, "ageing_bucket": bucket,
            }))

    # Past 60 days a reminder has stopped working; this belongs with a person who can
    # renegotiate terms or place a credit hold.
    if days >= 60:
        out.append((ActionType.ESCALATE_HUMAN, {
            **base, "escalation_reason": f"receivable aged {bucket} ({days} days)",
            "days_overdue": days, "ageing_bucket": bucket}))
    return out


def candidate_actions(ctx: DecisionContext) -> list[tuple[ActionType, dict]]:
    """Enumerate the (type, time, channel) options worth pricing."""
    ev = ctx.event
    base = {"amount_paise": ev.amount_paise, "source_type": ev.source_type}
    out: list[tuple[ActionType, dict]] = [(ActionType.NO_ACTION, dict(base))]

    # A merchant integration bug is not a customer problem. Escalate, nothing else.
    if ctx.recoverability is not Recoverability.CUSTOMER_RECOVERABLE:
        out.append((ActionType.ESCALATE_HUMAN, {
            **base, "escalation_reason": f"{ctx.recoverability.value}: {ctx.mapping.reason}"}))
        return out

    # Receivables take the ageing ladder, not the gateway-retry path.
    if ev.source_type == "invoice":
        out.extend(_receivable_candidates(ctx, base))
        out.append((ActionType.ESCALATE_HUMAN, {
            **base, "escalation_reason": "high-value receivable"}))
        return out

    min_delay = ctx.mapping.min_retry_delay_hours or 0.0

    # Retries. Razorpay-stated cooldowns are honoured before anything else.
    if min_delay <= 0.0:
        out.append((ActionType.RETRY_NOW, dict(base)))
    for offset in RETRY_OFFSETS_H:
        if offset >= min_delay:
            out.append((ActionType.RETRY_SCHEDULED, {
                **base, "scheduled_at": ctx.now + timedelta(hours=offset)}))
    if ctx.downtime_active and ctx.downtime_clears_in_h > 0:
        out.append((ActionType.RETRY_SCHEDULED, {
            **base,
            "scheduled_at": ctx.now + timedelta(hours=ctx.downtime_clears_in_h + 0.5),
            "reason_hint": "waiting for bank downtime to clear"}))
    if ctx.failure_class is FailureClass.FUNDS:
        salary = _next_salary_window(ctx.now)
        # Only if it actually lands inside the episode. An action we could never take
        # is not a candidate, and scheduling past the horizon silently expired episodes.
        if (salary - ctx.now).total_seconds() / 3600.0 <= EPISODE_HORIZON_H:
            out.append((ActionType.RETRY_SCHEDULED, {
                **base, "scheduled_at": salary,
                "reason_hint": "aligned to salary-cycle replenishment"}))

    # Method switch - to a rail that actually exists in this market.
    for rail in ctx.market.alternatives_to(ev.method)[:1]:
        out.append((ActionType.SWITCH_METHOD, {**base, "suggested_rail": rail}))

    # Nudges, only on consented channels, only via a registered template.
    history = ev.customer_history
    for channel in history.consented_channels:
        for offset in NUDGE_OFFSETS_H:
            out.append((ActionType.NUDGE, {
                **base, "channel": channel.value,
                "template_id": f"tpl_{ctx.failure_class.value.lower()}_01",
                "language": history.language,
                "scheduled_at": ctx.now + timedelta(hours=offset),
            }))

    out.append((ActionType.ESCALATE_HUMAN, {
        **base, "escalation_reason": f"high-value {ctx.failure_class.value}"}))
    return out


def _p_for(ctx: DecisionContext, action: ActionType, params: dict,
           model: PropensityModel, rng: np.random.Generator, explore: bool) -> float:
    beliefs = ctx.class_beliefs or ((ctx.failure_class, 1.0),)
    raw = (model.sample_marginal(beliefs, action, rng) if explore
           else model.expected_marginal(beliefs, action))
    at = params.get("scheduled_at") or ctx.now
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    # Downtime only still bites if the action lands before it clears.
    hours_out = (at - ctx.now).total_seconds() / 3600.0
    still_down = ctx.downtime_active and hours_out < ctx.downtime_clears_in_h
    return adjust(
        raw, action=action, failure_class=ctx.failure_class,
        history=ctx.event.customer_history,
        attempt_number=ctx.event.attempt_number + ctx.attempts_made,
        at=at, downtime_active=still_down,
    )


def score_candidates(
    ctx: DecisionContext,
    model: PropensityModel,
    rng: np.random.Generator,
    explore: bool = True,
) -> list[CandidateEV]:
    """Price every candidate. Returns them ALL - section 4 requires logging runner-ups."""
    # Per-merchant margin, falling back to the configured default.
    #
    # This field existed on MerchantContext from the start and was silently ignored:
    # every merchant was priced at one global 30%. Margin multiplies straight through
    # every expected-value calculation, and Razorpay's merchants are not homogeneous -
    # a subscription SaaS and a food-delivery order have nothing like the same margin,
    # so the same failed payment is worth very different amounts to chase.
    merchant = ctx.event.merchant_context
    margin = (merchant.margin_bps if merchant is not None else margin_bps()) / 10_000.0
    amount = ctx.event.amount_paise
    contacts = ctx.event.customer_history.contacts_last_7d

    # The do-nothing baseline uses the posterior MEAN, never a Thompson draw.
    #
    # This is subtle and it matters: every candidate is scored as an INCREMENT over this
    # baseline. Drawing the baseline randomly too makes each increment a difference of
    # two independent random variables - mean zero, negative half the time - so the agent
    # refuses at random rather than on evidence. Exploration belongs on the action arms,
    # which is where the information actually is; the counterfactual is a fixed reference
    # point. (Caught by the ablation study: a random chooser was beating the optimiser.)
    p_no_action = _p_for(ctx, ActionType.NO_ACTION, {}, model, rng, explore=False)

    scored: list[CandidateEV] = []
    for action, params in candidate_actions(ctx):
        if action is ActionType.NO_ACTION:
            p = p_no_action
        else:
            p = _p_for(ctx, action, params, model, rng, explore)

        channel = params.get("channel")
        direct = direct_cost_paise(action, Channel(channel) if channel else None)
        attention = attention_cost_paise(action, contacts)

        # Incremental gross value: what acting buys us OVER doing nothing, discounted
        # by the chance the customer recovers unaided before the episode ends.
        horizon_discount = (1.0 - p_no_action) ** max(0, ctx.steps_remaining - 1)
        incremental_gross = int(round(
            (p - p_no_action) * horizon_discount * amount * margin))
        scored.append(CandidateEV(
            action=action,
            params={k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in params.items()},
            p_recover=round(p, 6),
            gross_value_paise=incremental_gross,
            direct_cost_paise=direct,
            attention_cost_paise=attention,
            expected_value_paise=incremental_gross - direct - attention,
        ))
    return scored


def choose(scored: list[CandidateEV]) -> CandidateEV:
    """argmax EV. NO_ACTION wins when nothing beats zero - derived, not special-cased."""
    best = max(scored, key=lambda c: (c.expected_value_paise, -c.direct_cost_paise))
    if best.expected_value_paise < 0:
        return next(c for c in scored if c.action is ActionType.NO_ACTION)
    return best

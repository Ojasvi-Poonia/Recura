"""Hidden latent state and the counterfactual response model.

SIMULATOR ONLY. Nothing under `src/` may import this module - that is asserted by
tests/test_invariants.py::test_agent_cannot_reach_latents. This is the mechanical
guarantee behind CLAUDE.md section 9.1 ("the agent must never read a latent field")
and section 9.5 ("no peeking").

Determinism (CLAUDE.md section 8): every random draw an outcome will ever need is
drawn ONCE at generation time and stored in `LatentState.draws`. Resolution is then a
pure function of (latent, action, time) with no RNG at all, so outcomes are identical
regardless of processing order, parallelism, or how many decisions an episode takes.

Every multiplier below carries a comment justifying it. A panel will ask (section 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models import ActionType, FailureClass

FC = FailureClass

# Baseline recovery with NO intervention, per true failure class.
# Source: eval/CALIBRATION.md section 3 (grade C - assumption, swept in Tier 3).
# CLAUDE.md section 9.2 requires this to be non-zero or the holdout is meaningless.
BASELINE_RECOVERY: dict[FailureClass, float] = {
    FC.TRANSIENT_INFRA: 0.45,     # rail self-heals; many customers simply retry
    FC.FUNDS: 0.30,               # salary lands; some pay unprompted
    FC.AUTH_ABANDON: 0.25,        # intent existed; some return unaided
    FC.LIMIT_EXCEEDED: 0.20,      # limit resets next cycle
    FC.INSTRUMENT_INVALID: 0.08,  # needs an explicit method change
    FC.RISK_DECLINE: 0.03,        # structurally blocked
    FC.UNKNOWN: 0.15,             # mixed bucket
}

# --- Action efficacy multipliers -------------------------------------------
# Applied to a per-class base. Each is a modelling choice, not a measurement.

RETRY_DEAD_INSTRUMENT = 0.02   # retrying an expired card cannot work, by construction
RETRY_RISK_DECLINE = 0.01      # issuer risk declines do not relent on retry
RETRY_ALIGNED = 2.05           # retry AFTER the blocking condition clears
RETRY_MISALIGNED = 0.45        # retry BEFORE it clears: worse than doing nothing,
                               # because it consumes one of 3 allowed attempts
RETRY_NEUTRAL = 1.15           # retry where timing is not the binding constraint

SWITCH_WHEN_DEAD = 3.40        # the only real remedy for a dead instrument
SWITCH_WHEN_ALIVE = 1.10       # mild help; adds friction when the rail was fine

NUDGE_INTENT_WEIGHT = 1.9      # a nudge converts intent; it cannot manufacture it
NUDGE_HOUR_MATCH_BONUS = 1.35  # sent in the customer's historical success hour
NUDGE_HOUR_MISS_PENALTY = 0.80 # sent outside it
ESCALATE_EFFICACY = 2.60       # a human is effective and expensive

# Superlinear fatigue: the Nth contact is worth less than the first.
FATIGUE_DECAY = 0.62           # efficacy multiplier per prior contact in the window

MAX_P = 0.97                   # nothing is certain


@dataclass(frozen=True)
class LatentState:
    """What is REALLY true about this event. The agent never sees any of it."""

    event_id: str
    true_failure_class: FailureClass  # may differ from what the emitted reason implies
    latent_intent: float              # 0-1: does this customer actually want to pay
    liquidity_day: int                # day-of-month funds replenish (FUNDS cases)
    instrument_dead: bool             # the rail is genuinely unusable
    downtime_clears_hours: float      # hours until the rail recovers (TRANSIENT_INFRA)
    annoyance_threshold: int          # contacts tolerated before opting out
    success_hour: int                 # hour (IST) this customer actually converts in
    draws: tuple[float, ...]          # pre-drawn uniforms, consumed by sequence number


@dataclass(frozen=True)
class Outcome:
    recovered: bool
    opted_out: bool
    p_used: float  # logged for eval diagnostics only; never shown to the agent


def _fatigue(prior_contacts: int) -> float:
    return FATIGUE_DECAY**prior_contacts


def success_probability(
    latent: LatentState,
    action: ActionType,
    at: datetime,
    hours_since_event: float,
    prior_contacts: int,
) -> float:
    """True probability this action recovers the money. Simulator-side only."""
    cls = latent.true_failure_class
    # Intent modulates everything. Beta(2,2) has mean 0.5, so this averages to 1.0
    # and leaves BASELINE_RECOVERY interpretable as a population average.
    intent_mult = 0.5 + latent.latent_intent
    base = BASELINE_RECOVERY[cls] * intent_mult

    if action is ActionType.NO_ACTION:
        return min(base, MAX_P)

    if action in (ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED):
        if latent.instrument_dead:
            return RETRY_DEAD_INSTRUMENT
        if cls is FC.RISK_DECLINE:
            return RETRY_RISK_DECLINE
        if cls is FC.TRANSIENT_INFRA:
            aligned = hours_since_event >= latent.downtime_clears_hours
            return min(base * (RETRY_ALIGNED if aligned else RETRY_MISALIGNED), MAX_P)
        if cls is FC.FUNDS:
            # Aligned means: the retry lands on or after the replenishment day.
            aligned = at.day >= latent.liquidity_day or hours_since_event >= 24 * 20
            return min(base * (RETRY_ALIGNED if aligned else RETRY_MISALIGNED), MAX_P)
        if cls is FC.LIMIT_EXCEEDED:
            aligned = hours_since_event >= 24.0  # daily/frequency caps reset
            return min(base * (RETRY_ALIGNED if aligned else RETRY_MISALIGNED), MAX_P)
        return min(base * RETRY_NEUTRAL, MAX_P)

    if action is ActionType.SWITCH_METHOD:
        mult = SWITCH_WHEN_DEAD if latent.instrument_dead else SWITCH_WHEN_ALIVE
        if cls is FC.RISK_DECLINE:
            mult = SWITCH_WHEN_ALIVE  # a risk decline follows the customer, not the rail
        return min(base * mult * _fatigue(prior_contacts), MAX_P)

    if action is ActionType.NUDGE:
        hour_mult = (
            NUDGE_HOUR_MATCH_BONUS
            if abs(at.hour - latent.success_hour) <= 1
            else NUDGE_HOUR_MISS_PENALTY
        )
        # A nudge converts existing intent. Against a dead instrument or a risk
        # decline it is close to useless - it asks for an action that cannot succeed.
        if latent.instrument_dead or cls is FC.RISK_DECLINE:
            return min(base * 1.05 * _fatigue(prior_contacts), MAX_P)
        mult = NUDGE_INTENT_WEIGHT * latent.latent_intent * hour_mult
        return min(base * mult * _fatigue(prior_contacts), MAX_P)

    if action is ActionType.ESCALATE_HUMAN:
        return min(base * ESCALATE_EFFICACY * _fatigue(prior_contacts), MAX_P)

    return min(base, MAX_P)


def resolve(
    latent: LatentState,
    action: ActionType,
    at: datetime,
    hours_since_event: float,
    prior_contacts: int,
    sequence_number: int,
) -> Outcome:
    """Pure function. Same inputs -> same outcome, always (CLAUDE.md section 8)."""
    p = success_probability(latent, action, at, hours_since_event, prior_contacts)
    draw = latent.draws[sequence_number % len(latent.draws)]
    recovered = draw < p

    # Contacting past the tolerance threshold destroys the relationship. This is the
    # real-world consequence the attention_cost term is meant to price.
    contacting = action in (ActionType.NUDGE, ActionType.ESCALATE_HUMAN)
    opted_out = (
        contacting and not recovered and prior_contacts + 1 > latent.annoyance_threshold
    )
    return Outcome(recovered=recovered, opted_out=opted_out, p_used=p)

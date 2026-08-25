"""Context multipliers applied to the sampled propensity (CLAUDE.md section 5).

Section 5: "Keep the multipliers in one file with a comment justifying each. A panel
will ask." This is that file.

IMPORTANT - these are the AGENT'S BELIEFS about how the world works. They are not the
world. The simulator's true response model (eval/latents.py) uses different values, and
the agent has no access to it. An agent whose model exactly matched the simulator would
be a circular experiment; the gap between these numbers and the true ones is precisely
what the agent has to overcome.
"""

from __future__ import annotations

from datetime import datetime

from src.models import ActionType, CustomerHistory, FailureClass

# Each additional prior attempt lowers the odds: the easy recoveries happen first, so
# what remains is progressively harder. Standard dunning attrition.
ATTEMPT_DECAY = 0.78

# Sending at an hour the customer has historically paid at. The effect is real but
# modest - people are not that predictable.
HOUR_MATCH_BONUS = 1.25
HOUR_MISS_PENALTY = 0.88
HOUR_MATCH_TOLERANCE = 1  # +/- 1 hour counts as a match

# Retrying into a live bank/PSP outage mostly fails; waiting for it to clear works.
# This is the Razorpay Downtime API's whole value to us (section 7).
DOWNTIME_ACTIVE_PENALTY = 0.35
DOWNTIME_CLEARED_BONUS = 1.60

# Each recent contact makes the next one land worse. Distinct from attention_cost,
# which prices the annoyance; this models the drop in effectiveness.
CONTACT_FATIGUE_DECAY = 0.70

# A customer who has recovered before is more likely to recover again.
PRIOR_RECOVERY_BONUS = 1.20
NEVER_RECOVERED_PENALTY = 0.90

# FUNDS cases recover around salary/replenishment time. We cannot see the customer's
# pay date, but early-month is the population-level proxy.
SALARY_WINDOW_DAYS = (1, 2, 3, 4, 5)
SALARY_WINDOW_BONUS = 1.45

CONTACT_ACTIONS = {ActionType.NUDGE, ActionType.ESCALATE_HUMAN}
RETRY_ACTIONS = {ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED}


def adjust(
    p: float,
    *,
    action: ActionType,
    failure_class: FailureClass,
    history: CustomerHistory,
    attempt_number: int,
    at: datetime,
    downtime_active: bool,
) -> float:
    """Apply every context multiplier to a sampled propensity. Clamped to [0, 0.97]."""
    adjusted = p

    # Attrition across attempts.
    adjusted *= ATTEMPT_DECAY ** max(0, attempt_number - 1)

    # Timing against the customer's observed success hours.
    if action in CONTACT_ACTIONS or action is ActionType.RETRY_SCHEDULED:
        hours = history.successful_payment_hours
        if hours:
            matched = any(abs(at.hour - h) <= HOUR_MATCH_TOLERANCE for h in hours)
            adjusted *= HOUR_MATCH_BONUS if matched else HOUR_MISS_PENALTY

    # Live downtime on the rail we are about to use.
    if action in RETRY_ACTIONS:
        adjusted *= DOWNTIME_ACTIVE_PENALTY if downtime_active else DOWNTIME_CLEARED_BONUS

    # Contact fatigue.
    if action in CONTACT_ACTIONS:
        adjusted *= CONTACT_FATIGUE_DECAY ** history.contacts_last_7d

    # Track record.
    if history.prior_recoveries > 0:
        adjusted *= PRIOR_RECOVERY_BONUS
    elif history.prior_failed_attempts > 0:
        adjusted *= NEVER_RECOVERED_PENALTY

    # Salary-cycle proximity, FUNDS only.
    if failure_class is FailureClass.FUNDS and at.day in SALARY_WINDOW_DAYS:
        adjusted *= SALARY_WINDOW_BONUS

    return float(min(max(adjusted, 0.0), 0.97))

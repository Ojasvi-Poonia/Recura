"""Metrics for the holdout comparison (CLAUDE.md section 8).

Adds one thing section 8's table does not ask for: **bootstrap confidence intervals**.
With 400 holdout events, a 7-point lift carries roughly a +/-4 point 95% interval. A
point estimate with no uncertainty attached invites exactly the question we do not want
to answer live. Reporting the interval answers it in advance.

The bootstrap RNG is seeded, so intervals are byte-identical across runs (section 8).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 20260826


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    events: int
    recovered_events: int
    recovery_rate: float
    recovered_paise: int
    cost_paise: int
    net_paise: int
    contacts: int
    contacts_per_customer: float
    actions_blocked: int
    escalated: int
    refused_negative_ev: int
    opted_out: int
    llm_fallbacks: int


@dataclass(frozen=True)
class Comparison:
    treatment: ArmMetrics
    holdout: ArmMetrics
    lift_pp: float                  # percentage points of recovery rate
    lift_ci_low_pp: float
    lift_ci_high_pp: float
    incremental_recovered_paise: int
    net_incremental_paise: int
    cost_per_recovery_paise: int
    significant: bool               # does the 95% interval exclude zero?


def summarise(arm: str, results: list) -> ArmMetrics:
    n = len(results)
    recovered = [r for r in results if r.recovered_paise > 0]
    cost = sum(r.cost_paise for r in results)
    gross = sum(r.recovered_paise for r in results)
    contacts = sum(r.contacts for r in results)
    return ArmMetrics(
        arm=arm, events=n, recovered_events=len(recovered),
        recovery_rate=len(recovered) / n if n else 0.0,
        recovered_paise=gross, cost_paise=cost, net_paise=gross - cost,
        contacts=contacts, contacts_per_customer=contacts / n if n else 0.0,
        actions_blocked=sum(r.actions_blocked for r in results),
        escalated=sum(1 for r in results if r.escalated),
        refused_negative_ev=sum(r.refused_negative_ev for r in results),
        opted_out=sum(1 for r in results if r.opted_out),
        llm_fallbacks=sum(r.llm_fallbacks for r in results),
    )


def bootstrap_lift_ci(treatment: list, holdout: list,
                      draws: int = BOOTSTRAP_DRAWS,
                      seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% percentile bootstrap on the difference in recovery rate, in points."""
    rng = np.random.default_rng(seed)
    t = np.array([1.0 if r.recovered_paise > 0 else 0.0 for r in treatment])
    h = np.array([1.0 if r.recovered_paise > 0 else 0.0 for r in holdout])
    if not len(t) or not len(h):
        return (0.0, 0.0)
    diffs = np.empty(draws)
    for i in range(draws):
        diffs[i] = (rng.choice(t, size=len(t), replace=True).mean()
                    - rng.choice(h, size=len(h), replace=True).mean())
    low, high = np.quantile(diffs, [0.025, 0.975])
    return (float(low) * 100.0, float(high) * 100.0)


def compare(treatment: list, holdout: list) -> Comparison:
    t = summarise("treatment", treatment)
    h = summarise("holdout", holdout)
    lift = (t.recovery_rate - h.recovery_rate) * 100.0
    low, high = bootstrap_lift_ci(treatment, holdout)

    # Incremental = what treatment recovered ABOVE the holdout rate on the same volume.
    counterfactual = int(round(h.recovery_rate * t.events
                               * (t.recovered_paise / max(1, t.recovered_events))))
    incremental = t.recovered_paise - counterfactual
    extra_recoveries = max(1, t.recovered_events - int(round(h.recovery_rate * t.events)))

    return Comparison(
        treatment=t, holdout=h,
        lift_pp=lift, lift_ci_low_pp=low, lift_ci_high_pp=high,
        incremental_recovered_paise=incremental,
        net_incremental_paise=incremental - t.cost_paise,
        cost_per_recovery_paise=int(t.cost_paise / extra_recoveries),
        significant=(low > 0.0),
    )


def as_dict(c: Comparison) -> dict:
    return {"treatment": asdict(c.treatment), "holdout": asdict(c.holdout),
            **{k: v for k, v in asdict(c).items() if k not in ("treatment", "holdout")}}

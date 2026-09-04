"""Metrics for the holdout comparison (CLAUDE.md section 8).

Adds one thing section 8's table does not ask for: **bootstrap confidence intervals**.
With 400 holdout events, a 7-point lift carries roughly a +/-4 point 95% interval. A
point estimate with no uncertainty attached invites exactly the question we do not want
to answer live. Reporting the interval answers it in advance.

The bootstrap RNG is seeded, so intervals are byte-identical across runs (section 8).
"""

from __future__ import annotations

from collections import Counter
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
    # Messages the system actually composed, and nudges it chose but could not write.
    # Reported because a run where every message silently failed to render looked
    # identical to a healthy one in every other number here - see BUILD_NOTES section R.
    messages_sent: int = 0
    template_failures: int = 0
    # Promise-to-pay is a named Track 03 direction. Reported because the counter read 0
    # across the whole cohort while the escalation rule for it was firing - the two used
    # different definitions of "broken".
    broken_promises: int = 0


@dataclass(frozen=True)
class Comparison:
    treatment: ArmMetrics
    holdout: ArmMetrics
    lift_pp: float                  # percentage points of recovery rate
    lift_ci_low_pp: float
    lift_ci_high_pp: float
    incremental_recovered_paise: int
    net_incremental_paise: int
    cost_per_recovery_paise: int | None
    roi: float                      # rupees recovered per rupee spent
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
        messages_sent=sum(getattr(r, "messages_sent", 0) for r in results),
        template_failures=sum(getattr(r, "template_failures", 0) for r in results),
        broken_promises=sum(getattr(r, "broken_promises", 0) for r in results),
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
    # NOT max(1, ...). Clamping a zero-or-negative denominator to 1 reports the entire
    # intervention spend as the cost of one recovery: the "weak interventions" sweep row
    # produced 159 FEWER recoveries than its control and was still quoted at a tidy
    # "Rs 1,06,442 per extra recovery". A cost per recovery is undefined when there are
    # no extra recoveries, and undefined is what we now report.
    extra_recoveries = t.recovered_events - int(round(h.recovery_rate * t.events))

    return Comparison(
        treatment=t, holdout=h,
        lift_pp=lift, lift_ci_low_pp=low, lift_ci_high_pp=high,
        incremental_recovered_paise=incremental,
        net_incremental_paise=incremental - t.cost_paise,
        cost_per_recovery_paise=(int(t.cost_paise / extra_recoveries)
                                 if extra_recoveries > 0 else None),
        # The number a merchant actually asks for: what does a rupee of spend return?
        roi=(incremental / t.cost_paise) if t.cost_paise else float("inf"),
        significant=(low > 0.0),
    )


def as_dict(c: Comparison) -> dict:
    return {"treatment": asdict(c.treatment), "holdout": asdict(c.holdout),
            **{k: v for k, v in asdict(c).items() if k not in ("treatment", "holdout")}}


# --------------------------------------------------------------------------------------
# Segmentation and stopping rules.
#
# The pooled headline answers "did it recover money". It does not answer the problem
# statement, which names three distinct surfaces - payment failures, checkout abandonment,
# and overdue receivables - and asks for stopping rules. A single number silently averages
# a surface we are good at with one we may be useless at. Both are reported below.
# --------------------------------------------------------------------------------------


def compare_segments(treatment: list, holdout: list, segment_of) -> dict:
    """Run the whole randomised comparison independently within each segment.

    `segment_of` maps an event_id to a segment label. Each segment is compared against
    ITS OWN holdout, so a segment's lift is not contaminated by the arm mix of any other.
    Segments with an empty arm are skipped rather than reported as zero - a lift needs
    both arms to exist.
    """
    labelled = [(segment_of(r.event_id), r) for r in treatment]
    labelled_h = [(segment_of(r.event_id), r) for r in holdout]
    keys = sorted({k for k, _ in labelled} | {k for k, _ in labelled_h})

    out = {}
    for key in keys:
        t = [r for k, r in labelled if k == key]
        h = [r for k, r in labelled_h if k == key]
        if not t or not h:
            continue
        out[key] = compare(t, h)
    return out


def stop_reasons(results: list) -> dict:
    """Why every episode ended.

    The judging bar asks for "stopping rules". This is the evidence that they exist, fire,
    and which ones actually bind - an agent that only ever stops on `exhausted` has no
    stopping rules worth the name, however many are written in the policy file.
    """
    counts = Counter(r.stop_reason for r in results)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

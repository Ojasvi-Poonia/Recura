# ADR 0005: The model returns a distribution, shrunk by its measured calibration

**Status:** Accepted · **Date:** 2026-08-27

## Context

The diagnosis layer is only consulted where Razorpay's error code carries no information
— roughly 17% of events, where the code is explicitly opaque (`payment_failed`,
`payment_declined`) or maps to `UNKNOWN`. On the other 83% the taxonomy already has the
answer and a model call buys nothing.

Our first design asked for a single label. On its first live call the model correctly
answered `UNKNOWN` — the honest response to an opaque code — and our agent, which only
used the model's class when it *wasn't* `UNKNOWN`, discarded the call entirely.

An honest model was contributing nothing because our interface could not represent
honesty.

## Decision

Two changes.

**Return a distribution.** The model reports probabilities across failure classes. The
propensity model marginalises: `p(action) = Σ_c P(c) · p(action | c)`. The bandit takes
**fractional credit** — a "60% FUNDS, 40% infra" diagnosis updates the FUNDS cell by 0.6
and the infra cell by 0.4.

**Shrink toward the taxonomy prior, by a weight set from measurement.**

```
p_used = w · p_model + (1 − w) · p_taxonomy
```

`make calibration` scores the model against ground truth and found it materially
overconfident: Brier 0.9838 against a 0.8196 base rate, expected calibration error 0.2742,
and 61%-confident predictions landing 20% of the time. Feeding probabilities like that
into an expected-value calculation degrades every decision downstream — the ablation
confirmed it, showing the agent scored **better with the model switched off**.

## Consequences

**Partial information flows through.** The model now says things like "FUNDS 40%,
TRANSIENT_INFRA 25%, AUTH_ABANDON 20%, UNKNOWN 5%", reasoning from position-in-month
toward a salary-cycle explanation. That is genuinely useful even when nothing is certain.

**The LLM's contribution went from −16% to +8%.** Modest, measured and defensible.

**`w` is a measurement, not a taste parameter.** It must be re-derived whenever the
diagnosis model changes. A better-calibrated model earns a higher weight.

**We deliberately did not pick the best-scoring `w`.** Sweeping 0.0 / 0.2 / 0.35 / 0.6 /
1.0 gave lifts that all sit inside each other's confidence intervals. Choosing the highest
would be tuning on the test set, which our own credibility rules forbid. We kept the
measurement-derived value and reported the sweep.

## Alternatives considered

**Take the model at face value (`w = 1`).** What most systems do. Measurably worse here.

**Drop the LLM entirely (`w = 0`).** Defensible, and within noise of what we ship. We keep
the model because the *measurement* of its contribution is itself a deliverable, and
because a better-calibrated model would earn a larger weight without an architecture
change.

**Fine-tune or few-shot to improve calibration.** Out of scope for the time available, and
it would not change the finding that raw stated confidence should not be trusted.

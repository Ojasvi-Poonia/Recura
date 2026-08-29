# ADR 0006: Negative controls are a first-class deliverable

**Status:** Accepted · **Date:** 2026-08-27

## Context

Every number this project reports comes from comparing two arms of a **synthetic** cohort
we wrote ourselves. That is a structurally suspicious arrangement, and a reviewer is right
to treat it as such. Asserting "our evaluation is sound" is not evidence.

The failure mode is not fraud. It is that a subtle asymmetry between arms produces a
confident, significant, entirely fictitious result — and nobody notices, because the
number looks plausible.

## Decision

Build a validity suite whose job is to **fail** if the benchmark is unsound, and run it as
a first-class target (`make validate`). Two of its six checks are negative controls:

- **A/A test.** Split the treated population in half. Both halves received identical
  treatment, so any measured difference is noise.
- **Placebo.** Make every action completely inert in the simulator — no uplift, and no
  opt-out either. Measured lift must collapse to zero.

Plus arm balance (standardised mean differences), holdout purity, latent isolation and
determinism.

## Consequences

**The placebo failed immediately, and cost us 85% of our headline.**

It reported **+18.57pp of lift from actions that did nothing at all.** The cause was a real
methodological flaw: the treatment arm was re-observed up to five times across a 21-day
episode while the control arm was observed exactly once. More draws on the same
probability manufactures lift out of nothing — and it was wrong on its own terms, because
a customer left alone for three weeks also gets several natural chances to pay.

Fixing it required observing the control across the same horizon, observing on blocked
steps too (the contract stops *us*, not the customer), and refusing to schedule actions
beyond the episode horizon.

**Our headline fell from +33.84pp to under +5pp.** Roughly 29 points of what we were about
to report was measurement artefact.

**The residual is negative, and that direction matters.** The placebo now reads −2.22pp:
under inert actions the harness scores treatment *below* control, so every lift we report
is conservative. A positive residual would invalidate the headline; a negative one only
understates it.

**The suite gates the result.** It exits non-zero on failure, so a broken measurement
cannot quietly ship.

## Alternatives considered

**Trust the design.** What most synthetic evaluations do. Ours was carefully designed and
still wrong by 29 percentage points.

**External review only.** Useful but slow, and it does not run in CI.

**Report without significance testing.** Would have hidden the problem entirely — the
broken pipeline produced a *tighter* interval, not a looser one.

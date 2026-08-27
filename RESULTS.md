# Results

Everything below is produced by `make eval`, `make validate`, `make ablate`,
`make sweep`, `make replay` and `make calibration`, from the committed fixture set,
offline, with no API key. Every run is byte-identical.

Cohort: 10,000 synthetic events, seeded, 80/20 randomised split. Generator frozen —
see the change log at the top of [`eval/generate_cohort.py`](eval/generate_cohort.py).
Parameter provenance: [`eval/CALIBRATION.md`](eval/CALIBRATION.md).

---

## 1. Headline

| Metric | Treatment | Holdout |
|---|---:|---:|
| Events | 8,053 | 1,947 |
| Recovery rate | 76.9% | 73.0% |
| Recovered | ₹2,34,97,159 | ₹50,28,349 |
| Intervention cost | ₹2,35,854 | ₹0 |
| Contacts per customer | 0.27 | 0.00 |
| Actions blocked by policy | 8,422 | — |
| Refused (EV < 0) | 2,771 | — |
| Escalated to human | 1,303 | — |
| Opted out | 54 | 0 |

**Incremental lift: +3.86 percentage points, 95% bootstrap CI [+1.68, +6.04].**

| | |
|---|---:|
| Incremental recovered | **₹11,78,107** |
| Net incremental (after cost) | **₹9,42,253** |
| Cost per extra recovery | ₹761 |
| Return on spend | **5.0×** |

The interval excludes zero, so the effect is significant at 95%. It is a **bootstrap
percentile interval** over 10,000 resamples with a fixed seed, not a normal
approximation.

### Read the holdout number carefully

The control arm recovers **73.0% unaided**. That is high, and it is the most important
caveat on this page. Our response model gives each event five recovery opportunities
across a 21-day episode and treats them as independent draws on the same per-step
probability. Real customers who fail once are less likely to pay spontaneously on each
subsequent attempt — the population self-selects — so 73% almost certainly overstates
what happens without intervention.

The direction of that error runs **against us**: a lower real baseline means more
headroom, so our measured lift is conservative. But the absolute recovery rates on this
page should not be read as forecasts of production performance.

---

## 2. Is the measurement itself sound?

`make validate`. These checks exist to fail if the benchmark is unsound.

| Check | Result | What it rules out |
|---|---|---|
| **A/A test** | +0.35pp, CI [−1.53, +2.15] | The harness inventing a difference where none exists |
| **Placebo (inert actions)** | −2.22pp, CI [−4.43, −0.00] | Lift that is an artefact of the pipeline rather than the agent |
| Arm balance | worst standardised difference 0.054 (threshold 0.10) | Confounding from an unbalanced randomisation |
| Holdout purity | cost 0, contacts 0, opt-outs 0 | Contamination of the control arm |
| Latent isolation | no hidden field present in the observable cohort | The agent reading the answer |
| Determinism | byte-identical across runs | Cherry-picked runs |

### The placebo control cut our headline by 85%

When first built, the placebo reported **+18.57pp of lift from actions made completely
inert**. Chasing it exposed a real methodological flaw: the treatment arm was
re-observed up to five times across an episode while the control arm was observed
exactly once. More draws on the same probability manufactures lift out of nothing.

Three fixes followed: observe the control across the same horizon; observe on blocked
steps too, because the contract stops *us* and not the customer; and stop offering
actions scheduled beyond the episode horizon, which was silently expiring episodes.

**The headline fell from +33.84pp to under +5pp.** Roughly 29 points of what we had been
about to report was measurement artefact.

The residual placebo reading is **−2.22pp** — negative. Direction matters more than
magnitude here: under a placebo the harness scores treatment *below* control, so every
number in this document is conservative. A positive residual would have invalidated the
headline.

---

## 3. What each component contributes

`make ablate`. Deliberately cripple the agent and measure the damage.

| Configuration | Lift | 95% CI | vs full | Net incremental | Cost/recovery |
|---|---:|---|---:|---:|---:|
| **Full agent** | **+3.86pp** | [+1.68, +6.04] | — | ₹9,42,253 | ₹761 |
| 1 · Random action chooser | +0.33pp | [−1.89, +2.57] | −91% | −₹4,70,368 | **₹21,722** |
| 2 · No taxonomy | +3.07pp | [+0.89, +5.26] | −20% | ₹6,99,080 | ₹1,012 |
| 3 · No policy gate | +4.31pp | [+2.14, +6.50] | **+12%** | ₹11,16,220 | ₹560 |
| 4 · No LLM (rules only) | +4.22pp | [+2.03, +6.40] | **+9%** | ₹10,27,509 | ₹722 |

Supporting counts:

| Configuration | Blocked | Refused | Contacts/customer | Escalated |
|---|---:|---:|---:|---:|
| Full agent | 8,663 | 2,551 | 0.270 | 1,459 |
| Random chooser | 7,256 | 2,683 | 0.988 | 1,624 |
| No taxonomy | 7,945 | 2,721 | 0.256 | 1,574 |
| No policy gate | 0 | 1,712 | 0.196 | 1,106 |
| No LLM | 8,760 | 2,611 | 0.250 | 1,336 |

**Random action selection is 28× less efficient**, at ₹21,722 per marginal recovery
against our ₹761, and it loses ₹4.7 lakh outright.

Being precise about what that does and does not establish: the lift interval
[−1.89, +2.57] contains zero, so "random is significantly worse at recovering" would be
overclaiming. What is not close is the cost of getting there. Anyone reporting only a
lift column would badly understate what a decision layer contributes.

**The policy gate costs 12% of achievable lift, and the LLM costs 9%.** Both are reported
as measured. See section 4 for what we did about the second one, and section 7 failure
case 4 for why we still ship it.

**The policy gate is not a tax.** Removing it makes results *worse* (−14%). It stops the
agent from doing counterproductive things, so compliance and performance point the same
way here. We expected this to cost money and it did not.

### The ablation found three bugs in our own code

It earned its place before it produced a single reportable number:

1. **A random chooser was beating the optimiser.** We were Thompson-sampling the
   `NO_ACTION` baseline alongside the action arms, so each increment was a difference of
   two independent draws — mean zero, negative half the time. The agent refused at
   random. Exploration belongs on the action arms; the counterfactual is a fixed
   reference point.
2. **Refusals were recorded as zero recovery.** Declining to act is a decision, not an
   exit — the customer may still pay, and that recovery belongs to the treatment arm.
   Worse, the `NO_ACTION` posterior never updated, so it sat at its 0.5 prior forever,
   against which every real action looked like a bad bet.
3. **The agent had degenerated into a retry bot.** A blocked action yields no outcome, so
   the bandit could not learn from it and re-proposed the same forbidden retry every
   step — 17,773 retries against 179 messages, which is the explicit anti-goal in our
   own spec.

---

## 4. What the LLM actually contributes

`make calibration`. The diagnosis layer returns a probability distribution over failure
classes and the expected-value layer marginalises over it. That is only sound if the
probabilities mean something, so we measured them against ground truth.

| | Model | Base rate |
|---|---:|---:|
| Brier score (lower better) | 0.9838 | **0.8196** |
| Top-1 accuracy | 18.2% | **22.5%** |
| Expected calibration error | **0.2742** | — |

Reliability, over 1,107 fixture-backed events:

| Stated confidence | n | It says | It is right |
|---|---:|---:|---:|
| 0.2 – 0.4 | 55 | 35.0% | 23.6% |
| 0.4 – 0.6 | 1,023 | 45.8% | 17.9% |
| 0.6 – 0.8 | 29 | 61.6% | **20.7%** |

**When the model says it is 61% confident, it is right 20% of the time.** It is worse
than predicting base rates and materially overconfident.

### What we did about it

The first honest ablation showed the LLM made the agent **worse** — removing it improved
lift by 16%. The calibration study explains why: feeding overconfident probabilities into
an expected-value calculation degrades every decision downstream.

So we shrink the model's distribution toward the deterministic taxonomy prior:

```
p_used = w · p_model + (1 − w) · p_taxonomy
```

`w` is set **from the calibration measurement**, not by taste. With shrinkage the LLM
contributes **+8%** rather than −16%.

We swept `w` across 0.0, 0.2, 0.35, 0.6 and 1.0. Every value lands inside every other
value's confidence interval, so the variation is noise. We kept the measurement-derived
weight rather than the highest-scoring one — picking the best-scoring `w` would be
tuning on the test set, which our own credibility rules forbid.

**Honest summary: on opaque payment declines, a small language model adds little. The
signals available do not support confident diagnosis, and the defensible architecture is
rules-first with the model as a shrunk, measured assist.**

---

## 5. Sensitivity to our assumptions

`make sweep`. Every grade-C parameter in `CALIBRATION.md` is an assumption, so the whole
evaluation is re-run across five parameterisations.

| Parameterisation | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---|---:|
| baseline (calibrated) | 73.0% | +3.86pp | [+1.7, +6.0] | ₹9,42,253 |
| pessimistic: high self-recovery | 82.7% | +3.51pp | [+1.6, +5.4] | ₹9,08,824 |
| optimistic: low self-recovery | 53.9% | +6.04pp | [+3.6, +8.5] | ₹16,01,502 |
| **weak interventions** | 73.0% | **−1.73pp** | [−3.9, +0.5] | **−₹5,90,091** |
| hard failure mix + noisier labels | 58.3% | +7.41pp | [+5.0, +9.7] | ₹19,69,953 |

**Envelope: −2.07 to +6.23pp.**

Under a pessimistic view of what dunning can achieve *at all*, Recura loses money. We do
not know which parameterisation reality resembles, and that row is in this table because
it is true.

---

## 6. What different policy contracts would cost

`make replay`. Because every decision and verdict is in an append-only ledger, we can
re-run the cohort under an altered contract and diff the outcome.

| Policy variant | Lift | Net incremental | vs shipped | Blocked | Contacts/cust |
|---|---:|---:|---:|---:|---:|
| **as committed** | +4.33pp | ₹10,83,117 | — | 8,663 | 0.270 |
| TRAI-only window (to 21:00) | +4.54pp | ₹11,45,098 | +₹61,981 | 8,627 | 0.268 |
| stricter: 1 contact / week | +3.57pp | ₹8,96,427 | −₹1,86,690 | 9,183 | 0.226 |
| looser: 5 contacts / week | +4.33pp | ₹10,83,117 | **₹0** | 8,663 | 0.270 |
| **no merchant spend cap at all** | +4.33pp | ₹10,83,117 | **₹0** | 8,663 | 0.270 |
| spend cap 5× tighter (₹5k/day) | +2.56pp | ₹6,28,894 | −₹4,54,223 | 14,611 | 0.166 |
| spend cap 25× tighter (₹1k/day) | **−0.44pp** | −₹1,54,079 | −₹12,37,195 | 18,183 | 0.061 |
| no human escalation at all | +0.40pp | ₹98,961 | **−₹9,84,156** | 10,691 | 0.100 |
| retry risk declines anyway | +3.73pp | ₹9,11,459 | −₹1,71,658 | 8,564 | 0.259 |

Four of these are worth stating plainly.

**Choosing the stricter RBI evening bound costs ₹61,981 of ₹10.83 lakh — 5.7%.** We took
the more conservative of two overlapping regulatory readings. It is not free, but it is
the price of not having to defend a contestable interpretation of TRAI's exemption.

**Loosening the contact cap, and removing the merchant spend cap entirely, both change
the result by exactly ₹0.** The agent already stops below both limits on its own, because
attention cost prices the risk of losing the customer and expected value prices the rest.
**The arithmetic binds before the contract does** — the gate is a backstop, not the thing
doing the work. That is the strongest single demonstration that this is a decision system
rather than a rules engine.

**Under-budgeting is actively value-destroying.** At a 25× tighter spend cap the agent
posts −0.44pp and a cost per recovery of ₹25,983: it exhausts a symbolic budget on
whatever it reaches first and never gets to the cases that matter. A merchant's instinct
to cap recovery spend tightly is exactly wrong.

**Removing human escalation costs 91% of the value.** See failure case 1 below.

---

## 7. Where this breaks

Six honest failure cases.

### 1. Ninety-one percent of the value depends on human availability

With `escalation.max_per_day: 0` the lift collapses from +4.33pp to **+0.40pp** — no
longer statistically significant — costing ₹9.84 lakh of ₹10.83 lakh.

Recura is a decision layer that routes work to people, not a system that replaces them.
A merchant with no collections staff gets very little of this. We would rather state
that than let the headline imply autonomy the system does not have.

### 2. Under a pessimistic response model the agent loses money

The "weak interventions" sweep row posts −2.07pp and −₹6.7 lakh. If dunning genuinely
moves the needle less than our calibrated assumptions suggest, this system is
value-destroying. Nothing in our evidence rules that out.

### 3. The holdout recovers 73% unaided, which is probably too generous

Our response model treats five recovery opportunities as independent draws. Real
customers self-select — someone who did not pay on attempt one is less likely to pay
unprompted on attempt four. The error is conservative in direction but the absolute
rates are not forecasts.

### 4. The language model currently costs 9%, not contributes

Removing it improves the result. We have measured this twice: with our original invented
contact-fatigue curve it contributed +8%; once that curve was fitted to 86,399 real
records it contributes **−9%**. An effect that flips sign when an unrelated parameter is
corrected was never real.

The calibration study says why — Brier 0.9838 against a 0.8196 base rate, and
61%-confident predictions landing 20% of the time. On opaque payment declines the
observable signals do not support confident diagnosis.

We ship it anyway, at a shrinkage weight derived from that measurement, because the
measurement is itself the deliverable and a better-calibrated model would earn a higher
weight with no architecture change. But nobody should read this project as evidence that
an LLM improves payment recovery. It is evidence that we checked.

### 5. Per-merchant margin is implemented but untested

`MerchantContext.margin_bps` now drives expected value — it was silently ignored for most
of the build — but the frozen cohort assigns every merchant 30%. The EV differences that
margin *should* create across a real merchant base are therefore unexercised. Razorpay's
merchants are not homogeneous, and this is where that would show up first.

### 6. Hinglish messaging is compliance-verified, not lift-verified

We prove that no free-form copy can escape the DLT template registry —
`verify_compliance()` refuses anything that cannot be matched back to a registered
pattern. We make **no claim** that Hinglish improves recovery, because the frozen
generator does not model language matching. The capability is real; the benefit is
unmeasured.

### Also worth knowing

- **`card_declined` is our likeliest taxonomy mis-classification.** A bare issuer
  decline may hide a risk decline; we class it as `INSTRUMENT_INVALID` because that is
  Razorpay's suggested remedy, and we flag it in `taxonomy/mapping.py`.
- **Escalating a merchant integration bug has value we do not model.** Fixing the bug
  prevents future failures, which our customer-recovery EV cannot see, so the agent
  under-escalates configuration problems.
- **The bandit sees 42 cells and ~8,000 events with delayed outcomes.** Learning is real
  but shallow; 37 of 42 cells acquire data.

---

## 8. Reproducing this

```bash
make install
make eval        # section 1
make validate    # section 2
make ablate      # section 3
make calibration # section 4
make sweep       # section 5
make replay      # section 6
```

No API key is required for any of it. `fixtures/` contains 870 cached model responses,
keyed by SHA-256 of (model, prompt, payload), so the LLM path replays exactly rather
than being re-queried. A key is needed only to regenerate them.

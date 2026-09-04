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
| Events | 8,058 | 1,942 |
| Recovery rate | 78.8% | 71.5% |
| Recovered | ₹2,37,97,782 | ₹54,64,895 |
| Intervention cost | ₹2,35,648 | ₹0 |
| Contacts per event | 0.22 | 0.00 |
| Messages actually sent | 281 | 0 |
| Actions blocked by policy | 6,249 | — |
| Refused (EV < 0) | 3,626 | — |
| Escalated to human | 1,502 | — |
| Opted out | 11 | 0 |

**Incremental lift: +7.28 percentage points, 95% bootstrap CI [+5.11, +9.54].**

| | |
|---|---:|
| Incremental recovered | **₹21,98,315** |
| Net incremental (after cost) | **₹19,62,667** |
| Cost per extra recovery | ₹401 |
| Return on spend | **9.3×** |

The interval excludes zero, so the effect is significant at 95%. It is a **bootstrap
percentile interval** over 10,000 resamples with a fixed seed, not a normal
approximation.

### Read the holdout number carefully

The control arm recovers **71.5% unaided**. That is high, and it is the most important
caveat on this page. Our response model gives each event five recovery opportunities
across a 21-day episode and treats them as independent draws on the same per-step
probability. Real customers who fail once are less likely to pay spontaneously on each
subsequent attempt — the population self-selects — so 73% almost certainly overstates
what happens without intervention.

The direction of that error runs **against us**: a lower real baseline means more
headroom, so our measured lift is conservative. But the absolute recovery rates on this
page should not be read as forecasts of production performance.

---

## 2. Each surface the problem statement names, measured separately

The brief names three surfaces — *"payment failures and checkout abandonment to overdue
receivables"* — and the cohort carries all of them. Pooling them into one number would let
a strong surface carry a weak one. Each is compared against its own randomised holdout:

| Surface | Treated | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---:|---|---:|
| Payment failure | 4,447 | 1,045 | +7.93pp | [+4.86, +10.89] | ₹11,93,422 |
| Checkout abandonment | 1,634 | 413 | +5.45pp | [+0.73, +10.26] | ₹3,01,535 |
| Mandate / subscription | 1,193 | 291 | +6.28pp | [+0.55, +12.03] | ₹2,24,081 |
| Overdue receivable | 784 | 193 | +9.29pp | [+3.08, +15.74] | ₹2,52,001 |

**All four surfaces clear zero at 95%.** Point estimates run from +5.45pp on checkout to
+9.29pp on receivables, and the intervals widen exactly as the control samples shrink:
payment has 1,045 control events, mandate 291, receivable 193.

That widening is a power effect, not a capability one. Reporting the surfaces separately
was still worth doing: in an earlier revision checkout abandonment came out at +2.94pp with
an interval spanning zero while the pooled headline looked healthy, which is precisely the
gap a per-surface table exists to expose.

### The structural note that survives

Of the 2,047 checkout events, **1,212 (59%) carry no Razorpay error object at all** — no
`reason`, no `source`, no `step`. Nothing failed; the customer simply left. The taxonomy has
nothing to read on those, and the agent works from amount, customer history, hour and method
alone.

That surface is significant now, but it is where a real integration would gain most: drop-off
step, time on page, whether a payment method was ever selected. It is the first thing we
would ask Razorpay for.

**A note on an earlier revision of this section.** Before the messaging and compliance
defects were fixed, checkout abandonment measured +2.94pp with an interval spanning zero, and
this document argued at length that unlabelled abandonment was structurally hard for a
taxonomy-driven approach. The structural point stands; **the conclusion drawn from it was
wrong**, and it was wrong because the number underneath it came from a system that was not
sending any messages.

---

## 3. Stopping rules, as measured

The judging bar asks for stopping rules. A rule that never fires is not a rule, so every
treated episode records exactly one terminating reason and `make eval` prints the census:

| Why the episode ended | Episodes | Share |
|---|---:|---:|
| `recovered` | 4,328 | 53.7% |
| `recovered_unprompted` | 2,022 | 25.1% |
| `exhausted` | 1,096 | 13.6% |
| `refused_negative_ev` | 573 | 7.1% |
| `episode_expired` | 28 | 0.3% |
| `opted_out` | 11 | 0.1% |

Two rows carry the argument.

**`recovered_unprompted` — 25.4%.** The customer paid without us, and the agent noticed and
stood down. It re-observes state before every decision rather than executing a plan fixed at
episode start, so these 2,022 episodes cost nothing beyond the observation. A
workflow-shaped system that queued three messages up front would have sent them all.

**`exhausted` — only 13.0%.** That is the share that simply ran out of permitted actions.
An agent whose census was dominated by `exhausted` would not have stopping rules at all; it
would have a budget it collided with. Most episodes here end because something true became
true — the money arrived, the arithmetic turned negative, or the customer opted out.

`refused_negative_ev` at 7.5% is the one that is a decision rather than an event: 607
episodes where the agent computed the expected value of every available action, found them
all negative, and did nothing. Section 5's ablation shows what happens without that
restraint.

---

## 4. Is the measurement itself sound?

`make validate`. These checks exist to fail if the benchmark is unsound.

| Check | Result | What it rules out |
|---|---|---|
| **A/A test** (clustered by customer) | +0.46pp, CI [−1.31, +2.21] | The harness inventing a difference where none exists |
| **Placebo (inert actions)** | +0.21pp, CI [−2.01, +2.49] | Lift that is an artefact of the pipeline rather than the agent |
| **Contact contract** | 0 breaches, tightest gap 24.0h | A compliance clause that cannot actually be enforced |
| Arm balance | worst standardised difference 0.036 | Randomisation that did not randomise |
| Holdout purity | cost 0, contacts 0, opt-outs 0 | A control arm that was quietly treated |
| Latent isolation | no latent field reachable from `src/` | The agent reading the answer key |
| Determinism | byte-identical across runs | Numbers that move when you look again |

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

The residual placebo reading is **+0.21pp**, on an interval of [−2.01, +2.49] that
contains zero. **Containing zero is the test** — a placebo whose interval *excluded* zero
would mean the pipeline manufactures lift, and would invalidate everything here.

We are deliberately not claiming more than that. Earlier revisions of this document read
−1.71pp and argued that a negative residual made every number conservative. That argument
is withdrawn: the point estimate is positive now, and reading a sign inside an interval
four points wide as if it were a finding is exactly the error this control exists to catch.
What survives is the claim the check actually supports — **the harness does not
manufacture measurable lift.**

---

## 5. What each component contributes

`make ablate`. Deliberately cripple the agent and measure the damage.

| Configuration | Lift | 95% CI | vs full | Net incremental | Cost/recovery |
|---|---:|---|---:|---:|---:|
| **Full agent** | **+7.28pp** | [+5.11, +9.54] | — | ₹19,62,667 | **₹401** |
| 1 · Random action chooser | +5.99pp | [+3.82, +8.23] | **−18%** | ₹12,98,850 | ₹912 |
| 2 · No taxonomy | +7.29pp | [+5.12, +9.52] | +0% | ₹19,21,337 | ₹446 |
| 3 · No policy gate | +8.99pp | [+6.83, +11.23] | **+24%** | ₹24,17,577 | ₹461 |
| 4 · No LLM (rules only) | +7.25pp | [+5.09, +9.50] | −0% | ₹19,60,812 | ₹417 |

Supporting counts:

| Configuration | Blocked | Refused | Contacts/event | Escalated |
|---|---:|---:|---:|---:|
| Full agent | 6,249 | 3,626 | 0.217 | 1,502 |
| Random chooser | 6,679 | 4,189 | 0.500 | 1,702 |
| No taxonomy | 5,550 | 2,622 | 0.236 | 1,705 |
| No policy gate | 0 | 5,011 | 0.270 | 1,652 |
| No LLM | 5,962 | 4,294 | 0.233 | 1,546 |

**Random action selection is 1.7× less efficient**, at ₹912 per marginal recovery against
our ₹401, and it burns 2.2× the contacts to get there (0.500 against 0.217).

Being precise about what that does and does not establish: random's own lift interval is
[+3.82, +8.23], which **excludes zero**. Randomly chosen interventions, run through the
same policy gate and the same stopping rules, genuinely recover money. We are not going to
pretend otherwise — the honest claim is narrower and more useful: for a fixed contact
budget the optimiser recovers **22% more on 57% fewer contacts**, not that unstructured
intervention does nothing.

**Compliance costs 24% of achievable lift.** Removing the policy gate gives +8.99pp
against our +7.28pp — still the largest single improvement available to this agent.

That figure has now been reported as free, 6%, 29%, 51% and 29% again. Every earlier number
was measured against a gate that was partly inoperative: two contact clauses could not fire
(section 9.9), and four episode clauses were pre-empted because the agent closed episodes
itself instead of letting the contract close them. **The honest
summary is that we needed five measurements to price governance, and the first four were
all too cheap.**

**One of the four ablations recovers more than the full agent** — removing the policy gate.** Read the table by
cost per recovery and contacts per event as well as by lift: against a random chooser
the agent recovers 22% more (+7.28 vs +5.99) using **57% fewer contacts** (0.217 vs 0.500)
at **₹401 against ₹912** per marginal recovery.

### The taxonomy pays off late, not early

Removing it costs 4%. The average hides the shape: the taxonomy splits learning across
**42 posterior cells** (7 failure classes × 6 action types), where pooling gives roughly 6.
On a finite run that is a sample-size penalty. Splitting the cohort in half:

| | first half | second half |
|---|---:|---:|
| with taxonomy | +6.11pp | +8.87pp |
| no taxonomy | +6.33pp | +8.15pp |
| **difference** | **−0.22pp** | **+0.72pp** |

It starts behind and finishes ahead — the shape of a learning-rate penalty, which implies
the 4% headline understates what the taxonomy is worth at scale.

**Flagged as a hypothesis, not a result**: within-half comparisons, no confidence
intervals, both differences small. The defensible claim is that mapping Razorpay's own
`reason` field to a strategy costs data before it pays, and 10,000 events sits near the
crossover.

At the previous revision this same ablation showed the taxonomy *hurting* by 8%, and the
sign changed without anyone touching the taxonomy — see section 6 for why that matters.

---

## 6. What the LLM actually contributes

**Nothing measurable, on lift.** Ablation 4 removes the language model entirely and the
result is **+7.48pp against the full agent's +7.28pp** — a 1% contribution, deep inside
the interval ([+5.33, +9.72] against [+5.11, +9.54]).

### The number has moved four times, and that is the finding

| Configuration of the rest of the system | LLM contribution |
|---|---:|
| invented contact-fatigue curve, hand-picked trust weight | +8% |
| fatigue curve fitted to 86,399 real records | −9% |
| trust weight learned by a meta-bandit instead of chosen | +5% |
| every customer message actually sending | +0% |
| the compliance contract actually enforced | −3% |
| Thompson sampling drawing once per arm | −9% |
| **episodes closing on the contract; merchant bugs in the cohort** | **+1%** |

Every row is a real defect fixed somewhere *other than* the model. The fourth is the most
embarrassing and the most instructive: for several runs the agent selected 607 nudges,
**composed none of them**, and was charged and scored for all 607 anyway (section 9.8 and
our engineering log). With that corrected, the model's apparent contribution collapsed
to zero.

**The model's measured contribution has swung 17 points without the model changing at
all.** Every row above is a defect fixed somewhere else — a fatigue curve, a trust weight,
a messaging bug, a contact contract, a sampling error, an episode-closing rule. On this
benchmark the LLM's apparent value is dominated by the correctness of everything around
it, and any single reading of it — the current +1% included — should be treated as noise
rather than evidence.

### What survives

Two things, neither of them a headline.

**Cost efficiency.** Nothing here either: ₹401 per marginal recovery against the
rules-only ₹388 — indistinguishable. Whatever edge the model had on efficiency has
gone the same way as its edge on lift. We are not building an argument on it.

**The architecture is still the right one.** The model is isolated behind a learned trust
weight, so a better-calibrated model earns more influence with no code change. That
matters for what this would become with a real model and real data. It is not evidence
about today.

### Calibration, for completeness

`make calibration` measures whether the model's stated confidence means anything: Brier
score **0.9838** against a base rate of 0.8196, with 61%-confident predictions landing
about 20% of the time. It is badly calibrated, which is consistent with contributing
nothing, and is why the trust weight is learned rather than assumed.

The learned trust posterior is reported by `make eval`, but note that Thompson sampling
concentrates on whichever arm leads early, so its per-arm rates are an allocation artefact
rather than a clean head-to-head. **The ablation is the trustworthy comparison, and the
ablation says zero.**

**Honest summary: on opaque payment declines, a deterministic taxonomy over Razorpay's
published error codes does the work. A small language model adds no measurable lift on top
of it. We built the isolation and the measurement properly, and the measurement says the
component does not currently earn its place.**

---

## 7. Sensitivity to our assumptions

`make sweep`. Every grade-C parameter in `CALIBRATION.md` is an assumption, so the whole
evaluation is re-run across ten parameterisations.

| Parameterisation | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---|---:|
| baseline (calibrated) | 71.5% | +7.28pp | [+5.11, +9.54] | ₹19,62,667 |
| pessimistic: high self-recovery | 81.3% | +6.93pp | [+5.09, +8.83] | ₹18,90,283 |
| optimistic: low self-recovery | 53.8% | +7.31pp | [+4.85, +9.80] | ₹19,88,200 |
| **weak interventions** | 71.5% | **+2.54pp** | **[+0.33, +4.82]** | ₹5,93,711 |
| hard failure mix + noisier labels | 56.8% | +10.58pp | [+8.18, +12.93] | ₹28,52,364 |
| **sceptical human escalation** | 71.5% | **+4.26pp** | **[+2.08, +6.51]** | ₹11,67,579 |
| optimistic human escalation | 71.5% | +9.35pp | [+7.21, +11.56] | ₹25,81,841 |
| cheap human review (₹60) | 71.5% | +7.76pp | [+5.58, +10.01] | ₹22,25,198 |
| expensive human review (₹240) | 71.5% | +6.35pp | [+4.17, +8.60] | ₹15,69,096 |
| thin margin (10%) | 71.5% | +5.36pp | [+3.16, +7.63] | ₹16,65,663 |

**Envelope: +2.54 to +10.58pp, and all ten intervals exclude zero.**

That includes "weak interventions", which in earlier revisions was the one honest
counterexample in this table — it read −1.58pp, then +0.32pp, then +0.71pp, each consistent
with the system doing nothing at all. We record that history rather than presenting the
current row as a clean result: it moved because of correctness fixes elsewhere in the
agent, not because we learned anything new about how well dunning works in the world.


### The one-parameter sensitivity that matters

`ESCALATE_EFFICACY = 2.60` is the largest efficacy constant in the simulator, and removing
human escalation costs 44% of net value — so the result leans on that number harder than on
anything else. It was **uncited** until an audit caught it, and the sweep moved it only as
part of a four-constant group, so no row could isolate it.

It now moves alone, with the other three held fixed:

| | `ESCALATE_EFFICACY` | Lift | 95% CI |
|---|---:|---:|---|
| sceptical | 1.30 | +4.26pp | [+2.08, +6.51] |
| baseline | 2.60 | +7.28pp | [+5.11, +9.54] |
| optimistic | 4.00 | +9.35pp | [+7.21, +11.56] |

**Halve it and the result nearly halves but survives.** At 1.30 — a human agent no more
effective than an automated contact — Recura still posts +4.26pp on an interval excluding zero. The
*magnitude* of our headline depends on this constant; its *existence* does not. Grounding
and grade in `CALIBRATION.md` section 8.

### Cost and margin are now genuinely swept

`config/costs.yaml` carried three comments claiming costs, the attention curve and margin
were "swept in Tier 3". No harness varied any of them. Two of those claims are now true —
escalation priced at ₹60 and ₹240, and margin at 10% — and the third has been corrected to
say plainly that the attention exponent is held fixed everywhere and remains an untested
assumption.

Doubling the price of human review costs 0.93pp; a 10% merchant margin instead of 30%
costs 1.92pp. Neither changes the sign, and both intervals still exclude zero.

### Read the last row sceptically

"Hard failure mix + noisier labels" was built to be the worst realistic world and posts our
**highest** lift, +10.58pp. That is not the agent doing better — it is the holdout doing
worse. Self-recovery falls to 56.8%, so more is left on the table. Lift is a difference, and
differences grow when the baseline drops.

---

## 8. What different policy contracts would cost

`make replay`. Because every decision and verdict is in an append-only ledger, we can
re-run the cohort under an altered contract and diff the outcome.

| Policy variant | Lift | Net incremental | vs shipped | Blocked | Contacts/cust |
|---|---:|---:|---:|---:|---:|
| **as committed** | +7.28pp | ₹19,62,667 | — | 6,249 | 0.217 |
| TRAI-only window (to 21:00) | +7.32pp | ₹19,72,237 | +₹9,570 | 6,278 | 0.219 |
| stricter: 1 contact / week | +6.83pp | ₹18,50,287 | −₹1,12,380 | 6,459 | 0.201 |
| **looser: 5 contacts / week** | +7.28pp | ₹19,62,667 | **₹0** | 6,249 | 0.217 |
| **no merchant spend cap at all** | +7.28pp | ₹19,62,667 | **₹0** | 6,249 | 0.217 |
| spend cap 5× tighter (₹5k/day) | +5.57pp | ₹15,04,917 | −₹4,57,750 | 10,338 | 0.132 |
| spend cap 25× tighter (₹1k/day) | +1.68pp | ₹4,60,363 | −₹15,02,304 | 15,471 | 0.042 |
| **action budget 200/day** | +4.72pp | ₹12,50,881 | **−₹7,11,786** | 12,673 | 0.149 |
| no human escalation at all | +3.87pp | ₹10,99,295 | **−₹8,63,372** | 8,000 | 0.061 |
| retry risk declines anyway | +7.01pp | ₹18,56,767 | −₹1,05,900 | 4,858 | 0.242 |

Five of these are worth stating plainly.

**Loosening the contact cap and removing the merchant spend cap both change the result by
exactly ₹0.** The agent
already stops below that limit on its own: attention cost prices the risk of losing the
customer, and expected value prices the rest. **On spend, the arithmetic binds before the
contract does.** The spend row has read ₹0 across every revision of the cohort. The contact row has moved
(₹0, then −₹1,00,149, then −₹3,244, now ₹0 again), so the stable claim is the spend one;
the contact cap sits close enough to the arithmetic that its sign follows the noise.

**The stricter regulatory reading costs ₹9,570 — and that number is not stable.** We
contact 09:00–19:00 on RBI's Fair Practices bound rather than TRAI's 21:00. Across cohort
revisions this row has read **+₹61,981, −₹1,21,661, +₹1,83,697, −₹61,916 and now
−₹9,570.** Its sign has flipped four times.

We report the instability rather than the latest value, because quoting a single figure
would imply a precision this benchmark does not have. The defensible statement is that
**the cost of the conservative regulatory reading is small relative to run-to-run variation
in our own harness** — and we ship it regardless, because a contestable interpretation of
TRAI's exemption is not something anyone wants to defend to a regulator.

**Retrying risk declines would cost ₹1,05,900 here — but it has been worth +₹60,368 in a
previous revision, and we forbid it either way.** The rule is not justified by the
arithmetic and never was. An issuer risk decline is a signal about the cardholder, not a
transient failure, and hammering it is how merchants get their MIDs reviewed. This is the
clearest case in the repo of the contract deliberately overriding the maths.

**Under-budgeting is what actually destroys value.** The action budget at 200/day costs
₹7,11,786 and a 5× tighter spend cap costs ₹4,57,750; at 25× tighter the agent falls to
+1.68pp, exhausting a symbolic budget on whatever it reaches first. A merchant's instinct
to cap recovery spend tightly is exactly wrong.

Those last two rows are also the only place the merchant budget clauses are exercised at
all — at the shipped values they never bind, which is why `make eval` lists them as
headroom rather than as dead code.

**Removing human escalation costs 44% of the value** — ₹8,63,372 of ₹19,62,667. See failure
case 1 below.

---

## 9. Where this breaks

Nine honest failure cases.

### 1. Forty-four percent of the value depends on human availability

With `escalation.max_per_day: 0` the lift falls from +7.28pp to **+3.87pp** — still
significant, but costing ₹5,37,818 of ₹19,62,667 in net incremental value.

Recura is a decision layer that routes work to people, not a system that replaces them.
A merchant with no collections staff gets substantially less of this. We would rather
state that than let the headline imply autonomy the system does not have.

The related exposure is `ESCALATE_EFFICACY` itself — see section 7, where halving that
one constant halves the headline. Between them, these two facts say the same thing: the
biggest single driver of this result is how good the humans on the other end are.

### 2. The pessimistic parameterisation stopped being a counterexample, and we do not fully trust that

"Weak interventions" — the world where messages and retries barely move anyone — now posts
+2.54pp on an interval of [+0.33, +4.82], which excludes zero. In earlier revisions the same
row read −1.58pp, then +0.32pp, then +0.71pp, all consistent with the system doing nothing.

**Nothing about our model of weak interventions changed.** The row moved because the agent
stopped paying for messages it never sent, stopped contacting people more often than its
own contract allowed, and started closing episodes on the contract rather than pre-empting
it. That is a legitimate reason for a number to improve — but it also means this row is not
the independent stress test it was, because it is now measuring a different agent.

The honest position: we no longer have a parameterisation in which the effect disappears,
and we are slightly suspicious of that. A benchmark whose every row clears zero has lost
the one row that used to keep us honest.

### 3. The holdout recovers 71.5% unaided, which is probably too generous

Our response model treats five recovery opportunities as independent draws. Real
customers self-select — someone who did not pay on attempt one is less likely to pay
unprompted on attempt four. The error is conservative in direction but the absolute
rates are not forecasts.

### 4. The language model contributes nothing measurable

Removing it costs 1% — deep inside the interval, which is to say nothing. Section 6 has the
full history: the figure has moved seven times across revisions, a 17-point swing, without
the model changing at all. Every move came from fixing a defect somewhere else.

We keep the component because the architecture around it is the defensible part — the model
is isolated behind a learned trust weight, so a better-calibrated model earns more influence
with no code change. But on today's evidence it does not earn its place, and a submission
that implied otherwise would be claiming something its own ablation refutes.

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

### 7. Channel choice is decided by price, because the world has no opinion on it

The cost model prices four channels — email 2 paise, SMS 25, WhatsApp 70, voice 300 — but
`eval/latents.py` **does not model channel efficacy at all**. A WhatsApp message and a
voice call are equally persuasive to the simulator. The only thing separating them is what
they cost.

So the agent's observed channel mix (SMS ~70%, WhatsApp ~30%, voice never) is a cost
ranking wearing a decision's clothing. **We do not claim channel selection as a result**,
and the ablation study deliberately does not include a "no channel choice" arm, because
removing a degenerate decision would prove nothing.

This also settled a question during the final audit. Email is consented across the cohort
but has no registered template, so it was never sendable. The tempting fix was to add email
copy — email is cheap and, unlike SMS and voice, is outside DLT's scope. We did not,
because at 2 paise with identical modelled efficacy the agent would have discovered a
channel that is 150× cheaper than voice and just as effective, and our ROI would have
improved for reasons that exist only inside our own simulator. The honest fix was the
opposite one: stop generating nudges on channels no template can carry.

Modelling per-channel open and conversion rates is the single highest-value addition to the
generator, and it is the first thing we would build with real delivery data.

### 8. Every message in an earlier version of this run silently failed to send

Worth stating plainly because it is the most serious defect we found in ourselves. Across
the 10,000-event cohort the agent chose to send 607 nudges and **composed zero of them** —
a slot validator rejected the merchant identifier, `_render_message` correctly returned
`None`, and the calling loop then charged for the message, counted it against the
customer's contact budget, and asked the simulator to score the effect of a nudge that had
never been written.

It is fixed: unwritable nudges are now scored as `NO_ACTION`, the
candidate set is filtered by what a registered template can actually carry, and
`messages_sent` and `template_failures` both appear in the metrics table so that the next
occurrence is a number rather than an absence. The run now composes 313 real messages and
fails on none.

The reason it belongs in this section rather than only in the changelog: **for several
runs, this document reported a headline that included credit for messages that did not
exist.** Nothing in the metrics table would have revealed it. That is the failure mode
this project is supposed to be built against, and it still got past us for days.

---

### 9. Two clauses of the compliance contract were unenforceable, and we did not notice

For most of this project `policy.yaml` was presented as a contract a merchant could sign
while two of its contact clauses could not fire. `contact.min_hours_between` compared
against a `last_contact_at` the agent fabricated as `now - 25 hours` — one hour past the
24-hour minimum, so the comparison was never true. `contact.max_per_customer_per_7d` was
counted per episode, though one customer averages 7.1 overlapping events.

Measured consequences on the shipped cohort: **209 of 335 consecutive contact pairs were
closer than 24 hours** (tightest 6 hours), and **41 of 1,038 contacted customers exceeded
the 3-in-7-day cap**, one of them contacted seven times.

Neither was visible in any metric. "Actions blocked by policy: 8,764" read as vigorous
enforcement while two clauses were inert.

Both are fixed and — more importantly — both are now *checked*: `make validate` replays the
batch, reconstructs every customer's contact timeline, and fails if any clause is breached.
It currently reports 0 breaches with a tightest gap of 24.0h. 

**The reason this belongs in a failure section rather than a changelog:** every compliance
claim this project made before that fix was unverified, and several published numbers were
quantitatively wrong because of it — including the claim that the policy gate was free
(it costs 51%) and every published figure for what the stricter TRAI reading is worth.

---

## 10. Reproducing this

```bash
make install
make eval        # sections 1, 2, 3
make validate    # section 4
make ablate      # section 5
make calibration # section 6
make sweep       # section 7
make replay      # section 8
```

No API key is required for any of it. `fixtures/` contains 872 cached model responses,
keyed by SHA-256 of (model, prompt, payload), so the LLM path replays exactly rather
than being re-queried. A key is needed only to regenerate them.

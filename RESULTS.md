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
| Recovery rate | 78.5% | 73.0% |
| Recovered | ₹2,32,69,611 | ₹50,28,349 |
| Intervention cost | ₹2,35,427 | ₹0 |
| Contacts per customer | 0.25 | 0.00 |
| Messages actually sent | 354 | 0 |
| Actions blocked by policy | 6,536 | — |
| Refused (EV < 0) | 2,835 | — |
| Escalated to human | 1,595 | — |
| Opted out | 7 | 0 |

**Incremental lift: +5.48 percentage points, 95% bootstrap CI [+3.30, +7.66].**

| | |
|---|---:|
| Incremental recovered | **₹16,24,626** |
| Net incremental (after cost) | **₹13,89,199** |
| Cost per extra recovery | ₹534 |
| Return on spend | **6.9×**  |

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

## 2. Each surface the problem statement names, measured separately

The brief names three surfaces — *"payment failures and checkout abandonment to overdue
receivables"* — and the cohort carries all of them. Pooling them into one number would let
a strong surface carry a weak one. Each is compared against its own randomised holdout:

| Surface | Treated | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---:|---|---:|
| Payment failure | 4,477 | 1,057 | +5.61pp | [+2.69, +8.56] | ₹8,02,274 |
| Checkout abandonment | 1,629 | 414 | +5.52pp | [+0.88, +10.13] | ₹2,89,353 |
| Mandate / subscription | 1,158 | 287 | +5.34pp | [−0.30, +11.19] | ₹1,86,768 |
| Overdue receivable | 789 | 189 | +4.96pp | [−0.86, +11.16] | ₹1,11,214 |

**The effect is consistent across all four surfaces** — every point estimate falls between
+4.96 and +5.61pp. What differs is confidence, and that is sample size alone: mandate and
receivable have 287 and 189 holdout events respectively, which cannot exclude zero at 95%
however real the effect.

The honest statement is therefore **not** "we are worse at receivables". It is that we
measure the same effect there and cannot yet prove it on 189 control events. That is a
power problem, and the fix is more data rather than more agent.

### The structural note that survives

Of the 2,043 checkout events, **1,232 (60%) carry no Razorpay error object at all** — no
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
| `recovered` | 4,453 | 55.3% |
| `recovered_unprompted` | 1,870 | 23.2% |
| `exhausted` | 1,073 | 13.3% |
| `refused_negative_ev` | 603 | 7.5% |
| `episode_expired` | 47 | 0.6% |
| `opted_out` | 7 | 0.1% |

Two rows carry the argument.

**`recovered_unprompted` — 23.2%.** The customer paid without us, and the agent noticed and
stood down. It re-observes state before every decision rather than executing a plan fixed at
episode start, so these 1,870 episodes cost nothing beyond the observation. A
workflow-shaped system that queued three messages up front would have sent them all.

**`exhausted` — only 13.3%.** That is the share that simply ran out of permitted actions.
An agent whose census was dominated by `exhausted` would not have stopping rules at all; it
would have a budget it collided with. Most episodes here end because something true became
true — the money arrived, the arithmetic turned negative, or the customer opted out.

`refused_negative_ev` at 7.5% is the one that is a decision rather than an event: 603
episodes where the agent computed the expected value of every available action, found them
all negative, and did nothing. Section 5's ablation shows what happens without that
restraint.

---

## 4. Is the measurement itself sound?

`make validate`. These checks exist to fail if the benchmark is unsound.

| Check | Result | What it rules out |
|---|---|---|
| **A/A test** (clustered by customer) | +0.21pp, CI [−1.58, +2.02] | The harness inventing a difference where none exists |
| **Placebo (inert actions)** | −1.68pp, CI [−3.88, +0.56] | Lift that is an artefact of the pipeline rather than the agent |
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

The residual placebo reading is **−1.68pp** — negative. Direction matters more than
magnitude here: under a placebo the harness scores treatment *below* control, so every
number in this document is conservative. A positive residual would have invalidated the
headline.

---

## 5. What each component contributes

`make ablate`. Deliberately cripple the agent and measure the damage.

| Configuration | Lift | 95% CI | vs full | Net incremental | Cost/recovery |
|---|---:|---|---:|---:|---:|
| **Full agent** | **+5.48pp** | [+3.30, +7.66] | — | ₹13,89,199 | **₹534** |
| 1 · Random action chooser | +4.24pp | [+2.07, +6.44] | −23% | ₹9,04,235 | ₹919 |
| 2 · No taxonomy | +5.52pp | [+3.34, +7.75] | +1% | ₹13,79,544 | ₹567 |
| 3 · No policy gate | +7.10pp | [+4.93, +9.28] | **+29%** | ₹17,87,645 | ₹616 |
| 4 · No LLM (rules only) | +5.67pp | [+3.50, +7.83] | +3% | ₹14,52,132 | ₹526 |

Supporting counts:

| Configuration | Blocked | Refused | Contacts/customer | Escalated |
|---|---:|---:|---:|---:|
| Full agent | 6,536 | 2,835 | 0.248 | 1,595 |
| Random chooser | 6,880 | 3,692 | 0.527 | 1,718 |
| No taxonomy | 5,975 | 2,331 | 0.252 | 1,701 |
| No policy gate | 0 | 2,480 | 0.328 | 1,811 |
| No LLM | 6,475 | 2,770 | 0.245 | 1,613 |

**Random action selection is 1.7× less efficient**, at ₹919 per marginal recovery against
our ₹534, and it burns twice the contacts to get there.

Being precise about what that does and does not establish: random's own lift interval is
[+2.07, +6.44], which **excludes zero**. Randomly chosen interventions, run through the
same policy gate and the same stopping rules, genuinely recover money. We are not going to
pretend otherwise — the honest claim is narrower and more useful: for a fixed contact
budget the optimiser recovers **29% more per contact spent**, not that unstructured
intervention does nothing.

**Compliance costs 29% of achievable lift.** Removing the policy gate gives +7.10pp
against our +5.48pp — the single largest improvement available to this agent. It contacts
people more often than the contract permits and recovers more money by doing so.

Earlier revisions of this document reported that the gate was free, then that it cost 6%.
Both were measured against a gate that was not working: two of its contact clauses were
structurally incapable of firing (section 9.9). **Governance is expensive, and we can only
quote the price now that the contract is genuinely enforced.**

**Three of the four ablations recover more than the full agent.** Read the table by cost
per recovery and contacts per customer, not by lift. Against a random chooser the agent
recovers 29% more (+5.48 vs +4.24) using **53% fewer contacts** (0.248 vs 0.527) at
**₹534 against ₹919** per marginal recovery. With a hard regulated contact budget, the
question is not "can you recover more by messaging more" — it is what you do with the
three contacts a customer is legally allowed.

We were prepared to report the opposite, and did for several runs: earlier revisions of
this document said the gate cost 12% of achievable lift. That number came from a
configuration where blocked actions taught the agent nothing, so it kept proposing them.
Once refusals became a learning signal, the gate stopped looking expensive. **The cost of
governance was our bug, not governance.**

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

## 6. What the LLM actually contributes

**Nothing measurable, on lift.** Ablation 4 removes the language model entirely and the
result is **+5.67pp against the full agent's +5.48pp** — marginally *better* without it,
well inside the interval ([+3.50, +7.83] against [+3.30, +7.66]).

### The number has moved four times, and that is the finding

| Configuration of the rest of the system | LLM contribution |
|---|---:|
| invented contact-fatigue curve, hand-picked trust weight | +8% |
| fatigue curve fitted to 86,399 real records | −9% |
| trust weight learned by a meta-bandit instead of chosen | +5% |
| every customer message actually sending | +0% |
| **the compliance contract actually enforced** | **−3%** |

Every row is a real defect fixed somewhere *other than* the model. The fourth is the most
embarrassing and the most instructive: for several runs the agent selected 607 nudges,
**composed none of them**, and was charged and scored for all 607 anyway (section 9.8 and
`BUILD_NOTES` section R). With that corrected, the model's apparent contribution collapsed
to zero.

**The honest conclusion is that the LLM's measured value was largely an artifact of other
bugs in our own system.** Each time we made the surrounding machinery more correct, the
model's edge shrank. An effect that only appears when the rest of the system is broken is
not an effect.

### What survives

Two things, neither of them a headline.

**Cost efficiency.** Even this is now marginal: the full agent recovers at ₹534 per
marginal recovery against the rules-only ₹526 — very slightly *worse*. Whatever edge the model had on efficiency has
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
evaluation is re-run across five parameterisations.

| Parameterisation | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---|---:|
| baseline (calibrated) | 73.0% | +5.48pp | [+3.30, +7.66] | ₹13,89,199 |
| pessimistic: high self-recovery | 82.7% | +5.13pp | [+3.28, +6.97] | ₹13,28,671 |
| optimistic: low self-recovery | 53.9% | +7.52pp | [+5.08, +9.97] | ₹19,89,953 |
| **weak interventions** | 73.0% | **+0.32pp** | **[−1.91, +2.54]** | **₹6,363** |
| hard failure mix + noisier labels | 58.3% | +9.36pp | [+6.97, +11.71] | ₹25,02,039 |

**Envelope: +0.32 to +9.36pp. Worst-case 95% lower bound: −1.91pp.**

Under a pessimistic model of what dunning can achieve *at all*, the effect collapses to
+0.32pp on an interval comfortably containing zero. It is no longer *negative* — earlier
revisions of this table showed −1.58pp, measured before the messaging and compliance
defects were fixed — but "if messages barely move anyone, we cannot show this works"
remains true and remains here.

The row to read sceptically is the last one. "Hard failure mix + noisier labels" was built
to be the worst realistic world — mostly dead instruments and risk declines, with an
unreliable reason code — and Recura posts its **highest** lift there, +9.36pp. That is not
the agent doing better; it is the holdout doing worse. Self-recovery falls to 58.3%, so
there is more left on the table. Lift is a difference, and differences grow when the
baseline drops. Read the net-incremental column alongside it.

Note that the sweep runs the deterministic rules path, since a re-parameterised cohort
cannot hit the committed fixture cache. Given that ablation 4 measures the LLM's
contribution at zero, that is no longer a meaningful caveat.

---

## 8. What different policy contracts would cost

`make replay`. Because every decision and verdict is in an append-only ledger, we can
re-run the cohort under an altered contract and diff the outcome.

| Policy variant | Lift | Net incremental | vs shipped | Blocked | Contacts/cust |
|---|---:|---:|---:|---:|---:|
| **as committed** | +5.48pp | ₹13,89,199 | — | 6,536 | 0.248 |
| TRAI-only window (to 21:00) | +5.05pp | ₹12,67,538 | **−₹1,21,661** | 6,491 | 0.248 |
| stricter: 1 contact / week | +4.75pp | ₹12,19,337 | −₹1,69,862 | 6,714 | 0.207 |
| looser: 5 contacts / week | +5.13pp | ₹12,89,050 | −₹1,00,149 | 6,577 | 0.245 |
| **no merchant spend cap at all** | +5.48pp | ₹13,89,199 | **₹0** | 6,536 | 0.248 |
| spend cap 5× tighter (₹5k/day) | +4.07pp | ₹10,59,673 | −₹3,29,526 | 9,947 | 0.156 |
| spend cap 25× tighter (₹1k/day) | **−0.29pp** | −₹1,10,634 | −₹14,99,833 | 15,216 | 0.044 |
| no human escalation at all | +1.96pp | ₹5,48,910 | **−₹8,40,288** | 8,016 | 0.065 |
| retry risk declines anyway | +5.46pp | ₹13,71,651 | −₹17,548 | 5,913 | 0.247 |

Four of these are worth stating plainly.

**The stricter regulatory reading is not a cost — it earns ₹1,21,661.** We contact between
09:00 and 19:00, taking RBI's Fair Practices bound rather than TRAI's more permissive
21:00. Replaying under the looser window recovers *less*. Once a per-customer contact cap
binds, extra evening hours do not buy extra contacts; they only move the same permitted
contacts into hours that convert worse.

This reverses what this document said in an earlier revision, when the looser window
appeared to gain ₹61,981. That measurement was taken while the contact cap was counted per
episode rather than per customer, so extra hours really did buy extra contacts — contacts
the contract did not permit. **We were quoting the benefit of a compliance bug.**

**Removing the merchant spend cap entirely changes the result by exactly ₹0.** The agent
already stops below that limit on its own: attention cost prices the risk of losing the
customer, and expected value prices the rest. **On spend, the arithmetic binds before the
contract does** — the gate is a backstop, not the thing doing the work.

Note carefully that this is no longer true of the *contact* cap. Loosening it to five per
week costs ₹1,00,149, which means the contact contract is now genuinely binding rather than
decorative. An earlier revision claimed both limits bought exactly ₹0 and called it the
strongest single demonstration in the document. Half of that claim was an artefact of the
cap not working; the half that survives is the spend cap.

**Under-budgeting is actively value-destroying.** At a 25× tighter spend cap the agent
posts −0.29pp and a cost per recovery of ₹25,949: it exhausts a symbolic budget on whatever
it reaches first and never gets to the cases that matter. A merchant's instinct to cap
recovery spend tightly is exactly wrong.

**Removing human escalation costs 60% of the value** — ₹8,40,288 of ₹13,89,199. See failure
case 1 below.

---

## 9. Where this breaks

Nine honest failure cases.

### 1. Sixty percent of the value depends on human availability

With `escalation.max_per_day: 0` the lift collapses from +5.48pp to **+1.96pp** — no
longer statistically significant — costing ₹9.84 lakh of ₹10.83 lakh.

Recura is a decision layer that routes work to people, not a system that replaces them.
A merchant with no collections staff gets very little of this. We would rather state
that than let the headline imply autonomy the system does not have.

### 2. Under a pessimistic response model the effect vanishes

The "weak interventions" sweep row posts +0.32pp on an interval of [−1.91, +2.54], and
₹6,363 of net incremental — indistinguishable from nothing. If dunning genuinely moves the
needle less than our calibrated assumptions suggest, **this system does nothing and the
money spent running it is wasted**. Nothing in our evidence rules that out.

Earlier revisions reported this row as −1.58pp, i.e. actively value-destroying. That was
measured while the agent was paying for messages it never sent and contacting people more
often than its own contract allowed. The row is no longer negative, but it still contains
zero, and "no effect" is the honest reading.

### 3. The holdout recovers 73% unaided, which is probably too generous

Our response model treats five recovery opportunities as independent draws. Real
customers self-select — someone who did not pay on attempt one is less likely to pay
unprompted on attempt four. The error is conservative in direction but the absolute
rates are not forecasts.

### 4. The language model contributes 5%, and only because trust is learned

Removing it costs 5%. That figure has moved three times: +8% with our original invented
fatigue curve, −9% once that curve was fitted to 86,399 real records, and +5% once the
agent stopped using a hand-picked trust weight and learned one.

The middle reading matters most. Correcting an unrelated parameter flipped the model's
apparent contribution from helpful to harmful — an effect that fragile was never an
effect, and we would not have known without the calibration work.

The learned trust posterior explains the third: `blended` — the hand-picked setting —
recovers 20.2% against 28.5% for taxonomy-only and 28.9% for the model. **We had chosen
the worst of three options** by splitting the difference between two better ones.

The model remains poorly calibrated (Brier 0.9838 against a 0.8196 base rate). Nothing
here should be read as evidence that a language model improves payment recovery. What it
shows is that a system which *measures* its model's contribution, and adapts how far it
trusts it, extracts value from a weak model without pretending the model is strong.

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

So the agent's observed channel mix (SMS 70.3%, WhatsApp 29.7%, voice never) is a cost
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

It is fixed (`BUILD_NOTES` section R): unwritable nudges are now scored as `NO_ACTION`, the
candidate set is filtered by what a registered template can actually carry, and
`messages_sent` and `template_failures` both appear in the metrics table so that the next
occurrence is a number rather than an absence. The run now composes 354 real messages and
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
It currently reports 0 breaches with a tightest gap of 24.2h. Details in `BUILD_NOTES`
section T.

**The reason this belongs in a failure section rather than a changelog:** every compliance
claim this project made before that fix was unverified, and several published numbers were
quantitatively wrong because of it — including the claim that the policy gate was free
(it costs 29%) and that the stricter TRAI reading cost us money (it earns ₹1.2 lakh).

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

No API key is required for any of it. `fixtures/` contains 870 cached model responses,
keyed by SHA-256 of (model, prompt, payload), so the LLM path replays exactly rather
than being re-queried. A key is needed only to regenerate them.

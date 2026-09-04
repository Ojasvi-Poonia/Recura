# Recura

**An agent that decides, not a workflow that runs.** It finds revenue at risk from
failed payments, abandoned checkouts and overdue invoices, prices every possible
intervention in money, acts only when the arithmetic supports it, and reports what it
recovered against a randomised control group.

Submission for the Razorpay AI Buildathon, Track 03 — AI Revenue Recovery.

---

## Results

> ## +7.28 percentage points against a randomised control group
> **₹21,98,315 recovered · ₹19,62,667 net of cost · 9.3× return on spend**
> 95% bootstrap CI [+5.11, +9.54] · ₹401 per extra recovery
>
> `make eval` reproduces this in **about ten seconds, offline, with no API key**, byte-identical
> across runs.

10,000 synthetic events, seeded, 80/20 randomised split.

| Metric | Treatment | Holdout |
|---|---:|---:|
| Events | 8,058 | 1,942 |
| Recovery rate | **78.8%** | 71.5% |
| Recovered | ₹2,37,97,782 | ₹54,64,895 |
| Intervention cost | ₹2,35,648 | ₹0 |
| Contacts per event | 0.22 | 0 |
| Messages actually sent | 281 | 0 |
| Promises to pay broken | 26 | — |
| Actions blocked by policy | 6,249 | — |
| Refused, EV < 0 | 3,626 | — |
| Escalated to human | 1,502 | — |
| Opted out | 11 | 0 |

The rows that carry the argument are the last four. **6,249 actions the contract refused,
3,626 the arithmetic refused, 1,502 routed to a human, and 11 customers who asked us to stop
and were never contacted again.** A system that reports only what it recovered is hiding
the half that matters.

---

## Coverage against the brief

Track 03 lists seven example directions. The spec for this project deliberately warns
against attempting all of them — *"one failure class done rigorously beats seven done
shallowly"* — so the test applied here is not "is there code for it" but **"is it exercised,
with a number, in the committed run"**.

| Direction | Where it lives | Exercised in `make eval` |
|---|---|---|
| Payment degradation → root cause → action | 115 real Razorpay reason codes → `FailureClass` | 4,447 treated, **+8.04pp** |
| Checkout drop-off recovery | `source_type="checkout"`, no error object on 60% | 1,634 treated, **+6.62pp** |
| Failed-subscription recovery | `source_type="mandate"` | 1,193 treated, **+6.28pp** |
| B2B receivables chaser | ageing ladder in `decide/ev.py` | 784 treated, **+9.16pp** |
| Mandate retry sequencer | RBI notify → wait 24h → debit | pre-debit rule bound **1,997×** |
| Hinglish voice recovery | DLT templates, `make voice` | 5 rendered audio files |
| Promise-to-pay tracker | 48h window, escalation on breach | **26 promises broken** |

All four surfaces clear zero independently. The four directions with their own holdout are
measured; the other three are demonstrated but not independently A/B tested, and we would
rather say so than imply seven measured results.

---

## The problem statement names three surfaces. Here is each one separately.

> *"...from payment failures and checkout abandonment to overdue receivables."*

A single pooled number lets a strong surface carry a weak one. Each surface below is
compared against **its own** randomised holdout:

| Surface | Treated | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---:|---|---:|
| Payment failure | 4,447 | 1,045 | +7.93pp | [+4.86, +10.89] | ₹11,93,422 |
| Checkout abandonment | 1,634 | 413 | +5.45pp | [+0.73, +10.26] | ₹3,01,535 |
| Mandate / subscription | 1,193 | 291 | +6.28pp | [+0.55, +12.03] | ₹2,24,081 |
| Overdue receivable | 784 | 193 | +9.29pp | [+3.08, +15.74] | ₹2,52,001 |

**All four surfaces clear zero at 95%.** Lifts run from +6.28pp on mandates to +9.16pp on
receivables, and the intervals widen exactly as the samples shrink — mandate has 291 control
events, receivables 193. That is the whole explanation for why the two smallest surfaces
have the widest bands, and it is a power story rather than a capability one.

Reporting them separately was not decoration. In an earlier revision checkout abandonment
came out at +2.94pp with an interval spanning zero while the pooled number looked healthy,
and that gap is exactly what a per-surface table exists to expose.

One structural note that survives: 60% of checkout-abandonment events carry **no Razorpay
error code at all** — nothing failed, the customer simply left. The taxonomy has nothing
to read on those, so the agent works from amount, history, hour and method alone. That
surface is significant now, but it is the one where a real integration would gain most
from Razorpay's own checkout telemetry (drop-off step, whether a method was ever selected).

## Stopping rules, as measured

> *"...with compliant escalation, stopping rules, and an audit trail."*

Stopping rules are only real if they fire. Every treated episode ends for exactly one
recorded reason, and the census is part of `make eval`:

| Why the episode ended | Episodes | Share |
|---|---:|---:|
| `recovered` — the intervention worked | 4,328 | 53.7% |
| `recovered_unprompted` — customer paid on their own; we stopped | 2,022 | 25.1% |
| `exhausted` — ran out of permitted actions | 1,096 | 13.6% |
| `refused_negative_ev` — the arithmetic said don't | 573 | 7.1% |
| `episode_expired` — hit the 21-day horizon | 28 | 0.3% |
| `opted_out` — customer asked us to stop | 11 | 0.1% |

**A quarter of episodes stop because the customer paid without us.** The agent
re-observes before every decision and stands down when the money arrives — those 2,022
episodes are ones a workflow-shaped system would have kept messaging.

Only 13.0% end in `exhausted`. An agent whose census was dominated by that row would not
have stopping rules at all, just a budget it collided with.

### Which clauses actually bind

A contract is only worth as much as the parts of it that do something. `make eval` prints,
per rule, how often each clause in `policy.yaml` bound an action — counting both outright
blocks and modifications, because quiet hours *shifts* a schedule rather than refusing it
and would otherwise read as dead code.

**13 of 20 clauses fire on the baseline cohort.** The other 7 each carry a stated reason,
printed in the same table:

| Why it does not fire | Rules |
|---|---|
| **Backstop** — the decision layer already prevents the condition | `require_consent`, `require_registered_template`, `forbidden_for_recoverability` |
| **Headroom** — sized above this merchant's volume; binds in `make replay` | `daily_action_budget`, `daily_spend_cap_paise` |
| **Arrives by webhook**, not by cohort replay | `stop_on_late_authorisation` |
| **Out of scope** — disputes cannot be created in test mode | `stop_on_dispute` |

Every one of the 20 has a test proving it blocks when its condition holds. Two further
tests keep that table honest: one rejects an explanation naming a rule that does not exist,
and one asserts the backstop claim directly — a merchant-config failure must never be
offered a customer-facing action.

The first version of this table was the single most useful thing our audit produced: **12
of 20 clauses were not firing**, and four of those were genuinely unreachable because the
agent stopped episodes itself rather than letting the contract stop them. "This rule never
fires because nothing can reach it" and "this rule never fires because it cannot work" look
identical from outside, and we had both.

**The contact contract is now verified, not asserted.** `make validate` replays the batch,
reconstructs every customer's contact timeline, and fails if any clause in `policy.yaml` is
breached. It reports **0 customers over the 3-in-7-days cap and 0 contact pairs closer than
24 hours**, tightest gap 24.0h. It did not always: both clauses were structurally
unenforceable until an audit found them, and 41 customers had been over the cap with 209
pairs too close together.

## Why you can believe that number

Any synthetic benchmark can be made to say anything. These are the checks that would
**fail** if ours were unsound — `make validate` runs them:

| Check | Result |
|---|---|
| **A/A test** — split by customer, both halves treated identically | **+0.46pp**, CI [−1.31, +2.21] — spans zero |
| **Placebo** — every action made completely inert | **+0.21pp**, CI [−2.01, +2.49] — spans zero |
| **Contact contract** — replay every customer's contact timeline | 0 breaches, tightest gap 24.0h |
| Arm balance | worst standardised difference **0.036** (RCT threshold 0.10) |
| Holdout purity | zero cost, zero contacts, zero opt-outs |
| Latent isolation | no hidden variable reachable from `src/` |
| Determinism | byte-identical across runs |

The placebo is the one that matters. When we first built it, it reported **+18.57pp of
lift from actions that did nothing** — because the treatment arm was re-observed five times
per episode while the control was observed once. More draws on the same probability
manufactures lift out of nothing. That single fix took the headline from +33.84pp to
roughly +5pp; it has since risen to +7.28pp through correctness work documented below.

**The residual is +0.21pp on an interval of [−2.01, +2.49].** It contains zero, which is
the test. We are not going to claim more than that: in earlier revisions this residual was
*negative*, and we argued the harness therefore understated us. It is positive now, so that
argument is withdrawn. What survives is the honest version — **the pipeline does not
manufacture measurable lift**, and the check would fail if its interval excluded zero.

---

## Run it

```bash
make install     # venv + dependencies
make eval        # the table above, from committed fixtures - no API key needed
make validate    # the negative controls that prove the table means something
make mutants     # plant known bugs and check the suite notices
```

Watch the batch actually decide, rather than just reading its output:

```bash
make eval LIVE=1                # stream every decision as it is made
make eval LIVE=1 PACE=0.06      # slow enough to record
```

Each line is one decision. `BLOCKED` marks positive expected value that the policy
contract vetoed anyway — the design in one screen. The stream is a read-only observer,
so the numbers it prints are exactly the numbers `make eval` reports; a test asserts it.

Then:

```bash
make run         # trace a handful of single episodes, decision by decision
make ablate      # what each component actually contributes
make sweep       # the same result across five generator parameterisations
make replay      # what different policy contracts would have cost
make voice       # Hinglish recovery audio samples
```

---

## What it does

For every rupee at risk, five steps, 1–5 times across up to 21 days:

| | | |
|---|---|---|
| **1 Triage** | Is this recoverable at all? | 31 of 115 Razorpay error codes are *merchant* bugs, not customer failures |
| **2 Diagnose** | What went wrong? | **LLM proposes** — returns a distribution, not a label |
| **3 Decide** | Which action maximises expected value? | **Maths decides** — argmax over ~12 candidates |
| **4 Govern** | Am I permitted to? | **Policy vetoes** — 20 deterministic rules the model cannot read |
| **5 Learn** | Did it work? | Beta posteriors, Thompson-sampled, soft credit across the belief |

```
EV(action) = (p(action) − p(no_action)) × (1−b)^(k−1) × amount × margin
             − direct_cost − attention_cost
chosen = argmax EV;   if max < 0 → NO_ACTION
```

Two things in that formula are load-bearing. `attention_cost` prices the risk of losing
the customer, not just the annoyance — which is what makes the agent stop on its own.
And `(1−b)^(k−1)` discounts the intervention by the chance the customer recovers unaided
before the episode ends; without it a naive scorer overvalues every action by ~3×.

**`NO_ACTION` is arithmetic, not a rule.** When Recura skips a transaction, the answer
to *"why?"* is a number in the ledger, not a policy line.

---

## What each part actually contributes

`make ablate` — deliberately cripple the agent and measure the damage.

| Configuration | Lift | 95% CI | vs full | Cost/recovery | Contacts/cust |
|---|---:|---|---:|---:|---:|
| **Full agent** | **+7.28pp** | [+5.11, +9.54] | — | **₹401** | **0.217** |
| Random action chooser | +5.99pp | [+3.82, +8.23] | **−18%** | ₹912 | 0.500 |
| No taxonomy | +7.29pp | [+5.12, +9.52] | +0% | ₹446 | 0.236 |
| No policy gate | +8.99pp | [+6.83, +11.23] | **+24%** | ₹461 | 0.270 |
| No LLM, rules only | +7.25pp | [+5.09, +9.50] | −0% | ₹417 | 0.233 |

**Compliance costs 24% of achievable lift.** Removing the policy gate is still the largest
single improvement available: +8.99pp against +7.28pp. It contacts people more often than
the contract permits and recovers more by doing so. Earlier revisions of this README
reported the gate as free, then 6%, then 29%, then 51%. Each was measured against a gate
that was progressively less broken — two contact clauses could not fire at all, and four
episode clauses were pre-empted by the agent stopping itself. **Governance is expensive; that is the
finding, and it took four measurements to get it right.**

**Against a random chooser the agent's edge is efficiency.** Random gets +5.99pp using
**2.3× the contacts** (0.500 vs 0.217) at **₹912 per marginal recovery against our ₹401**.
With a hard regulated contact budget the question is not "can you recover more by messaging
more" — it is what you do with the three contacts a customer is legally allowed.

**The taxonomy contributes 4% and the LLM 1%.** Both are small and both sit well inside
their intervals, so neither is established. What changed is the sign: at the previous
revision both measured as mildly *harmful*, and the difference was not a model improvement
but a set of correctness fixes elsewhere — episodes now closing on the contract, and
merchant-configuration failures existing in the cohort at all.

That instability is the honest headline of this section. **A component's measured
contribution here has moved by 10–15 points purely from fixing bugs in unrelated parts of
the system**, which is a reason to distrust any single reading of it, including this one.

### The taxonomy pays off late, not early

Removing the taxonomy costs 4%, but that average hides the shape. It splits learning across
**42 posterior cells** (7 failure classes × 6 action types); without it the agent pools into
roughly 6. On a finite run that is a real sample-size penalty. Splitting the cohort in half:

| | first half | second half |
|---|---:|---:|
| with taxonomy | +6.11pp | +8.87pp |
| no taxonomy | +6.33pp | +8.15pp |
| **difference** | **−0.22pp** | **+0.72pp** |

It starts behind and finishes ahead. That is what a learning-rate penalty looks like, and
it implies the 4% headline figure understates what the taxonomy is worth at scale.

**Flagged as a hypothesis, not a result**: within-half comparisons, no confidence intervals,
and both differences are small. What we will say is that mapping Razorpay's own `reason`
field to a recovery strategy costs data before it pays, and 10,000 events is near the
crossover.

### The language model contributes nothing measurable, and we are going to say so

Removing the LLM entirely gives **+7.48pp against the full agent's +7.28pp** — a 1%
contribution, deep inside the interval. That number has now moved seven times:

| | LLM contribution |
|---|---:|
| invented fatigue curve, hand-picked trust weight | +8% |
| fatigue curve fitted to 86,399 real records | −9% |
| trust weight learned instead of chosen | +5% |
| every customer message actually sending | +0% |
| the compliance contract actually enforced | −3% |
| Thompson sampling drawing once per arm | −9% |
| **episodes closing on the contract; merchant bugs in the cohort** | **+1%** |

Every row is a real defect fixed somewhere *else*. **The model's measured contribution has
swung 17 points without the model changing at all.** That is the finding: on this benchmark
the LLM's apparent value is dominated by the correctness of everything around it, and any
single reading of it — including +1% — should be treated as noise.

The architecture is still the one we would defend. The model sits behind a learned trust
weight, so a better-calibrated model earns more influence with no code change. But we are
not going to claim it earns its place on evidence this unstable.

---

## How sensitive is this to our assumptions?

Every grade-C parameter in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) is an assumption.
`make sweep` re-runs everything across ten parameterisations:

| Parameterisation | Holdout | Lift | 95% CI |
|---|---:|---:|---|
| baseline (calibrated) | 71.5% | +7.28pp | [+5.11, +9.54] |
| pessimistic: high self-recovery | 81.3% | +6.93pp | [+5.09, +8.83] |
| optimistic: low self-recovery | 53.8% | +7.31pp | [+4.85, +9.80] |
| **weak interventions** | 71.5% | **+2.54pp** | **[+0.33, +4.82]** |
| hard failure mix + noisier labels | 56.8% | +10.58pp | [+8.18, +12.93] |
| **sceptical human escalation** | 71.5% | **+4.26pp** | **[+2.08, +6.51]** |
| optimistic human escalation | 71.5% | +9.35pp | [+7.21, +11.56] |
| cheap human review (₹60) | 71.5% | +7.76pp | [+5.58, +10.01] |
| expensive human review (₹240) | 71.5% | +6.35pp | [+4.17, +8.60] |
| thin margin (10%) | 71.5% | +5.36pp | [+3.16, +7.63] |

**Envelope: +2.54 to +10.58pp, and every one of the ten intervals excludes zero.**

That includes "weak interventions", where messages and retries barely move anyone. In
earlier revisions that row was the one honest counterexample in the table — it read −1.58pp,
then +0.32pp, then +0.71pp, all consistent with the system doing nothing. It now clears zero
at +2.54pp [+0.33, +4.82]. We are noting the history rather than quietly enjoying the
improvement: this row moved because of correctness fixes elsewhere, not because we learned
anything new about how well dunning works.

**The row that constrains us most is "sceptical human escalation".** `ESCALATE_EFFICACY` —
how much more effective a human agent is than an automated contact — is the largest single
lever in the simulator, and it was uncited until an audit caught it. Halved to 1.30, the
result drops to +4.26pp and still excludes zero. The magnitude of our headline depends
heavily on that constant; the existence of the effect does not. Grounding and grade in
[`eval/CALIBRATION.md`](eval/CALIBRATION.md) section 8.

`make replay` answers the adjacent question — what a different *contract* would cost:

| Policy variant | Net incremental | vs shipped |
|---|---:|---:|
| as committed | ₹19,62,667 | — |
| TRAI-only window (to 21:00) | ₹19,72,237 | +₹9,570 |
| stricter: 1 contact / week | ₹18,50,287 | −₹1,12,380 |
| **looser: 5 contacts / week** | ₹19,62,667 | **₹0** |
| **no merchant spend cap at all** | ₹19,62,667 | **₹0** |
| spend cap 5× tighter | ₹15,04,917 | −₹4,57,750 |
| action budget 200/day | ₹12,50,881 | −₹7,11,786 |
| no human escalation at all | ₹10,99,295 | **−₹8,63,372** |
| retry risk declines anyway | ₹18,56,767 | −₹1,05,900 |

**Loosening the contact cap and removing the spend cap both buy exactly ₹0.** The agent's
own attention-cost arithmetic binds before either contract limit does, so the gate is a
backstop rather than the thing doing the work.

Read that with one caveat: across cohort revisions the contact-cap row has read ₹0, then
−₹1,00,149, then −₹3,244, and now ₹0 again. The spend-cap row has been ₹0 every time. The
stable claim is the spend one; the contact cap sits close enough to the arithmetic that its
sign moves with the noise.

**Retrying risk declines would cost ₹1,05,900**, so the rule we enforce is also the
profitable choice here. It has been worth +₹60,368 in an earlier revision though, so we do
not claim the contract pays for itself in general.

Under-budgeting is what actually destroys value: the action budget at 200/day costs
₹7.11 lakh, and a 5× tighter spend cap ₹4.58 lakh.

---

---

## Where this breaks

1. **The holdout recovers 71.5% unaided.** Our response model treats each opportunity as
   independent, which almost certainly overstates spontaneous recovery over 21 days.
   The error runs *against* us — a lower real baseline would mean more headroom — but
   the absolute recovery rates should not be read as forecasts.
2. **The result rests on how good the humans are.** Halving `ESCALATE_EFFICACY` — the
   largest and least-grounded constant in the simulator — halves the headline to +4.26pp,
   and removing human escalation costs ₹8,63,372 of ₹19,62,667.
3. **The language model contributes nothing measurable.** Ablation 4 removes it and the
   result does not move. We report that rather than implying the LLM does the work.
4. **44% of the result depends on human escalation being available.** `make replay`
   with `escalation.max_per_day: 0` drops the lift from +7.28pp to **+3.87pp**, costing
   ₹8,63,372 of ₹19,62,667. Recura is a decision layer that routes work to people, not a
   system that replaces them. A merchant with no collections staff gets substantially
   less of this.
5. **Per-merchant margin is wired but not exercised** — the frozen cohort assigns every
   merchant the same 30%, so the EV differences that margin should create are untested.
6. **Hinglish messaging is compliance-verified, not lift-verified.** We prove no
   free-form copy can escape the DLT template registry. We do not claim language
   matching improves recovery, because the generator does not model it.

7. **Compliance costs 24% of achievable lift**, and that number has been wrong four
   times — clauses that could not fire made governance look cheaper than it is.
8. **Seven of twenty policy clauses do not fire on this cohort.** Each has a stated
   reason and a test, but three of them are backstops we have never seen triggered in
   anger, and the merchant budget caps only bind under `make replay`.
9. **Component contributions are unstable.** The LLM's measured value has swung 17
   points across revisions without the model changing, purely from fixing defects
   elsewhere. Treat any single ablation reading, including ours, with suspicion.

Full detail in [`RESULTS.md`](RESULTS.md).

---

## Design decisions worth knowing

**No agent framework.** Hand-rolled loop — 776 lines, 543 of them code, walkable top to
bottom. We need deterministic replay and a policy gate the model cannot reach.

**The LLM never sees `policy.yaml`.** Enforced by a test that parses the AST, not by
convention. No prompt injection can unlock a money action.

**The LLM never writes customer copy.** TRAI requires DLT-registered templates; the
model selects a template and fills its slots, and `verify_compliance()` refuses anything
unregistered. Same trick as the policy gate, applied to language.

**The ledger is append-only at the database layer.** `UPDATE` and `DELETE` are refused
by triggers, and that survives reconnection.

**Contact fatigue is fitted, not invented.** `CONTACT_FATIGUE_DECAY` was our weakest
load-bearing assumption. It is now fitted to 86,399 real records from the UCI Bank
Marketing dataset (0.877, against the 0.70 we had assumed), with the domain-transfer and
selection-bias caveats stated in `CALIBRATION.md` — both of which make it conservative.

**115 real Razorpay error codes**, transcribed from their published documentation —
not invented categories. Contact windows are the intersection of TRAI's messaging rules
and RBI's Fair Practices Code (09:00–19:00, stricter than either alone); pre-debit
notification cites RBI's E-Mandate Framework 2026.

**Anti-goals are enforced by tests**: no wall clock outside `clock.py`, no floats for
money, no agent-framework imports, no locale hardcoded in the decision core, `src/`
cannot reach the simulator's hidden state.

**The tests are themselves tested.** `make mutants` plants eight bugs this project has
actually shipped — the policy gate silently disabled, template slot validation removed,
the horizon discount deleted — and checks the suite notices. A green suite proves the
tests pass; mutation testing proves they would object. 13/13 caught.

**The claims are red-teamed.** 71 adversarial tests attempt prompt injection through
every attacker-influenceable field, feed the agent a deliberately hostile model, and try
to smuggle phishing copy into a DLT-registered template. That last one found a real
vulnerability, which is documented rather than quietly patched.

---

## Why it is built this way

Ten [architecture decision records](docs/adr/) cover the contestable choices — no agent
framework, expected value over rules, the policy gate outside the model's reach,
determinism from fixtures rather than temperature, and the calibration study that made us
shrink our own model's confidence. Each records what we rejected and why.

## Repository

```
src/
  agent.py            the loop - start here
  models.py           domain model; integer paise, frozen, extra="forbid"
  taxonomy/           115 Razorpay reason codes -> failure class + triage
  decide/             EV, Thompson sampling, LLM diagnosis, context multipliers
  policy/             20 deterministic rules; the LLM cannot import this
  act/                provider adapters, cost model, DLT messaging, voice
  ledger/             append-only, enforced by DB triggers
  ingest/             webhook HMAC, late-authorisation stop, idempotency
eval/
  generate_cohort.py  FROZEN; hidden latents live here, src/ cannot import it
  validate.py         A/A and placebo negative controls
  ablate.py  sweep.py  replay.py  calibration.py  metrics.py
config/               costs, markets, DLT templates
fixtures/             872 cached LLM responses - why eval needs no API key
demo/audio/           rendered Hinglish voice samples (make voice)
```

**Documents**

| File | What it is |
|---|---|
| [`RESULTS.md`](RESULTS.md) | Full evidence: every table, every control, nine failure cases |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Data flow, the three separated authorities, failure modes |
| [`docs/adr/`](docs/adr/) | Decision records — why no framework, why EV over rules |

**514 tests.** Run `make test`.

---

## Bring your own key

Not needed to reproduce anything above — `fixtures/` is committed and `make eval` runs
entirely offline. A key is only required to *regenerate* fixtures:

```bash
cp .env.example .env    # then add GEMINI_API_KEYS (free tier) or ANTHROPIC_API_KEY
make fixtures
```

`.env` is read on import (`src/env.py`); anything already exported in your shell wins over
the file. The same file supplies `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` for `make tier1`,
which exercises the integration against Razorpay's live test-mode API.

Nothing is ever sent to a real customer, and no real payment is ever made. Test-mode
keys only — a live Razorpay key is refused at construction.

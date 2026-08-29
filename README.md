# Recura

**An agent that decides, not a workflow that runs.** It finds revenue at risk from
failed payments, abandoned checkouts and overdue invoices, prices every possible
intervention in money, acts only when the arithmetic supports it, and reports what it
recovered against a randomised control group.

Submission for the Razorpay AI Buildathon, Track 03 — AI Revenue Recovery.

---

## Results

10,000 synthetic events, seeded, 80/20 randomised split. `make eval` reproduces this
exactly, offline, with no API key.

| Metric | Treatment | Holdout |
|---|---:|---:|
| Events | 8,053 | 1,947 |
| Recovery rate | **78.4%** | 73.0% |
| Recovered | ₹2,44,53,999 | ₹51,25,121 |
| Intervention cost | ₹2,63,582 | ₹0 |
| Contacts per customer | 0.28 | 0 |
| Messages actually sent | 419 | 0 |
| Actions blocked by policy | 6,229 | — |
| Refused, EV < 0 | 3,681 | — |
| Escalated to human | 1,782 | — |
| Opted out | 14 | 0 |

> ### +5.33 percentage points — 95% CI [+3.14, +7.52]
> **₹16,64,066 incremental recovered · ₹14,00,484 net · 6.3× return on spend**
> Cost per extra recovery: ₹614. Runs in 4 seconds. Byte-identical across runs.

---

## The problem statement names three surfaces. Here is each one separately.

> *"...from payment failures and checkout abandonment to overdue receivables."*

A single pooled number lets a strong surface carry a weak one. Each surface below is
compared against **its own** randomised holdout:

| Surface | Treated | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---:|---|---:|
| Payment failure | 4,477 | 1,057 | +5.34pp | [+2.45, +8.28] | ₹8,09,708 |
| Checkout abandonment | 1,629 | 414 | +5.70pp | [+0.97, +10.33] | ₹3,00,646 |
| Mandate / subscription | 1,158 | 287 | +5.00pp | [−0.65, +10.76] | ₹1,70,772 |
| Overdue receivable | 789 | 189 | +5.08pp | [−0.82, +11.26] | ₹1,21,179 |

**The effect is consistent across all four surfaces** — every point estimate sits between
+5.00 and +5.70pp. What differs is confidence, and that is purely sample size: the two
smallest surfaces have 287 and 189 holdout events, which is not enough to exclude zero at
95% no matter how real the effect is.

So the honest statement is **not** "we are worse at receivables". It is *"we measure the
same effect on receivables and cannot yet prove it at 95% on 189 control events"*. The
fix is more data, not more agent, and we would rather say that than quietly report only
the pooled number that hides it.

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
| `recovered` — the intervention worked | 4,267 | 53.0% |
| `recovered_unprompted` — customer paid on their own; we stopped | 2,044 | 25.4% |
| `exhausted` — ran out of permitted actions | 1,148 | 14.3% |
| `refused_negative_ev` — the arithmetic said don't | 531 | 6.6% |
| `episode_expired` — hit the 21-day horizon | 49 | 0.6% |
| `opted_out` — customer asked us to stop | 14 | 0.2% |

**A quarter of episodes stop because the customer paid without us.** The agent
re-observes before every decision and stands down when the money arrives — those 2,044
episodes are ones a workflow-shaped system would have kept messaging.

Only 14.3% end in `exhausted`. An agent whose census was dominated by that row would not
have stopping rules at all, just a budget it collided with.

**The contact contract is now verified, not asserted.** `make validate` replays the batch,
reconstructs every customer's contact timeline, and fails if any clause in `policy.yaml` is
breached. It reports **0 customers over the 3-in-7-days cap and 0 contact pairs closer than
24 hours**, tightest gap 24.0h. It did not always: both clauses were structurally
unenforceable until an audit found them, and 41 customers had been over the cap with 209
pairs too close together ([BUILD_NOTES](BUILD_NOTES.md) section T).

## Why you can believe that number

Any synthetic benchmark can be made to say anything. These are the checks that would
**fail** if ours were unsound — `make validate` runs them:

| Check | Result |
|---|---|
| **A/A test** — split by customer, both halves treated identically | **−0.09pp**, CI [−1.89, +1.72] — spans zero, no phantom lift |
| **Placebo** — every action made completely inert | **−1.71pp**, CI [−3.92, +0.52] — *negative*, so the harness understates us |
| Arm balance | worst standardised difference **0.054** (RCT threshold 0.10) |
| Holdout purity | zero cost, zero contacts, zero opt-outs |
| **Contact contract** | 0 customers over the 3-in-7d cap, 0 pairs closer than 24h |
| Latent isolation | no hidden variable reachable from `src/` |
| Determinism | byte-identical across runs |

The placebo control is the one that matters. When we first built it, it reported
**+18.57pp of lift from actions that did nothing** — because the treatment arm was
re-observed five times across an episode while the control arm was observed once. More
draws on the same probability manufactures lift out of nothing. Fixing it cut our
headline from +33.84pp to under +5pp. We would rather have found that than have you
find it.

The residual is **negative**, and the direction matters more than the size: under a
placebo the harness scores treatment *below* control, so every number above is
conservative.

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
make run         # trace three single episodes: one acted, one refused, one BLOCKED
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
| **Full agent** | **+5.33pp** | [+3.14, +7.52] | — | ₹614 | 0.281 |
| Random action chooser | +3.74pp | [+1.60, +5.94] | **−30%** | ₹1,035 | 0.511 |
| No taxonomy | +5.78pp | [+3.61, +7.98] | +8% | ₹521 | 0.227 |
| No policy gate | +8.03pp | [+5.85, +10.23] | **+51%** | ₹583 | 0.329 |
| No LLM, rules only | +5.79pp | [+3.62, +7.96] | +9% | ₹550 | — |

**Three of four ablations beat the full agent.** We are publishing that as the headline of
this section rather than burying it, because it is the most informative thing we measured.

**Compliance costs 51% of achievable lift.** Removing the policy gate is by far the largest
single improvement available: +8.03pp against +5.33pp. It contacts people more often than
the contract permits, outside permitted hours, and recovers substantially more by doing so.
Earlier revisions of this README reported the gate as free, then as costing 6%, then 29%.
Each of those was measured against a gate that was progressively less broken — two of its
contact clauses could not fire at all until an audit found them
([BUILD_NOTES](BUILD_NOTES.md) section T). **Governance is expensive. That is the finding.**

**Against a random chooser the agent's value is efficiency, not recovery.** Random gets
+3.74pp using **1.8× the contacts** (0.511 vs 0.281) at **₹1,035 per marginal recovery
against our ₹614**. With a hard regulated contact budget the question is not "can you
recover more by messaging more" — it is what you do with the three contacts a customer is
legally allowed.

### The taxonomy and the LLM both currently measure as slightly harmful

Removing either improves the result by 8–9%. Both intervals overlap the full agent's
almost entirely, so the honest reading is "no measurable benefit", not "actively harmful" —
but it is certainly not the result we wanted.

For the LLM this is the fifth consecutive measurement trending toward zero, and we have
stopped arguing with it (see below).

For the taxonomy we have one specific, testable explanation. It splits learning across
**42 posterior cells** (7 failure classes × 6 action types); without it the agent pools
into roughly 6. On a 10,000-event run that is a real sample-size penalty, and it should
disappear with more data. Splitting the run in half:

| | first half | second half |
|---|---:|---:|
| with taxonomy | +6.88pp | +4.73pp |
| no taxonomy | +7.57pp | +4.01pp |
| **difference** | **−0.69pp** | **+0.72pp** |

The taxonomy starts behind and ends ahead, crossing over partway through — consistent with
it needing more data to fill its cells. **We are flagging this as a hypothesis, not a
result**: these are within-half comparisons without confidence intervals, and both
differences are small. The honest statement today is that the taxonomy does not pay for
itself inside 10,000 events, and we have a specific reason to expect it would at scale.

### The language model contributes nothing measurable, and we are going to say so

Removing the LLM entirely gives **+5.79pp against the full agent's +5.33pp**. That number
has now moved six times, and the pattern is the finding:

| | LLM contribution |
|---|---:|
| invented fatigue curve, hand-picked trust weight | +8% |
| fatigue curve fitted to 86,399 real records | −9% |
| trust weight learned instead of chosen | +5% |
| every customer message actually sending | +0% |
| the compliance contract actually enforced | −3% |
| **Thompson sampling drawing once per arm** | **−9%** |

Every row is a real defect fixed somewhere *else* in the system. **Each time we made the
surrounding machinery more correct, the model's apparent contribution shrank.** An effect
that only survives while the rest of the system is broken was never an effect.

The architecture is still the one we would defend — the model sits behind a learned trust
weight, so a better-calibrated model earns more influence with no code change. But the
evidence today does not support claiming it earns its place, and we are not going to imply
otherwise.

---

## How sensitive is this to our assumptions?

Every grade-C parameter in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) is an assumption.
`make sweep` re-runs everything across five parameterisations:

| Parameterisation | Holdout | Lift | 95% CI |
|---|---:|---:|---|
| baseline (calibrated) | 73.0% | +5.33pp | [+3.14, +7.52] |
| pessimistic: high self-recovery | 82.7% | +5.10pp | [+3.26, +6.96] |
| optimistic: low self-recovery | 53.9% | +7.71pp | [+5.27, +10.18] |
| **weak interventions** | 73.0% | **+0.71pp** | **[−1.50, +2.94]** |
| hard failure mix + noisier labels | 58.3% | +9.17pp | [+6.79, +11.52] |
| **sceptical human escalation** | 73.0% | **+2.86pp** | **[+0.67, +5.06]** |
| optimistic human escalation | 73.0% | +8.11pp | [+5.95, +10.28] |
| cheap human review (₹60) | 73.0% | +5.99pp | [+3.81, +8.18] |
| expensive human review (₹240) | 73.0% | +4.71pp | [+2.52, +6.89] |
| thin margin (10%) | 73.0% | +4.10pp | [+1.92, +6.30] |

**Envelope: +0.71 to +9.17pp.** Nine parameterisations, eight of which exclude zero.

**The row that matters most is "sceptical human escalation".** Removing human escalation
costs 38% of net value, so the single biggest lever in the simulator is
`ESCALATE_EFFICACY = 2.60` — how much more effective a human agent is than an automated
contact. That constant was uncited and, until an audit caught it, the sweep only moved it
as part of a group of four, so it could never answer "how much of this rests on that one
number?"

It now moves alone. **At `ESCALATE_EFFICACY = 1.30` — a human being no more effective than
an SMS — Recura still posts +2.86pp on an interval excluding zero.** The magnitude of our
result depends heavily on that constant; the existence of the effect does not. Full
grounding and grade in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) section 8.

Under "weak interventions", where messages and retries barely move anyone, the effect
collapses to +0.71pp on an interval containing zero. If that is the real world, this system
does nothing and the money spent running it is wasted.

`make replay` answers the adjacent question — what a different *contract* would cost:

| Policy variant | Net incremental | vs shipped |
|---|---:|---:|
| as committed | ₹14,00,484 | — |
| TRAI-only window (to 21:00) | ₹15,84,181 | +₹1,83,697 |
| stricter: 1 contact / week | ₹13,85,210 | −₹15,273 |
| looser: 5 contacts / week | ₹13,55,555 | −₹44,929 |
| **no merchant spend cap at all** | ₹14,00,484 | **₹0** |
| spend cap 5× tighter | ₹11,15,266 | −₹2,85,218 |
| spend cap 25× tighter | −₹1,22,719 | −₹15,23,202 |
| no human escalation at all | ₹8,62,665 | **−₹5,37,818** |
| retry risk declines anyway | ₹14,60,852 | +₹60,368 |

**Removing the merchant spend cap entirely buys exactly ₹0.** On spend, the agent's own
attention-cost arithmetic binds before the contract does — the cap is a backstop, not the
thing doing the work. This row has been ₹0 across every version of the cohort.

**The stricter regulatory reading costs ₹1,83,697.** We contact 09:00–19:00 on RBI's Fair
Practices bound rather than TRAI's more permissive 21:00, and it is not free.

Be careful with that number, though: across cohort revisions this row has been **+₹61,981,
then −₹1,21,661, now +₹1,83,697**. Its sign is not stable. The defensible statement is that
the cost of choosing the conservative regulatory reading is *small relative to the noise in
our own benchmark* — and we ship it regardless, because a contestable interpretation of
TRAI's exemption is not something we want to defend in front of a regulator.

**Retrying risk declines would earn ₹60,368**, and we forbid it anyway. An issuer risk
decline is a signal about the cardholder, not a transient failure; hammering it is how
merchants get their MIDs reviewed. This is the clearest case in the repo of the contract
overriding the arithmetic on purpose.

Under-budget the agent and it destroys value: at a 25× tighter spend cap it posts −0.32pp
and a negative net, exhausting a symbolic budget on whatever it reaches first.

---

## Where this breaks

1. **The holdout recovers 73% unaided.** Our response model treats each opportunity as
   independent, which almost certainly overstates spontaneous recovery over 21 days.
   The error runs *against* us — a lower real baseline would mean more headroom — but
   the absolute recovery rates should not be read as forecasts.
2. **Under weak interventions the effect vanishes** (+0.71pp, CI [−1.50, +2.94]). If
   messages and retries barely move anyone, we cannot show this works at all.
3. **The language model contributes nothing measurable.** Ablation 4 removes it and the
   result does not move. We report that rather than implying the LLM does the work.
4. **38% of the result depends on human escalation being available.** `make replay`
   with `escalation.max_per_day: 0` drops the lift from +5.33pp to **+2.90pp**, costing
   ₹5.38 lakh of ₹14.00 lakh. Recura is a decision layer that routes work to people, not
   a system that replaces them. If a merchant has no collections staff, most of this
   value does not exist.
5. **Per-merchant margin is wired but not exercised** — the frozen cohort assigns every
   merchant the same 30%, so the EV differences that margin should create are untested.
6. **Hinglish messaging is compliance-verified, not lift-verified.** We prove no
   free-form copy can escape the DLT template registry. We do not claim language
   matching improves recovery, because the generator does not model it.

7. **Compliance costs 51% of achievable lift**, and we can only say so now that the
   contract is genuinely enforced — two of its contact clauses were previously incapable
   of firing.
8. **Half the policy contract is never exercised.** `make eval` prints per-rule bind
   counts: 10 of 20 clauses never fire on this cohort. Some are defence-in-depth behind
   an earlier filter; others are simply untested.

Full detail in [`RESULTS.md`](RESULTS.md).

---

## Design decisions worth knowing

**No agent framework.** Hand-rolled loop — 525 lines, 391 of them code, walkable top to
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
fixtures/             870 cached LLM responses - why eval needs no API key
```

**509 tests.** Run `make test`.

---

## Bring your own key

Not needed to reproduce anything above — `fixtures/` is committed and `make eval` runs
entirely offline. A key is only required to *regenerate* fixtures:

```bash
cp .env.example .env    # then add GEMINI_API_KEYS (free tier) or ANTHROPIC_API_KEY
make fixtures
```

Nothing is ever sent to a real customer, and no real payment is ever made. Test-mode
keys only — a live Razorpay key is refused at construction.

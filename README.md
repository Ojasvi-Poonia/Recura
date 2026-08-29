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
| Recovery rate | **78.5%** | 73.0% |
| Recovered | ₹2,32,69,611 | ₹50,28,349 |
| Intervention cost | ₹2,35,427 | ₹0 |
| Contacts per customer | 0.25 | 0 |
| Messages actually sent | 354 | 0 |
| Actions blocked by policy | 6,536 | — |
| Refused, EV < 0 | 2,835 | — |
| Escalated to human | 1,595 | — |
| Opted out | 7 | 0 |

> ### +5.48 percentage points — 95% CI [+3.30, +7.66]
> **₹16,24,626 incremental recovered · ₹13,89,199 net · 6.9× return on spend**
> Cost per extra recovery: ₹534. Runs in 4 seconds. Byte-identical across runs.

---

## The problem statement names three surfaces. Here is each one separately.

> *"...from payment failures and checkout abandonment to overdue receivables."*

A single pooled number lets a strong surface carry a weak one. Each surface below is
compared against **its own** randomised holdout:

| Surface | Treated | Holdout | Lift | 95% CI | Net incremental |
|---|---:|---:|---:|---|---:|
| Payment failure | 4,477 | 1,057 | +5.61pp | [+2.69, +8.56] | ₹8,02,274 |
| Checkout abandonment | 1,629 | 414 | +5.52pp | [+0.88, +10.13] | ₹2,89,353 |
| Mandate / subscription | 1,158 | 287 | +5.34pp | [−0.30, +11.19] | ₹1,86,768 |
| Overdue receivable | 789 | 189 | +4.96pp | [−0.86, +11.16] | ₹1,11,214 |

**The effect is consistent across all four surfaces** — every point estimate sits between
+4.96 and +5.61pp. What differs is confidence, and that is purely sample size: the two
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
| `recovered` — the intervention worked | 4,453 | 55.3% |
| `recovered_unprompted` — customer paid on their own; we stopped | 1,870 | 23.2% |
| `exhausted` — ran out of permitted actions | 1,073 | 13.3% |
| `refused_negative_ev` — the arithmetic said don't | 603 | 7.5% |
| `episode_expired` — hit the 21-day horizon | 47 | 0.6% |
| `opted_out` — customer asked us to stop | 7 | 0.1% |

**Nearly a quarter of episodes stop because the customer paid without us.** The agent
re-observes before every decision and stands down when the money arrives — those 1,870
episodes are ones a workflow-shaped system would have kept messaging.

Only 13.3% end in `exhausted`. An agent whose census was dominated by that row would not
have stopping rules at all, just a budget it collided with.

**The contact contract is now verified, not asserted.** `make validate` replays the batch,
reconstructs every customer's contact timeline, and fails if any clause in `policy.yaml` is
breached. It reports **0 customers over the 3-in-7-days cap and 0 contact pairs closer than
24 hours**, tightest gap 24.2h. It did not always: both clauses were structurally
unenforceable until an audit found them, and 41 customers had been over the cap with 209
pairs too close together ([BUILD_NOTES](BUILD_NOTES.md) section T).

## Why you can believe that number

Any synthetic benchmark can be made to say anything. These are the checks that would
**fail** if ours were unsound — `make validate` runs them:

| Check | Result |
|---|---|
| **A/A test** — split by customer, both halves treated identically | **+0.21pp**, CI [−1.58, +2.02] — spans zero, no phantom lift |
| **Placebo** — every action made completely inert | **−1.68pp**, CI [−3.88, +0.56] — *negative*, so the harness understates us |
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
| **Full agent** | **+5.48pp** | [+3.30, +7.66] | — | **₹534** | **0.248** |
| Random action chooser | +4.24pp | [+2.07, +6.44] | −23% | ₹919 | 0.527 |
| No taxonomy | +5.52pp | [+3.34, +7.75] | +1% | ₹567 | 0.252 |
| No policy gate | +7.10pp | [+4.93, +9.28] | **+29%** | ₹616 | 0.328 |
| No LLM, rules only | +5.67pp | [+3.50, +7.83] | +3% | ₹526 | — |

**Read this table by the last two columns, not the first.** Three of the four ablations
recover *more* than the full agent. That is not a bug — it is what happens once the
compliance contract is genuinely enforced.

**Compliance costs 29% of achievable lift.** Removing the policy gate is the single
biggest improvement available to this agent: +7.10pp against our +5.48pp. It contacts
people more often than the contract permits, outside the hours it permits, and it
recovers more money by doing so. Earlier versions of this README reported that the gate
was free. It was free because two of its contact clauses were structurally incapable of
firing ([BUILD_NOTES](BUILD_NOTES.md) section T) — the gate looked costless because it
was not doing anything. **The honest number is that governance is expensive, and we can
only quote it now that the contract is actually enforced.**

**Against a random chooser, the agent's value is efficiency, not recovery.** Random gets
+4.24pp — only 23% below us — but spends **twice the contacts** (0.527 vs 0.248) and
**₹919 per marginal recovery against our ₹534**. Given a fixed, regulated contact budget,
the question is not "can you recover more by messaging more", it is "what do you do with
the three contacts a customer is legally allowed". We recover 29% more than random on
53% fewer contacts.

**The taxonomy now contributes nothing to lift, and about 6% to cost.** This is a genuine
reversal: before the contact cap was enforced it was worth 25–31%. With a hard per-customer
cap, the binding constraint is *how many* times you may act, not *which* action you pick,
so most of the taxonomy's value is squeezed out. It survives only in efficiency (₹534 vs
₹567). We would rather report that than keep quoting a number measured against a broken gate.

### The language model contributes nothing measurable, and we are going to say so

Removing the LLM entirely gives **+5.67pp against the full agent's +5.48pp** — it is
marginally *better* without it, well inside the interval. That number has now moved five
times, and the pattern is the finding:

| | LLM contribution |
|---|---:|
| invented fatigue curve, hand-picked trust weight | +8% |
| fatigue curve fitted to 86,399 real records | −9% |
| trust weight learned instead of chosen | +5% |
| every customer message actually sending | +0% |
| **the compliance contract actually enforced** | **−3%** |

Every row is a real defect fixed somewhere *else* in the system. **Each time we made the
surrounding machinery more correct, the model's apparent contribution shrank.** An effect
that only survives while the rest of the system is broken was never an effect.

The architecture is still the one we would defend: the model is isolated behind a learned
trust weight, so a better-calibrated model earns more influence with no code change. But
the evidence today does not support claiming it earns its place, and we are not going to
imply otherwise.

---

## How sensitive is this to our assumptions?

Every grade-C parameter in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) is an assumption.
`make sweep` re-runs everything across five parameterisations:

| Parameterisation | Holdout | Lift | 95% CI |
|---|---:|---:|---|
| baseline (calibrated) | 73.0% | +5.48pp | [+3.30, +7.66] |
| pessimistic: high self-recovery | 82.7% | +5.13pp | [+3.28, +6.97] |
| optimistic: low self-recovery | 53.9% | +7.52pp | [+5.08, +9.97] |
| **weak interventions** | 73.0% | **+0.32pp** | **[−1.91, +2.54]** |
| hard failure mix + noisier labels | 58.3% | +9.36pp | [+6.97, +11.71] |

**Envelope: +0.32 to +9.36pp.** Under a pessimistic view of what dunning can achieve at
all, the effect collapses to nothing — +0.32pp on an interval that comfortably contains
zero. It no longer goes *negative*, as it did before the compliance and messaging bugs
were fixed, but "we cannot show this works if messages barely move anyone" remains true
and remains in the table.

The last row is the one to read sceptically. "Hard failure mix" is built to be the worst
realistic world and produces our **highest** lift, +9.36pp. That is not the agent doing
better — it is the holdout doing worse. Self-recovery drops to 58.3%, so there is simply
more left on the table. Lift is a difference, and differences grow when the baseline falls.

`make replay` answers the adjacent question — what a different *contract* would cost:

| Policy variant | Net incremental | vs shipped |
|---|---:|---:|
| as committed | ₹13,89,199 | — |
| **TRAI-only window (to 21:00)** | ₹12,67,538 | **−₹1,21,661** |
| looser: 5 contacts per week | ₹12,89,050 | −₹1,00,149 |
| **no merchant spend cap at all** | ₹13,89,199 | **₹0** |
| spend cap 5× tighter | ₹10,59,673 | −₹3,29,526 |
| spend cap 25× tighter | −₹1,10,634 | −₹14,99,833 |
| no human escalation at all | ₹5,48,910 | **−₹8,40,288** |

**The stricter regulatory reading earns money.** We contact 09:00–19:00 on RBI's Fair
Practices bound rather than TRAI's more permissive 21:00. Replaying under the looser window
recovers ₹1,21,661 *less*: once a per-customer contact cap binds, extra evening hours buy no
extra contacts, they only move the permitted ones into hours that convert worse.

**Removing the merchant spend cap entirely still buys exactly ₹0** — on spend, the agent's
own attention-cost arithmetic binds before the contract does. That is not true of the
*contact* cap, which is genuinely binding: loosening it costs ₹1,00,149. An earlier revision
of this README claimed both bought ₹0 and called it our strongest demonstration. Half of
that was an artefact of the contact cap not actually working
([BUILD_NOTES](BUILD_NOTES.md) section T).

Under-budget the agent and it destroys value: at a 25× tighter spend cap it posts −0.29pp
and ₹25,949 per recovery, exhausting a symbolic budget on whatever it reaches first.

---

## Where this breaks

1. **The holdout recovers 73% unaided.** Our response model treats each opportunity as
   independent, which almost certainly overstates spontaneous recovery over 21 days.
   The error runs *against* us — a lower real baseline would mean more headroom — but
   the absolute recovery rates should not be read as forecasts.
2. **Under weak interventions the effect vanishes** (+0.32pp, CI [−1.91, +2.54]). If
   messages and retries barely move anyone, we cannot show this works at all.
3. **The language model contributes nothing measurable.** Ablation 4 removes it and the
   result does not move. We report that rather than implying the LLM does the work.
4. **60% of the result depends on human escalation being available.** `make replay`
   with `escalation.max_per_day: 0` drops the lift from +5.48pp to **+1.96pp**, costing
   ₹8.40 lakh of ₹13.89 lakh. Recura is a decision layer that routes work to people, not
   a system that replaces them. If a merchant has no collections staff, most of this
   value does not exist.
5. **Per-merchant margin is wired but not exercised** — the frozen cohort assigns every
   merchant the same 30%, so the EV differences that margin should create are untested.
6. **Hinglish messaging is compliance-verified, not lift-verified.** We prove no
   free-form copy can escape the DLT template registry. We do not claim language
   matching improves recovery, because the generator does not model it.

7. **Compliance costs 29% of achievable lift**, and we can only say so now that the
   contract is genuinely enforced — two of its contact clauses were previously incapable
   of firing.

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
tests pass; mutation testing proves they would object. 12/12 caught.

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

**498 tests.** Run `make test`.

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

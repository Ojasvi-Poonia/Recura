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
| Recovery rate | **77.5%** | 73.0% |
| Recovered | ₹2,36,93,099 | ₹50,28,349 |
| Intervention cost | ₹2,57,021 | ₹0 |
| Contacts per customer | 0.28 | 0 |
| Actions blocked by policy | 8,397 | — |
| Refused, EV < 0 | 2,676 | — |
| Escalated to human | 1,439 | — |
| Opted out | 50 | 0 |

> ### +4.43 percentage points — 95% CI [+2.25, +6.61]
> **₹13,53,887 incremental recovered · ₹10,96,866 net · 5.3× return on spend**
> Cost per extra recovery: ₹722. Runs in 4 seconds. Byte-identical across runs.

---

## Why you can believe that number

Any synthetic benchmark can be made to say anything. These are the checks that would
**fail** if ours were unsound — `make validate` runs them:

| Check | Result |
|---|---|
| **A/A test** — split treatment in half, both halves treated identically | **−1.47pp**, CI [−3.33, +0.35] — interval spans zero, no phantom lift |
| **Placebo** — every action made completely inert | **−1.94pp**, CI [−4.17, +0.29] — *negative*, so the harness understates us |
| Arm balance | worst standardised difference **0.054** (RCT threshold 0.10) |
| Holdout purity | zero cost, zero contacts, zero opt-outs |
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

| Configuration | Lift | 95% CI | vs full | Cost/recovery |
|---|---:|---|---:|---:|
| **Full agent** | **+4.43pp** | [+2.25, +6.61] | — | ₹722 |
| Random action chooser | +0.14pp | [−2.06, +2.39] | −97% | **₹51,756** |
| No taxonomy | +3.07pp | [+0.89, +5.26] | **−31%** | ₹1,012 |
| No policy gate | +3.66pp | [+1.46, +5.87] | **−17%** | ₹621 |
| No LLM, rules only | +4.22pp | [+2.03, +6.40] | **−5%** | ₹722 |

**Random action selection is 28× less efficient**, at ₹21,722 per marginal recovery
against our ₹761. Its *lift* interval contains zero, so "random is worse at recovering"
would be overclaiming — what is not close is the cost of getting there.

**The taxonomy contributes 31%**, and without it the result is barely significant. Mapping
Razorpay's own `reason` field to a recovery strategy is the single largest source of lift
in the system — more than the model, more than the bandit.

**The policy gate pays for itself.** We expected compliance to cost money. Removing it
makes the result *worse* — +3.66pp against +4.43pp, at ₹621 per recovery against ₹722 —
because an ungoverned agent spends more to recover slightly more, and burns customer
patience doing it. Governance and performance point the same way here. We were prepared
to report the opposite.

### How much to trust the model is learned, not configured

Ablation 4 says removing the language model costs **5%**. That number has moved three
times, and how it moved is the interesting part.

| | LLM contribution |
|---|---:|
| invented fatigue curve, hand-picked trust weight | +8% |
| **fatigue curve fitted to real data**, same hand-picked weight | **−9%** |
| fitted curve, **trust weight learned** | **+5%** |

The middle row is the honest one to dwell on: correcting an *unrelated* parameter flipped
the model's apparent contribution from helpful to harmful. An effect that fragile was
never an effect.

So we stopped picking the weight. `make calibration` had already shown the model is badly
calibrated — Brier 0.9838 against a 0.8196 base rate, 61%-confident predictions landing
20% of the time — and our response had been to shrink its output toward the taxonomy
prior by a constant we chose. **Choosing that constant was the same mistake we refuse
everywhere else: a parameter set by an author and tuned on the metric it moves.**

The agent now treats it as another arm and learns it, by the same Thompson sampling it
uses for actions. Three sources — ignore the model, blend it, believe it — each with a
posterior updated from whether acting on that diagnosis actually recovered the money:

```
model      recovery rate  28.9%   n=834
taxonomy   recovery rate  28.5%   n=967
blended    recovery rate  20.2%   n=117
```

**Our hand-picked weight was the worst of the three.** We had split the difference
between two better options and landed below both. The agent found that in one run.

Three properties follow. It **adapts** — a better-calibrated model earns more trust with
no code change, which is the honest answer to "your model is weak". It is **auditable** —
"how much does this agent trust its model" now has a number in the ledger instead of an
opinion in a config file. And it is **not tuning** — the weight is learned from outcomes,
not chosen against the headline.

Read plainly: on opaque payment declines a small model contributes a little, the
deterministic taxonomy contributes about as much, and the system is better off deciding
between them per-case than committing to either.

## How sensitive is this to our assumptions?

Every grade-C parameter in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) is an assumption.
`make sweep` re-runs everything across five parameterisations:

| Parameterisation | Holdout | Lift |
|---|---:|---:|
| baseline (calibrated) | 73.0% | +4.54pp |
| pessimistic: high self-recovery | 82.7% | +3.14pp |
| optimistic: low self-recovery | 53.9% | +5.84pp |
| **weak interventions** | 73.0% | **−1.58pp** |
| hard failure mix + noisier labels | 58.3% | +6.93pp |

**Envelope: −1.58 to +6.93pp.** Under a pessimistic view of what dunning can achieve at
all, Recura **loses money** — −1.58pp, CI [−3.79, +0.64], a net loss of ₹5,74,615. That
is in the table because it is true. Four of five worlds are positive; the fifth is the one
where messages and retries barely move anyone, and in that world no dunning agent is worth
running, ours included.

`make replay` answers the adjacent question — what a different *contract* would cost:

| Policy variant | Net incremental | vs shipped |
|---|---:|---:|
| as committed | ₹10,83,117 | — |
| looser: 5 contacts per week | ₹10,83,117 | **₹0** |
| **no merchant spend cap at all** | ₹10,83,117 | **₹0** |
| spend cap 5× tighter | ₹6,28,894 | −₹4,54,223 |
| spend cap 25× tighter | −₹1,54,079 | −₹12,37,195 |
| no human escalation at all | ₹98,961 | **−₹9,84,156** |

**Loosening the contact cap and removing the spend cap entirely both buy exactly ₹0.**
The agent's own attention-cost arithmetic binds before either contract limit does — the
gate is a backstop, not the thing doing the work. Under-budget it, though, and the agent
becomes value-destroying: at a 25× tighter cap it posts −0.44pp.

---

## Where this breaks

1. **The holdout recovers 73% unaided.** Our response model treats each opportunity as
   independent, which almost certainly overstates spontaneous recovery over 21 days.
   The error runs *against* us — a lower real baseline would mean more headroom — but
   the absolute recovery rates should not be read as forecasts.
2. **Under weak interventions the agent is value-destroying** (−1.58pp). We do not know
   which parameterisation reality resembles.
3. **The diagnosis model is poorly calibrated** and worse than base rate on raw scores.
   It is usable only because we measured that and shrank it.
4. **91% of the result depends on human escalation being available.** `make replay`
   with `escalation.max_per_day: 0` drops the lift from +4.33pp to **+0.40pp** — no
   longer significant — costing ₹9.84 lakh of ₹10.83 lakh. Recura is a decision layer
   that routes work to people, not a system that replaces them. If a merchant has no
   collections staff, most of this value does not exist.
5. **Per-merchant margin is wired but not exercised** — the frozen cohort assigns every
   merchant the same 30%, so the EV differences that margin should create are untested.
6. **Hinglish messaging is compliance-verified, not lift-verified.** We prove no
   free-form copy can escape the DLT template registry. We do not claim language
   matching improves recovery, because the generator does not model it.

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
tests pass; mutation testing proves they would object. 10/10 caught.

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

**488 tests.** Run `make test`.

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

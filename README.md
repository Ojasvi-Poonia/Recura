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
| Recovery rate | **77.4%** | 73.0% |
| Recovered | ₹2,35,25,176 | ₹50,28,349 |
| Intervention cost | ₹2,32,692 | ₹0 |
| Contacts per customer | 0.27 | 0 |
| Actions blocked by policy | 8,663 | — |
| Refused, EV < 0 | 2,551 | — |
| Escalated to human | 1,459 | — |
| Opted out | 30 | 0 |

> ### +4.33 percentage points — 95% CI [+2.13, +6.51]
> **₹13,15,809 incremental recovered · ₹10,83,117 net · 5.7× return on spend**
> Cost per extra recovery: ₹669. Runs in 4 seconds. Byte-identical across runs.

---

## Why you can believe that number

Any synthetic benchmark can be made to say anything. These are the checks that would
**fail** if ours were unsound — `make validate` runs them:

| Check | Result |
|---|---|
| **A/A test** — split treatment in half, both halves treated identically | **+0.35pp**, CI [−1.53, +2.15] — no phantom lift |
| **Placebo** — every action made completely inert | **−2.22pp** — *negative*, so the harness understates us |
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
| **Full agent** | **+4.33pp** | [+2.13, +6.51] | — | ₹669 |
| Random action chooser | **−2.09pp** | [−4.30, +0.14] | −148% | ₹5,67,412 |
| No taxonomy | +3.50pp | [+1.33, +5.70] | −19% | ₹915 |
| No policy gate | +3.72pp | [+1.51, +5.95] | −14% | ₹596 |
| No LLM, rules only | +3.98pp | [+1.78, +6.16] | −8% | ₹676 |

**Acting without expected-value reasoning destroys value** — a random chooser posts
−2.09pp and loses ₹11.7 lakh, at 850× our cost per recovery. Note its interval just
touches zero, so "significantly negative" would be overclaiming; what is unambiguous is
the cost, which is not close.

The policy gate *costs nothing* — removing it makes results slightly worse, because it
stops the agent doing counterproductive things. Compliance is not a tax here.

### The LLM contributes 8%, and we had to earn it

Our first honest measurement said the LLM made the agent **worse**. `make calibration`
explains why:

| | Model | Base rate |
|---|---:|---:|
| Brier score | 0.9838 | **0.8196** |
| Top-1 accuracy | 18.2% | **22.5%** |
| Expected calibration error | **0.2742** | — |

**When it said 61% confident, it was right 20% of the time.** Feeding probabilities like
that into an expected-value calculation degrades the decision.

So we shrink them toward the deterministic taxonomy prior, with a weight set *from that
measurement* rather than by taste. The LLM then contributes +8%. That is a modest,
measured, defensible number — and the loop that produced it (measure → diagnose → fix →
re-measure) is more of the point than the number.

---

## How sensitive is this to our assumptions?

Every grade-C parameter in [`eval/CALIBRATION.md`](eval/CALIBRATION.md) is an assumption.
`make sweep` re-runs everything across five parameterisations:

| Parameterisation | Holdout | Lift |
|---|---:|---:|
| baseline (calibrated) | 73.0% | +4.33pp |
| pessimistic: high self-recovery | 82.7% | +2.97pp |
| optimistic: low self-recovery | 53.9% | +5.91pp |
| **weak interventions** | 73.0% | **−2.07pp** |
| hard failure mix + noisier labels | 58.3% | +6.23pp |

**Envelope: −2.07 to +6.23pp.** Under a pessimistic view of what dunning can achieve at
all, Recura **loses money**. That is in the table because it is true.

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
2. **Under weak interventions the agent is value-destroying** (−2.07pp). We do not know
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

**115 real Razorpay error codes**, transcribed from their published documentation —
not invented categories. Contact windows are the intersection of TRAI's messaging rules
and RBI's Fair Practices Code (09:00–19:00, stricter than either alone); pre-debit
notification cites RBI's E-Mandate Framework 2026.

**Anti-goals are enforced by tests**: no wall clock outside `clock.py`, no floats for
money, no agent-framework imports, no locale hardcoded in the decision core, `src/`
cannot reach the simulator's hidden state.

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

**319 tests.** Run `make test`.

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

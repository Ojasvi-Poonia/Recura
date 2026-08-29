# Architecture

Recura treats revenue recovery as a **sequential decision problem under budget
constraints**, not a workflow. A workflow executes steps. Recura chooses actions, and
the sequence is an output rather than a specification.

---

## The governing idea

> **The LLM proposes, the maths decides, the policy gate vetoes.**

Three authorities, none able to override the others:

| Authority | Owns | Cannot |
|---|---|---|
| **LLM** | Root-cause synthesis from heterogeneous signals; filling registered message templates | Choose an action, see `policy.yaml`, or author free-form copy |
| **Expected value** | Pricing every candidate action in money; the argmax | Bypass the contract |
| **Policy engine** | Deterministic evaluation of `policy.yaml`; pass / block / modify | Contain business logic beyond the YAML, or consult a model |

This separation is not a convention. `tests/test_invariants.py` parses the AST of every
module and fails the build if `src/decide/` imports the policy engine, if `src/` reaches
the simulator's hidden state, if a wall clock appears outside `clock.py`, if money is
stored as a float, or if an agent framework is imported.

So *"no prompt injection can unlock a money action"* is a property a test checks, not a
claim in a slide.

---

## Data flow

```
   payment.failed   checkout dropped   subscription.halted   invoice overdue
          │                 │                   │                  │
          └─────────────────┴─────────┬─────────┴──────────────────┘
                                      │
                       ingest/  HMAC over RAW bytes, dedupe on
                                x-razorpay-event-id, late-authorisation
                                is a STOP, order-independent
                                      │
                                      ▼
                              RiskEvent  (observables only)
                                      │
                    ┌─────────────────┴──────────────────┐
                    ▼                                    ▼
          TREATMENT ARM (80%)                    HOLDOUT ARM (20%)
                    │                        no action, observed over
                    │                        the SAME horizon
      ┌─────────────┴─────────────┐                      │
      │  1 TRIAGE   taxonomy/     │                      │
      │  2 DIAGNOSE decide/llm    │  LLM proposes        │
      │  3 DECIDE   decide/ev     │  maths decides       │
      │  4 GOVERN   policy/       │  contract vetoes     │
      │  5 LEARN    decide/bandit │                      │
      └─────────────┬─────────────┘                      │
                    ▼                                    │
              act/  simulated execution                  │
                    │                                    │
                    └─────────────────┬──────────────────┘
                                      ▼
              ledger/   append-only, DB-trigger enforced.
                        Every decision, every runner-up EV,
                        every block, every refusal.
                                      ▼
              eval/     incremental recovery = Treatment − Holdout
```

Both arms receive **exactly one recovery opportunity per step**. That symmetry is the
core fairness property of the whole benchmark and is asserted by both a unit test and
the placebo negative control — see [RESULTS.md §2](RESULTS.md).

---

## The decision, in full

```
EV(action) = (p(action) − p(no_action))          incremental, not absolute
             × (1 − b)^(k−1)                     horizon discount
             × amount_paise × margin             merchant-specific margin
             − direct_cost(action)               channel price
             − attention_cost(customer, action)  fatigue + expected opt-out loss

chosen = argmax EV over candidates
if max(EV) < 0 → NO_ACTION
```

Four terms are load-bearing, and each was added because something measurable was wrong
without it.

**Incremental, not absolute.** Scoring `p(action) × amount` makes `NO_ACTION` unreachable,
because doing nothing already has positive value. Pricing the *increment* puts
`EV(NO_ACTION)` at exactly zero and makes "every option destroys value" a meaningful
test.

**Horizon discount `(1−b)^(k−1)`.** An episode has several decision points. Valuing an
action as if this were the customer's last chance credits it with recoveries that would
have happened anyway later. At a 0.26 baseline with four chances remaining the factor is
0.30 — a naive scorer overstates every intervention roughly threefold. Before this was
added, the agent escalated 2,264 cases, 97% of all spend.

**Attention cost includes expected opt-out loss.** `base × (n+1)^exponent` prices
annoyance; `P(opt_out | n) × lifetime_value` prices the relationship ending. Only the
first was implemented for most of the build, which made contacting people nearly free in
the arithmetic. With both, the first message costs ₹14 of attention and the fifth costs
₹475. **This is what makes the agent stop** — not a rule.

**`p_recover` is a marginal over a distribution.** The diagnosis layer returns
probabilities across failure classes, not a label, and the propensity model marginalises:
`p = Σ_c P(c) · p(action | c)`. A single-label interface throws away an honest "UNKNOWN",
which on opaque error codes is most of what a well-calibrated model has to say.

`NO_ACTION` is **derived arithmetic, not a special case**. Asked why Recura skipped a
transaction, the answer is a number in the ledger.

---

## Propensity estimation

One Beta posterior per `(failure_class × action_type)` cell — 42 cells — sampled by
Thompson sampling so exploration falls out of the maths rather than an epsilon schedule.

Two decisions worth defending:

**Uninformative priors, deliberately.** We could seed each cell from the published
baseline recovery rates in `CALIBRATION.md`. We do not, because the simulator's response
model is built from those same figures — seeding would be peeking, and the "learning"
would be an artefact.

**Soft credit assignment.** A diagnosis of "60% FUNDS, 40% infra" updates the FUNDS cell
by 0.6 and the infra cell by 0.4. Betting the whole observation on the argmax would
discard the model's uncertainty at exactly the point it matters.

The RNG is threaded explicitly through every call. There is no module-level random state
anywhere, which is what lets a stochastic algorithm produce byte-identical eval runs.

---

## Module boundaries

| Module | Owns | Must not |
|---|---|---|
| `ingest/` | Webhook receipt, HMAC verification, normalisation | Make decisions |
| `taxonomy/` | 115 Razorpay reason codes → failure class + triage | Choose actions |
| `decide/` | Root cause, candidate generation, EV, propensity | Execute; import `policy/` |
| `policy/` | Evaluate `policy.yaml` → pass / block / modify | Consult a model |
| `act/` | Execute permitted actions; render DLT templates | Decide; bypass policy |
| `ledger/` | Append-only record of everything | Be mutable, ever |
| `eval/` | Cohort generation, hidden latents, metrics, controls | Be imported by `src/` |
| `market/` | Currency, timezone, rails, lawful contact window | Be hardcoded in the core |

`src/agent.py` is the orchestrator and the only module allowed to know about both
`decide/` and `policy/`. It is 525 lines, 391 of them code, and reads top to bottom.

---

## Design decisions

Each of these has a full [ADR](docs/adr/) with the alternatives we rejected.

### No agent framework

Hand-rolled loop. We need three things a framework makes harder: deterministic replay,
a policy gate outside the model's reach, and a control flow an interviewer can walk line
by line. The loop is ~200 lines of actual control flow; the rest is the five steps and
their logging.

### The taxonomy is theirs; the judgement is ours

`data/razorpay_error_reasons.csv` holds 115 reason codes transcribed from Razorpay's
published documentation. It is **their data and we do not edit it**. Our mapping lives
separately in `taxonomy/mapping.py`, one row per real reason, with a written rationale
wherever the call is contestable.

A second dimension, `Recoverability`, sits orthogonal to `FailureClass`. Razorpay's list
mixes genuine customer failures with **merchant integration bugs** — `invalid_order_id`,
`live_mode_not_enabled` — and 31 of 115 are the latter. Nudging a customer because the
*merchant* sent a malformed request would be indefensible, so triage is modelled
separately from diagnosis.

Two sources carry no error code at all, because nothing reached a gateway: a **dropped
checkout** (nobody attempted a charge) and an **overdue invoice** (nobody charged it).
Both are classified from source rather than from an error, and receivables get a
different action space entirely — an ageing ladder, no gateway retry, because there is no
instrument to retry.

### The contract is a file a merchant could sign

`policy.yaml` is 20 rules, each returning pass or block **with a human-readable reason**,
and each grounded in a citation with the date checked. The contact window is the
intersection of TRAI's messaging rules and RBI's Fair Practices Code — 09:00–19:00,
stricter than either alone. Pre-debit notification cites RBI's E-Mandate Framework 2026,
which replaced the circulars most integrations still reference.

**A blocked action is evidence, not an error.** Blocks are written to the ledger and the
refusal count is a headline metric.

Quiet hours *modify* rather than block: a 22:30 nudge shifts to 09:00 rather than being
cancelled. Silent gateway retries are exempt, because they disturb nobody.

### The LLM never writes customer copy

TRAI requires every commercial message to match a DLT-registered template with matching
variable fields. Free-form copy is a breach, not a style choice. So the model selects a
registered template and fills its slots, and `verify_compliance()` re-derives the
template from rendered text and raises on anything unregistered.

`RISK_DECLINE` and `UNKNOWN` have **no template at all** — we do not message someone about
an issuer risk decline, and we do not assert a cause we could not determine.

Hinglish is Hindi-English code-mixing in **Latin** script, which is what Indian consumers
actually read and what renders consistently across handsets. For voice, Latin-script
Hinglish uses an *Indian English* voice — a Devanagari voice fed Latin text produces
nonsense — and Devanagari text uses a Hindi voice.

### Time is injected

`datetime.now()` appears nowhere outside `clock.py`, enforced by an AST-parsing test.
Every policy window resolves through the market's timezone. Without this, eval is not
replayable and quiet hours are wrong by however far the server is from IST.

### Money is integer minor units

Integer paise throughout; conversion happens once, at display, through the market
profile. A test fails the build on any `*_paise: float`.

### The ledger cannot be rewritten

`UPDATE` and `DELETE` are refused by database triggers, and that survives reconnection.
Application-level discipline is not enough — one careless migration and the audit trail
argument collapses. Trigger syntax is implemented for both SQLite and MySQL, so the
schema ports.

### Determinism comes from fixtures, not temperature

`temperature` no longer exists on current Claude models and returns HTTP 400. It was
never a real guarantee anyway — server-side batching makes identical requests diverge.
Instead every model response is content-addressed by SHA-256 of (model, prompt, payload)
and committed to `fixtures/`. That makes `make eval` byte-identical **and removes the API
key requirement entirely**, which is what makes clone-and-reproduce achievable.

The provider is an adapter boundary (Anthropic, Gemini, or none) for the same reason the
payment provider is: the decision core should not know or care which model produced a
root cause.

### Locale is data

Currency, minor units, timezone, lawful contact window, payment rails and languages live
in `config/markets.yaml`. An invariant test fails the build if a currency symbol appears
in the decision core. Scope is India; the structure exists so a second market is a config
file rather than a rewrite, and we ship only profiles verified against primary regulation.

---

## Operational failure modes

How the system fails, and what it does about it.

| Failure | Behaviour |
|---|---|
| Webhook signature mismatch | 401, nothing processed. Verification is over raw bytes before any parsing — re-serialising breaks the digest |
| Webhook redelivered | Deduped on `x-razorpay-event-id`; the provider layer also refuses a repeated idempotency key |
| Webhooks arrive out of order | Every payload classified on its own merits; Razorpay states ordering is not guaranteed |
| Late authorisation | Hard stop. Money already arrived; acting would dun a paying customer |
| Webhook secret unset | Fails **closed** — 503, never processes an unverifiable payload |
| LLM unreachable or returns invalid output | Falls back to the deterministic taxonomy path, logged as a handled failure. This is also ablation 4 |
| No API key at all | Fixtures replay; if a fixture is missing, rules-only. `make eval` still runs |
| Corrupt fixture | Treated as a miss, never crashes the batch |
| Provider quota exhausted | Rotates to the next key; transient network errors retry the same key |
| Live Razorpay key supplied | `LiveKeyRefused` at construction. Test mode only, always |
| Policy blocks an action | Recorded as evidence; the agent does not re-propose what the contract already refused this episode |
| Broken promise-to-pay | Further automated contact blocked; case routes to a human |
| Human escalation capacity spent | Blocked for the rest of the day |
| Episode exceeds 21 days | Closed |
| Customer opts out | Episode ends immediately and permanently |

---

## What is deliberately absent

- **A dashboard.** A polished UI on an unmeasured system is the failure mode this brief
  filters for. `make run` traces episodes; `make eval` reports numbers.
- **A retry reimplementation.** Razorpay already ships smart retries and dunning. Recura
  is the decision layer *above* them.
- **Real sending.** No message is ever delivered, no real payment is ever made.
- **Real PII.** Synthetic customers only; even the anonymous-customer fallback in ingest
  refuses to key on an email or phone number.

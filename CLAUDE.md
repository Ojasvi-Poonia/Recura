# CLAUDE.md — Recura

> Autonomous revenue-recovery agent. Submission for the Razorpay AI Buildathon, Track 03 (AI Revenue Recovery).
> This file is the single source of truth. Read it fully before writing code. If a decision here conflicts with your instinct, follow this file or raise the conflict explicitly.

---

## 0. Mission and stakes

This is a **hiring submission**, not a product launch. Razorpay is recruiting AI Builder Interns purely on what candidates build and can defend in front of a panel. Deliverables are a public GitHub repo, a 5-minute video, and an architecture doc. **Deadline: 5 September 2026.**

The judging bar, in Razorpay's own words for this track:

> "Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."

Everything in this repo exists to satisfy that sentence literally. **The differentiator is not the idea — it is the evidence.** Roughly 90% of submissions will be an unmeasured demo. We ship a measured system.

### The one benchmark that matters

> A Razorpay engineer clones this repo and reproduces the headline number in under 10 minutes.

If a change does not serve that, deprioritise it.

---

## 1. What Recura is

**Recura treats revenue recovery as a sequential decision problem under budget constraints, not a workflow.**

A workflow executes steps. Recura chooses actions. For every rupee at risk, at every point in time, it answers five questions and then loops:

1. **Triage** — is this recoverable at all?
2. **Diagnose** — what actually went wrong (per Razorpay's `source`/`step`/`reason` taxonomy)?
3. **Decide** — which action maximises expected value, net of cost?
4. **Govern** — am I permitted to do it?
5. **Learn** — did it work, and what does that change?

A recovery episode is typically 1–5 decisions across several days, with re-observation between each.

### Governing principle

> **The LLM proposes, the math decides, the policy gate vetoes.**

- LLM: root-cause synthesis across heterogeneous signals; drafting customer-facing copy.
- Math: expected-value arithmetic, propensity estimates, scheduling.
- Policy gate: deterministic, outside the model's context, non-negotiable.

The LLM never sees `policy.yaml` and cannot modify it. No prompt injection can unlock a money action.

---

## 2. Hard constraints

| Constraint | Value |
|---|---|
| Language | Python 3.11+ |
| Agent framework | **None.** Hand-rolled loop. This is a deliberate, defensible choice. |
| LLM | **Provider-agnostic** (`decide/providers.py`): Anthropic, Gemini (free tier), or none. Pinned model per provider. **`temperature` CORRECTED 2026-08-26** — removed on current Claude models (400 if sent); Gemini still supports it. Determinism comes from content-addressed fixture caching. Bring your own key to regenerate fixtures; **no key needed to reproduce results.** |
| Real money | Never. Test mode only (`rzp_test_`). |
| Real messages | Never sent. Simulated and logged. |
| Real PII | Never. Synthetic customers only. |
| Offensive capability | None. Defense/recovery only. |
| Wall-clock in logic | Forbidden. Injected clock only. |
| Timezone | Asia/Kolkata for all policy windows |
| Currency | Integer paise internally. Rupees only at display. |

---

## 3. Architecture

```
                 payment.failed   checkout dropped   mandate failed   invoice overdue
                        │                │                 │                │
                        └────────────────┴────────┬────────┴────────────────┘
                                                  ▼
                                      ingest/  → RiskEvent (normalised)
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                          TREATMENT ARM (80%)            HOLDOUT ARM (20%)
                                    │                     no action, observed
                                    ▼                           │
                        taxonomy/  → FailureClass                │
                                    ▼                           │
                        decide/    → Decision (typed)            │
                                    ▼                           │
                        policy/    → PolicyVerdict               │
                                    ▼                           │
                        act/       → ActionResult                │
                                    │                           │
                                    └─────────────┬─────────────┘
                                                  ▼
                                    ledger/  append-only, every decision + reason
                                                  ▼
                                    eval/    incremental recovery = T − C
```

### Module responsibilities

| Module | Owns | Must not |
|---|---|---|
| `ingest/` | Webhook receipt, signature verification, normalisation to `RiskEvent` | Make decisions |
| `taxonomy/` | Map Razorpay error object → `FailureClass` | Choose actions |
| `decide/` | Root cause, candidate actions, EV computation, `Decision` | Execute anything |
| `policy/` | Evaluate `policy.yaml`, return pass/block + reason | Contain business logic beyond the YAML |
| `act/` | Execute a permitted `Decision` via provider adapters | Decide, or bypass policy |
| `ledger/` | Append-only record of everything | Be mutable. Ever. |
| `eval/` | Cohort generation, batch runs, metrics, ablations | Be touched after freeze (see §9) |

---

## 4. Domain model

All models are Pydantic. Define in `src/models.py`.

```python
class FailureClass(StrEnum):
    TRANSIENT_INFRA    # bank/gateway/network downtime → retry soon
    FUNDS              # insufficient balance → retry aligned to replenishment
    AUTH_ABANDON       # OTP timeout, 3DS drop, window closed → re-engage fast
    INSTRUMENT_INVALID # expired card, revoked mandate → method switch required
    RISK_DECLINE       # issuer risk decline → do NOT retry, inform/escalate
    LIMIT_EXCEEDED     # per-txn or daily limit → retry next cycle or lower amount
    UNKNOWN            # conservative handling

class ActionType(StrEnum):
    NO_ACTION
    RETRY_NOW
    RETRY_SCHEDULED    # carries scheduled_at
    SWITCH_METHOD      # carries suggested_rail
    NUDGE              # carries channel, template_id, language, scheduled_at
    ESCALATE_HUMAN     # carries escalation_reason

class RiskEvent:
    event_id: str
    merchant_id: str
    customer_id: str
    source_type: Literal["payment","checkout","mandate","invoice"]
    amount_paise: int
    currency: str
    observed_at: datetime
    razorpay_error: ErrorObject | None   # code, description, source, step, reason, metadata
    method: str | None                   # card, upi, netbanking, wallet, emandate
    bank: str | None
    attempt_number: int
    customer_history: CustomerHistory    # observable only — see §9
    merchant_context: MerchantContext

class Decision:
    event_id: str
    failure_class: FailureClass
    root_cause: str                      # LLM-authored, <=200 chars
    action: ActionType
    params: dict
    expected_value_paise: int
    p_recover: float
    confidence: float
    rationale: str                       # why THIS over the runners-up
    considered: list[CandidateEV]        # every option with its EV — always logged
    decided_at: datetime

class PolicyVerdict:
    allowed: bool
    rules_evaluated: list[str]
    rules_blocked: list[BlockedRule]     # rule_id + human-readable reason
    modified_params: dict | None         # e.g. quiet hours shifted scheduled_at

class LedgerEntry:
    entry_id, event_id, arm ("treatment"|"holdout"), sequence_number,
    observed_state, decision, policy_verdict, action_result,
    cost_paise, recovered_paise, clock_time, wall_time
```

**Added 2026-08-26 — `Recoverability` (triage dimension, orthogonal to `FailureClass`):**
`CUSTOMER_RECOVERABLE` / `MERCHANT_CONFIG` / `TERMINAL`. Razorpay's published reason list mixes
genuine customer-side failures with merchant integration bugs (`invalid_order_id`,
`live_mode_not_enabled`); 31 of 115 reasons are the latter. Nudging a customer because the
merchant sent a malformed request would be indefensible, so triage (§1 step 1) is modelled
separately from diagnosis. `order_already_paid` is the sole `TERMINAL` reason — it is how late
authorisation surfaces on a retry.

`considered` is not optional. Logging the runner-up EVs is how a panel verifies the decision was reasoned rather than hardcoded.

---

## 5. The decision model

The intellectual spine of the project. In `decide/ev.py`:

```
EV(action) = p_recover(action, context) × amount_paise × margin
           − direct_cost(action)
           − attention_cost(customer, action)

chosen = argmax EV over candidates
if max(EV) < 0 → NO_ACTION
```

**`NO_ACTION` is a derived decision, not a special case.** When asked why Recura skipped a transaction, the answer is arithmetic in the ledger, not "there's a rule."

### Propensity estimation

`p_recover` comes from a Beta posterior per `(failure_class × action_type)` cell, updated from observed outcomes, sampled via **Thompson sampling** to balance exploration and exploitation. Context adjusts the sampled value through a small set of documented multipliers (attempt number, hour-of-day match to customer's historical success window, active downtime signal, recent contact fatigue).

Keep the multipliers in one file with a comment justifying each. A panel will ask.

### Action space is `(type, time, channel)`

**Timing is a first-class decision dimension.** It is the highest-leverage variable in payment recovery and almost nobody models it. `RETRY_SCHEDULED` at the right hour beats five messages at the wrong one. Timing inputs: bank downtime windows (Razorpay Downtime API), the customer's historical successful-payment hour, salary-cycle proximity for `FUNDS`, and mandate cycle position.

### Cost model

Direct costs per channel in `config/costs.yaml` (SMS, WhatsApp, voice, email, retry attempt). `attention_cost` rises superlinearly with recent contact count — this is what makes the agent stop.

---

## 6. Policy engine

`policy.yaml` is a contract a merchant could read. The engine is a deterministic evaluator; **no LLM involvement**.

Rules to implement:

```yaml
contact:
  max_per_customer_per_7d: 3
  min_hours_between: 24
  quiet_hours: { start: "19:00", end: "09:00", tz: "Asia/Kolkata" }  # VERIFIED 2026-08-26: see below
  require_consent: [sms, whatsapp, voice]

retry:
  max_attempts_per_episode: 3
  max_attempts_per_mandate_cycle: 2
  pre_debit_notification_hours: 24        # VERIFIED 2026-08-26: RBI E-Mandate Framework 2026
  forbidden_for_classes: [RISK_DECLINE, INSTRUMENT_INVALID]

episode:
  max_days: 21
  stop_on_payment: true
  stop_on_opt_out: true
  stop_on_dispute: true

merchant:
  daily_action_budget: 500
  daily_spend_cap_paise: 500000

escalation:
  to_human_above_paise: 5000000
  after_broken_promise_to_pay: true
```

Every rule returns pass/block **with a human-readable reason**. Blocked actions are written to the ledger — **a blocked action is evidence, not an error.** The count of refusals is a headline metric.

### Verified 2026-08-26

Three findings from grounding the rules in real regulation (full citations in `eval/CALIBRATION.md`):

1. **Contact window tightened to 09:00–19:00 IST.** TRAI's TCCCPR allows 09:00–21:00 for
   promotional messaging, but RBI's Fair Practices Code direction on recovery agents limits
   borrower contact to 08:00–19:00 across *all* channels. A recovery nudge is causally a
   collections contact, so the stricter evening bound governs. The draft value of 21:00 above
   was two hours non-compliant and has been corrected.
2. **`pre_debit_notification_hours: 24` confirmed — but under new law.** RBI's *Digital Payments
   — E-Mandate Framework, 2026* (issued 21 April 2026) consolidates and **replaces** all earlier
   e-mandate circulars. Cite the 2026 framework; the 2019/2021 circulars are superseded.
   Post-debit confirmation is also mandatory. AFA exemption: ₹15,000 general, ₹1,00,000 for
   insurance / mutual funds / credit-card bills.
3. **Registered templates constrain the LLM.** DLT template registration means generated copy
   cannot be free-form. The LLM selects and fills a registered template; it never authors an
   unbounded SMS. Mixing promotional content into a transactional message reclassifies it.

### Regulatory grounding — verify before hardcoding

Ground quiet hours, consent, and pre-debit notification in the real Indian rules: RBI's pre-debit notification requirement for e-mandates, TRAI's commercial-messaging restrictions and registered templates, and fair-practice limits on collections contact. **Cite the source in a comment next to each rule with the date checked.** These regulations move; a citation turns a potential gotcha into evidence of rigour.

---

## 7. Razorpay integration

### Use their error taxonomy as our ground truth

Razorpay's error object carries `code`, `description`, `source`, `step`, `reason`, `metadata`, and their docs are explicit that the purpose is to let merchants build their own remedial logic. **Our `FailureClass` mapping must be driven by their real `reason` values — do not invent categories.** Pull the published error-reasons list from their docs and commit it as `data/razorpay_error_reasons.csv`. The mapping table lives in `taxonomy/mapping.py` with one row per real reason.

This is the single highest-leverage integration decision. It is what makes the repo droppable into their world.

### Webhooks

Consume `payment.failed`, `payment.authorized`, `subscription.halted`, `subscription.charged`, `order.paid`, `invoice.*`.

- Verify HMAC-SHA256 over the **raw request body**, before parsing. Re-serialising breaks the signature.
- Public URL required — localhost is rejected, ports 80/443 only.
- **CORRECTED 2026-08-26: do NOT use ngrok.** Razorpay blacklists `ngrok.io` along with
  `webhook.site`, `requestbin.com`, `beeceptor.com`, `hookbin.com`, `mockbin.org`,
  `loca.lt` and several pentest collaborator domains. Their documented recommendation
  for local development is **zrok**. Test-mode webhook setup prompts for OTP `754081`.
- **Idempotency key is the `x-razorpay-event-id` header** — Razorpay documents it as
  unique per event and as the intended dedupe key. Do not invent our own.
- **Webhook order is NOT guaranteed.** Razorpay states `payment.authorized` then
  `payment.captured` "may not be followed at all times". Ingest classifies every payload
  on its own merits and never assumes ordering.
- If the webhook secret is rotated, old retries must still be verified with the OLD secret.
- Handle **late authorisation**: `payment.authorized` can arrive after an apparent failure. Handling this edge case is a differentiator; treat it as a stop condition.
- Idempotency keys on every write. Webhooks can be redelivered.

### Downtime API

Poll or subscribe to Razorpay's payment downtime signals and feed them into the timing decision. An agent that says *"don't retry — this bank is degraded, wait 20 minutes"* is using a Razorpay-native capability almost no applicant knows exists. Cheap to add, highly visible.

### Adapter boundary

One `PaymentProvider` protocol, one `RazorpayProvider` implementation, one `SimulatedProvider` for eval. The decision core must be fully testable with zero network.

### Test-mode gotchas

- Test mode is free, no KYC. Keys start `rzp_test_`. Secret shown once.
- Subscriptions may need enabling on the account — **check on day 1**, raise support immediately if off.
- Disputes cannot be created in test mode. Do not build a chargeback scenario.
- Rate limits return HTTP 429. **Never push the 2,000-record cohort at the live test API** — that is what `SimulatedProvider` is for. Test mode proves the plumbing; the simulator produces the statistics.

---

## 8. Evaluation — the part that wins

This is the most important module in the repo. Build it before the UI.

### Three-tier validation ladder

1. **Tier 1 — real API, small N.** Handful of live test-mode transactions proving the plumbing is authentic. Screenshot-able.
2. **Tier 2 — synthetic cohort at scale.** 10,000 events, 80/20 treatment/holdout, seeded.
   (Raised from 2,000 on 2026-08-26 after an a-priori power calculation — 2,000 gave a
   95% CI containing zero. See the POST-FREEZE CHANGE LOG in `eval/generate_cohort.py`.) This produces the headline numbers.
3. **Tier 3 — sensitivity sweep.** Same eval across 5 generator parameterisations (pessimistic/optimistic baseline recovery, different failure mixes). Report the envelope.

### Required metrics

| Metric | Treatment | Holdout |
|---|---|---|
| Events | 7,992 | 2,008 |
| Recovery rate | — | — |
| Recovered (₹) | — | — |
| **Incremental recovered (₹)** | **headline** | — |
| Intervention cost (₹) | — | ₹0 |
| Net incremental (₹) | — | — |
| Cost per recovery (₹) | — | — |
| Contacts per customer | — | 0 |
| Actions blocked by policy | — | — |
| Escalated to human | — | — |
| Refused (EV < 0) | — | — |

### Ablation study — highest-value single item

Deliberately cripple the agent and show metrics degrade. Run all four:

1. Random intervention chooser → lift should collapse
2. No taxonomy (all failures treated identically) → lift drops
3. No policy gate → more actions, worse cost-per-recovery
4. **LLM removed, rules only** → shows what the LLM actually contributes

**Publish result 4 honestly even if unflattering.** If the LLM only adds 12% over rules, say 12%. Everyone else will imply the LLM does all the work. Being the one person who measured it and reported an uncomfortable number is what makes the *other* numbers believable.

### Determinism requirement

`make eval` run twice must produce byte-identical metrics. Seeded generator, pinned model, and **every LLM response cached to `fixtures/`** under a content-addressed key. Temperature 0 is not available (and was never a real guarantee — server-side batching makes identical requests diverge). The committed fixture set is the guarantee, and it also means `make eval` needs **no API key at all**, which is what makes the 10-minute clone-and-reproduce benchmark achievable. This alone puts the repo in the top ~2% of submissions.

---

## 9. Credibility invariants — never violate

The single biggest risk to this project is **fooling ourselves with our own synthetic data.** A panel will spot circularity in thirty seconds. These rules are non-negotiable:

1. **Hidden latents.** Each synthetic event carries a latent recovery propensity. The agent sees only observables: error object, amount, customer's *past outcome history*, hour, bank, method. It must never read a latent field.
2. **Non-zero baseline.** Some customers recover with no intervention. Without this the holdout is 0% and the comparison is meaningless.
3. **Freeze the generator.** Commit `eval/generate_cohort.py` by day 2 and do not modify it while tuning the agent. Any post-freeze change must be a separate commit with a stated reason.
4. **Calibrate, don't invent.** Every generator parameter (failure mix, baseline recovery rates, amount distributions) cites a published source in `eval/CALIBRATION.md` — RBI/NPCI payment statistics, Razorpay's own published error and downtime material, industry dunning benchmarks.
5. **No peeking.** Never tune the agent by inspecting latent variables.
6. **Report failures.** `RESULTS.md` must contain a "Where this breaks" section with at least five honest failure cases.

---

## 10. Repo structure

```
recura/
├── CLAUDE.md               ← this file
├── README.md               ← metrics table FIRST, then 3-command quickstart
├── RESULTS.md              ← full eval output + "Where this breaks"
├── ARCHITECTURE.md         ← diagram, data flow, failure modes
├── policy.yaml
├── Makefile                ← seed | run | eval | ablate | sweep | replay
├── Dockerfile
├── docs/adr/               ← 0001-no-agent-framework.md, 0002-ev-over-rules.md, ...
├── data/razorpay_error_reasons.csv
├── config/costs.yaml
├── src/
│   ├── models.py
│   ├── clock.py            ← injected virtual clock; no datetime.now() anywhere else
│   ├── ingest/
│   ├── taxonomy/
│   ├── decide/  (llm.py, ev.py, bandit.py, prompts/)
│   ├── policy/
│   ├── act/     (executors + provider adapters)
│   ├── ledger/
│   └── api/     (FastAPI, /health, structured JSON logs)
├── eval/
│   ├── generate_cohort.py  ← FROZEN after day 2
│   ├── CALIBRATION.md
│   ├── run_batch.py
│   ├── metrics.py
│   ├── ablate.py
│   ├── sweep.py
│   └── replay.py           ← policy-diff harness
├── fixtures/               ← cached LLM responses
├── console/                ← ONE DAY MAX. ledger view + metrics. nothing else.
└── tests/
```

Storage: SQLAlchemy over SQLite locally. **No Postgres-specific SQL** — Razorpay runs MySQL and the schema should port.

---

## 11. Build order

Ship in tiers. Every tier is a complete, defensible submission on its own.

| Days | Tier | Deliverable |
|---|---|---|
| 1–2 | Foundation | Cohort generator + calibration + taxonomy mapping. **Freeze generator day 2.** |
| 3–4 | Plumbing | Test-mode wiring, webhook receipt + signature verification, provider adapters |
| 5–6 | Governance | Policy engine + append-only ledger |
| 7–8 | **Core** | Decide layer: LLM root cause, EV computation, deterministic fallback. Holdout eval running end-to-end. |
| 9–11 | Advanced | Thompson sampling, downtime-aware timing, virtual clock, replay/policy-diff |
| 12–13 | Stretch | Ablation study, sensitivity sweep, Hinglish message generation |
| 13 | Console | One day. Ledger + metrics. Resist. |
| 14 | Ship | Docs, ADRs, video, submit. **Do not submit on the 5th.** |

If behind on day 11, cut stretch and ship. **One exception: if only one stretch item fits, make it the ablation study.**

---

## 12. Anti-goals

Things that feel like progress and are not:

- ❌ **Any agent framework** (LangChain, CrewAI, LangGraph). We need deterministic replay and a policy gate the model cannot reach. Hand-rolled loop, ~200 lines, walkable line by line in an interview.
- ❌ **A beautiful dashboard.** The classic product-person failure mode: a polished UI on an unmeasured system is exactly what this buildathon filters out. Ugly and instrumented beats polished and unproven.
- ❌ **Breadth over depth.** One failure class done rigorously beats seven done shallowly. Do not attempt all seven of Razorpay's example directions.
- ❌ **A retry bot.** Razorpay already ships smart retries and dunning. We are the decision layer *above* them, not a reimplementation.
- ❌ **`datetime.now()`** anywhere outside `clock.py`.
- ❌ **Floats for money.** Integer paise only.
- ❌ **Sending anything real.** All customer contact is simulated and logged.
- ❌ **One giant commit.** Commit history should show two weeks of work.

---

## 13. Definition of done

Per component:

- **Ingest** — a real test-mode `payment.failed` webhook produces a valid `RiskEvent`, signature verified, replay-safe.
- **Taxonomy** — every reason in `razorpay_error_reasons.csv` maps to a `FailureClass`; unmapped reasons fall to `UNKNOWN` and are counted.
- **Decide** — emits a valid typed `Decision` with `considered` populated; invalid LLM output falls back to rules and is logged as a handled failure.
- **Policy** — every rule in `policy.yaml` has a test proving it blocks; blocks appear in the ledger with reasons.
- **Ledger** — append-only enforced at the DB layer; every action traceable to its reason.
- **Eval** — `make eval` twice → identical output. Metrics table matches README.

Project done when: `git clone && make eval` reproduces the README headline number in under 10 minutes on a clean machine.

---

## 14. The pitch

Memorise this. It is the video's opening and the interview's first answer:

> *"Recura is an agent that treats revenue recovery as a decision problem, not a workflow. It computes the expected value of every possible intervention, refuses to act when the math says don't, executes only within a policy contract it can't modify, and reports what it recovered against a randomised control group."*

Every clause is pointable-to in the repo. That is the whole trick.

### Video structure (5 min, hard)

| Time | Content |
|---|---|
| 0:00–1:00 | Problem, with a real number |
| 1:00–2:30 | Live batch run — 2,000 events, ledger filling |
| 2:30–4:00 | Metrics table **and failure cases** |
| 4:00–5:00 | Architecture: policy gate, why no framework |

Nobody else will spend 90 seconds admitting what didn't work. That is the moment they decide to call you in.

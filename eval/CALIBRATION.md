# Calibration

> CLAUDE.md section 9.4: *"Calibrate, don't invent. Every generator parameter cites a
> published source."* This file is that citation list. All sources checked **2026-08-26**.

## How to read this

Every parameter is graded by source quality. This matters more than the numbers:
a panel should be able to see instantly which figures are load-bearing regulation
and which are industry folklore.

| Grade | Meaning |
|---|---|
| **A — Primary regulatory** | RBI / TRAI / NPCI circular or regulation. Treated as binding. |
| **B — Primary operator** | Razorpay's own published documentation. Authoritative for taxonomy. |
| **C — Industry secondary** | Vendor benchmark reports. **Self-interested** — vendors selling recovery tools publish recovery statistics. Used only to bound *ranges*, never as point estimates. |

Every **C**-grade parameter is swept in Tier 3 (CLAUDE.md section 8). If the headline
result depends on a C-grade point estimate, the result is not real. The sweep exists
precisely so that no conclusion rests on a vendor's marketing number.

---

## 1. Regulatory constraints (grade A) — these bind `policy.yaml`

### 1.1 Contact window — **the spec's draft value is too permissive**

Two regimes overlap for a payment-recovery message:

| Regime | Window | Scope |
|---|---|---|
| TRAI TCCCPR | **09:00–21:00** | Promotional commercial communication; transactional messages exempt |
| RBI Fair Practices Code | **08:00–19:00** | Contact with borrowers for recovery — *all* channels: call, SMS, WhatsApp, email |

CLAUDE.md section 6 sketches `quiet_hours: {start: "21:00", end: "09:00"}`, i.e. a
09:00–21:00 contact window. That follows TRAI but **breaches RBI's collections
guidance by two hours every evening.**

**Decision: intersect them. Contact window 09:00–19:00 IST.** A recovery nudge is
causally a collections contact, so the stricter regime governs. Being conservative
here costs a little measured recovery and removes an entire category of objection.

- RBI direction to REs on recovery agents (Aug 2022): no contact before 08:00 or after 19:00,
  and ultimate responsibility for outsourced agents rests with the regulated entity.
- TRAI TCCCPR — promotional time-window restriction 09:00–21:00; transactional traffic
  is exempt *but the classification must be defensible*. We do not claim the exemption.

### 1.2 E-mandate pre-debit notification — **verified, and the framework is new**

CLAUDE.md section 6 flags `pre_debit_notification_hours: 24` as "VERIFY". Verified:

- **RBI Digital Payments — E-Mandate Framework, 2026 (issued 21 April 2026)** consolidates
  and *replaces* all earlier e-mandate circulars. Pre-debit notification to the customer
  **at least 24 hours before every debit**, with full transaction details and an opt-out
  facility. Post-debit confirmation is also mandatory.
- AFA (OTP) exemption threshold: **₹15,000** per recurring transaction; **₹1,00,000** for
  insurance premiums, mutual-fund subscriptions and credit-card bill payments.
- No charges may be levied on the customer for the e-mandate facility.

`24` is correct. Cite the **2026 framework**, not the 2019/2021 circulars — anyone
checking against the superseded rules is working from stale law.

### 1.3 Consent and templates

- Commercial messaging requires DLT-registered templates with matching variable fields.
- Mixing promotional content into a transactional/OTP message reclassifies the whole
  message as promotional, pulling it under DND scrubbing and the time window.
- Consequence for us: `require_consent: [sms, whatsapp, voice]` stays, and generated
  copy must map to a fixed template id. Free-form LLM text cannot be sent as-is. The
  LLM selects and fills a registered template; it does not author unbounded SMS.

---

## 2. Failure mix (grade A anchor, C-grade interior)

NPCI circular **OC-149 (June 2022)** sets ecosystem thresholds:

| Metric | Threshold | Observed |
|---|---|---|
| Technical Decline (TD) — bank/NPCI systems, network | **< 1%** | ~0.8% system-wide (2025), down from 8–10% in 2016 |
| Business Decline (BD) — funds, limits, PIN, risk | **< 5%** | per-bank figures published monthly by NPCI |

NPCI publishes per-bank BD/TD and uptime monthly, so this is verifiable rather than assumed.

**Anchor used:** TD:BD ≈ **1:5**. Since TD is definitionally infrastructure and BD is
definitionally customer/instrument-side, this gives:

| FailureClass | Share of failures | Grade | Basis |
|---|---|---|---|
| `TRANSIENT_INFRA` | ~17% | **A** | TD:BD ratio from OC-149 thresholds |
| `FUNDS` | ~30% | **C** | Industry consensus that insufficient funds dominates business declines |
| `AUTH_ABANDON` | ~22% | **C** | OTP/3DS drop-off; swept |
| `INSTRUMENT_INVALID` | ~14% | **C** | Expired cards, revoked mandates; swept |
| `LIMIT_EXCEEDED` | ~8% | **C** | Per-txn and NPCI frequency caps; swept |
| `RISK_DECLINE` | ~5% | **C** | Issuer risk declines; swept |
| `UNKNOWN` | ~4% | **B** | Razorpay documents `payment_failed`/`payment_declined` as "exact reason not communicated" |

Only the `TRANSIENT_INFRA` share and the existence of a large `UNKNOWN` bucket are
well-grounded. **The interior split is swept across five parameterisations in Tier 3.**

### Scope of this mix (added 2026-08-26)

NPCI's TD/BD statistics describe **declines** — transactions that reached a gateway and
were refused. That is not the whole population of revenue-at-risk. Two of Track 03's
named sources never reach a gateway at all:

- a **dropped checkout** is abandoned before any charge is attempted
- an **overdue invoice** was never charged; it simply passed its terms

Applying a decline-derived mix to those events would be a category error. They are
generated with their own distribution — checkout leaning to `AUTH_ABANDON`, receivables
to `FUNDS` — and both are deliberately noisy (70% / 75% aligned) so that `source_type`
constrains the truth without revealing it.

The consequence is that the **cohort-wide** mix no longer equals the table above: with
~20% checkout and ~10% invoice events, `AUTH_ABANDON` rises to roughly 31%. The table
above remains the calibration target for **gateway-attempted events**, which is the
population the cited sources actually describe, and that is what the generator test
asserts against.

---

## 3. Baseline recovery without intervention (grade C — swept, load-bearing)

This is the single most important generator parameter, because the holdout arm *is*
this number. CLAUDE.md section 9.2 requires it to be non-zero.

Published figures, all vendor-sourced and therefore graded **C**:

| Figure | Value | Source type |
|---|---|---|
| Industry median recovery rate | ~47.6% | Vendor benchmark |
| Layered programs (retry + email + SMS) | 70–85% | Vendor benchmark |
| Overall recovery with retries + email + SMS | ~70% | Churnkey, State of Retention 2025 |
| Median *attempted* recovery, B2B SaaS | 12.7% | Vendor sample, n=119, May 2026 |
| Incremental lift per dunning email | +1–2% | Vendor benchmark |
| Smart/adaptive dunning vs static rules | up to +25% | Vendor benchmark |
| Involuntary churn as share of subscription losses | 20–40% | Vendor benchmark |

**Critical reading:** these are *treated* recovery rates from companies running dunning.
None of them is an untreated baseline, which is exactly the number we need and exactly
the number nobody publishes — because measuring it requires a holdout, which is what
this project is arguing for.

**Decision:** baseline (no-intervention) recovery is modelled per failure class and
**swept from pessimistic to optimistic**. Point estimates used in the default run:

| FailureClass | Baseline recovery, no action | Rationale |
|---|---|---|
| `TRANSIENT_INFRA` | 0.45 | Rail recovers on its own; the customer often just retries |
| `FUNDS` | 0.30 | Salary arrives; some customers pay unprompted |
| `AUTH_ABANDON` | 0.25 | Genuine intent existed; some return without a nudge |
| `LIMIT_EXCEEDED` | 0.20 | Limit resets next cycle |
| `INSTRUMENT_INVALID` | 0.08 | Requires an explicit method change |
| `RISK_DECLINE` | 0.03 | Structurally blocked |
| `UNKNOWN` | 0.15 | Mixed bucket |

**These are assumptions, not measurements.** They are the reason Tier 3 exists. Any
headline number is reported as an envelope across the sweep, never as a single figure.

---

## 4. Amounts

UPI, June 2026: **22,716.07 million** transactions, down 2.09% month-on-month from
23,201.93 million in May 2026 (fewer calendar days). Grade **A** (NPCI published series).

Amount distribution is modelled log-normal, calibrated so the median sits in the
small-ticket UPI range with a heavy right tail for subscription and invoice events.
The escalation threshold in `policy.yaml` (`to_human_above_paise: 5000000` = ₹50,000)
must sit in the tail, not beyond it, or the escalation path never exercises. Grade **C**.

---

## 5. What is deliberately NOT calibrated

Honesty beats coverage:

- **Per-channel response rates** (SMS vs WhatsApp vs email uplift) — no credible
  India-specific published source found. Modelled as a documented assumption and swept.
- **Salary-cycle effect size** — the *direction* (month-start replenishment) is
  well-established; the magnitude is assumed.
- **Downtime windows** — shape taken from Razorpay's Downtime API semantics; frequency assumed.
- **Attention cost** — no published source exists for the monetary cost of annoying a
  customer. It is a modelling choice, documented in `config/costs.yaml`, and it is what
  makes the agent stop. It is swept.

---

## Source list

Primary (A):
- RBI, *Digital Payments — E-Mandate Framework, 2026*, issued 21 April 2026
- RBI direction to regulated entities on recovery agents, August 2022 (08:00–19:00 contact window)
- TRAI, *Telecom Commercial Communications Customer Preference Regulations* (TCCCPR), incl. 2025 amendments
- NPCI circular OC-149, June 2022 (TD < 1%, BD < 5%); NPCI UPI Ecosystem Statistics / BD-TD & Uptime pages

Operator (B):
- Razorpay error documentation — errors list, e-mandate errors, payment error parameters
  (transcribed to `data/razorpay_error_reasons.csv`)

Secondary (C) — vendor benchmarks, treated as ranges only:
- Baremetrics, Churnkey (*State of Retention 2025*), Recurly, Slicker, RetentionLens

---

## 6. Emission noise (added 2026-08-26) — the anti-circularity parameter

The generator treats the Razorpay `reason` as a **noisy emission** of the true cause,
not a perfect observation of it. Design split:

| Emission | Share | Basis |
|---|---|---|
| Truthful — reason indicates the true class | ~78% | Most reason codes genuinely are diagnostic (grade **B**) |
| Opaque — reason carries no information | 12% | Razorpay documents `payment_failed` / `payment_declined` as "exact reason not communicated" (grade **B**) |
| Misleading — reason points at the wrong class | 10% | Modelling choice (grade **C**), swept |

**Measured on the frozen cohort: a naive lookup on the reason code mis-classifies
24.1% of events** (1 in 4). Note this exceeds the 22% injected noise, because the ~4%
of events whose *true* class is `UNKNOWN` also emit opaque codes.

Why this parameter is load-bearing: if the label were always truthful, a lookup table
would be the optimal agent, and the taxonomy and LLM ablations in section 8 would
correctly measure **zero** contribution. The noise is what creates an inference problem
for the agent to solve — and therefore what makes the ablation study informative rather
than decorative. It is deliberately grounded in Razorpay's own documented behaviour so
that it reads as realism rather than as a handicap chosen to flatter the agent.

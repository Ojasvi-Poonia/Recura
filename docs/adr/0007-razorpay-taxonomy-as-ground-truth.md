# ADR 0007: Razorpay's error taxonomy is ground truth; triage is a separate dimension

**Status:** Accepted · **Date:** 2026-08-27

## Context

Every recovery system needs a notion of *what went wrong*. The tempting move is to invent
a clean set of categories that fit the model you want to build.

Razorpay already publishes one. Their error object carries `code`, `description`,
`source`, `step` and `reason`, and their documentation is explicit that the purpose is to
let merchants build their own remedial logic. Inventing categories alongside that would
make the repo undroppable into their world.

## Decision

**Their data stays theirs.** `data/razorpay_error_reasons.csv` holds 115 reason codes
transcribed from four published tables — Bad Request, Gateway, E-mandate Registration,
E-mandate Subsequent. We do not edit it. Their documentation renders client-side, so it
was extracted with a real browser rather than a fetch.

**Our judgement lives separately.** `taxonomy/mapping.py` maps each of the 115 to a
`FailureClass`, with a written rationale wherever the call is contestable. A test asserts
115/115 coverage and that no key exists which Razorpay does not publish.

**Triage is orthogonal to diagnosis.** Razorpay's list mixes genuine customer failures
with **merchant integration bugs** — `invalid_order_id`, `live_mode_not_enabled`,
`merchant_not_activated`. **31 of 115 are the latter.** Nudging a customer because the
*merchant* sent a malformed request would be indefensible, so a second enum,
`Recoverability` (`CUSTOMER_RECOVERABLE` / `MERCHANT_CONFIG` / `TERMINAL`), sits alongside
the failure class.

**Two sources carry no error code at all.** A dropped checkout never reached a gateway and
an overdue invoice was never charged, so neither can have one. Both are classified from
`source_type` instead — and receivables get a different action space entirely, since there
is no instrument to retry.

## Consequences

**The mapping is auditable against their docs.** A Razorpay engineer can diff our CSV
against their published tables.

**Merchant bugs never reach a customer.** 31 reason codes can only produce escalation to
the merchant's own team, never a nudge — and escalating a config bug does not consume the
customer's contact budget.

**`order_already_paid` is a stop condition.** It is how late authorisation surfaces on a
retry. Acting there would dun a customer who has already paid.

**Some calls are genuinely uncertain, and we say so.** `card_declined` is flagged in the
mapping as our likeliest mis-classification — a bare issuer decline may hide a risk
decline. We class it as `INSTRUMENT_INVALID` because that matches Razorpay's suggested
remedy, and we surface the doubt in `RESULTS.md`.

**Their taxonomy changes.** Codes get added; `gemini-2.5-*` style retirements happen to
everyone. Unmapped reasons fall to `UNKNOWN` and are counted rather than silently
swallowed.

## Alternatives considered

**Invent our own categories.** Cleaner to model against, and immediately less credible.

**Use `source`/`step` directly as the class.** Too coarse — `source=customer` covers both
insufficient funds and a mistyped OTP, which need opposite treatments.

**A single enum combining failure and triage.** Was our first design. It forces
`invalid_order_id` into a *recovery* category, which is a category error.

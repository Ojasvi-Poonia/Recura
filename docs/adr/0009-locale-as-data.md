# ADR 0009: Locale is data, not code

**Status:** Accepted · **Date:** 2026-08-27

## Context

An audit for hardcoded assumptions found seven India-isms compiled into the decision core:
the rupee symbol in the money formatter, `Asia/Kolkata` in the clock, UPI and netbanking
in the rail-alternatives table, RBI and TRAI in the policy engine, `INR` as a default.

That matters more than it first appears. Razorpay operates in **India, Malaysia (as
Curlec) and Singapore**, and their own error documentation lists all three plus the United
States. The worst of the seven was the rail table: a Curlec merchant's customer would have
been told to pay by UPI, which does not exist in Malaysia.

A payment-recovery agent with rupees and RBI compiled into it is an Indian script, not a
product.

## Decision

Move everything locale-shaped into `config/markets.yaml`: currency code and symbol, minor
units per major, timezone, lawful contact window, payment rails and their alternatives,
languages, regulators, notice periods. The decision core asks a `Market` object.

An AST-parsing test fails the build if a currency symbol appears in `src/` outside the two
sanctioned modules.

**We ship India only.** The structure supports more; we ship only what we have verified
against primary regulation. A profile we had not checked would be a liability dressed as a
feature.

## Consequences

**No locale is compiled into the decision core.** A second market is a config file rather
than a rewrite.

**Money conversion is never assumed.** `minor_per_major` happens to be 100 in all three
markets, but currencies exist where it is not, so the divisor is read rather than
hardcoded.

**An unknown market raises rather than defaulting to India.** Silently applying RBI rules
to a Singapore merchant is worse than failing loudly.

**Two files could disagree.** `policy.yaml` states the contact window because it is the
human-readable contract; `markets.yaml` states it because it is the regulatory source.
Both are legitimate, so a test asserts they match — otherwise the contract and the engine
could drift apart silently.

**Unverified profiles must declare themselves.** `Market.caveat()` exists so a placeholder
cannot pass as researched. Currently unused, because we ship only verified profiles.

## Alternatives considered

**Keep it India-only and hardcoded.** Simpler, and defensible for a hackathon — but it
misrepresents how much work a second market would be, and the UPI-in-Malaysia bug is the
kind of thing that reads badly in review.

**Full i18n with locale-aware formatting.** Over-engineered. The problem is not
pluralisation rules, it is that regulation and payment rails differ.

**Ship Malaysia and Singapore profiles anyway.** We built them and removed them. Shipping
contact windows we had not checked against Bank Negara or MAS would be exactly the kind of
unverified claim this project exists to avoid.

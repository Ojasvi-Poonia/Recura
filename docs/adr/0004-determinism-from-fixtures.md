# ADR 0004: Determinism comes from fixture caching, not temperature

**Status:** Accepted · **Date:** 2026-08-27 · **Supersedes:** the original spec's
"temperature 0, pinned model string"

## Context

The project's definition of done is that a Razorpay engineer clones the repo and
reproduces the headline number in under ten minutes. That requires `make eval` to be
byte-identical across runs and machines.

Our original plan was the conventional one: pin the model, set `temperature=0`, done.

Two things were wrong with it.

**`temperature` no longer exists.** It was removed on Claude Opus 5, Sonnet 5, Fable 5
and the 4.6+ family; sending it returns HTTP 400. Verified against the SDK documentation
on 2026-08-26.

**It was never a real guarantee anyway.** Temperature 0 selects the argmax token, but
server-side batching means identical requests can still diverge. It was a convention
people trusted rather than a property anyone checked.

## Decision

Content-address every model response and commit the cache.

The key is `SHA-256(model, system_prompt, payload)`, truncated to 32 hex characters.
Responses live in `fixtures/` — 870 files — and are committed to the repository.

Payloads are **banded** before hashing: an amount becomes `Rs2k-10k`, an hour becomes
`afternoon`. A root cause does not depend on the last two digits of an amount, and
banding collapses near-identical questions into one cached answer. The EV layer still
sees exact paise, because the money does.

## Consequences

**`make eval` needs no API key at all.** This is the important one. A reviewer with no
Anthropic or Google account reproduces the headline number offline. Without it, the
ten-minute benchmark is unachievable for most readers.

**Determinism is structural.** There is no model call during eval, so there is nothing to
diverge. Verified by `make validate`.

**Every prompt change invalidates the entire cache.** We learned this the expensive way,
regenerating three times during the build. The prompt is now frozen alongside the
generator.

**The cache is model-scoped.** Keys include the model id, so an Anthropic fixture set and
a Gemini one coexist — which is what makes a provider-comparison ablation possible.

**A stale fixture is invisible.** If the cohort changes, old keys simply stop matching and
counts silently disagree. `--prune` was added after that bit us; a dry run against a
missing key now warns loudly rather than reporting a plan for a set that cannot match.

## Alternatives considered

**Re-query on every run.** Non-deterministic, costs money per run, requires a key, and
rate-limits at scale. Fails the benchmark outright.

**Record a single golden output file.** Cheaper, but only proves the run *was* recorded —
not that the pipeline still produces it. Fixtures replay the actual code path.

**Seeded local model.** Genuinely deterministic, but adds a heavyweight dependency and
weakens the "droppable into their world" argument.

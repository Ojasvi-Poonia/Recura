# ADR 0002: Expected value over rules

**Status:** Accepted · **Date:** 2026-08-27

## Context

Every dunning product is a rule engine: *card expired → send card-update email; wait 3
days; retry; wait 5 days; retry; give up.* Rules are legible, testable and easy to sell.

They are also indifferent to money. The same ladder runs for a ₹200 failure and a ₹50,000
one, for a customer contacted yesterday and one contacted never, at a time that suits the
scheduler rather than the payer.

The brief asks for an agent that "determines the right intervention". *Right* is a
comparative, and comparatives need a scale.

## Decision

Price every candidate action in money and take the argmax:

```
EV(action) = (p(action) − p(no_action)) × (1−b)^(k−1) × amount × margin
             − direct_cost − attention_cost
chosen = argmax EV;   if max(EV) < 0 → NO_ACTION
```

Every candidate — including the rejected ones and their expected values — is written to
the ledger.

## Consequences

**`NO_ACTION` becomes arithmetic rather than a rule.** Asked why a transaction was
skipped, the answer is a number, not a policy line. In the reported run the agent refuses
2,551 times, and each refusal has a reason a merchant can audit.

**The agent stops on its own.** Attention cost rises superlinearly and includes expected
opt-out loss, so the fourth message prices itself out. Replay shows that loosening the
contact cap from 3 to 5 changes the outcome by **exactly ₹0** — the maths binds before the
contract does.

**Every term must be justified.** `margin`, `direct_cost`, `attention_cost` and the
propensity model are all assumptions. They are cited in `CALIBRATION.md` and swept in
Tier 3, and the sweep shows a parameterisation where the agent *loses money*.

**It can be wrong in ways rules cannot.** A mis-estimated propensity produces a confidently
wrong action. The ablation is the guard: a random chooser posts −4.70pp, so we can at
least demonstrate the optimiser is doing real work.

## Alternatives considered

**Rules with priorities.** Simpler and more predictable, but cannot answer "is this worth
doing" — only "what is next". Kept as ablation 1's contrast.

**Bandit over actions with no cost model.** Learns what works but not what is worth doing;
would happily spend ₹120 to recover ₹200.

**Absolute rather than incremental EV.** Our first implementation. It makes `NO_ACTION`
unreachable, because doing nothing already has positive value. See ADR 0006.

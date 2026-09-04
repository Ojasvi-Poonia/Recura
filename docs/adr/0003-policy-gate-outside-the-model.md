# ADR 0003: The policy gate lives outside the model's reach

**Status:** Accepted · **Date:** 2026-08-27

## Context

Recovery touches money and regulated communication. An agent that can be talked out of
its own compliance rules is not deployable, and "we put the rules in the system prompt"
is not a control.

The usual failure mode is subtle: the model is given the policy so it can "reason about
compliance", which means the policy is now in a context window that also contains
attacker-controlled data — a customer name, an error description, a merchant note.

## Decision

Three separated authorities:

| Authority | Owns | Cannot |
|---|---|---|
| LLM | Root-cause synthesis; filling registered templates | Choose an action; see `policy.yaml`; author free-form copy |
| Expected value | Pricing candidates; the argmax | Bypass the contract |
| Policy engine | Deterministic evaluation of `policy.yaml` | Consult a model |

Enforced mechanically, not by convention. `tests/test_invariants.py` parses the AST of
every module under `src/decide/` and fails the build on any import of `src.policy` or any
string constant containing `policy.yaml`.

The prompt is separately tested to contain none of "policy.yaml", "quiet hours",
"consent", "budget" or "escalat", and to contain the instruction *"Do not recommend an
action."*

## Consequences

**"No prompt injection can unlock a money action" is a property a test checks.** That
sentence is the one we most want to be able to say, and it is now cheap to verify.

**The model cannot explain a block.** It does not know the rules exist. Explanation comes
from the ledger, which records the rule id and a human-readable reason for every block —
6,256 of them in the reported run.

**Compliance is legible.** `policy.yaml` is 20 rules a merchant could read, each with a
citation and a date checked. Changing behaviour means editing a contract, not a prompt.

**The gate cannot be smart.** It cannot weigh a genuinely novel situation. Everything
nuanced must live in the EV layer or be escalated to a person.

## Alternatives considered

**Policy in the system prompt.** Cheapest, and the thing we are arguing against. One
crafted `error_description` away from failure.

**Model-generated policy checks.** Flexible and unverifiable. A gate that is
non-deterministic is not a gate.

**Post-hoc audit instead of a pre-execution gate.** Detects breaches after the message
has been sent. For regulated contact that is too late.

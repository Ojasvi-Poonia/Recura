"""Mutation testing — do our tests actually catch the bugs they claim to?

A green test suite proves the tests pass. It does not prove they would notice if the
code were wrong. Those are different claims, and only the second one matters.

So we deliberately break the system in ways we have already been broken, run the whole
suite, and check something fails. A mutant that SURVIVES is a gap: behaviour the project
asserts in prose and does not actually verify.

Every mutant below is a real bug this project shipped at some point, or a defence it
claims in the README:

    inbound opt-out ignored        a compliance failure we shipped and found by probing
    policy gate bypassed           a bug introduced while fixing another bug
    slot validation disabled       the vulnerability the red team found
    horizon discount removed       the fix that cut escalations by half
    source=business triage dropped the bug real Razorpay data found
    NO_ACTION no longer zero       the zero point of the whole EV formula
    episode bound tripled          the fairness property behind the headline

Source files are restored after every run, including on failure.

    make mutants
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    label: str
    path: str
    old: str
    new: str
    why: str
    # A second (old, new) pair applied in the same file. Needed when a defence is read
    # from more than one place: mutating one site leaves the other still enforcing it,
    # and the mutant survives because the system is fine, not because a test is missing.
    also: tuple[str, str] | None = None


MUTANTS: list[Mutant] = [
    # Both places the inbound flag is read. Seeding it in one and not the other still
    # protects the customer, so a single-site mutant survives for the wrong reason: the
    # remaining path catches it. This drops the inbound flag everywhere.
    Mutant("inbound opt-out ignored", "src/agent.py",
           "paid=paid, opted_out=opted_out or history.opted_out,",
           "paid=paid, opted_out=opted_out,",
           "a customer who unsubscribed last month gets contacted",
           also=("                opted_out=opted_out or history.opted_out,",
                 "                opted_out=opted_out,")),
    Mutant("policy gate bypassed entirely", "src/agent.py",
           "            if self.use_policy:\n"
           "                verdict = evaluate(decision, state, now, self.policy)",
           "            if False:\n"
           "                verdict = evaluate(decision, state, now, self.policy)",
           "the contract stops being enforced at all"),
    Mutant("episode bound tripled", "src/agent.py",
           "for seq in range(MAX_DECISIONS):", "for seq in range(MAX_DECISIONS * 3):",
           "treatment gets more draws than control - manufactured lift"),
    Mutant("slot validation disabled", "src/act/messaging.py",
           '    safe = {s: validate_slot(s, slots[s]) for s in tpl["slots"]}',
           '    safe = {s: str(slots[s]) for s in tpl["slots"]}',
           "arbitrary text rides into a customer SMS inside a template slot"),
    Mutant("NO_ACTION no longer scores zero", "src/decide/ev.py",
           "        if action is ActionType.NO_ACTION:\n            p = p_no_action",
           "        if action is ActionType.NO_ACTION:\n            p = p_no_action * 0.9",
           "refusal stops being the arithmetic zero point"),
    Mutant("horizon discount removed", "src/decide/ev.py",
           "horizon_discount = (1.0 - p_no_action) ** max(0, ctx.steps_remaining - 1)",
           "horizon_discount = 1.0",
           "every intervention is overvalued ~3x"),
    Mutant("source=business triage dropped", "src/taxonomy/mapping.py",
           'MERCHANT_SOURCES = frozenset({"business"})', "MERCHANT_SOURCES = frozenset()",
           "customers get messaged about the merchant's own misconfiguration"),
    Mutant("unsent messages are credited anyway", "src/agent.py",
           "                if rendered is None:",
           "                if False:",
           "a nudge that could not be written is still scored as though it had been sent"),
    Mutant("Thompson draws per candidate, not per arm", "src/decide/ev.py",
           "            p = _p_for(ctx, action, params, model, rng, explore, draws)",
           "            p = _p_for(ctx, action, params, model, rng, explore)",
           "argmax over k draws from one posterior is an order statistic, so actions "
           "with more candidates win on best-of-k rather than on evidence"),
    Mutant("the action space stops respecting what we can write", "src/decide/ev.py",
           "        if not can_render(ctx.failure_class, channel, ev.source_type):\n"
           "            continue\n"
           "        for offset in NUDGE_OFFSETS_H:",
           "        for offset in NUDGE_OFFSETS_H:",
           "unwritable nudges re-enter the candidate set and compete on EV against "
           "actions that can actually be executed"),
    Mutant("model trust stops being learned", "src/agent.py",
           "        trust = (self.model.sample_source(self.rng) if self.explore\n"
           "                 else self.model.expected_source())",
           "        trust = DiagnosisSource.BLENDED",
           "the trust weight reverts to a constant an author picked - the worst of three"),
    # Targets the method itself, not one call site. The agent credits the diagnosis
    # source from two different outcome paths, so disabling either one alone leaves the
    # other still learning and the mutant survives for the wrong reason.
    Mutant("diagnosis source never gets credited", "src/decide/bandit.py",
           "        self._sources[source] = self._sources.get(source, self._prior)"
           ".updated(success)",
           "        return",
           "the meta-bandit never learns; trust stays at the uninformative prior"),
    Mutant("attention cost ignores opt-out risk", "src/act/costs.py",
           "    expected_loss = opt_out_probability(contacts_last_7d) * opt_out_risk_paise()",
           "    expected_loss = 0",
           "contacting people becomes nearly free; the agent stops stopping"),
]


def run_suite() -> list[str]:
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "--tb=no",
         "-p", "no:cacheprovider", "-x"],
        capture_output=True, text=True, cwd=ROOT)
    return [line.split("::")[-1].split()[0]
            for line in result.stdout.splitlines() if line.startswith("FAILED")]


def main() -> None:
    rule = "─" * 96
    print(f"\n{rule}\n  RECURA — mutation testing: do the tests notice when the code is wrong?\n{rule}")
    print(f"  {'planted bug':<38}{'caught':<9}{'consequence if it shipped'}")
    print("-" * 96)

    survivors, skipped = [], []
    for m in MUTANTS:
        path = ROOT / m.path
        original = path.read_text()
        if m.also and m.also[0] not in original:
            raise SystemExit(f"mutant {m.label!r}: secondary anchor no longer present")
        if m.old not in original:
            skipped.append(m.label)
            print(f"  {m.label:<38}{'SKIP':<9}anchor no longer present - mutant is stale")
            continue
        try:
            mutated = original.replace(m.old, m.new, 1)
            if m.also:
                mutated = mutated.replace(m.also[0], m.also[1], 1)
            path.write_text(mutated)
            failed = run_suite()
        finally:
            path.write_text(original)          # restored even if the run explodes
        if failed:
            print(f"  {m.label:<38}{'yes':<9}{m.why}")
        else:
            survivors.append(m.label)
            print(f"  {m.label:<38}{'NO':<9}SURVIVED — {m.why}")

    print("-" * 96)
    tested = len(MUTANTS) - len(skipped)
    print(f"  {tested - len(survivors)}/{tested} planted bugs caught")
    if skipped:
        print(f"  {len(skipped)} stale mutant(s): {skipped} — the code moved, update them")
    if survivors:
        print(f"\n  SURVIVORS: {survivors}")
        print("  Each is behaviour this project claims and does not actually verify.")
    else:
        print("\n  No survivors. Every defence the README claims is one a test would miss"
              "\n  the absence of.")
    raise SystemExit(1 if survivors else 0)


if __name__ == "__main__":
    main()

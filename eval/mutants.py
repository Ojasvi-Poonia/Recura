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


MUTANTS: list[Mutant] = [
    Mutant("inbound opt-out ignored", "src/agent.py",
           "opted_out=opted_out or history.opted_out,", "opted_out=opted_out,",
           "a customer who unsubscribed last month gets contacted"),
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
    Mutant("model trust stops being learned", "src/agent.py",
           "        trust = (self.model.sample_source(self.rng) if self.explore\n"
           "                 else self.model.expected_source())",
           "        trust = DiagnosisSource.BLENDED",
           "the trust weight reverts to a constant an author picked - the worst of three"),
    Mutant("diagnosis source never gets credited", "src/agent.py",
           "            if dx.trust is not None:\n"
           "                # Credit or blame the diagnosis source we chose to act on.\n"
           "                self.model.update_source(dx.trust, got_it)",
           "            if False:\n"
           "                self.model.update_source(dx.trust, got_it)",
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
        if m.old not in original:
            skipped.append(m.label)
            print(f"  {m.label:<38}{'SKIP':<9}anchor no longer present - mutant is stale")
            continue
        try:
            path.write_text(original.replace(m.old, m.new, 1))
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

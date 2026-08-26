"""Policy-diff harness (CLAUDE.md section 10).

"What would this contract have cost us?"

A merchant asked to accept a stricter policy will want to know the price of it, and a
compliance team asked to loosen one will want to know what it buys. Because every
decision, its expected value, and its policy verdict are in an append-only ledger, we
can re-run the entire cohort under a different `policy.yaml` and diff the outcomes.

This is what an audit trail is FOR. Not "we logged things" - "we can answer
counterfactual questions about our own governance."

    make replay
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from eval.run_batch import RunConfig, run
from src.models import rupees
from src.policy.engine import load_policy

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "replay.json"


@dataclass(frozen=True)
class Variant:
    label: str
    question: str
    patch: dict          # dotted path -> value


VARIANTS: list[Variant] = [
    Variant("as committed", "What does the shipped contract deliver?", {}),
    Variant(
        "TRAI-only contact window",
        "What do the two extra evening hours buy, if we followed TRAI alone?",
        {"contact.quiet_hours.start": "21:00"}),
    Variant(
        "stricter: 1 contact per week",
        "What would a maximally cautious merchant give up?",
        {"contact.max_per_customer_per_7d": 1}),
    Variant(
        "looser: 5 contacts per week",
        "What would relaxing the contact cap earn, and at what cost per recovery?",
        {"contact.max_per_customer_per_7d": 5}),
    Variant(
        "no human escalation at all",
        "How much of the result depends on having people available?",
        {"escalation.max_per_day": 0}),
    Variant(
        "spend cap 5x tighter (Rs 5k/day)",
        "What does a merchant give up by under-budgeting recovery?",
        {"merchant.daily_spend_cap_paise": 500000}),
    Variant(
        "spend cap 25x tighter (Rs 1k/day)",
        "And at the point where the budget is purely symbolic?",
        {"merchant.daily_spend_cap_paise": 100000}),
    Variant(
        "no merchant spend cap at all",
        "Does the shipped budget cost anything, or does the agent stop first?",
        {"merchant.daily_spend_cap_paise": 10**12}),
    Variant(
        "retry risk declines anyway",
        "What if we ignored the rule against retrying risk declines?",
        {"retry.forbidden_for_classes": ["INSTRUMENT_INVALID"]}),
]


def apply_patch(policy: dict, patch: dict) -> dict:
    out = copy.deepcopy(policy)
    for dotted, value in patch.items():
        node = out
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return out


def main() -> None:
    base_policy = load_policy()
    rows = []
    for variant in VARIANTS:
        policy = apply_patch(base_policy, variant.patch)
        comparison, _ = run(RunConfig(label=variant.label, policy=policy), quiet=True)
        rows.append((variant, comparison))
        print(f"  replayed {variant.label}")

    baseline = rows[0][1]
    print(f"\n{'=' * 108}\n  RECURA - policy replay / contract diff\n{'=' * 108}")
    print(f"{'policy variant':<30}{'lift pp':>9}{'net incremental':>19}"
          f"{'vs shipped':>18}{'cost/recov':>12}{'blocked':>10}{'contacts':>10}")
    print("-" * 108)
    for variant, c in rows:
        delta = ("--" if c is baseline
                 else rupees(c.net_incremental_paise - baseline.net_incremental_paise))
        print(f"{variant.label:<30}{c.lift_pp:>+9.2f}"
              f"{rupees(c.net_incremental_paise):>19}{delta:>18}"
              f"{rupees(c.cost_per_recovery_paise):>12}"
              f"{c.treatment.actions_blocked:>10,}{c.treatment.contacts_per_customer:>10.3f}")
    print("-" * 108)
    print("\nwhat each variant answers:")
    for variant, _ in rows:
        print(f"  {variant.label:<30} {variant.question}")

    OUT_PATH.write_text(json.dumps(
        {v.label: {"question": v.question, "patch": v.patch,
                   "lift_pp": c.lift_pp,
                   "net_incremental_paise": c.net_incremental_paise,
                   "delta_vs_shipped_paise": c.net_incremental_paise
                   - baseline.net_incremental_paise,
                   "actions_blocked": c.treatment.actions_blocked}
         for v, c in rows}, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

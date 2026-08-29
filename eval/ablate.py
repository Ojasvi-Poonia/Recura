"""Ablation study (CLAUDE.md section 8) - the highest-value single item in the repo.

Deliberately cripple the agent four ways and show the metrics degrade. If a component
is load-bearing, removing it must cost measured money. If it is not, the honest thing
is to publish that.

    1 random chooser     ignore EV entirely  -> lift should collapse
    2 no taxonomy        all failures alike  -> lift should drop
    3 no policy gate     no governance       -> more actions, worse cost per recovery
    4 no LLM             rules only          -> what the model actually contributes

Section 8: "Publish result 4 honestly even if unflattering. If the LLM only adds 12%
over rules, say 12%." Being the one applicant who measured an uncomfortable number is
what makes the comfortable ones believable.

    make ablate
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.metrics import as_dict
from eval.run_batch import RunConfig, run
from src.models import rupees

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "ablations.json"

ABLATIONS: list[RunConfig] = [
    RunConfig(label="full agent"),
    RunConfig(label="1 random chooser", random_chooser=True),
    RunConfig(label="2 no taxonomy", use_taxonomy=False),
    RunConfig(label="3 no policy gate", use_policy=False),
    RunConfig(label="4 no LLM (rules only)", use_llm=False),
]


def main() -> None:
    rows = []
    for config in ABLATIONS:
        comparison, _ = run(config, quiet=True)
        rows.append((config.label, comparison))
        print(f"  ran {config.label}")

    baseline = rows[0][1]
    print(f"\n{'=' * 96}\n  RECURA - ablation study\n{'=' * 96}")
    header = (f"{'configuration':<26}{'lift pp':>10}{'95% CI':>18}"
              f"{'net incremental':>20}{'cost/recovery':>14}{'vs full':>8}")
    print(header)
    print("-" * 96)
    for label, c in rows:
        ci = f"[{c.lift_ci_low_pp:+.2f}, {c.lift_ci_high_pp:+.2f}]"
        delta = ("--" if c is baseline
                 else f"{(c.lift_pp - baseline.lift_pp) / abs(baseline.lift_pp) * 100:+.0f}%"
                 if baseline.lift_pp else "n/a")
        print(f"{label:<26}{c.lift_pp:>+10.2f}{ci:>18}"
              f"{rupees(c.net_incremental_paise):>20}"
              f"{(rupees(c.cost_per_recovery_paise) if c.cost_per_recovery_paise is not None else 'n/a'):>14}{delta:>8}")
    print("-" * 96)

    print("\nsupporting counts:")
    print(f"{'configuration':<26}{'actions':>10}{'blocked':>10}{'refused':>10}"
          f"{'contacts/cust':>15}{'escalated':>11}")
    for label, c in rows:
        t = c.treatment
        print(f"{label:<26}{t.events:>10}{t.actions_blocked:>10}"
              f"{t.refused_negative_ev:>10}{t.contacts_per_customer:>15.3f}"
              f"{t.escalated:>11}")

    OUT_PATH.write_text(
        json.dumps({label: as_dict(c) for label, c in rows}, indent=2, sort_keys=True),
        encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

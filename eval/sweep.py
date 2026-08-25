"""Tier 3: sensitivity sweep (CLAUDE.md section 8).

Every grade-C parameter in eval/CALIBRATION.md is an assumption, not a measurement.
Reporting a headline that rests on a point estimate of an assumption is exactly the kind
of claim a panel should distrust. So we re-run the whole evaluation across five
generator parameterisations and report the ENVELOPE.

The frozen generator is not modified. Each parameterisation temporarily overrides the
module-level constants it is designed to vary, generates a cohort IN MEMORY, runs the
full agent against it, and restores. `data/cohort.json` is never touched.

What is deliberately varied (all grade C in CALIBRATION.md):
  - baseline recovery per class      how much recovers with no intervention at all
  - failure-class mix                what kind of failures dominate
  - emission noise                   how often the reason code lies
  - action efficacy                  how much any intervention can move the needle

    make sweep
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from eval import generate_cohort as gen
from eval import latents as lat
from eval.metrics import compare
from eval.run_batch import RunConfig, make_observe, run
from src.models import FailureClass as FC
from src.models import rupees

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "sweep.json"


@dataclass(frozen=True)
class Parameterisation:
    label: str
    rationale: str
    baseline_scale: float = 1.0          # scales BASELINE_RECOVERY
    efficacy_scale: float = 1.0          # scales how much actions help
    failure_mix: dict | None = None
    p_opaque: float | None = None
    p_misleading: float | None = None


SWEEP: list[Parameterisation] = [
    Parameterisation(
        "baseline (calibrated)",
        "The documented parameterisation. Everything else moves relative to this."),
    Parameterisation(
        "pessimistic: high self-recovery",
        "Customers recover far more on their own, so there is less for us to add.",
        baseline_scale=1.45),
    Parameterisation(
        "optimistic: low self-recovery",
        "Almost nobody pays unprompted; intervention has maximum headroom.",
        baseline_scale=0.60),
    Parameterisation(
        "weak interventions",
        "Actions barely move the needle - the pessimistic view of what dunning can do.",
        efficacy_scale=0.55),
    Parameterisation(
        "hard failure mix + noisier labels",
        "Dominated by dead instruments and risk declines, with a less reliable "
        "reason code. The worst realistic world for a recovery agent.",
        failure_mix={FC.TRANSIENT_INFRA: 0.10, FC.FUNDS: 0.18, FC.AUTH_ABANDON: 0.14,
                     FC.INSTRUMENT_INVALID: 0.30, FC.LIMIT_EXCEEDED: 0.08,
                     FC.RISK_DECLINE: 0.14, FC.UNKNOWN: 0.06},
        p_opaque=0.20, p_misleading=0.16),
]

# Multipliers in eval/latents.py that govern how much an action can help.
_EFFICACY_KEYS = ("RETRY_ALIGNED", "SWITCH_WHEN_DEAD", "NUDGE_INTENT_WEIGHT",
                  "ESCALATE_EFFICACY")


@contextmanager
def parameterisation(params: Parameterisation):
    """Temporarily apply a parameterisation, then restore every constant."""
    saved_baseline = dict(lat.BASELINE_RECOVERY)
    saved_efficacy = {k: getattr(lat, k) for k in _EFFICACY_KEYS}
    saved_mix = dict(gen.FAILURE_MIX)
    saved_noise = (gen.P_OPAQUE, gen.P_MISLEADING)
    try:
        for cls, value in saved_baseline.items():
            lat.BASELINE_RECOVERY[cls] = min(0.95, value * params.baseline_scale)
        for key in _EFFICACY_KEYS:
            setattr(lat, key, saved_efficacy[key] * params.efficacy_scale)
        if params.failure_mix:
            gen.FAILURE_MIX.clear()
            gen.FAILURE_MIX.update(params.failure_mix)
        if params.p_opaque is not None:
            gen.P_OPAQUE = params.p_opaque
        if params.p_misleading is not None:
            gen.P_MISLEADING = params.p_misleading
        yield
    finally:
        lat.BASELINE_RECOVERY.clear()
        lat.BASELINE_RECOVERY.update(saved_baseline)
        for key, value in saved_efficacy.items():
            setattr(lat, key, value)
        gen.FAILURE_MIX.clear()
        gen.FAILURE_MIX.update(saved_mix)
        gen.P_OPAQUE, gen.P_MISLEADING = saved_noise


def run_one(params: Parameterisation):
    with parameterisation(params):
        events, latents, arms = gen.generate()
        cohort = list(zip(events, arms))
        cohort.sort(key=lambda pair: (pair[0].observed_at, pair[0].event_id))
        return run(RunConfig(label=params.label), quiet=True,
                   cohort=cohort, latents=latents)[0]


def main() -> None:
    rows = []
    for params in SWEEP:
        rows.append((params, run_one(params)))
        print(f"  ran {params.label}")

    lifts = [c.lift_pp for _, c in rows]
    nets = [c.net_incremental_paise for _, c in rows]

    print(f"\n{'=' * 94}\n  RECURA - Tier 3 sensitivity sweep\n{'=' * 94}")
    print(f"{'parameterisation':<36}{'holdout':>10}{'lift pp':>10}{'95% CI':>18}"
          f"{'net incremental':>20}")
    print("-" * 94)
    for params, c in rows:
        ci = f"[{c.lift_ci_low_pp:+.1f}, {c.lift_ci_high_pp:+.1f}]"
        print(f"{params.label:<36}{c.holdout.recovery_rate:>9.1%}{c.lift_pp:>+10.2f}"
              f"{ci:>18}{rupees(c.net_incremental_paise):>20}")
    print("-" * 94)
    print(f"{'ENVELOPE (lift)':<36}{f'{min(lifts):+.2f} to {max(lifts):+.2f} pp':>58}")
    print(f"{'ENVELOPE (net incremental)':<36}"
          f"{rupees(min(nets)) + '  to  ' + rupees(max(nets)):>58}")
    worst = min(rows, key=lambda r: r[1].lift_ci_low_pp)[1]
    verdict = ("POSITIVE AND SIGNIFICANT IN EVERY PARAMETERISATION"
               if worst.lift_ci_low_pp > 0 else
               f"NOT significant under '{min(rows, key=lambda r: r[1].lift_ci_low_pp)[0].label}'")
    print(f"{'WORST-CASE 95% LOWER BOUND':<36}{f'{worst.lift_ci_low_pp:+.2f} pp':>58}")
    print(f"{'':<36}{verdict:>58}")
    print("=" * 94)
    print("\nwhy each parameterisation exists:")
    for params, _ in rows:
        print(f"  {params.label:<36} {params.rationale}")
    print("\nNOTE: the sweep runs on the deterministic rules path. Fixture keys are tied to")
    print("event content, so a re-parameterised cohort misses the committed cache. This")
    print("measures how sensitive the DECISION LAYER is to our generator assumptions; the")
    print("LLM's separate contribution is measured by ablation 4 in `make ablate`.")

    OUT_PATH.write_text(json.dumps(
        {p.label: {"rationale": p.rationale, "lift_pp": c.lift_pp,
                   "ci": [c.lift_ci_low_pp, c.lift_ci_high_pp],
                   "net_incremental_paise": c.net_incremental_paise,
                   "holdout_recovery_rate": c.holdout.recovery_rate}
         for p, c in rows}, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

"""Tier 2: run the agent over the frozen cohort and report the headline (section 8).

This is the file that produces the number in the README. It must be byte-identical
across runs (section 8), which is why every source of randomness is seeded and every
LLM response comes from a committed fixture.

Note where the latents are used: HERE, in eval/, inside the `observe` callback. The
agent receives that callback and never sees what is behind it (section 9.1).

    make eval
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from eval.latents import LatentState, resolve
from eval.metrics import as_dict, compare
from src.act.provider import SimulatedProvider
from src.agent import Agent
from src.decide.bandit import PropensityModel
from src.decide.providers import NullProvider, resolve_provider
from src.ledger.store import Ledger
from src.models import ActionType, FailureClass, RiskEvent, rupees

ROOT = Path(__file__).resolve().parents[1]
COHORT_PATH = ROOT / "data" / "cohort.json"
LATENTS_PATH = ROOT / "data" / "latents.json"
RESULTS_PATH = ROOT / "data" / "results.json"
RUN_SEED = 20260826


def load_cohort() -> list[tuple[RiskEvent, str]]:
    if not COHORT_PATH.exists():
        sys.exit("cohort not found - run `make seed` first")
    rows = json.loads(COHORT_PATH.read_text(encoding="utf-8"))
    events = [(RiskEvent(**{k: v for k, v in r.items() if k != "arm"}), r["arm"])
              for r in rows]
    # Chronological, not file order. A real system processes events as they arrive; this
    # is also what makes learning causally honest (an outcome can only inform decisions
    # that come after it) and what lets a per-day merchant budget bind at all.
    events.sort(key=lambda pair: (pair[0].observed_at, pair[0].event_id))
    return events


def load_latents() -> dict[str, LatentState]:
    raw = json.loads(LATENTS_PATH.read_text(encoding="utf-8"))
    out = {}
    for key, value in raw.items():
        value = dict(value)
        value["true_failure_class"] = FailureClass(value["true_failure_class"])
        value["draws"] = tuple(value["draws"])
        out[key] = LatentState(**value)
    return out


def make_observe(latent: LatentState):
    """The ONLY bridge between the agent and hidden truth. Injected, never imported."""

    def observe(action: ActionType, at: datetime, hours_since_event: float,
                prior_contacts: int, sequence: int) -> tuple[bool, bool]:
        outcome = resolve(latent, action, at, hours_since_event, prior_contacts, sequence)
        return outcome.recovered, outcome.opted_out

    return observe


@dataclass
class RunConfig:
    label: str = "full"
    use_llm: bool = True
    use_taxonomy: bool = True
    use_policy: bool = True
    explore: bool = True
    random_chooser: bool = False


def run(config: RunConfig, ledger_url: str | None = None, quiet: bool = False):
    cohort = load_cohort()
    latents = load_latents()

    provider = resolve_provider() if config.use_llm else NullProvider()
    ledger = Ledger(url=ledger_url) if ledger_url else None

    agent = Agent(
        model=PropensityModel(),
        llm_provider=provider,
        executor=SimulatedProvider(),
        ledger=ledger,
        rng=np.random.default_rng(RUN_SEED),
        explore=config.explore,
        use_llm=config.use_llm,
        use_taxonomy=config.use_taxonomy,
        use_policy=config.use_policy,
        random_chooser=config.random_chooser,
        allow_network=False,   # eval NEVER calls out; fixtures only (section 8)
    )

    treatment, holdout = [], []
    for event, arm in cohort:
        latent = latents.get(event.event_id)
        if latent is None:
            continue
        result = agent.run_episode(event, arm, make_observe(latent))
        (treatment if arm == "treatment" else holdout).append(result)

    comparison = compare(treatment, holdout)
    if not quiet:
        report(config, comparison, agent)
    return comparison, agent


def report(config: RunConfig, c, agent: Agent) -> None:
    t, h = c.treatment, c.holdout
    print(f"\n{'=' * 72}\n  RECURA - Tier 2 batch  [{config.label}]\n{'=' * 72}")
    print(f"{'metric':<32}{'TREATMENT':>19}{'HOLDOUT':>19}")
    print("-" * 72)
    rows = [
        ("Events", f"{t.events:,}", f"{h.events:,}"),
        ("Recovery rate", f"{t.recovery_rate:.1%}", f"{h.recovery_rate:.1%}"),
        ("Recovered", rupees(t.recovered_paise), rupees(h.recovered_paise)),
        ("Intervention cost", rupees(t.cost_paise), rupees(h.cost_paise)),
        ("Contacts per customer", f"{t.contacts_per_customer:.2f}", f"{h.contacts_per_customer:.2f}"),
        ("Actions blocked by policy", f"{t.actions_blocked:,}", "-"),
        ("Escalated to human", f"{t.escalated:,}", "-"),
        ("Refused (EV < 0)", f"{t.refused_negative_ev:,}", "-"),
        ("Opted out", f"{t.opted_out:,}", f"{h.opted_out:,}"),
    ]
    for name, a, b in rows:
        print(f"{name:<32}{a:>19}{b:>19}")
    print("-" * 72)
    print(f"{'LIFT (percentage points)':<32}{c.lift_pp:>+18.2f}")
    print(f"{'  95% bootstrap CI':<32}{f'[{c.lift_ci_low_pp:+.2f}, {c.lift_ci_high_pp:+.2f}]':>19}")
    print(f"{'  significant at 95%':<32}{('YES' if c.significant else 'NO'):>19}")
    print(f"{'INCREMENTAL RECOVERED':<32}{rupees(c.incremental_recovered_paise):>19}")
    print(f"{'NET INCREMENTAL':<32}{rupees(c.net_incremental_paise):>19}")
    print(f"{'Cost per extra recovery':<32}{rupees(c.cost_per_recovery_paise):>19}")
    print("=" * 72)
    print(f"bandit cells learned: {agent.model.cells_learned}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default=None, help="sqlite URL to write the ledger to")
    ap.add_argument("--json", action="store_true", help="also write data/results.json")
    args = ap.parse_args()

    comparison, _ = run(RunConfig(), ledger_url=args.ledger)
    if args.json:
        RESULTS_PATH.write_text(json.dumps(as_dict(comparison), indent=2, sort_keys=True),
                                encoding="utf-8")
        print(f"wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()

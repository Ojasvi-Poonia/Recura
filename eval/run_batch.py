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
from eval.metrics import as_dict, compare, compare_segments, stop_reasons
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
    policy: dict | None = None   # None = policy.yaml as committed
    use_llm: bool = True
    use_taxonomy: bool = True
    use_policy: bool = True
    explore: bool = True
    random_chooser: bool = False


def run(config: RunConfig, ledger_url: str | None = None, quiet: bool = False,
        cohort=None, latents=None, live=None, collect: dict | None = None):
    """Run one configuration.

    `cohort`/`latents` may be supplied in memory. The sensitivity sweep uses that to
    vary generator parameters without ever overwriting the frozen files on disk.

    `collect`, if given, is filled with the per-surface breakdown and stop-reason census.
    It is an out-parameter rather than a third return value so that every existing caller
    (`ablate`, `sweep`, `replay`, `validate`) keeps unpacking two values unchanged.
    """
    if cohort is None:
        cohort = load_cohort()
    if latents is None:
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
        policy=config.policy,
        observer=live,
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

    # `live` need only be callable; a footer is optional.
    if live is not None and hasattr(live, "footer"):
        live.footer()
    comparison = compare(treatment, holdout)

    # The problem statement names three surfaces by name, so segment by the one field
    # that distinguishes them. Built here, in eval/, from the cohort the agent already saw.
    surface = {event.event_id: event.source_type for event, _ in cohort}
    segments = compare_segments(treatment, holdout, lambda eid: surface.get(eid, "unknown"))
    stops = stop_reasons(treatment)

    if collect is not None:
        collect["segments"] = {k: as_dict(v) for k, v in segments.items()}
        collect["stop_reasons"] = stops

    if not quiet:
        report(config, comparison, agent, segments, stops)
    return comparison, agent


# Why a rule does not fire in the baseline run. "Never fired" is not one fact but three,
# and collapsing them reads as "half the contract is dead" when most of it is a backstop
# doing exactly what a backstop should. Every rule here has a test proving it blocks when
# its condition holds - see tests/test_policy.py::test_rule_blocks.
WHY_QUIET = {
    "contact.require_consent":
        "backstop - candidate_actions only proposes consented channels",
    "contact.require_registered_template":
        "backstop - can_render() filters unwritable channels out of the action space",
    "retry.forbidden_for_recoverability":
        "backstop - a merchant-config event only ever gets ESCALATE_HUMAN proposed",
    "merchant.daily_action_budget":
        "headroom - 2,500/day vs a peak of 638; binds in `make replay` at 200/day",
    "merchant.daily_spend_cap_paise":
        "headroom - Rs 25k/day vs a peak of Rs 11.5k; binds in `make replay` at Rs 5k",
    "episode.stop_on_late_authorisation":
        "arrives by webhook, not by cohort replay - see tests/test_ingest.py",
    "episode.stop_on_dispute":
        "out of scope - disputes cannot be created in Razorpay test mode (section 7)",
}

# How Razorpay's four event sources map to the three surfaces the problem statement names.
SURFACE_LABEL = {
    "payment": "payment failure",
    "checkout": "checkout abandonment",
    "invoice": "overdue receivable",
    "mandate": "mandate / subscription",
}


def report(config: RunConfig, c, agent: Agent,
           segments: dict | None = None, stops: dict | None = None) -> None:
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
        ("Messages actually sent", f"{t.messages_sent:,}", f"{h.messages_sent:,}"),
        ("  unwritable (no template)", f"{t.template_failures:,}", "-"),
        ("Promises to pay broken", f"{t.broken_promises:,}", "-"),
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
    cpr = rupees(c.cost_per_recovery_paise) if c.cost_per_recovery_paise is not None \
        else 'n/a (no extra recoveries)'
    print(f"{'Cost per extra recovery':<32}{cpr:>19}")
    print(f"{'RETURN ON SPEND':<32}{f'{c.roi:.1f}x':>19}")
    print("=" * 72)
    print(f"bandit cells learned: {agent.model.cells_learned}")

    if segments:
        print("\n  BY SURFACE - the problem statement names these separately, so we report")
        print("  them separately. Each is compared against its own randomised holdout.")
        print(f"  {'surface':<24}{'N':>7}{'lift':>9}{'95% CI':>18}{'net':>16}")
        print("  " + "-" * 72)
        for key, seg in sorted(segments.items(),
                               key=lambda kv: -kv[1].treatment.events):
            label = SURFACE_LABEL.get(key, key)
            ci = f"[{seg.lift_ci_low_pp:+.2f}, {seg.lift_ci_high_pp:+.2f}]"
            mark = " " if seg.significant else "*"
            print(f"  {label:<24}{seg.treatment.events:>7,}{seg.lift_pp:>+8.2f}{mark}"
                  f"{ci:>17}{rupees(seg.net_incremental_paise):>16}")
        print("  " + "-" * 72)
        # Only explain the marker if a row actually carries one. Printing the legend
        # unconditionally sends a reader hunting for an asterisk that is not there.
        if any(not seg.significant for seg in segments.values()):
            print("  * interval includes zero - not significant at 95% on this surface alone")

    from src.policy.engine import rule_ids
    fired = agent.rule_blocks
    if fired is not None:
        print("\n  POLICY RULES - how often each clause bound (blocked or shifted) an action:")
        for rule_id in sorted(rule_ids(), key=lambda r: (-fired.get(r, 0), r)):
            n = fired.get(rule_id, 0)
            if n:
                print(f"   {rule_id:<44}{n:>8,}")

        quiet = [r for r in rule_ids() if not fired.get(r)]
        if quiet:
            print(f"\n  Not triggered by this cohort ({len(quiet)} of {len(rule_ids())}). "
                  "Every one has a test proving it blocks;")
            print("  these are the reasons the baseline run does not reach them:")
            for rule_id in sorted(quiet):
                why = WHY_QUIET.get(rule_id, "UNCATEGORISED - investigate")
                print(f"    {rule_id:<44}{why}")

    if stops:
        total = sum(stops.values())
        print("\n  STOPPING RULES - why each treated episode ended:")
        for name, n in stops.items():
            bar = "#" * int(n / max(1, total) * 40)
            print(f"  {name:<24}{n:>7,}  {n / total:>6.1%}  {bar}")
    trust = agent.model.source_snapshot()
    if trust:
        print("\nhow much the agent LEARNED to trust its diagnosis model:")
        for name, p in sorted(trust.items(), key=lambda kv: -kv[1]["mean"]):
            bar = "#" * int(p["mean"] * 40)
            print(f"  {name:<10} recovery rate {p['mean']:>6.1%}  n={p['n']:>6,}  {bar}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default=None, help="sqlite URL to write the ledger to")
    ap.add_argument("--json", action="store_true", help="also write data/results.json")
    ap.add_argument("--live", action="store_true", help="stream every decision as it is made")
    ap.add_argument("--pace", type=float, default=0.0, help="seconds between lines")
    ap.add_argument("--limit", type=int, default=None, help="stop streaming after N lines")
    args = ap.parse_args()

    stream = None
    if args.live:
        from eval.live import LiveStream
        from src.market import get_market
        stream = LiveStream(market=get_market(), pace=args.pace, limit=args.limit)
        stream.header()

    extras: dict = {}
    comparison, _ = run(RunConfig(), ledger_url=args.ledger, live=stream, collect=extras)
    if args.json:
        RESULTS_PATH.write_text(
            json.dumps({**as_dict(comparison), **extras}, indent=2, sort_keys=True),
            encoding="utf-8")
        print(f"wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()

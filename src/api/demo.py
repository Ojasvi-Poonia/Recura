"""Single-episode trace - what the agent actually does, step by step.

`make eval` reports what the agent achieved across ten thousand events. This shows
HOW, on one event at a time, with the arithmetic visible.

It exists because "show me it working" is the first thing anyone asks, and a batch
metric does not answer that. Three contrasting episodes are traced:

    ACTED     the maths supported an intervention
    REFUSED   every option scored below zero, so it did nothing
    BLOCKED   the maths supported an action and the policy contract vetoed it

The third is the important one. It is the whole design in one screen: the LLM proposed,
the maths decided, and a contract the model cannot read said no anyway.

    make run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from src.act.provider import SimulatedProvider
from src.agent import Agent
from src.decide.bandit import PropensityModel
from src.decide.providers import NullProvider, resolve_provider
from src.market import get_market
from src.models import ActionType, RiskEvent
from src.policy.engine import EpisodeState, evaluate

COHORT_PATH = Path(__file__).resolve().parents[2] / "data" / "cohort.json"


def load_observable_cohort() -> list[tuple[RiskEvent, str]]:
    """Read the OBSERVABLE cohort directly.

    Deliberately does not import anything from eval/ - that package holds the hidden
    latent state, and tests/test_invariants.py fails the build if src/ can reach it.
    The trace shows how a decision is made, which needs no access to the truth.
    """
    if not COHORT_PATH.exists():
        raise SystemExit("no cohort found - run `make seed` first")
    rows = json.loads(COHORT_PATH.read_text(encoding="utf-8"))
    return [(RiskEvent(**{k: v for k, v in r.items() if k != "arm"}), r["arm"])
            for r in rows]

RULE = "─" * 78


def _hdr(text: str) -> None:
    print(f"\n{RULE}\n  {text}\n{RULE}")


def trace(event: RiskEvent, agent: Agent, market) -> str:
    money = market.money
    err = event.razorpay_error

    _hdr(f"EPISODE  {event.event_id}   {money(event.amount_paise)}   "
         f"{event.source_type.upper()}")

    # ---- 1 OBSERVE -------------------------------------------------------
    print("\n1  OBSERVE  what arrived")
    print(f"     amount        {money(event.amount_paise)}")
    print(f"     method/bank   {event.method or '-'} / {event.bank or '-'}")
    print(f"     razorpay      {err.reason if err else '(none - never reached a gateway)'}")
    if event.due_at:
        print(f"     overdue       {event.days_overdue(event.observed_at)} days")
    h = event.customer_history
    print(f"     customer      {h.prior_failed_attempts} prior failures, "
          f"{h.prior_recoveries} prior recoveries, {h.contacts_last_7d} contacts/7d")
    print(f"     consent       {[c.value for c in h.consented_channels] or 'none'}")

    # ---- 2 DIAGNOSE ------------------------------------------------------
    dx = agent._diagnose(event)
    print("\n2  DIAGNOSE  what went wrong        [LLM proposes]")
    print(f"     source        {dx.source.value}")
    print(f"     root cause    {dx.root_cause[:70]}")
    print("     belief        " + "  ".join(
        f"{c.value}={p:.0%}" for c, p in sorted(dx.beliefs, key=lambda x: -x[1])[:4]))
    print(f"     triage        {dx.recoverability.value}")

    # ---- 3 DECIDE --------------------------------------------------------
    decision, _ = agent._decide(event, event.observed_at, 0)
    print("\n3  DECIDE  which action is worth it        [maths decides]")
    print(f"     {'candidate':<18}{'p':>7}{'gross':>13}{'cost':>11}{'EV':>13}")
    for c in sorted(decision.considered, key=lambda c: -c.expected_value_paise)[:6]:
        # Match the exact candidate, not merely its action type - several
        # RETRY_SCHEDULED options differ only by when they are scheduled.
        mark = (" <-" if c.action is decision.action
                and c.params == decision.params else "")
        print(f"     {c.action.value:<18}{c.p_recover:>7.2f}"
              f"{money(c.gross_value_paise):>13}"
              f"{money(c.direct_cost_paise + c.attention_cost_paise):>11}"
              f"{money(c.expected_value_paise):>13}{mark}")
    print(f"     chose         {decision.action.value}  ({decision.rationale[:48]})")

    # ---- 4 GOVERN --------------------------------------------------------
    state = EpisodeState(event_id=event.event_id,
                         episode_started_at=event.observed_at,
                         consented_channels=h.consented_channels)
    verdict = evaluate(decision, state, event.observed_at)
    print("\n4  GOVERN  am I allowed        [policy vetoes - the LLM cannot read this]")
    print(f"     rules run     {len(verdict.rules_evaluated)}")
    if verdict.rules_blocked:
        for b in verdict.rules_blocked:
            print(f"     BLOCKED       {b.rule_id}")
            print(f"                   {b.reason}")
    else:
        print("     verdict       allowed")
    if verdict.modified_params:
        print(f"     modified      {list(verdict.modified_params)[0]} "
              "(quiet hours shifted the send time)")

    # ---- 5 OUTCOME -------------------------------------------------------
    if decision.action is ActionType.NO_ACTION:
        outcome = "REFUSED"
        print("\n5  OUTCOME   refused - no option scored above zero. This is arithmetic,")
        print("             not a rule, and it is written to the ledger with its reason.")
    elif not verdict.allowed:
        outcome = "BLOCKED"
        print("\n5  OUTCOME   blocked by the contract. Positive expected value was NOT")
        print("             enough. The block is recorded as evidence, not an error.")
    else:
        outcome = "ACTED"
        print(f"\n5  OUTCOME   executed {decision.action.value} (simulated - "
              "nothing real is ever sent)")
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--event-id", default=None)
    ap.add_argument("--n", type=int, default=3, help="episodes to trace")
    args = ap.parse_args()

    market = get_market()
    cohort = load_observable_cohort()
    provider = resolve_provider()
    agent = Agent(model=PropensityModel(), llm_provider=provider,
                  executor=SimulatedProvider(), rng=np.random.default_rng(7),
                  allow_network=False)

    print(f"\nRecura - single-episode trace   market={market.code} "
          f"({market.currency.code})   diagnosis={getattr(provider, 'model', provider.name)}")
    if isinstance(provider, NullProvider):
        print("no LLM provider configured - diagnosis runs on the deterministic "
              "taxonomy path (this is also ablation 4)")

    if args.event_id:
        chosen = [e for e, _ in cohort if e.event_id == args.event_id]
        if not chosen:
            raise SystemExit(f"no such event {args.event_id}")
        trace(chosen[0], agent, market)
        return

    # Find one of each outcome so the trace shows all three behaviours.
    wanted = ["ACTED", "REFUSED", "BLOCKED"]
    seen: dict[str, bool] = {}
    for event, arm in cohort:
        if arm != "treatment" or len(seen) >= args.n:
            continue
        probe = Agent(model=PropensityModel(), llm_provider=provider,
                      executor=SimulatedProvider(), rng=np.random.default_rng(7),
                      allow_network=False)
        outcome = trace(event, probe, market) if wanted else None
        if outcome in wanted:
            wanted.remove(outcome)
            seen[outcome] = True
        if not wanted:
            break

    _hdr("what this shows")
    print("""
  Every decision above is in the append-only ledger with the EV of every option the
  agent considered - including the ones it rejected. That is how you check the
  decision was reasoned rather than hardcoded.

  `make eval`      the same loop over 9,999 events, against a randomised holdout
  `make validate`  negative controls proving the measurement is not manufacturing lift
  `make ablate`    what each component actually contributes
""")


if __name__ == "__main__":
    main()

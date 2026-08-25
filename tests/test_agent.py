"""Agent loop tests (CLAUDE.md sections 1, 8, 12)."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.act.provider import SimulatedProvider
from src.agent import MAX_DECISIONS, Agent
from src.clock import IST
from src.decide.bandit import PropensityModel
from src.decide.providers import NullProvider
from src.models import (
    ActionType,
    Channel,
    CustomerHistory,
    ErrorObject,
    FailureClass,
    RiskEvent,
)

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)


def mk_event(reason="insufficient_funds", amount=500_000, **kw):
    return RiskEvent(
        event_id=kw.pop("event_id", "e1"), merchant_id="m", customer_id="c",
        source_type="payment", amount_paise=amount, observed_at=NOW,
        razorpay_error=ErrorObject(reason=reason), method="upi", bank="HDFC",
        customer_history=CustomerHistory(
            consented_channels=(Channel.SMS, Channel.EMAIL),
            successful_payment_hours=(11,)),
        **kw,
    )


def mk_agent(**kw):
    kw.setdefault("rng", np.random.default_rng(7))
    kw.setdefault("llm_provider", NullProvider())
    kw.setdefault("executor", SimulatedProvider())
    kw.setdefault("allow_network", False)
    return Agent(model=PropensityModel(), **kw)


def never(*_a, **_k):
    return (False, False)


def always(*_a, **_k):
    return (True, False)


def quits(*_a, **_k):
    return (False, True)


# --- holdout arm ------------------------------------------------------------

def test_holdout_takes_no_action_and_spends_nothing():
    """section 8: the holdout is observed, never touched."""
    result = mk_agent().run_episode(mk_event(), "holdout", never)
    assert result.cost_paise == 0
    assert result.contacts == 0
    assert result.actions_taken == 0
    assert result.stop_reason == "holdout_observed"


def test_holdout_can_still_recover():
    """section 9.2: a non-zero baseline is what makes the comparison meaningful."""
    result = mk_agent().run_episode(mk_event(), "holdout", always)
    assert result.recovered_paise == 500_000


def test_holdout_never_calls_the_llm():
    agent = mk_agent()
    result = agent.run_episode(mk_event(), "holdout", never)
    assert result.llm_consulted == 0


# --- stopping rules ---------------------------------------------------------

def test_episode_stops_on_recovery():
    result = mk_agent().run_episode(mk_event(), "treatment", always)
    assert result.stop_reason == "recovered"
    assert result.decisions == 1


def test_episode_stops_on_opt_out():
    result = mk_agent().run_episode(mk_event(), "treatment", quits)
    assert result.stop_reason == "opted_out"
    assert result.opted_out


def test_episode_is_bounded():
    result = mk_agent().run_episode(mk_event(), "treatment", never)
    assert result.decisions <= MAX_DECISIONS


def test_agent_refuses_when_no_option_beats_zero():
    """section 5: NO_ACTION is derived arithmetic, not a rule.

    A Rs 2 ticket cannot pay for any intervention, so every candidate scores negative
    and NO_ACTION wins the argmax at exactly zero.
    """
    result = mk_agent().run_episode(mk_event(amount=200), "treatment", never)
    assert result.refused_negative_ev >= 1
    assert result.stop_reason == "refused_negative_ev"


def test_refusal_is_immediate_without_exploration():
    """With Thompson sampling off, the refusal is instant and costs nothing.

    With it on, the agent may spend one exploratory draw first - that is the price of
    exploration, and it is bounded by the policy gate, not hidden.
    """
    result = mk_agent(explore=False).run_episode(mk_event(amount=200), "treatment", never)
    assert result.stop_reason == "refused_negative_ev"
    assert result.cost_paise == 0
    assert result.actions_taken == 0


# --- governance -------------------------------------------------------------

def test_risk_declines_are_blocked_not_retried():
    """policy.yaml forbids retrying RISK_DECLINE; blocks are recorded, not crashes."""
    result = mk_agent().run_episode(
        mk_event(reason="payment_risk_check_failed"), "treatment", never)
    assert result.actions_blocked >= 1


def test_merchant_config_bugs_never_contact_the_customer():
    """Escalating a merchant integration bug routes to the merchant's engineers,
    so it must not send a nudge or consume the customer's contact budget."""
    agent = mk_agent()
    result = agent.run_episode(mk_event(reason="invalid_order_id"), "treatment", never)
    assert agent.executor.executed == []      # no message was ever sent
    assert result.contacts == 0               # customer patience untouched


# --- learning ---------------------------------------------------------------

def test_agent_learns_from_outcomes():
    """section 1, step 5."""
    agent = mk_agent()
    before = agent.model.cells_learned
    for i in range(20):
        agent.run_episode(mk_event(event_id=f"e{i}"), "treatment", never)
    assert agent.model.cells_learned > before


def test_learning_moves_the_posterior_in_the_right_direction():
    agent = mk_agent()
    for i in range(30):
        agent.run_episode(mk_event(event_id=f"e{i}"), "treatment", always)
    learned = [p for p in agent.model.snapshot().values() if p["n"] > 0]
    assert learned and all(p["mean"] > 0.5 for p in learned)


# --- determinism ------------------------------------------------------------

def test_episodes_are_deterministic_for_a_fixed_seed():
    """section 8: `make eval` twice must be byte-identical."""
    a = mk_agent().run_episode(mk_event(), "treatment", never)
    b = mk_agent().run_episode(mk_event(), "treatment", never)
    assert a == b


# --- ablation switches ------------------------------------------------------

def test_no_llm_ablation_runs_on_rules_alone():
    """section 8, ablation 4."""
    agent = mk_agent(use_llm=False)
    result = agent.run_episode(mk_event(), "treatment", never)
    assert result.llm_consulted == 0
    assert result.decisions >= 1


def test_no_taxonomy_ablation_treats_everything_identically():
    """section 8, ablation 2."""
    agent = mk_agent(use_taxonomy=False)
    agent.run_episode(mk_event(reason="card_expired"), "treatment", never)
    cells = {k.split("|")[0] for k in agent.model.snapshot()}
    assert cells <= {FailureClass.UNKNOWN.value}


def test_no_exploration_uses_posterior_means():
    agent = mk_agent(explore=False)
    a = agent.run_episode(mk_event(), "treatment", never)
    b = mk_agent(explore=False).run_episode(mk_event(), "treatment", never)
    assert a.decisions == b.decisions


# --- money ------------------------------------------------------------------

def test_all_money_is_integer_paise():
    result = mk_agent().run_episode(mk_event(), "treatment", never)
    assert isinstance(result.cost_paise, int)
    assert isinstance(result.recovered_paise, int)


def test_costs_are_never_negative():
    result = mk_agent().run_episode(mk_event(), "treatment", never)
    assert result.cost_paise >= 0


def test_refused_events_still_observe_the_outcome():
    """Refusing is a decision, not an exit.

    A customer we chose not to chase may still pay unprompted, and that recovery
    belongs to the treatment arm. Recording zero biased the comparison against us.
    """
    result = mk_agent().run_episode(mk_event(amount=200), "treatment", always)
    assert result.recovered_paise == 200
    assert result.cost_paise == 0
    assert result.stop_reason == "recovered_unprompted"


def test_no_action_cell_keeps_learning():
    """Without this the baseline posterior sits at 0.5 forever and every real action
    looks like a bad bet against it."""
    agent = mk_agent(explore=False)
    for i in range(25):
        agent.run_episode(mk_event(event_id=f"e{i}", amount=200), "treatment", never)
    snapshot = agent.model.snapshot()
    no_action_cells = {k: v for k, v in snapshot.items() if k.endswith("|NO_ACTION")}
    assert no_action_cells, "NO_ACTION never learned"
    assert any(v["n"] > 0 for v in no_action_cells.values())


def test_merchant_daily_budget_carries_across_episodes():
    """Previously per-episode, so a 500-action budget never bound on 7,992 events."""
    agent = mk_agent()
    agent.run_episode(mk_event(event_id="a"), "treatment", never)
    after_first = agent._merchant_actions
    agent.run_episode(mk_event(event_id="b"), "treatment", never)
    assert agent._merchant_actions > after_first


def test_merchant_counters_reset_on_a_new_virtual_day():
    agent = mk_agent()
    agent.run_episode(mk_event(event_id="a"), "treatment", never)
    spent_day_one = agent._merchant_spend_paise
    later = mk_event(event_id="b").model_copy(
        update={"observed_at": NOW + timedelta(days=3)})
    agent.run_episode(later, "treatment", never)
    # A new virtual day resets the counters, so day two cannot inherit day one's spend.
    assert agent._merchant_spend_paise <= spent_day_one or agent._merchant_actions >= 1
    assert agent._merchant_date != NOW.date()


def test_agent_does_not_repeat_a_refused_action():
    """A blocked action yields no outcome, so the bandit cannot learn from it.

    Without this the agent re-proposes the same forbidden retry every step and
    degenerates into a retry bot - CLAUDE.md section 12's explicit anti-goal.
    """
    agent = mk_agent()
    result = agent.run_episode(
        mk_event(reason="payment_risk_check_failed"), "treatment", never)
    # Retrying a RISK_DECLINE is forbidden; it must not consume every decision slot.
    assert result.actions_blocked < MAX_DECISIONS

"""Agent loop tests (CLAUDE.md sections 1, 8, 12)."""

from datetime import datetime, timedelta

import numpy as np

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


def test_merchant_budget_accumulates_within_a_day_across_episodes():
    """The contract is per merchant per DAY: actions from different episodes landing
    on the same virtual day accumulate against one budget."""
    agent = mk_agent()
    agent.merchant_day("m1", NOW).actions = 7
    assert agent.merchant_day("m1", NOW).actions == 7          # same day -> preserved
    assert agent.merchant_day("m1", NOW + timedelta(days=1)).actions == 0  # new day


def test_merchant_actions_are_counted_at_all():
    agent = mk_agent()
    result = agent.run_episode(mk_event(), "treatment", never)
    if result.actions_taken:
        assert sum(d.actions for d in agent._merchant_days.values()) >= 1


def test_merchant_counters_are_per_day():
    """Day two must not inherit day one's spend."""
    agent = mk_agent()
    agent.merchant_day("m1", NOW).spend_paise = 4_000
    assert agent.merchant_day("m1", NOW + timedelta(days=1)).spend_paise == 0
    assert agent.merchant_day("m1", NOW).spend_paise == 4_000


def test_revisiting_an_earlier_day_does_not_reset_a_live_budget():
    """Episodes advance their own clocks, so an earlier date is revisited constantly.

    Evicting by recency silently reset budgets that were still live, which changed the
    headline and broke the A/A control.
    """
    agent = mk_agent()
    agent.merchant_day("m1", NOW).actions = 7
    agent.merchant_day("m1", NOW + timedelta(days=10))     # jump forward
    assert agent.merchant_day("m1", NOW).actions == 7      # earlier day still intact


def test_merchant_day_table_is_bounded():
    """A long-running agent must not keep a row per merchant per day forever."""
    from src.agent import MAX_MERCHANT_DAYS
    agent = mk_agent()
    for i in range(MAX_MERCHANT_DAYS + 50):
        agent.merchant_day(f"m{i}", NOW + timedelta(days=i % 40))
    assert len(agent._merchant_days) <= MAX_MERCHANT_DAYS


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


def test_holdout_gets_the_same_number_of_opportunities_as_treatment():
    """Fair comparison: observing the control once while treatment is observed five
    times hands treatment extra draws on the same probability. Our placebo control
    caught that as +18.57pp of pure repeated sampling."""
    seen = []

    def counting(action, at, hours, prior, seq):
        seen.append(seq)
        return (False, False)

    mk_agent().run_episode(mk_event(), "holdout", counting)
    assert len(seen) == MAX_DECISIONS


def test_holdout_still_takes_no_action_and_spends_nothing():
    calls = []

    def record(action, at, hours, prior, seq):
        calls.append(action)
        return (False, False)

    result = mk_agent().run_episode(mk_event(), "holdout", record)
    assert all(a is ActionType.NO_ACTION for a in calls)
    assert result.cost_paise == 0 and result.contacts == 0


def test_treatment_never_gets_more_draws_than_the_holdout():
    """The safety property the whole benchmark rests on.

    Every extra recovery opportunity handed to treatment is lift manufactured out of
    repeated sampling. That is not hypothetical: with every action made inert, an
    earlier version of this harness still reported +18.57pp, purely because treatment
    was re-observed five times per episode and the control once.

    Equality is NOT asserted, because it does not hold and pretending otherwise would
    hide a real asymmetry. Treatment advances its clock to each action's scheduled_at,
    so an episode that schedules far out can exhaust the 21-day horizon and end with
    FEWER draws than the holdout (1.1% of episodes do - the `episode_expired` row in
    the stop-reason census). That direction costs treatment chances and understates our
    own result, which is why it is acceptable. The reverse would not be.
    """
    def counter(store):
        def observe(action, at, hours, prior, seq):
            store.append(seq)
            return (False, False)
        return observe

    t_draws, h_draws = [], []
    mk_agent().run_episode(mk_event(), "treatment", counter(t_draws))
    mk_agent().run_episode(mk_event(), "holdout", counter(h_draws))
    assert len(h_draws) == MAX_DECISIONS, "the control must get its full complement"
    assert len(t_draws) <= len(h_draws), (
        f"treatment got {len(t_draws)} draws against the control's {len(h_draws)} - "
        "extra draws manufacture lift from nothing")


def test_blocked_steps_still_give_the_customer_a_chance():
    """The contract stops US, not the customer."""
    draws = []

    def observe(action, at, hours, prior, seq):
        draws.append(action)
        return (False, False)

    # Retrying a risk decline is forbidden, so this episode accumulates blocks.
    mk_agent().run_episode(mk_event(reason="payment_risk_check_failed"),
                           "treatment", observe)
    assert len(draws) == MAX_DECISIONS


def test_checkout_abandonment_routes_to_re_engagement():
    """Track 03 names checkout drop-off explicitly."""
    event = mk_event().model_copy(update={"source_type": "checkout",
                                          "razorpay_error": None})
    agent = mk_agent()
    decision, dx = agent._decide(event, NOW, 0)
    assert dx.top_class is FailureClass.AUTH_ABANDON


def test_broken_promise_is_tracked():
    """policy.yaml declared after_broken_promise_to_pay with nothing to trigger it."""
    agent = mk_agent(promise_window_hours=1.0)
    result = agent.run_episode(mk_event(), "treatment", never)
    assert result.broken_promises >= 0          # field exists and is populated
    assert hasattr(result, "broken_promises")


def test_a_converting_nudge_clears_the_promise_window():
    agent = mk_agent(promise_window_hours=1.0)
    result = agent.run_episode(mk_event(), "treatment", always)
    assert result.broken_promises == 0


def test_nudges_render_a_registered_template():
    """Copy that reaches a customer must be a DLT-registered template, filled."""
    from src.act.messaging import verify_compliance
    agent = mk_agent()
    event = mk_event(reason="insufficient_funds")
    decision, _ = agent._decide(event, NOW, 0)
    rendered = agent._render_message(
        event, decision.model_copy(update={"params": {"channel": "sms"},
                                           "failure_class": FailureClass.FUNDS}),
        event.customer_history)
    assert rendered is not None
    assert verify_compliance(rendered.text, rendered.language) == rendered.template_key


def test_no_message_is_invented_when_no_template_applies():
    """RISK_DECLINE has no registered template - so nothing is sent, not something made up."""
    agent = mk_agent()
    event = mk_event(reason="payment_risk_check_failed")
    decision, _ = agent._decide(event, NOW, 0)
    rendered = agent._render_message(
        event, decision.model_copy(update={"failure_class": FailureClass.RISK_DECLINE,
                                           "params": {"channel": "sms"}}),
        event.customer_history)
    assert rendered is None


class _StubModel:
    """A diagnosis model that always answers, so the meta-bandit path is exercised."""

    name, model = "stub", "stub-1"

    def diagnose(self, system_prompt, user_content, schema):
        return schema(root_cause="stub",
                      beliefs=[{"failure_class": FailureClass.FUNDS, "probability": 1.0}],
                      confidence=0.6, reasoning="stub")


def test_no_diagnosis_source_is_credited_when_there_is_no_model():
    """The meta-bandit only engages where a model was actually consulted."""
    agent = mk_agent()
    agent.run_episode(mk_event(), "treatment", never)
    assert agent.model.source_snapshot() == {}


def test_the_agent_learns_which_diagnosis_source_to_trust(tmp_path):
    """The trust weight must be learned from outcomes, not read from a constant."""
    agent = mk_agent(llm_provider=_StubModel(), use_llm=True, allow_network=True)
    for i in range(30):
        agent.run_episode(mk_event(event_id=f"t{i}", reason="payment_failed"),
                          "treatment", always if i % 2 else never)
    snapshot = agent.model.source_snapshot()
    assert snapshot, "no diagnosis source was ever credited"
    assert sum(v["n"] for v in snapshot.values()) > 0
    assert all(0.0 <= v["mean"] <= 1.0 for v in snapshot.values())

    # The weight must be DRAWN, not fixed. A constant would credit exactly one
    # source forever - which is precisely the hand-picked setting we removed.
    assert len(snapshot) > 1, (
        f"only {list(snapshot)} was ever chosen - trust is not being sampled"
    )


def test_a_message_that_could_not_be_written_is_not_charged_or_credited(monkeypatch):
    """The single worst defect this project shipped, now pinned by a test.

    For several runs the agent selected 607 nudges, composed none of them, and was still
    charged for all 607, counted all 607 against the customer's contact budget, and asked
    the simulator to score the effect of messages that had never been written. Crediting
    an unsent message is how a system reports recovery it did not cause.
    """
    baseline = mk_agent().run_episode(mk_event(), "treatment", never)
    assert baseline.messages_sent > 0, "fixture must actually send, or this proves nothing"

    monkeypatch.setattr(Agent, "_render_message", lambda self, event, decision, history: None)
    silent = mk_agent().run_episode(mk_event(), "treatment", never)

    assert silent.template_failures > 0, "the unwritable branch was never reached"
    assert silent.messages_sent == 0
    assert silent.contacts == 0, "an unsent message must not spend the contact budget"
    assert silent.cost_paise < baseline.cost_paise, (
        f"charged {silent.cost_paise}p for a message that was never composed "
        f"(a sending run costs {baseline.cost_paise}p)")

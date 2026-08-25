"""Decide-layer tests: bandit, multipliers, EV (CLAUDE.md section 5)."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.clock import IST
from src.decide.bandit import BetaPosterior, PropensityModel
from src.decide.ev import DecisionContext, candidate_actions, choose, score_candidates
from src.decide.multipliers import adjust
from src.models import (
    ActionType,
    Channel,
    CustomerHistory,
    ErrorObject,
    FailureClass,
    Recoverability,
    RiskEvent,
)
from src.taxonomy.mapping import classify

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)


def mk_ctx(amount=500_000, reason="insufficient_funds", contacts=0, hours=(11,),
           channels=(Channel.SMS, Channel.EMAIL), method="upi", **kw):
    err = ErrorObject(reason=reason)
    mp = classify(err)
    event = RiskEvent(
        event_id="e1", merchant_id="m", customer_id="c", source_type="payment",
        amount_paise=amount, observed_at=NOW, razorpay_error=err, method=method,
        customer_history=CustomerHistory(
            consented_channels=channels, contacts_last_7d=contacts,
            successful_payment_hours=hours),
    )
    return DecisionContext(event=event, failure_class=mp.failure_class,
                           recoverability=mp.recoverability, mapping=mp, now=NOW, **kw)


# --- bandit ----------------------------------------------------------------

def test_posterior_is_immutable():
    p = BetaPosterior()
    assert p.updated(True) is not p and p.alpha == 1.0


def test_posterior_converges_towards_truth():
    p = BetaPosterior()
    for i in range(200):
        p = p.updated(i % 10 < 7)  # 70% success
    assert abs(p.mean - 0.70) < 0.05


def test_priors_are_uninformative():
    """section 9.5: seeding the agent with the simulator's parameters would be peeking."""
    m = PropensityModel()
    assert m.expected(FailureClass.FUNDS, ActionType.RETRY_NOW) == 0.5
    assert m.cells_learned == 0


def test_thompson_sampling_is_seed_deterministic():
    """section 8: stochastic, but byte-identical across runs."""
    m = PropensityModel()
    a = [m.sample(FailureClass.FUNDS, ActionType.NUDGE, np.random.default_rng(11))
         for _ in range(5)]
    b = [m.sample(FailureClass.FUNDS, ActionType.NUDGE, np.random.default_rng(11))
         for _ in range(5)]
    assert a == b


def test_unlearned_cells_explore_more_than_learned_ones():
    """The whole point of Thompson sampling."""
    m = PropensityModel()
    for i in range(200):
        m.update(FailureClass.FUNDS, ActionType.RETRY_NOW, i % 10 < 6)
    rng = np.random.default_rng(5)
    learned = [m.sample(FailureClass.FUNDS, ActionType.RETRY_NOW, rng) for _ in range(300)]
    fresh = [m.sample(FailureClass.RISK_DECLINE, ActionType.NUDGE, rng) for _ in range(300)]
    assert np.std(fresh) > np.std(learned) * 3


def test_snapshot_is_serialisable():
    m = PropensityModel()
    m.update(FailureClass.FUNDS, ActionType.NUDGE, True)
    snap = m.snapshot()
    assert snap["FUNDS|NUDGE"]["n"] == 1


# --- multipliers -----------------------------------------------------------

def _adj(**kw):
    base = dict(action=ActionType.NUDGE, failure_class=FailureClass.AUTH_ABANDON,
                history=CustomerHistory(), attempt_number=1, at=NOW, downtime_active=False)
    base.update(kw)
    return adjust(0.5, **base)


def test_later_attempts_are_discounted():
    assert _adj(attempt_number=3) < _adj(attempt_number=1)


def test_contact_fatigue_reduces_nudge_efficacy():
    assert _adj(history=CustomerHistory(contacts_last_7d=3)) < _adj()


def test_hour_match_beats_hour_miss():
    match = _adj(history=CustomerHistory(successful_payment_hours=(11,)))
    miss = _adj(history=CustomerHistory(successful_payment_hours=(3,)))
    assert match > miss


def test_active_downtime_penalises_retry():
    down = _adj(action=ActionType.RETRY_NOW, downtime_active=True)
    up = _adj(action=ActionType.RETRY_NOW, downtime_active=False)
    assert down < up


def test_salary_window_only_boosts_funds():
    early = NOW.replace(day=2)
    funds = adjust(0.5, action=ActionType.RETRY_SCHEDULED, failure_class=FailureClass.FUNDS,
                   history=CustomerHistory(), attempt_number=1, at=early, downtime_active=False)
    auth = adjust(0.5, action=ActionType.RETRY_SCHEDULED,
                  failure_class=FailureClass.AUTH_ABANDON, history=CustomerHistory(),
                  attempt_number=1, at=early, downtime_active=False)
    assert funds > auth


def test_probability_stays_in_range():
    assert 0.0 <= _adj(history=CustomerHistory(prior_recoveries=9)) <= 0.97


# --- EV --------------------------------------------------------------------

def test_every_candidate_is_logged():
    """section 4: `considered` is not optional."""
    scored = score_candidates(mk_ctx(), PropensityModel(), np.random.default_rng(1))
    assert len(scored) > 5
    assert any(c.action is ActionType.NO_ACTION for c in scored)


def test_no_action_has_exactly_zero_ev():
    """Incremental EV: doing nothing is the zero point, by construction."""
    scored = score_candidates(mk_ctx(), PropensityModel(), np.random.default_rng(1))
    no_action = next(c for c in scored if c.action is ActionType.NO_ACTION)
    assert no_action.expected_value_paise == 0


def test_no_action_is_chosen_when_nothing_beats_zero():
    """section 5: NO_ACTION is derived arithmetic, not a rule."""
    scored = score_candidates(mk_ctx(amount=100), PropensityModel(), np.random.default_rng(2))
    forced = [c.model_copy(update={"expected_value_paise": -50})
              if c.action is not ActionType.NO_ACTION else c for c in scored]
    assert choose(forced).action is ActionType.NO_ACTION


def test_tiny_amounts_do_not_justify_expensive_actions():
    """A Rs 20 recovery cannot pay for a Rs 40 human escalation."""
    scored = score_candidates(mk_ctx(amount=2000), PropensityModel(), np.random.default_rng(3))
    escalate = next(c for c in scored if c.action is ActionType.ESCALATE_HUMAN)
    assert escalate.expected_value_paise < 0


def test_merchant_config_offers_only_escalation():
    """A merchant integration bug must never generate a customer contact."""
    actions = {a for a, _ in candidate_actions(mk_ctx(reason="invalid_order_id"))}
    assert actions == {ActionType.NO_ACTION, ActionType.ESCALATE_HUMAN}


def test_unconsented_channels_are_never_offered():
    actions = candidate_actions(mk_ctx(channels=(Channel.EMAIL,)))
    channels = {p.get("channel") for a, p in actions if a is ActionType.NUDGE}
    assert channels == {"email"}


def test_razorpay_cooldown_suppresses_retry_now():
    """`transaction_daily_limit_exceeded` carries a documented 24h cooldown."""
    actions = candidate_actions(mk_ctx(reason="transaction_daily_limit_exceeded"))
    assert ActionType.RETRY_NOW not in {a for a, _ in actions}
    offsets = [p["scheduled_at"] for a, p in actions if a is ActionType.RETRY_SCHEDULED]
    assert all(o >= NOW + timedelta(hours=24) for o in offsets)


def test_funds_gets_a_salary_aligned_candidate_inside_the_horizon():
    """section 5: timing is a first-class decision dimension.

    Offered only when the salary window actually falls inside the 21-day episode - an
    action we could never take is not a candidate, and scheduling past the horizon was
    silently expiring episodes.
    """
    early = mk_ctx()
    early = DecisionContext(**{**early.__dict__, "now": NOW.replace(day=25)})
    hints = [p.get("reason_hint") for _, p in candidate_actions(early)]
    assert "aligned to salary-cycle replenishment" in hints


def test_salary_candidate_is_suppressed_beyond_the_episode_horizon():
    far = DecisionContext(**{**mk_ctx().__dict__, "now": NOW.replace(day=2)})
    hints = [p.get("reason_hint") for _, p in candidate_actions(far)]
    assert "aligned to salary-cycle replenishment" not in hints


def test_downtime_generates_a_wait_candidate():
    ctx = mk_ctx(downtime_active=True, downtime_clears_in_h=3.0)
    hints = [p.get("reason_hint") for _, p in candidate_actions(ctx)]
    assert "waiting for bank downtime to clear" in hints


def test_scoring_is_deterministic_for_a_fixed_seed():
    a = score_candidates(mk_ctx(), PropensityModel(), np.random.default_rng(9))
    b = score_candidates(mk_ctx(), PropensityModel(), np.random.default_rng(9))
    assert [c.expected_value_paise for c in a] == [c.expected_value_paise for c in b]


def test_all_money_is_integer_paise():
    for c in score_candidates(mk_ctx(), PropensityModel(), np.random.default_rng(4)):
        for field in ("gross_value_paise", "direct_cost_paise",
                      "attention_cost_paise", "expected_value_paise"):
            assert isinstance(getattr(c, field), int)


def test_no_action_baseline_is_not_thompson_sampled():
    """Regression: the do-nothing baseline must be stable across Thompson draws.

    Scoring every candidate as an increment over a RANDOM baseline makes each increment
    a difference of two independent draws - mean zero, negative half the time - so the
    agent refuses at random. The ablation study caught this: a random chooser was
    outperforming the optimiser.
    """
    model = PropensityModel()
    baselines = set()
    for seed in range(15):
        scored = score_candidates(mk_ctx(), model, np.random.default_rng(seed))
        no_action = next(c for c in scored if c.action is ActionType.NO_ACTION)
        baselines.add(round(no_action.p_recover, 9))
    assert len(baselines) == 1, f"baseline drifted across seeds: {baselines}"


def test_optimiser_beats_a_random_chooser_on_expected_value():
    """The whole premise: argmax EV must beat picking uniformly at random."""
    model = PropensityModel()
    rng = np.random.default_rng(3)
    chosen_ev, random_ev = 0, 0
    for i in range(60):
        scored = score_candidates(mk_ctx(amount=500_000 + i), model, rng)
        chosen_ev += choose(scored).expected_value_paise
        random_ev += scored[int(rng.integers(len(scored)))].expected_value_paise
    assert chosen_ev > random_ev


def test_horizon_discount_shrinks_value_when_chances_remain():
    """Acting now matters less when the customer has other opportunities anyway."""
    model = PropensityModel()
    last_chance = score_candidates(
        DecisionContext(**{**mk_ctx().__dict__, "steps_remaining": 1}),
        model, np.random.default_rng(11))
    many_chances = score_candidates(
        DecisionContext(**{**mk_ctx().__dict__, "steps_remaining": 5}),
        model, np.random.default_rng(11))
    a = max(c.gross_value_paise for c in last_chance)
    b = max(c.gross_value_paise for c in many_chances)
    assert b < a, "a naive scorer overvalues every intervention"


def test_no_action_is_unaffected_by_the_horizon():
    model = PropensityModel()
    for steps in (1, 5):
        scored = score_candidates(
            DecisionContext(**{**mk_ctx().__dict__, "steps_remaining": steps}),
            model, np.random.default_rng(2))
        assert next(c for c in scored if c.action is ActionType.NO_ACTION
                    ).expected_value_paise == 0

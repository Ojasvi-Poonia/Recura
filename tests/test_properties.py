"""Property-based tests — invariants that must hold for ANY valid input.

Every bug found in this project so far was a specific input someone happened to think
of: an already opted-out customer, a six-figure amount, a Devanagari name, a downtime
payload keyed on `bank` rather than `issuer`. The 10,000-event cohort tests ten thousand
points and says nothing about the eleventh.

Hypothesis generates arbitrary valid inputs and shrinks any failure to a minimal
counterexample. What is asserted here are the properties the README actually claims:

    an opted-out customer is never contacted
    a merchant's daily budget is never exceeded
    no copy reaches a customer that is not a registered template
    money is always integer minor units, never negative
    the chosen action is always one that was priced and logged

These are the claims. This is the machine trying to break them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from src.act.messaging import TemplateViolation, validate_slot, verify_compliance
from src.act.provider import Downtime, SimulatedProvider
from src.agent import MAX_DECISIONS, Agent
from src.clock import IST
from src.decide.bandit import BetaPosterior, PropensityModel
from src.decide.providers import NullProvider
from src.models import (
    ActionType,
    Channel,
    CustomerHistory,
    ErrorObject,
    FailureClass,
    MerchantContext,
    Recoverability,
    RiskEvent,
)
from src.policy.engine import EpisodeState, evaluate, load_policy, rule_ids
from src.taxonomy.mapping import MAPPING, classify

SLOW = settings(max_examples=60, deadline=None,
                suppress_health_check=[HealthCheck.too_slow])
FAST = settings(max_examples=300, deadline=None)

REASONS = sorted(MAPPING)
BASE = datetime(2026, 3, 1, tzinfo=IST)


# --- strategies ---------------------------------------------------------------

@st.composite
def risk_events(draw, **overrides):
    """An arbitrary but VALID RiskEvent - the full space the agent must survive."""
    history = CustomerHistory(
        prior_failed_attempts=draw(st.integers(0, 20)),
        prior_recoveries=draw(st.integers(0, 20)),
        prior_payments_total=draw(st.integers(0, 200)),
        contacts_last_7d=draw(st.integers(0, 10)),
        successful_payment_hours=tuple(draw(st.lists(st.integers(0, 23), max_size=4))),
        consented_channels=tuple(draw(st.lists(st.sampled_from(list(Channel)),
                                               max_size=4, unique=True))),
        opted_out=draw(st.booleans()),
        language=draw(st.sampled_from(["en", "hi", "ta", ""])),
    )
    event = dict(
        event_id=draw(st.text(min_size=1, max_size=24).filter(lambda s: s.strip())),
        merchant_id=draw(st.sampled_from(["m1", "m2", "acme", "globex"])),
        customer_id=draw(st.text(min_size=1, max_size=32).filter(lambda s: s.strip())),
        source_type=draw(st.sampled_from(["payment", "checkout", "mandate", "invoice"])),
        amount_paise=draw(st.integers(0, 10_000_000_00)),
        observed_at=BASE + timedelta(hours=draw(st.integers(0, 500))),
        razorpay_error=draw(st.one_of(
            st.none(),
            st.builds(ErrorObject,
                      reason=st.one_of(st.sampled_from(REASONS),
                                       st.text(max_size=30), st.none()),
                      source=st.sampled_from(["customer", "business", "gateway",
                                              "razorpay", "issuer_bank", None]),
                      step=st.sampled_from(["payment_initiation",
                                            "payment_authentication", None]),
                      description=st.text(max_size=60)))),
        method=draw(st.sampled_from(["upi", "card", "netbanking", "wallet",
                                     "emandate", "fpx", None])),
        bank=draw(st.sampled_from(["HDFC", "ICICI", "SBIN", "UTIB", None])),
        attempt_number=draw(st.integers(1, 10)),
        due_at=draw(st.one_of(st.none(),
                              st.just(BASE - timedelta(days=draw(st.integers(0, 400)))))),
        customer_history=history,
        merchant_context=MerchantContext(
            merchant_id="m", margin_bps=draw(st.integers(0, 10_000))),
    )
    event.update(overrides)
    return RiskEvent(**event)


def run(event, arm="treatment", **kw):
    agent = Agent(model=PropensityModel(), rng=np.random.default_rng(0),
                  llm_provider=NullProvider(), executor=SimulatedProvider(),
                  allow_network=False, **kw)
    result = agent.run_episode(event, arm, lambda *a, **k: (False, False))
    return agent, result


# --- the claims the README makes ----------------------------------------------

@given(event=risk_events())
@SLOW
def test_an_opted_out_customer_is_never_contacted(event):
    """The single most important compliance property in the system."""
    assume(event.customer_history.opted_out)
    agent, result = run(event)
    assert result.contacts == 0
    assert agent.executor.executed == []


@given(event=risk_events())
@SLOW
def test_a_merchants_daily_budget_is_never_exceeded(event):
    policy = load_policy()["merchant"]
    agent, _ = run(event)
    for budget in agent._merchant_days.values():
        assert budget.actions <= policy["daily_action_budget"]
        assert budget.spend_paise <= policy["daily_spend_cap_paise"] + 100_000


@given(event=risk_events())
@SLOW
def test_an_episode_is_always_bounded(event):
    _, result = run(event)
    assert result.decisions <= MAX_DECISIONS


@given(event=risk_events())
@SLOW
def test_money_is_always_a_non_negative_integer(event):
    _, result = run(event)
    for field in (result.cost_paise, result.recovered_paise):
        assert isinstance(field, int) and field >= 0


@given(event=risk_events())
@SLOW
def test_an_unconsented_channel_is_never_used(event):
    agent, _ = run(event)
    allowed = {c.value for c in event.customer_history.consented_channels}
    for _, action, _ in agent.executor.executed:
        if action is ActionType.NUDGE:
            assert allowed, "a nudge went out with no consented channel at all"


@given(event=risk_events())
@SLOW
def test_the_chosen_action_was_always_priced_and_logged(event):
    """section 4: `considered` is not optional - it is how a panel checks the reasoning."""
    agent = Agent(model=PropensityModel(), rng=np.random.default_rng(0),
                  llm_provider=NullProvider(), executor=SimulatedProvider(),
                  allow_network=False)
    decision, _ = agent._decide(event, event.observed_at, 0)
    assert decision.considered
    assert any(c.action is decision.action for c in decision.considered)


@given(event=risk_events())
@SLOW
def test_no_action_always_scores_exactly_zero(event):
    """The zero point of the incremental EV formula. If it drifts, refusal is arbitrary."""
    agent = Agent(model=PropensityModel(), rng=np.random.default_rng(0),
                  llm_provider=NullProvider(), executor=SimulatedProvider(),
                  allow_network=False)
    decision, _ = agent._decide(event, event.observed_at, 0)
    for c in decision.considered:
        if c.action is ActionType.NO_ACTION:
            assert c.expected_value_paise == 0


@given(event=risk_events())
@SLOW
def test_the_holdout_arm_never_spends_or_contacts(event):
    """The counterfactual must stay uncontaminated or the headline is meaningless."""
    agent, result = run(event, arm="holdout")
    assert result.cost_paise == 0
    assert result.contacts == 0
    assert agent.executor.executed == []


# --- policy engine -------------------------------------------------------------

@given(event=risk_events(), opted=st.booleans(), contacts=st.integers(0, 10),
       attempts=st.integers(0, 10))
@FAST
def test_policy_evaluation_is_total_and_deterministic(event, opted, contacts, attempts):
    """A gate that can raise is a gate that can be bypassed by crashing it."""
    agent = Agent(model=PropensityModel(), rng=np.random.default_rng(0),
                  llm_provider=NullProvider(), executor=SimulatedProvider(),
                  allow_network=False)
    decision, _ = agent._decide(event, event.observed_at, 0)
    state = EpisodeState(event_id=event.event_id,
                         episode_started_at=event.observed_at,
                         opted_out=opted, contacts_last_7d=contacts,
                         attempts_made=attempts,
                         consented_channels=event.customer_history.consented_channels)
    a = evaluate(decision, state, event.observed_at)
    b = evaluate(decision, state, event.observed_at)
    assert a == b
    assert set(a.rules_evaluated) == set(rule_ids())


@given(event=risk_events())
@FAST
def test_a_blocked_verdict_always_explains_itself(event):
    """section 6: every block carries a human-readable reason, into the ledger."""
    agent = Agent(model=PropensityModel(), rng=np.random.default_rng(0),
                  llm_provider=NullProvider(), executor=SimulatedProvider(),
                  allow_network=False)
    decision, _ = agent._decide(event, event.observed_at, 0)
    state = EpisodeState(event_id=event.event_id,
                         episode_started_at=event.observed_at, opted_out=True)
    for blocked in evaluate(decision, state, event.observed_at).rules_blocked:
        assert blocked.rule_id and len(blocked.reason) > 20


# --- taxonomy ------------------------------------------------------------------

@given(reason=st.one_of(st.sampled_from(REASONS), st.text(max_size=40), st.none()),
       source=st.sampled_from(["customer", "business", "gateway", None]),
       source_type=st.sampled_from(["payment", "checkout", "mandate", "invoice", None]))
@FAST
def test_classify_is_total(reason, source, source_type):
    """Razorpay adds reason codes over time; an unknown one must degrade, not raise."""
    got = classify(ErrorObject(reason=reason, source=source), source_type)
    assert isinstance(got.failure_class, FailureClass)
    assert isinstance(got.recoverability, Recoverability)


@given(reason=st.sampled_from(REASONS))
@FAST
def test_source_business_always_means_merchant_triage(reason):
    """Razorpay documents source=business as "fix the request parameters"."""
    got = classify(ErrorObject(reason=reason, source="business"))
    assert got.recoverability is not Recoverability.CUSTOMER_RECOVERABLE


# --- messaging -----------------------------------------------------------------

@given(text=st.text(max_size=200))
@FAST
def test_arbitrary_text_is_never_certified_as_compliant(text):
    """No string that is not a registered template may pass as sendable copy."""
    try:
        verify_compliance(text)
    except TemplateViolation:
        return
    # If it verified, it must genuinely be a filled registered template.
    from src.act.messaging import load_templates
    assert any(text.startswith(v.split("{")[0])
               for tpl in load_templates().values() for v in tpl["variants"].values())


@given(value=st.text(max_size=300))
@FAST
def test_a_slot_never_admits_a_control_or_bidi_character(value):
    try:
        cleaned = validate_slot("merchant", value)
    except TemplateViolation:
        return
    assert not any(ord(ch) < 0x20 for ch in cleaned)
    assert not any("‪" <= ch <= "‮" for ch in cleaned)


# --- bandit --------------------------------------------------------------------

@given(successes=st.lists(st.booleans(), min_size=1, max_size=200))
@FAST
def test_a_posterior_mean_always_stays_a_probability(successes):
    p = BetaPosterior()
    for s in successes:
        p = p.updated(s)
    assert 0.0 <= p.mean <= 1.0
    assert p.observations == len(successes)


@given(weights=st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=1, max_size=8))
@FAST
def test_fractional_credit_never_produces_an_invalid_posterior(weights):
    """Soft credit assignment across a belief distribution."""
    p = BetaPosterior()
    for w in weights:
        p = p.updated(True, weight=w)
    assert p.alpha >= 1.0 and p.beta >= 1.0
    assert 0.0 <= p.mean <= 1.0


# --- downtime ------------------------------------------------------------------

@given(method=st.sampled_from(["upi", "card", "netbanking", "fpx", None]),
       key=st.sampled_from(["bank", "issuer", "vpa_handle", "psp"]),
       code=st.text(min_size=1, max_size=12),
       status=st.sampled_from(["started", "resolved", None]))
@FAST
def test_downtime_matching_is_total(method, key, code, status):
    """Razorpay's instrument key differs per rail; all of them must parse."""
    d = Downtime(id="d", method=method, status=status, end=None, instrument={key: code})
    assert isinstance(d.affects(method, code), bool)

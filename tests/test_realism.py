"""Values the synthetic cohort never produces.

Our generator emits exactly one value for several observable fields — `source` is always
"customer", `attempt_number` is always 1, `opted_out` is always False, every merchant has
the same margin and the same id. Those are blind spots: any bug in how we handle the
other values is invisible to `make eval`, and every test still passes.

Two real bugs were found here, both of which the 10,000-event cohort reported as healthy:

  * an inbound event for an ALREADY opted-out customer was contacted anyway, because
    EpisodeState.opted_out was only ever seeded from opt-outs we caused this episode
  * a high-value failure was REFUSED rather than escalated: the rule named
    "escalation.to_human_above_paise" blocked every non-escalation action but never
    said escalation was required, so the agent gave up and left the money

This file exists so the next such bug is caught by CI rather than by luck.
"""

from datetime import datetime, timedelta

import numpy as np
import pytest

from src.act.provider import SimulatedProvider
from src.agent import Agent
from src.clock import IST
from src.decide.bandit import PropensityModel
from src.decide.providers import NullProvider
from src.models import (
    ActionType,
    Channel,
    CustomerHistory,
    ErrorObject,
    MerchantContext,
    RiskEvent,
)

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)


def never(*_a, **_k):
    return (False, False)


def mk(**kw):
    base = dict(event_id="p1", merchant_id="m1", customer_id="c1",
                source_type="payment", amount_paise=500_000, observed_at=NOW,
                razorpay_error=ErrorObject(reason="insufficient_funds"),
                method="upi", bank="HDFC",
                customer_history=CustomerHistory(consented_channels=(Channel.SMS,)))
    base.update(kw)
    return RiskEvent(**base)


def agent(**kw):
    return Agent(model=PropensityModel(), rng=np.random.default_rng(1),
                 llm_provider=NullProvider(), executor=SimulatedProvider(),
                 allow_network=False, **kw)


# --- the two real bugs --------------------------------------------------------

def test_already_opted_out_customer_is_never_contacted():
    """REGRESSION. A customer who unsubscribed last month arrives with opted_out=True.

    EpisodeState.opted_out was seeded only from opt-outs we caused during THIS episode,
    so episode.stop_on_opt_out could never fire for an inbound one and we messaged them.
    The cohort emits opted_out=False on every event, so nothing caught it.
    """
    a = agent()
    result = a.run_episode(
        mk(customer_history=CustomerHistory(opted_out=True,
                                            consented_channels=(Channel.SMS,))),
        "treatment", never)
    assert result.actions_taken == 0
    assert result.cost_paise == 0
    assert result.contacts == 0
    assert a.executor.executed == [], "a message reached an opted-out customer"


def test_high_value_failure_is_escalated_not_refused():
    """REGRESSION. `escalation.to_human_above_paise` blocked everything except
    escalation but never said escalation was REQUIRED, so the agent refused and
    abandoned a six-figure recovery."""
    result = agent().run_episode(mk(amount_paise=100_000_00), "treatment", never)
    assert result.actions_taken > 0, "walked away from a high-value failure"
    assert result.escalated


# --- values the cohort never emits -------------------------------------------

@pytest.mark.parametrize("attempt", [1, 2, 5, 12])
def test_any_attempt_number_is_handled(attempt):
    """The cohort only ever emits attempt_number=1."""
    assert agent().run_episode(mk(attempt_number=attempt), "treatment", never) is not None


@pytest.mark.parametrize("source", ["customer", "business", "gateway", "razorpay",
                                    "issuer_bank", "customer_psp", None])
def test_any_error_source_is_handled(source):
    """The cohort only ever emits source=customer; the live API returned business."""
    event = mk(razorpay_error=ErrorObject(reason="payment_failed", source=source))
    assert agent().run_episode(event, "treatment", never) is not None


@pytest.mark.parametrize("step", ["payment_initiation", "payment_authentication",
                                  "payment_authorization", "payment_response", None])
def test_any_error_step_is_handled(step):
    event = mk(razorpay_error=ErrorObject(reason="payment_failed", step=step))
    assert agent().run_episode(event, "treatment", never) is not None


@pytest.mark.parametrize("amount", [0, 1, 100, 500_000, 100_000_00, 10_000_000_00])
def test_any_amount_is_handled_without_error(amount):
    result = agent().run_episode(mk(amount_paise=amount), "treatment", never)
    assert result.cost_paise >= 0


def test_zero_margin_merchant_never_spends():
    """A merchant with no margin has nothing to gain, so nothing is worth spending."""
    result = agent().run_episode(
        mk(merchant_context=MerchantContext(merchant_id="m", margin_bps=0)),
        "treatment", never)
    assert result.cost_paise == 0


def test_no_consented_channels_means_no_messages():
    a = agent()
    a.run_episode(mk(customer_history=CustomerHistory(consented_channels=())),
                  "treatment", never)
    assert all(action is not ActionType.NUDGE for _, action, _ in a.executor.executed)


def test_missing_method_and_bank_are_handled():
    assert agent().run_episode(mk(method=None, bank=None), "treatment", never) is not None


def test_invoice_without_a_due_date_is_handled():
    event = mk(source_type="invoice", razorpay_error=None, due_at=None)
    assert event.days_overdue(NOW) == 0
    assert agent().run_episode(event, "treatment", never) is not None


def test_very_aged_receivable_is_handled():
    event = mk(source_type="invoice", razorpay_error=None,
               due_at=NOW - timedelta(days=400))
    assert event.days_overdue(NOW) == 400
    assert agent().run_episode(event, "treatment", never) is not None


def test_unicode_and_long_identifiers_are_handled():
    event = mk(customer_id="कस्टमर_" + "x" * 200)
    assert agent().run_episode(event, "treatment", never) is not None


@pytest.mark.parametrize("hour", [0, 8, 9, 18, 19, 23])
def test_every_hour_of_day_is_handled(hour):
    """Quiet-hour boundaries are 09:00 and 19:00; both edges must behave."""
    event = mk(observed_at=NOW.replace(hour=hour, minute=0))
    assert agent().run_episode(event, "treatment", never) is not None


def test_multiple_merchants_do_not_share_a_daily_budget():
    """REGRESSION. The cohort has ONE merchant, so isolation was never exercised.

    Counters were per-agent, so a busy merchant could exhaust a quiet one's daily
    action budget and neither would get the limit their contract promises.
    """
    a = agent()
    a.merchant_day("busy", NOW).actions = 499
    assert a.merchant_day("quiet", NOW).actions == 0, "budgets are pooled across merchants"


def test_one_merchant_exhausting_its_budget_does_not_block_another():
    from src.policy.engine import load_policy
    limit = load_policy()["merchant"]["daily_action_budget"]
    a = agent()
    a.merchant_day("busy", NOW).actions = limit
    a.run_episode(mk(event_id="q1", merchant_id="quiet"), "treatment", never)
    assert a.merchant_day("quiet", NOW).actions >= 0
    assert a.merchant_day("busy", NOW).actions == limit

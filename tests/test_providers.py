"""Provider boundary tests (CLAUDE.md sections 2 and 7)."""

import pytest

from src.act.costs import (
    attention_cost_paise,
    direct_cost_paise,
    opt_out_probability,
    opt_out_risk_paise,
)
from src.act.provider import (
    Downtime,
    LiveKeyRefused,
    RazorpayProvider,
    SimulatedProvider,
    idempotency_key,
)
from src.models import ActionType, Channel


def test_live_key_is_refused():
    """section 2: real money, never. Enforced in code, not by convention."""
    with pytest.raises(LiveKeyRefused):
        RazorpayProvider(key_id="rzp_live_abcdef", key_secret="s")


def test_test_key_is_accepted():
    assert RazorpayProvider(key_id="rzp_test_abcdef", key_secret="s").key_id.startswith("rzp_test_")


def test_nudges_are_never_really_sent():
    """section 2: all customer contact is simulated and logged."""
    for provider in (SimulatedProvider(), RazorpayProvider(key_id="rzp_test_a", key_secret="s")):
        res = provider.send_nudge("e1", Channel.SMS, "tpl", "en", "k1")
        assert res.simulated is True


def test_idempotency_prevents_double_charging():
    """section 7: webhooks can be redelivered."""
    p = SimulatedProvider()
    key = idempotency_key("e1", 0, ActionType.NUDGE)
    first = p.send_nudge("e1", Channel.SMS, "tpl", "en", key)
    replay = p.send_nudge("e1", Channel.SMS, "tpl", "en", key)
    assert first.executed and first.cost_paise > 0
    assert not replay.executed and replay.cost_paise == 0


def test_idempotency_keys_are_deterministic():
    assert idempotency_key("e1", 2, ActionType.RETRY_NOW) == idempotency_key("e1", 2, ActionType.RETRY_NOW)
    assert idempotency_key("e1", 2, ActionType.RETRY_NOW) != idempotency_key("e1", 3, ActionType.RETRY_NOW)


def test_simulated_provider_needs_no_network():
    """section 7: the decision core must be fully testable with zero network."""
    p = SimulatedProvider()
    assert p.fetch_downtimes() == ()
    assert p.retry_payment("e1", 1000, "k").executed


def test_downtime_matches_only_the_affected_rail():
    d = Downtime(id="d1", method="upi", status="started", instrument={"issuer": "HDFC"})
    assert d.affects("upi", "HDFC")
    assert not d.affects("card", "HDFC")
    assert not d.affects("upi", "SBI")


def test_resolved_downtime_does_not_block():
    d = Downtime(id="d1", method="upi", status="resolved", end=1772000000)
    assert not d.affects("upi", None)


# --- cost model ------------------------------------------------------------

def test_attention_cost_is_superlinear():
    """section 5: this is what makes the agent stop."""
    costs = [attention_cost_paise(ActionType.NUDGE, n) for n in range(5)]
    gaps = [b - a for a, b in zip(costs, costs[1:])]
    assert all(b > a for a, b in zip(gaps, gaps[1:])), f"not superlinear: {costs}"


def test_silent_retry_costs_no_attention():
    """A gateway retry annoys nobody."""
    assert attention_cost_paise(ActionType.RETRY_NOW, 5) == 0
    assert attention_cost_paise(ActionType.NO_ACTION, 5) == 0


def test_nudge_requires_a_channel_to_be_priced():
    with pytest.raises(ValueError):
        direct_cost_paise(ActionType.NUDGE, None)


def test_all_costs_are_integer_paise():
    """section 12: no floats for money."""
    for ch in Channel:
        assert isinstance(direct_cost_paise(ActionType.NUDGE, ch), int)
    assert isinstance(attention_cost_paise(ActionType.NUDGE, 3), int)


def test_attention_cost_prices_the_opt_out_risk():
    """costs.yaml declared opt_out_risk_paise from day one but never applied it.

    Annoyance is cheap; losing the customer is not. Pricing only fatigue let the agent
    contact people almost freely.
    """
    from src.act.costs import opt_out_probability
    cost = attention_cost_paise(ActionType.NUDGE, 0)
    fatigue_only = 150  # base_paise from config/costs.yaml
    assert cost > fatigue_only
    assert cost >= opt_out_probability(0) * opt_out_risk_paise()


def test_opt_out_probability_rises_superlinearly_and_is_capped():
    probs = [opt_out_probability(n) for n in range(6)]
    gaps = [b - a for a, b in zip(probs, probs[1:])]
    assert all(b > a for a, b in zip(gaps, gaps[1:]))
    assert all(0.0 <= p <= 0.5 for p in probs)


def test_fifth_contact_costs_far_more_than_the_first():
    first = attention_cost_paise(ActionType.NUDGE, 0)
    fifth = attention_cost_paise(ActionType.NUDGE, 4)
    assert fifth > first * 20

"""Market profile tests.

Razorpay operates in India, Malaysia (Curlec) and Singapore. An agent that hardcodes
rupees, IST and RBI is an Indian script, not a product.
"""

import pytest

from src.decide.ev import DecisionContext, candidate_actions
from src.market import (
    DEFAULT_MARKET,
    UnknownMarket,
    get_market,
    known_markets,
    market_for_currency,
)
from src.models import ActionType, ErrorObject, RiskEvent, money
from src.taxonomy.mapping import classify
from tests.test_decide import NOW


def test_only_verified_markets_ship():
    """Scope is India. The structure supports more; we ship only what we checked."""
    assert set(known_markets()) == {"IN"}
    assert all(get_market(c).verified for c in known_markets())


@pytest.mark.parametrize("code", ["IN"])
def test_every_market_is_complete(code):
    m = get_market(code)
    assert m.currency.code and m.currency.symbol and m.currency.minor_per_major > 0
    assert m.timezone is not None
    assert m.rails and m.languages
    assert m.contact_window_start < m.contact_window_end


def test_money_formats_from_the_market_profile():
    """Formatting is data-driven, so a second market is config rather than a rewrite."""
    assert money(249900, "IN") == "₹2,499.00"


def test_money_never_assumes_100_minor_units():
    """Currencies exist where it is not 100; the divisor is read, never assumed."""
    m = get_market("IN")
    assert m.currency.minor_per_major == 100
    assert m.money(1) == "₹0.01"


def test_indian_rails_are_complete():
    """The rails an Indian merchant actually accepts through Razorpay."""
    rails = set(get_market("IN").rails)
    assert {"upi", "card", "netbanking", "wallet", "emandate"} <= rails


def test_method_switch_only_suggests_rails_that_exist_here():
    """The agent must never recommend a payment rail unavailable in this market."""
    err = ErrorObject(reason="card_expired")
    event = RiskEvent(event_id="e", merchant_id="m", customer_id="c",
                      source_type="payment", amount_paise=500_000, observed_at=NOW,
                      razorpay_error=err, method="card")
    mapping = classify(err)
    ctx = DecisionContext(event=event, failure_class=mapping.failure_class,
                          recoverability=mapping.recoverability, mapping=mapping,
                          now=NOW, market=get_market("IN"))
    rails = [p["suggested_rail"] for a, p in candidate_actions(ctx)
             if a is ActionType.SWITCH_METHOD]
    assert rails and all(r in get_market("IN").rails for r in rails)


def test_currency_resolves_to_a_market():
    assert market_for_currency("inr").code == "IN"
    with pytest.raises(UnknownMarket):
        market_for_currency("XYZ")


def test_unknown_market_raises_rather_than_defaulting_to_india():
    """Silently falling back would mis-apply Indian regulation to a foreign merchant."""
    with pytest.raises(UnknownMarket):
        get_market("ZZ")


def test_shipped_market_is_verified():
    """A profile we had not checked would be a liability dressed as a feature."""
    assert get_market("IN").caveat() is None


def test_default_market_is_india():
    assert DEFAULT_MARKET == "IN" and get_market().code == "IN"

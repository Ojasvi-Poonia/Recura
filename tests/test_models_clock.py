"""Domain-model and clock invariants (CLAUDE.md sections 2, 4, 12)."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.clock import IST, SystemClock, VirtualClock
from src.models import ErrorObject, RiskEvent, rupees


def _event(**kw):
    base = dict(
        event_id="e1", merchant_id="m1", customer_id="c1", source_type="payment",
        amount_paise=100_000, observed_at=datetime(2026, 3, 2, 14, 0, tzinfo=IST),
    )
    return RiskEvent(**{**base, **kw})


def test_models_are_immutable():
    with pytest.raises(ValidationError):
        _event().amount_paise = 1


def test_money_rejects_negative_amounts():
    with pytest.raises(ValidationError):
        _event(amount_paise=-1)


def test_money_is_integer_paise_not_float():
    assert isinstance(_event().amount_paise, int)


def test_rupee_display_is_the_only_conversion():
    assert rupees(249_900) == "₹2,499.00"
    assert rupees(0) == "₹0.00"


def test_extra_fields_are_rejected():
    """A latent field must not be smuggled onto an observation (section 9.1)."""
    with pytest.raises(ValidationError):
        _event(latent_propensity=0.9)


def test_virtual_clock_requires_timezone():
    with pytest.raises(ValueError):
        VirtualClock(datetime(2026, 3, 2, 14, 0))


def test_virtual_clock_cannot_move_backwards():
    c = VirtualClock(datetime(2026, 3, 2, 14, 0, tzinfo=IST))
    with pytest.raises(ValueError):
        c.advance(hours=-1)
    with pytest.raises(ValueError):
        c.set_to(datetime(2026, 3, 1, 14, 0, tzinfo=IST))


def test_virtual_clock_advances_deterministically():
    c = VirtualClock(datetime(2026, 3, 2, 14, 0, tzinfo=IST))
    assert c.advance(hours=26).isoformat() == "2026-03-03T16:00:00+05:30"


def test_all_clocks_are_ist():
    assert VirtualClock(datetime(2026, 3, 2, tzinfo=IST)).now().tzinfo is IST
    assert SystemClock().now().tzinfo is IST

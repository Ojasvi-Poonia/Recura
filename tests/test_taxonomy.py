"""Taxonomy tests (CLAUDE.md section 13: definition of done for `taxonomy/`)."""

import pytest

from src.models import ErrorObject, FailureClass, Recoverability
from src.taxonomy import mapping as m


def test_every_published_reason_is_mapped():
    """CLAUDE.md section 13: every reason in the CSV maps to a FailureClass."""
    missing = m.published_reasons() - set(m.MAPPING)
    assert not missing, f"unmapped published reasons: {sorted(missing)}"


def test_no_invented_reasons():
    """CLAUDE.md section 7: do not invent categories. Every key must be Razorpay's."""
    invented = set(m.MAPPING) - m.published_reasons()
    assert not invented, f"reasons not in Razorpay's published list: {sorted(invented)}"


def test_unknown_reason_falls_back_and_is_counted():
    m.reset_unmapped_counts()
    got = m.classify(ErrorObject(reason="not_a_real_razorpay_reason"))
    assert got.failure_class is FailureClass.UNKNOWN
    assert m.unmapped_counts() == {"not_a_real_razorpay_reason": 1}
    m.reset_unmapped_counts()


def test_missing_error_object_is_handled():
    assert m.classify(None).failure_class is FailureClass.UNKNOWN
    assert m.classify(ErrorObject()).failure_class is FailureClass.UNKNOWN


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("insufficient_funds", FailureClass.FUNDS),
        ("bank_technical_error", FailureClass.TRANSIENT_INFRA),
        ("incorrect_otp", FailureClass.AUTH_ABANDON),
        ("card_expired", FailureClass.INSTRUMENT_INVALID),
        ("payment_risk_check_failed", FailureClass.RISK_DECLINE),
        ("transaction_daily_limit_exceeded", FailureClass.LIMIT_EXCEEDED),
    ],
)
def test_anchor_reasons(reason, expected):
    """Anchors from CLAUDE.md section 4's own class descriptions."""
    assert m.classify(ErrorObject(reason=reason)).failure_class is expected


def test_merchant_config_reasons_never_look_customer_recoverable():
    """A merchant integration bug must never trigger a customer contact."""
    for reason in ("invalid_order_id", "live_mode_not_enabled", "merchant_not_activated"):
        assert m.MAPPING[reason].recoverability is Recoverability.MERCHANT_CONFIG


def test_order_already_paid_is_terminal():
    """Late authorisation stop condition (CLAUDE.md section 7)."""
    assert m.MAPPING["order_already_paid"].recoverability is Recoverability.TERMINAL


def test_stated_retry_delays_are_positive():
    for row in m.MAPPING.values():
        if row.min_retry_delay_hours is not None:
            assert row.min_retry_delay_hours > 0, row.reason


def test_ambiguous_calls_carry_a_rationale():
    """Judgment calls a panel will question must be justified in the table."""
    for reason in ("card_declined", "authorisation_declined_by_psp", "order_already_paid"):
        assert len(m.MAPPING[reason].note) > 40, f"{reason} needs a rationale"

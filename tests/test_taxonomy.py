"""Taxonomy tests (spec §13: definition of done for `taxonomy/`)."""

import pytest

from src.models import ErrorObject, FailureClass, Recoverability
from src.taxonomy import mapping as m


def test_every_published_reason_is_mapped():
    """spec §13: every reason in the CSV maps to a FailureClass."""
    missing = m.published_reasons() - set(m.MAPPING)
    assert not missing, f"unmapped published reasons: {sorted(missing)}"


def test_no_invented_reasons():
    """spec §7: do not invent categories. Every key must be Razorpay's."""
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
    """Anchors from spec §4's own class descriptions."""
    assert m.classify(ErrorObject(reason=reason)).failure_class is expected


def test_merchant_config_reasons_never_look_customer_recoverable():
    """A merchant integration bug must never trigger a customer contact."""
    for reason in ("invalid_order_id", "live_mode_not_enabled", "merchant_not_activated"):
        assert m.MAPPING[reason].recoverability is Recoverability.MERCHANT_CONFIG


def test_order_already_paid_is_terminal():
    """Late authorisation stop condition (spec §7)."""
    assert m.MAPPING["order_already_paid"].recoverability is Recoverability.TERMINAL


def test_stated_retry_delays_are_positive():
    for row in m.MAPPING.values():
        if row.min_retry_delay_hours is not None:
            assert row.min_retry_delay_hours > 0, row.reason


def test_ambiguous_calls_carry_a_rationale():
    """Judgment calls a panel will question must be justified in the table."""
    for reason in ("card_declined", "authorisation_declined_by_psp", "order_already_paid"):
        assert len(m.MAPPING[reason].note) > 40, f"{reason} needs a rationale"


def test_checkout_abandonment_is_not_unknown():
    """A dropped checkout carries no Razorpay error - nothing reached the gateway.

    "Checkout abandonment" is a named Track 03 direction; classing it UNKNOWN would
    discard real information about a customer who demonstrably had intent.
    """
    got = m.classify(None, source_type="checkout")
    assert got.failure_class is FailureClass.AUTH_ABANDON
    assert got.recoverability is Recoverability.CUSTOMER_RECOVERABLE


def test_missing_error_without_checkout_context_is_still_unknown():
    assert m.classify(None).failure_class is FailureClass.UNKNOWN
    assert m.classify(None, source_type="payment").failure_class is FailureClass.UNKNOWN


# --- Razorpay's `source` field carries triage information ---------------------

def test_source_business_forces_merchant_triage():
    """Razorpay documents source=business as "fix the request parameters".

    Found by Tier 1: the first real failed payment returned
    international_transaction_not_allowed with source=business - which our
    reason-only table classed CUSTOMER_RECOVERABLE. Nudging a customer about the
    merchant's own configuration would be wrong.
    """
    err = ErrorObject(reason="international_transaction_not_allowed", source="business")
    assert m.classify(err).recoverability is Recoverability.MERCHANT_CONFIG


def test_source_customer_leaves_triage_alone():
    err = ErrorObject(reason="international_transaction_not_allowed", source="customer")
    assert m.classify(err).recoverability is Recoverability.CUSTOMER_RECOVERABLE


def test_source_refinement_explains_itself_in_the_note():
    err = ErrorObject(reason="insufficient_funds", source="business")
    assert "source=business" in m.classify(err).note


def test_source_never_downgrades_an_already_merchant_reason():
    err = ErrorObject(reason="invalid_order_id", source="business")
    assert m.classify(err).recoverability is Recoverability.MERCHANT_CONFIG


def test_missing_source_is_harmless():
    assert m.classify(ErrorObject(reason="insufficient_funds")).recoverability \
        is Recoverability.CUSTOMER_RECOVERABLE


def test_real_tier1_payload_triages_correctly():
    """The exact error object the live API returned on 2026-08-27."""
    err = ErrorObject(code="BAD_REQUEST_ERROR",
                      reason="international_transaction_not_allowed",
                      source="business", step="payment_initiation")
    got = m.classify(err, "payment")
    assert got.failure_class is FailureClass.INSTRUMENT_INVALID
    assert got.recoverability is Recoverability.MERCHANT_CONFIG

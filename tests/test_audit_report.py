"""Recovery-audit report tests.

The audit is what a merchant receives after handing over a failure export. It must add up,
must never claim lift it cannot measure, and must not let a string from the merchant's own
file execute in the browser of whoever opens the report.
"""

from pathlib import Path

import pytest

from src.ingest.audit_report import build_audit, render_html
from src.ingest.import_file import ImportError_, load_events, to_risk_event
from src.market import get_market

SAMPLE = Path("data/samples/example_failures.csv")


def row(**kw):
    base = {"event_id": "e1", "amount": "249900", "failed_at": "2026-03-10T14:22:00",
            "error_reason": "insufficient_funds", "method": "upi", "bank": "HDFC",
            "customer_id": "c1", "source_type": "payment", "consented_channels": "sms"}
    base.update(kw)
    return base


def audit_of(*rows):
    events = [to_risk_event(r, "m", False) for r in rows]
    return build_audit(events, source_name="t.csv", rows_read=len(rows), rejected=())


@pytest.fixture(scope="module")
def sample_audit():
    events, rejected, _ = load_events(SAMPLE, "m", amounts_are_major=True)
    return build_audit(events, source_name=SAMPLE.name, rows_read=13, rejected=rejected)


def test_load_events_collects_rejections_instead_of_failing_the_file(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("event_id,amount,failed_at\ne1,100,2026-03-10T10:00:00\ne2,,\n")

    events, rejected, skipped = load_events(path, "m", amounts_are_major=False)

    assert [e.event_id for e in events] == ["e1"]
    assert len(rejected) == 1 and "e2" in rejected[0]
    assert skipped == 0


def test_sample_totals_match_the_export(sample_audit):
    assert sample_audit.events == 13
    assert sample_audit.at_risk_minor == 13_889_500


def test_every_breakdown_accounts_for_every_event_and_every_rupee(sample_audit):
    for buckets in (sample_audit.recoverability, sample_audit.failure_classes,
                    sample_audit.actions):
        assert sum(b.events for b in buckets) == sample_audit.events
        assert sum(b.amount_minor for b in buckets) == sample_audit.at_risk_minor


def test_merchant_integration_bugs_are_listed_by_reason_code():
    audit = audit_of(row(event_id="e1", error_reason="invalid_order_id"),
                     row(event_id="e2", error_reason="invalid_order_id"),
                     row(event_id="e3"))

    assert [(b.label, b.events) for b in audit.merchant_bugs] == [("invalid_order_id", 2)]
    assert audit.merchant_bugs[0].amount_minor == 2 * 249900


def test_policy_refusals_are_reported_by_rule():
    """A mandate debit with no pre-debit notice on record is the contract's to refuse."""
    audit = audit_of(row(source_type="mandate", method="emandate",
                         error_reason="bank_technical_error"))

    assert [(b.label, b.events) for b in audit.blocked_rules] == [
        ("retry.pre_debit_notification_hours", 1)]
    assert [b.label for b in audit.actions] == ["BLOCKED_BY_POLICY"]


def test_doing_nothing_about_a_merchant_bug_is_not_reported_as_a_policy_block():
    """NO_ACTION is a refusal by arithmetic. The contract did not stop anything."""
    audit = audit_of(row(error_reason="invalid_order_id"))

    assert audit.blocked_rules == ()
    assert [b.label for b in audit.actions] == ["NO_ACTION"]


def test_a_high_value_failure_is_routed_to_a_person():
    audit = audit_of(row(amount="9000000", error_reason="bank_technical_error"))

    assert [b.label for b in audit.actions] == ["ESCALATE_HUMAN"]


def test_the_proposed_action_does_not_depend_on_where_the_row_sits_in_the_file():
    """Thompson draws made the same event read differently when rows were reordered."""
    rows = [row(event_id=f"e{i}", error_reason=reason, amount=str(100000 * (i + 1)))
            for i, reason in enumerate(("insufficient_funds", "card_expired",
                                        "bank_technical_error", "incorrect_otp"))]

    forward = audit_of(*rows)
    backward = audit_of(*reversed(rows))

    assert forward.actions == backward.actions


def test_rows_a_profile_skips_are_reported_on_the_page():
    events = [to_risk_event(row(), "m", False)]
    audit = build_audit(events, source_name="t.json", rows_read=4, rejected=(), skipped=3)

    assert "3 rows were not failures" in render_html(audit, get_market())


def test_events_without_a_consented_contact_channel_are_counted():
    audit = audit_of(row(event_id="e1", consented_channels=""), row(event_id="e2"))

    assert audit.no_consent_events == 1


def test_unrecognised_codes_are_surfaced_not_guessed():
    audit = audit_of(row(error_reason="some_new_code_we_never_saw"))

    assert audit.unmapped == (("some_new_code_we_never_saw", 1),)


def test_an_empty_export_is_refused_with_a_usable_message():
    with pytest.raises(ImportError_):
        build_audit([], source_name="t.csv", rows_read=0, rejected=())


def test_the_audit_is_deterministic(sample_audit):
    events, rejected, _ = load_events(SAMPLE, "m", amounts_are_major=True)
    again = build_audit(events, source_name=SAMPLE.name, rows_read=13, rejected=rejected)

    assert again == sample_audit
    assert render_html(again, get_market()) == render_html(sample_audit, get_market())


def test_html_shows_the_amount_at_risk(sample_audit):
    market = get_market()

    html = render_html(sample_audit, market)

    assert market.money(13_889_500) in html
    assert html.lstrip().lower().startswith("<!doctype html>")


def test_html_escapes_strings_that_came_from_the_merchants_file():
    hostile = "<script>alert(1)</script>"
    events = [to_risk_event(row(error_reason=hostile), "m", False)]
    audit = build_audit(events, source_name=hostile, rows_read=1,
                        rejected=(f"row {hostile} is missing: ['amount']",))

    html = render_html(audit, get_market())

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_shipped_sample_shows_an_integration_bug_and_a_policy_refusal(sample_audit):
    """The sample is what a visitor sees first. Its two most useful sections must not
    read "None found"."""
    assert [(b.label, b.events) for b in sample_audit.merchant_bugs] == [
        ("invalid_order_id", 2)]
    assert [b.label for b in sample_audit.blocked_rules] == [
        "retry.pre_debit_notification_hours"]


def test_html_states_that_it_is_not_a_lift_estimate(sample_audit):
    html = render_html(sample_audit, get_market())

    assert "not a forecast" in html
    assert "holdout" in html

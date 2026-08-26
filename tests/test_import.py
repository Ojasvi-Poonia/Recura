"""Bring-your-own-data importer tests.

The synthetic cohort establishes internal validity. This path exists so a reviewer can
point Recura at their OWN export instead of arguing about our data.
"""

import json

import pytest

from src.ingest.import_file import (
    ImportError_,
    coverage_report,
    read_rows,
    to_risk_event,
)
from src.models import Channel, FailureClass

SAMPLE = "data/samples/example_failures.csv"


def row(**kw):
    base = {"event_id": "e1", "amount": "2499", "failed_at": "2026-03-10T14:22:00",
            "error_reason": "insufficient_funds", "method": "upi", "bank": "HDFC",
            "customer_id": "c1", "source_type": "payment"}
    base.update(kw)
    return base


def test_shipped_sample_imports_cleanly():
    rows = read_rows(__import__("pathlib").Path(SAMPLE))
    events = [to_risk_event(r, "m", True) for r in rows]
    assert len(events) == 10


def test_required_columns_are_enforced_with_a_usable_message():
    with pytest.raises(ImportError_) as exc:
        to_risk_event({"event_id": "e1"}, "m", False)
    assert "amount" in str(exc.value) and "failed_at" in str(exc.value)


def test_major_units_are_converted_to_minor():
    """A merchant exporting rupees must not have them read as paise."""
    assert to_risk_event(row(amount="2499"), "m", True).amount_paise == 249900
    assert to_risk_event(row(amount="249900"), "m", False).amount_paise == 249900


def test_epoch_timestamps_are_accepted():
    """Razorpay exports epoch seconds."""
    assert to_risk_event(row(failed_at="1772000000"), "m", False).observed_at.year == 2026


def test_iso_timestamps_are_accepted():
    assert to_risk_event(row(failed_at="2026-03-10T14:22:00"), "m", False).observed_at.day == 10


def test_unknown_reason_codes_do_not_crash_the_import():
    """A code we have never seen must degrade, not fail the file."""
    event = to_risk_event(row(error_reason="brand_new_code_2027"), "m", True)
    assert event.razorpay_error.reason == "brand_new_code_2027"
    report = coverage_report([event])
    assert report["unrecognised_codes"] == 1
    assert report["classes"]["UNKNOWN"] == 1


def test_rows_without_an_error_object_are_allowed():
    """Checkout drops and receivables legitimately have no error code."""
    event = to_risk_event(row(error_reason="", source_type="checkout"), "m", True)
    assert event.razorpay_error is None
    assert coverage_report([event])["classes"]["AUTH_ABANDON"] == 1


def test_consent_is_parsed_and_unknown_channels_dropped():
    hist = to_risk_event(row(consented_channels="sms, email, carrier_pigeon"), "m", True
                         ).customer_history
    assert set(hist.consented_channels) == {Channel.SMS, Channel.EMAIL}


def test_customer_id_falls_back_without_leaking_pii():
    event = to_risk_event(row(customer_id=""), "m", True)
    assert "@" not in event.customer_id and event.customer_id.startswith("anon_")


def test_unknown_source_type_falls_back_to_payment():
    assert to_risk_event(row(source_type="carrier_pigeon"), "m", True).source_type == "payment"


def test_coverage_report_counts_every_event():
    events = [to_risk_event(row(event_id=f"e{i}"), "m", True) for i in range(5)]
    report = coverage_report(events)
    assert report["events"] == 5
    assert sum(report["classes"].values()) == 5


def test_missing_file_gives_a_usable_message(tmp_path):
    with pytest.raises(ImportError_) as exc:
        read_rows(tmp_path / "nope.csv")
    assert "no such file" in str(exc.value)


def test_json_exports_are_accepted(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps([row()]), encoding="utf-8")
    assert len(read_rows(path)) == 1


def test_json_must_be_a_list(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(ImportError_):
        read_rows(path)


def test_imported_events_carry_per_merchant_margin():
    event = to_risk_event(row(margin_bps="1200"), "m", True)
    assert event.merchant_context.margin_bps == 1200

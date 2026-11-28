"""PSP profile tests.

A profile lets a merchant hand over their gateway's export as it is. The risk is that a
profile quietly invents: maps a code to a reason that does not exist, drops rows nobody
hears about, or reads paise as rupees. Each of those has a test.
"""

import csv
import json
from pathlib import Path

import pytest

from src.ingest.import_file import ImportError_, coverage_report, load_events
from src.ingest.psp import (
    PROFILE_DIR,
    ProfileError,
    is_failure,
    known_profiles,
    load_profile,
    translate,
)
from src.models import FailureClass, Recoverability
from src.taxonomy.mapping import MAPPING, classify

STRIPE_SAMPLE = Path("data/samples/stripe_charges.json")
STRIPE_CODES = Path("data/stripe_decline_codes.csv")


def charge(**kw):
    base = {"id": "ch_1", "amount": 249900, "currency": "inr", "created": 1773130920,
            "status": "failed", "customer": "cus_1", "failure_code": "card_declined",
            "outcome": {"reason": "insufficient_funds", "type": "issuer_declined"},
            "payment_method_details": {"type": "card"}}
    base.update(kw)
    return base


def write_json(tmp_path, rows, wrapper="data"):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({wrapper: rows}))
    return path


@pytest.mark.parametrize("key", known_profiles())
def test_every_profile_translates_only_to_reasons_the_taxonomy_maps(key):
    """A profile may not invent a category. Every target must be a real mapped reason."""
    profile = load_profile(key)

    targets = set(profile.reasons.values()) | {reason for _, _, reason in profile.ranges}
    invented = sorted(targets - set(MAPPING))

    assert not invented, f"{key}.yaml maps to reasons that do not exist: {invented}"


PUBLISHED = [k for k in known_profiles() if load_profile(k).published_codes]


@pytest.mark.parametrize("key", PUBLISHED)
def test_every_code_a_gateway_publishes_has_a_translation(key):
    profile = load_profile(key)
    with profile.published_codes.open(encoding="utf-8") as f:
        published = [row[0] for row in csv.reader(f)][1:]

    ranges = {f"{low}-{high}" for low, high, _ in profile.ranges}
    untranslated = sorted(set(published) - set(profile.reasons) - ranges)
    unpublished = sorted(set(profile.reasons) - set(published))

    assert published and not untranslated, f"{key}: no translation for {untranslated}"
    assert not unpublished, f"{key}: translates codes it never published: {unpublished}"


def test_a_code_inside_a_published_range_takes_the_ranges_reason():
    braintree = load_profile("braintree")

    assert braintree.reason_for("2500") == "payment_declined"
    assert braintree.reason_for("2001") == "insufficient_funds"
    assert braintree.reason_for("9999") == "9999"


def test_phonepe_rows_that_read_as_technical_are_not_all_treated_as_retryable():
    """PhonePe describes tampering, its own checksum errors and a completed payment
    with the same words: "temporary technical issue". None of them may be retried."""
    phonepe = load_profile("phonepe")

    def mapped(code):
        return MAPPING[phonepe.reason_for(code)]

    assert mapped("TXN_DATA_TAMPERED").failure_class is FailureClass.RISK_DECLINE
    assert mapped("CHECKSUM_MISMATCH").recoverability is Recoverability.MERCHANT_CONFIG
    assert mapped("SUCCESS").recoverability is Recoverability.TERMINAL
    assert mapped("TXN_ALREADY_PROCESSED").recoverability is Recoverability.TERMINAL


def test_checkout_payment_list_imports_declines_and_skips_captures(tmp_path):
    """Shaped like the GET /payments example in Checkout.com's docs: amount in paise,
    requested_on with seven fractional digits, status "Declined" or "Captured"."""
    declined = {"id": "pay_1", "requested_on": "2026-04-21T13:39:44.3167397Z",
                "source": {"type": "card"}, "amount": 249900, "currency": "INR",
                "status": "Declined", "approved": False, "response_code": "20051"}
    captured = {**declined, "id": "pay_2", "status": "Captured", "approved": True,
                "response_code": "10000"}

    events, rejected, skipped = load_events(write_json(tmp_path, [declined, captured]),
                                            "m", False, psp="checkout")

    assert (rejected, skipped) == ((), 1)
    event = events[0]
    assert event.event_id == "pay_1"
    assert event.amount_paise == 249900
    assert event.method == "card"
    assert classify(event.razorpay_error).failure_class is FailureClass.FUNDS


def test_a_code_translated_twice_is_refused(tmp_path, monkeypatch):
    (tmp_path / "twice.yaml").write_text(
        "name: Twice\ntranslate:\n  insufficient_funds: [X1]\n  card_expired: [X1]\n")
    monkeypatch.setattr("src.ingest.psp.PROFILE_DIR", tmp_path)

    with pytest.raises(ProfileError, match="twice"):
        load_profile("twice")


def test_every_published_stripe_decline_code_has_a_translation():
    with STRIPE_CODES.open(encoding="utf-8") as f:
        published = {r["decline_code"] for r in csv.DictReader(f)}
    stripe = load_profile("stripe")

    assert len(published) == 50
    assert not sorted(published - set(stripe.reasons))


def test_stripe_charge_becomes_a_risk_event_without_renaming_anything(tmp_path):
    events, rejected, skipped = load_events(write_json(tmp_path, [charge()]), "m",
                                            False, psp="stripe")

    assert (rejected, skipped) == ((), 0)
    event = events[0]
    assert event.event_id == "ch_1"
    assert event.amount_paise == 249900          # already minor units
    assert event.observed_at.year == 2026
    assert event.customer_id == "cus_1"
    assert event.method == "card"
    assert classify(event.razorpay_error).failure_class is FailureClass.FUNDS


def test_the_gateways_own_code_is_kept_beside_the_translation(tmp_path):
    rows = [charge(outcome={"reason": "expired_card"})]

    events, _, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")

    assert events[0].razorpay_error.reason == "card_expired"
    assert events[0].razorpay_error.code == "expired_card"


def test_successful_charges_are_skipped_and_counted(tmp_path):
    rows = [charge(), charge(id="ch_2", status="succeeded"), charge(id="ch_3", status="pending")]

    events, rejected, skipped = load_events(write_json(tmp_path, rows), "m", False,
                                            psp="stripe")

    assert [e.event_id for e in events] == ["ch_1"]
    assert skipped == 2 and rejected == ()


def test_an_export_with_no_status_column_is_not_emptied_by_the_status_filter():
    """A sheet already filtered to failures has no `status`. Keep its rows."""
    stripe = load_profile("stripe")

    assert is_failure({"id": "ch_1", "amount": "100"}, stripe)
    assert not is_failure({"id": "ch_1", "status": "succeeded"}, stripe)


def test_an_untranslated_code_passes_through_and_is_counted_not_guessed(tmp_path):
    rows = [charge(outcome={"reason": "a_code_stripe_adds_next_year"})]

    events, _, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")
    coverage = coverage_report(events)

    assert coverage["unmapped"] == {"a_code_stripe_adds_next_year": 1}
    assert classify(events[0].razorpay_error).failure_class is FailureClass.UNKNOWN


def test_a_radar_block_is_never_retried(tmp_path):
    rows = [charge(outcome={"reason": "highest_risk_level", "type": "blocked"})]

    events, _, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")

    assert classify(events[0].razorpay_error).failure_class is FailureClass.RISK_DECLINE


def test_a_test_card_in_the_export_never_becomes_customer_contact(tmp_path):
    rows = [charge(outcome={"reason": "testmode_decline"})]

    events, _, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")

    assert (classify(events[0].razorpay_error).recoverability
            is Recoverability.MERCHANT_CONFIG)


def test_falls_back_to_failure_code_when_no_decline_code_is_present(tmp_path):
    rows = [charge(outcome={"reason": None}, failure_code="processing_error")]

    events, _, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")

    assert events[0].razorpay_error.reason == "gateway_technical_error"


def test_major_flag_contradicting_a_minor_unit_profile_is_refused(tmp_path):
    """Reading paise as rupees would inflate every amount a hundredfold."""
    with pytest.raises(ImportError_) as exc:
        load_events(write_json(tmp_path, [charge()]), "m", True, psp="stripe")

    assert "100x" in str(exc.value)


def test_a_foreign_currency_charge_is_rejected_with_the_reason(tmp_path):
    rows = [charge(currency="usd")]

    events, rejected, _ = load_events(write_json(tmp_path, rows), "m", False, psp="stripe")

    assert events == [] and "USD" in rejected[0]


def test_razorpay_collection_reads_as_exported(tmp_path):
    rows = [
        {"id": "pay_1", "amount": 150000, "currency": "INR", "status": "failed",
         "method": "upi", "created_at": 1773130920, "error_reason": "insufficient_funds",
         "error_source": "customer", "customer_id": "cust_1"},
        {"id": "pay_2", "amount": 99900, "currency": "INR", "status": "captured",
         "method": "card", "created_at": 1773130930},
    ]

    events, rejected, skipped = load_events(write_json(tmp_path, rows, "items"), "m",
                                            False, psp="razorpay")

    assert [e.event_id for e in events] == ["pay_1"]
    assert events[0].razorpay_error.reason == "insufficient_funds"
    assert (skipped, rejected) == (1, ())


def test_a_razorpay_export_keeps_razorpays_error_code_not_the_reason_twice(tmp_path):
    rows = [{"id": "pay_1", "amount": 150000, "currency": "INR", "status": "failed",
             "created_at": 1773130920, "error_code": "BAD_REQUEST_ERROR",
             "error_reason": "insufficient_funds"}]

    events, _, _ = load_events(write_json(tmp_path, rows, "items"), "m", False,
                               psp="razorpay")

    assert events[0].razorpay_error.code == "BAD_REQUEST_ERROR"
    assert events[0].razorpay_error.reason == "insufficient_funds"


def test_translate_does_not_modify_the_row_it_was_given():
    original = charge()
    snapshot = json.dumps(original, sort_keys=True)

    translate(original, load_profile("stripe"))

    assert json.dumps(original, sort_keys=True) == snapshot


def test_unknown_profile_names_the_ones_that_exist():
    with pytest.raises(ProfileError) as exc:
        load_profile("paypal")

    assert "stripe" in str(exc.value) and "razorpay" in str(exc.value)


@pytest.mark.parametrize("key", ["../../policy", "../psp/stripe", "", "/etc/passwd"])
def test_a_profile_name_cannot_leave_the_profile_directory(key):
    with pytest.raises(ProfileError):
        load_profile(key)


def test_profile_naming_a_column_the_importer_does_not_read_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "typo.yaml").write_text("name: Typo\ncolumns:\n  evnt_id: [id]\n")
    monkeypatch.setattr("src.ingest.psp.PROFILE_DIR", tmp_path)

    with pytest.raises(ProfileError) as exc:
        load_profile("typo")

    assert "evnt_id" in str(exc.value)


def test_shipped_stripe_sample_imports_with_the_shipped_profile():
    events, rejected, skipped = load_events(STRIPE_SAMPLE, "m", False, psp="stripe")

    assert len(events) == 5
    assert skipped == 1                       # the succeeded charge
    assert len(rejected) == 1                 # the USD charge
    assert coverage_report(events)["unrecognised_codes"] == 0


def test_profile_directory_is_the_one_the_docs_name():
    assert PROFILE_DIR.parts[-2:] == ("config", "psp")

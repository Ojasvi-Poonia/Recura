"""Bring-your-own-data importer tests.

The synthetic cohort establishes internal validity. This path exists so a reviewer can
point Recura at their OWN export instead of arguing about our data.
"""

import json
import os

import pytest

from src.ingest.import_file import (
    ImportError_,
    _int,
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


def _write(tmp_path, name, content: bytes):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_excel_utf8_bom_does_not_reject_every_row(tmp_path):
    """Excel's "CSV UTF-8" export writes a BOM. We must not choke on the commonest
    spreadsheet export in existence, least of all with a message blaming the user for a
    column that is plainly present."""
    path = _write(tmp_path, "bom.csv",
                  "﻿event_id,amount,failed_at\nevt_1,500.00,2026-08-30T10:00:00\n"
                  .encode("utf-8"))
    rows = read_rows(path)
    assert "event_id" in rows[0], f"BOM leaked into the header: {list(rows[0])[0]!r}"
    assert to_risk_event(rows[0], "m", amounts_are_major=True).event_id == "evt_1"


def test_an_eight_digit_date_is_a_date_not_an_epoch(tmp_path):
    """"20260830" as epoch seconds is 24 August 1970, which silently moves the whole
    file half a century into the past and makes every ageing calculation nonsense."""
    path = _write(tmp_path, "d.csv",
                  b"event_id,amount,failed_at\nevt_1,500.00,20260830\n")
    event = to_risk_event(read_rows(path)[0], "m", amounts_are_major=True)
    assert event.observed_at.year == 2026
    assert (event.observed_at.month, event.observed_at.day) == (8, 30)


def test_a_foreign_currency_is_refused_rather_than_silently_mislabelled(tmp_path):
    """USD amounts were read as INR minor units and printed with a rupee symbol."""
    path = _write(tmp_path, "usd.csv",
                  b"event_id,amount,failed_at,currency\nevt_1,500.00,2026-08-30T10:00:00,USD\n")
    with pytest.raises(ImportError_, match="USD"):
        to_risk_event(read_rows(path)[0], "m", amounts_are_major=True)


def test_a_declared_zero_margin_is_honoured_not_replaced_by_the_default():
    """`row.get(key) or default` treats a legitimate 0 as absent. A merchant declaring
    zero margin was silently given 3000bps and charged for interventions the arithmetic
    should have refused."""
    assert _int({"margin_bps": 0}, "margin_bps", 3000) == 0
    assert _int({"margin_bps": "0"}, "margin_bps", 3000) == 0
    assert _int({"margin_bps": ""}, "margin_bps", 3000) == 3000
    assert _int({}, "margin_bps", 3000) == 3000


def test_money_does_not_pass_through_a_float(tmp_path):
    """spec §12 forbids floats for money; this path was violating it."""
    path = _write(tmp_path, "m.csv",
                  b"event_id,amount,failed_at\nevt_1,1234.45,2026-08-30T10:00:00\n")
    event = to_risk_event(read_rows(path)[0], "m", amounts_are_major=True)
    assert event.amount_paise == 123445


def test_dotenv_is_actually_loaded_and_never_clobbers_the_shell(tmp_path, monkeypatch):
    """The README says `cp .env.example .env`. Nothing read that file.

    So a reviewer followed the documented setup, got a .env on disk, and `make tier1`
    still told them the key was missing. A documented step that silently does nothing is
    worse than no step.
    """
    from src.env import load_env

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nFROM_FILE=file-value\nALREADY_SET=file-value\nMALFORMED\n",
        encoding="utf-8")

    monkeypatch.setenv("ALREADY_SET", "shell-value")
    monkeypatch.delenv("FROM_FILE", raising=False)

    loaded = load_env(env)

    assert os.environ["FROM_FILE"] == "file-value", ".env was not applied"
    assert os.environ["ALREADY_SET"] == "shell-value", (
        "a variable exported in the shell was overwritten by the file")
    assert loaded == 1


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    """Most reviewers never create one - `make eval` needs no key at all."""
    from src.env import load_env

    assert load_env(tmp_path / "nope.env") == 0

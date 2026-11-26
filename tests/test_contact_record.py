"""Contact record tests.

The policy check used to count contacts from whatever the caller said. These tests pin
down the record that replaced that: a caller can make an answer more cautious and never
less, a permission is good for one contact, a retried confirmation is not a second
contact, and the record an auditor exports says which contacts broke the contract.
"""

import csv
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import src.api.main as api
from src.clock import IST, VirtualClock
from src.policy.check import (
    ConfirmedContact,
    ContactRejected,
    PolicyCheckLog,
    ProposedAction,
    breaches_in,
    check,
    export_contacts,
)

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)        # Tuesday, inside contact hours
API_KEY = "key_contacts_test"


def proposal(**kw) -> ProposedAction:
    customer = {"consented_channels": ["sms"], **kw.pop("customer", {})}
    base = {"event_id": "pay_1", "customer_id": "cust_1", "action": "NUDGE",
            "amount_paise": 249900, "episode_started_at": (NOW - timedelta(days=1)).isoformat(),
            "failure_reason": "insufficient_funds", "channel": "sms",
            "template_id": "tpl_funds_01", "customer": customer}
    base.update(kw)
    return ProposedAction.model_validate(base)


def contact(at: datetime, **kw) -> ConfirmedContact:
    base = {"event_id": "pay_1", "customer_id": "cust_1", "channel": "sms",
            "contacted_at": at.isoformat()}
    base.update(kw)
    return ConfirmedContact.model_validate(base)


@pytest.fixture
def log():
    return PolicyCheckLog(create_engine("sqlite://", future=True))


def rules(result) -> list[str]:
    return [r.rule_id for r in result.blocked_by]


# -- the record outranks the caller -------------------------------------------------


def test_recorded_contacts_are_counted_when_the_caller_reports_none(log):
    for days_ago in (5, 3, 2):
        log.confirm(contact(NOW - timedelta(days=days_ago)), NOW)

    result = check(proposal(customer={"contacts_last_7d": 0}), NOW, log)

    assert "contact.max_per_customer_per_7d" in rules(result)
    assert result.history.recorded_contacts_last_7d == 3
    assert result.history.contacts_last_7d == 3
    assert result.history.caller_under_reported is True


def test_the_gap_since_the_last_contact_comes_from_the_record(log):
    log.confirm(contact(NOW - timedelta(hours=4)), NOW)

    result = check(proposal(), NOW, log)

    assert "contact.min_hours_between" in rules(result)
    assert result.history.last_contact_at == NOW - timedelta(hours=4)
    assert result.history.last_contact_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_a_caller_reporting_more_than_the_record_is_believed(log):
    """A caller may make the answer stricter. It just cannot make it looser."""
    result = check(proposal(customer={"contacts_last_7d": 3}), NOW, log)

    assert "contact.max_per_customer_per_7d" in rules(result)
    assert result.history.caller_under_reported is False


def test_contacts_older_than_seven_days_have_left_the_window(log):
    log.confirm(contact(NOW - timedelta(days=8)), NOW)
    log.confirm(contact(NOW - timedelta(days=9)), NOW)
    log.confirm(contact(NOW - timedelta(days=10)), NOW)

    result = check(proposal(), NOW, log)

    assert result.allowed
    assert result.history.recorded_contacts_last_7d == 0


def test_another_customers_contacts_are_not_counted(log):
    for days_ago in (1, 2, 3):
        log.confirm(contact(NOW - timedelta(days=days_ago), customer_id="cust_other"), NOW)

    assert check(proposal(), NOW, log).allowed


def test_the_logged_check_keeps_both_what_was_said_and_what_was_used(log):
    log.confirm(contact(NOW - timedelta(days=2)), NOW)

    check(proposal(customer={"contacts_last_7d": 0}), NOW, log)
    row = log.for_customer("cust_1")[0]

    assert '"contacts_last_7d":0' in row["request"]
    assert '"recorded_contacts_last_7d":1' in row["history_used"]


def test_without_a_record_the_reply_says_none_is_kept():
    result = check(proposal(customer={"contacts_last_7d": 1}), NOW)

    assert result.history.recorded_contacts_last_7d is None
    assert result.history.contacts_last_7d == 1


# -- what a confirmation may claim --------------------------------------------------


def test_a_contact_confirmed_against_an_allowed_check_is_checked(log):
    allowed = check(proposal(), NOW, log)

    recorded = log.confirm(contact(NOW, check_id=allowed.check_id), NOW)

    assert recorded.checked and not recorded.already_recorded
    assert recorded.recorded_contacts_last_7d == 1


def test_one_allowed_check_is_permission_for_one_contact(log):
    allowed = check(proposal(), NOW, log)
    log.confirm(contact(NOW, check_id=allowed.check_id), NOW)

    with pytest.raises(ContactRejected, match="already been used"):
        log.confirm(contact(NOW, check_id=allowed.check_id), NOW)


def test_a_refused_check_cannot_be_cited_as_permission(log):
    refused = check(proposal(customer={"opted_out": True}), NOW, log)

    with pytest.raises(ContactRejected, match="refused"):
        log.confirm(contact(NOW, check_id=refused.check_id), NOW)


@pytest.mark.parametrize("change, reason", [
    ({"check_id": "0" * 32}, "no check"),
    ({"customer_id": "cust_someone_else"}, "different customer"),
])
def test_a_check_cannot_be_borrowed(log, change, reason):
    allowed = check(proposal(), NOW, log)

    with pytest.raises(ContactRejected, match=reason):
        log.confirm(contact(NOW, **{"check_id": allowed.check_id, **change}), NOW)


def test_a_check_for_a_retry_is_not_permission_to_contact(log):
    retry = check(proposal(action="RETRY_NOW", channel=None, template_id=None), NOW, log)
    assert retry.allowed

    with pytest.raises(ContactRejected, match="not a contact"):
        log.confirm(contact(NOW, check_id=retry.check_id), NOW)


def test_a_contact_made_without_asking_is_still_recorded_and_counted(log):
    """The contact that skipped the gate is the one an auditor most needs to see."""
    recorded = log.confirm(contact(NOW - timedelta(hours=1)), NOW)

    assert recorded.checked is False
    assert log.contacts("cust_1")[0].checked is False
    assert "contact.min_hours_between" in rules(check(proposal(), NOW, log))


def test_a_contact_dated_in_the_future_is_a_plan_not_a_record(log):
    with pytest.raises(ContactRejected, match="future"):
        log.confirm(contact(NOW + timedelta(hours=1)), NOW)


def test_small_clock_drift_between_caller_and_server_is_tolerated(log):
    assert log.confirm(contact(NOW + timedelta(minutes=2)), NOW).recorded_contacts_last_7d == 1


def test_a_retried_confirmation_is_recognised_not_counted_twice(log):
    first = log.confirm(contact(NOW, contact_ref="sms_msg_1"), NOW)
    again = log.confirm(contact(NOW, contact_ref="sms_msg_1"), NOW)

    assert again.already_recorded and again.contact_id == first.contact_id
    assert again.recorded_contacts_last_7d == 1


def test_reusing_a_ref_for_a_different_contact_is_refused(log):
    log.confirm(contact(NOW, contact_ref="sms_msg_1"), NOW)

    with pytest.raises(ContactRejected, match="different contact"):
        log.confirm(contact(NOW, contact_ref="sms_msg_1", customer_id="cust_2"), NOW)


@pytest.mark.parametrize("statement", [
    "UPDATE policy_contacts SET contacted_at = '2020-01-01T00:00:00+00:00'",
    "DELETE FROM policy_contacts",
])
def test_the_contact_record_cannot_be_rewritten(log, statement):
    log.confirm(contact(NOW), NOW)

    with pytest.raises(Exception, match="append-only"), log.engine.begin() as conn:
        conn.execute(text(statement))
    assert len(log.contacts("cust_1")) == 1


# -- judging the record against the contract ----------------------------------------


def test_a_contact_outside_the_contact_window_is_flagged():
    evening = datetime(2026, 3, 10, 20, 15, tzinfo=IST)

    assert breaches_in([evening]) == [("contact.quiet_hours",)]


def test_the_fourth_contact_in_a_week_is_the_one_that_breaks_the_cap():
    times = [NOW - timedelta(days=d) for d in (6, 4, 2, 0)]

    flagged = breaches_in(times)

    assert flagged[:3] == [(), (), ()]
    assert flagged[3] == ("contact.max_per_customer_per_7d",)


def test_two_contacts_closer_than_the_minimum_gap_flag_the_second():
    flagged = breaches_in([NOW - timedelta(hours=2), NOW])      # 09:00 and 11:00

    assert flagged == [(), ("contact.min_hours_between",)]


def test_a_clean_history_has_no_breaches():
    assert breaches_in([NOW - timedelta(days=d) for d in (6, 3, 0)]) == [(), (), ()]


def test_breaches_are_judged_in_time_order_whatever_order_they_were_confirmed(log):
    log.confirm(contact(NOW), NOW)
    log.confirm(contact(NOW - timedelta(hours=3)), NOW)

    entries = log.contacts("cust_1")

    assert [e.contacted_at for e in entries] == [NOW - timedelta(hours=3), NOW]
    assert entries[1].breaches == ("contact.min_hours_between",)


def test_export_lists_every_contact_with_its_status(log, tmp_path):
    allowed = check(proposal(), NOW, log)
    log.confirm(contact(NOW, check_id=allowed.check_id), NOW)
    log.confirm(contact(NOW + timedelta(minutes=1), customer_id="cust_1"), NOW + timedelta(minutes=1))
    log.confirm(contact(datetime(2026, 3, 9, 22, 0, tzinfo=IST), customer_id="cust_2"), NOW)

    out = tmp_path / "contacts.csv"
    total, unchecked, breached = export_contacts(log, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))

    assert (total, unchecked, breached) == (3, 2, 2)
    assert [r["customer_id"] for r in rows] == ["cust_1", "cust_1", "cust_2"]
    assert rows[0]["checked"] == "yes" and rows[1]["checked"] == "no"
    assert rows[1]["breaches"] == "contact.min_hours_between"
    assert rows[2]["breaches"] == "contact.quiet_hours"


# -- the API surface ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(api.SHADOW_DB_ENV, f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv(api.API_KEY_ENV, API_KEY)
    monkeypatch.setattr(api, "_clock", VirtualClock(NOW))
    api._recorder_for.cache_clear()
    api._check_log_for.cache_clear()
    yield TestClient(api.app)
    api._recorder_for.cache_clear()
    api._check_log_for.cache_clear()


AUTH = {"Authorization": f"Bearer {API_KEY}"}


def post_contact(client, **kw):
    return client.post("/policy/contacts", headers=AUTH,
                       json=contact(NOW, **kw).model_dump(mode="json"))


def test_api_check_then_confirm_then_check_again(client):
    first = client.post("/policy/check", headers=AUTH,
                        json=proposal().model_dump(mode="json")).json()
    assert first["allowed"]

    confirmed = post_contact(client, check_id=first["check_id"])
    assert confirmed.status_code == 200 and confirmed.json()["checked"] is True

    second = client.post("/policy/check", headers=AUTH,
                         json=proposal().model_dump(mode="json")).json()
    assert second["allowed"] is False
    assert second["blocked_by"][0]["rule_id"] == "contact.min_hours_between"
    assert second["history"]["caller_under_reported"] is True


def test_api_refuses_a_confirmation_that_would_make_the_record_untrue(client):
    r = post_contact(client, check_id="0" * 32)

    assert r.status_code == 409 and "no check" in r.json()["detail"]


def test_api_returns_the_customers_contacts_with_breaches(client):
    post_contact(client)
    post_contact(client, event_id="pay_2")

    entries = client.get("/policy/customers/cust_1/contacts", headers=AUTH).json()

    assert len(entries) == 2
    assert entries[1]["breaches"] == ["contact.min_hours_between"]


def test_api_contact_endpoints_need_the_key(client):
    assert client.post("/policy/contacts", json={}).status_code == 401
    assert client.get("/policy/customers/cust_1/contacts").status_code == 401


def test_api_contact_endpoints_say_so_when_no_record_is_kept(client, monkeypatch):
    monkeypatch.delenv(api.SHADOW_DB_ENV)

    assert post_contact(client).status_code == 503
    assert client.get("/policy/customers/cust_1/contacts",
                      headers=AUTH).status_code == 503


def test_the_contract_check_needs_no_model_and_no_priors(client, monkeypatch):
    # Building the shadow recorder builds an agent: priors, and an LLM provider.
    def no_agent(url):
        raise AssertionError("an agent was built to answer a contract check")
    no_agent.cache_clear = lambda: None
    monkeypatch.setattr(api, "_recorder_for", no_agent)

    r = client.post("/policy/check", headers=AUTH, json=proposal().model_dump(mode="json"))

    assert r.status_code == 200 and r.json()["logged"] is True
    assert post_contact(client, check_id=r.json()["check_id"]).status_code == 200

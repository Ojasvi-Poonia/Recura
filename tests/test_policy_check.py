"""Policy check tests.

POST /policy/check lets a system that is not Recura ask the contract before it contacts
a customer. A wrong "allowed" here is a compliance incident at whoever trusted it, so
each clause a caller is likely to hit has a test, and so does the record that proves
what was asked.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import src.api.main as api
from src.clock import IST, VirtualClock
from src.ledger.store import Ledger
from src.models import ActionType, RiskEvent
from src.policy.check import PolicyCheckLog, ProposedAction, check, verdict_for
from src.shadow import ShadowRecorder

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)        # a Tuesday, inside contact hours
API_KEY = "key_policy_test"


def proposal(**kw) -> ProposedAction:
    customer = {"consented_channels": ["sms"], **kw.pop("customer", {})}
    base = {"event_id": "pay_1", "customer_id": "cust_1", "action": "NUDGE",
            "amount_paise": 249900, "episode_started_at": (NOW - timedelta(hours=2)).isoformat(),
            "failure_reason": "insufficient_funds", "channel": "sms",
            "template_id": "tpl_funds_01", "customer": customer}
    base.update(kw)
    return ProposedAction.model_validate(base)


def blocked_rules(p: ProposedAction, now=NOW) -> list[str]:
    return [r.rule_id for r in check(p, now).blocked_by]


# -- the clauses a caller will actually hit -----------------------------------------


def test_a_consented_templated_message_in_contact_hours_is_allowed():
    result = check(proposal(), NOW)

    assert result.allowed and result.blocked_by == () and result.send_at is None
    assert result.rules_evaluated == 20


@pytest.mark.parametrize("change, rule", [
    ({"customer": {"consented_channels": []}}, "contact.require_consent"),
    ({"template_id": None}, "contact.require_registered_template"),
    ({"customer": {"contacts_last_7d": 3}}, "contact.max_per_customer_per_7d"),
    ({"customer": {"last_contact_at": (NOW - timedelta(hours=5)).isoformat()}},
     "contact.min_hours_between"),
    ({"customer": {"opted_out": True}}, "episode.stop_on_opt_out"),
    ({"customer": {"paid": True}}, "episode.stop_on_payment"),
    ({"customer": {"disputed": True}}, "episode.stop_on_dispute"),
    ({"customer": {"broken_promise_to_pay": True}}, "escalation.after_broken_promise_to_pay"),
    ({"episode_started_at": (NOW - timedelta(days=40)).isoformat()}, "episode.max_days"),
    ({"failure_reason": "invalid_order_id"}, "retry.forbidden_for_recoverability"),
    ({"action": "RETRY_NOW", "failure_reason": "payment_risk_check_failed"},
     "retry.forbidden_for_classes"),
    ({"action": "RETRY_NOW", "customer": {"attempts_made": 3}},
     "retry.max_attempts_per_episode"),
    ({"action": "RETRY_NOW", "source_type": "mandate"},
     "retry.pre_debit_notification_hours"),
])
def test_each_clause_refuses_the_action_it_exists_to_refuse(change, rule):
    assert rule in blocked_rules(proposal(**change))


def test_every_refusal_carries_a_reason_a_person_can_read():
    result = check(proposal(customer={"contacts_last_7d": 3}), NOW)

    assert "3 times in 7 days" in result.blocked_by[0].reason


def test_a_message_proposed_at_night_is_allowed_but_moved_to_the_morning():
    """Quiet hours shift a contact; they do not cancel it."""
    late = datetime(2026, 3, 10, 21, 30, tzinfo=IST)

    result = check(proposal(scheduled_at=late.isoformat()), NOW)

    assert result.allowed
    assert result.send_at == datetime(2026, 3, 11, 9, 0, tzinfo=IST)


def test_a_blocked_action_is_not_offered_a_later_send_time():
    """ "Blocked, send at 09:00" reads as permission to try again in the morning."""
    late = datetime(2026, 3, 10, 21, 30, tzinfo=IST)

    result = check(proposal(scheduled_at=late.isoformat(),
                            customer={"contacts_last_7d": 3}), NOW)

    assert not result.allowed and result.send_at is None


def test_a_time_with_no_zone_is_read_in_the_markets_zone():
    """21:30 with no offset must mean 21:30 in India, not 21:30 UTC (03:00 IST)."""
    result = check(proposal(scheduled_at="2026-03-10T21:30:00"), NOW)

    assert result.send_at == datetime(2026, 3, 11, 9, 0, tzinfo=IST)


def test_an_amount_above_the_automation_ceiling_must_go_to_a_person():
    result = check(proposal(amount_paise=9_000_000), NOW)

    assert not result.allowed
    assert result.required_action is ActionType.ESCALATE_HUMAN


def test_a_mandate_debit_is_allowed_once_the_notice_is_a_day_old():
    notified = {"pre_debit_notice_sent_at": (NOW - timedelta(hours=25)).isoformat()}

    assert blocked_rules(proposal(action="RETRY_NOW", source_type="mandate",
                                  customer=notified)) == []


def test_an_unknown_failure_reason_is_handled_not_refused():
    """A reason the taxonomy has never seen is UNKNOWN and customer-side: conservative,
    but it must not turn every check for another gateway's codes into a block."""
    assert check(proposal(failure_reason="a_code_from_another_gateway"), NOW).allowed


def test_a_nudge_with_no_channel_is_answered_rather_than_crashing():
    result = check(proposal(channel=None), NOW)

    assert isinstance(result.allowed, bool)


def test_the_answer_depends_only_on_what_was_asked():
    p = proposal(customer={"contacts_last_7d": 2})

    assert verdict_for(p, NOW) == verdict_for(p, NOW)


def test_a_misspelt_field_is_an_error_not_a_silently_ignored_fact():
    with pytest.raises(ValueError):
        proposal(customer={"opted_ou": True})


# -- the record ---------------------------------------------------------------------


@pytest.fixture
def log(tmp_path):
    return PolicyCheckLog(create_engine(f"sqlite:///{tmp_path / 'checks.db'}", future=True))


def test_the_question_and_the_answer_are_both_kept(log):
    result = check(proposal(customer={"contacts_last_7d": 3}), NOW, log)

    rows = log.for_customer("cust_1")

    assert result.logged and len(rows) == 1
    assert rows[0]["check_id"] == result.check_id
    assert rows[0]["allowed"] == 0
    assert "contact.max_per_customer_per_7d" in rows[0]["blocked_rules"]
    assert '"contacts_last_7d":3' in rows[0]["request"]


def test_without_somewhere_to_write_the_reply_says_it_was_not_logged():
    assert check(proposal(), NOW).logged is False


@pytest.mark.parametrize("statement", [
    "UPDATE policy_checks SET allowed = 1",
    "DELETE FROM policy_checks",
])
def test_the_record_cannot_be_rewritten(log, statement):
    check(proposal(customer={"opted_out": True}), NOW, log)

    with pytest.raises(Exception, match="append-only"), log.engine.begin() as conn:
        conn.execute(text(statement))
    assert log.for_customer("cust_1")[0]["allowed"] == 0


@pytest.mark.parametrize("statement", [
    "UPDATE shadow_assignments SET arm = 'treatment'",
    "DELETE FROM shadow_assignments",
    "DELETE FROM shadow_stops",
    "DELETE FROM shadow_episode_keys",
    "DELETE FROM webhook_receipts",
])
def test_shadow_tables_described_as_insert_only_are_enforced_by_the_database(tmp_path,
                                                                             statement):
    """An arm assignment that could be edited after outcomes are known is not one."""
    recorder = ShadowRecorder(Ledger(url=f"sqlite:///{tmp_path / 's.db'}"),
                              clock=VirtualClock(NOW))
    recorder.record(RiskEvent(event_id="pay_1", merchant_id="m", customer_id="cust_1",
                              source_type="payment", amount_paise=1000, observed_at=NOW))
    recorder.remember("evt_1")
    recorder.stop(("pay_1",), "payment_captured", NOW)

    with pytest.raises(Exception, match="append-only"), recorder.ledger.engine.begin() as conn:
        conn.execute(text(statement))


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


def ask(client, key=API_KEY, **kw):
    return client.post("/policy/check", json=proposal(**kw).model_dump(mode="json"),
                       headers={"Authorization": f"Bearer {key}"})


def test_api_answers_and_logs(client):
    body = ask(client, customer={"contacts_last_7d": 3}).json()

    assert body["allowed"] is False and body["logged"] is True
    assert body["blocked_by"][0]["rule_id"] == "contact.max_per_customer_per_7d"


def test_api_without_a_database_still_answers_and_says_it_did_not_log(client, monkeypatch):
    monkeypatch.delenv(api.SHADOW_DB_ENV)

    body = ask(client).json()

    assert body["allowed"] is True and body["logged"] is False


def test_api_refuses_a_caller_without_the_key(client):
    assert ask(client, key="wrong").status_code == 401


def test_api_is_closed_when_no_key_is_configured(client, monkeypatch):
    monkeypatch.delenv(api.API_KEY_ENV)

    assert ask(client).status_code == 503


def test_api_rejects_an_unknown_field_and_an_unknown_action(client):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    good = proposal().model_dump(mode="json")

    assert client.post("/policy/check", json={**good, "surprise": 1},
                       headers=headers).status_code == 422
    assert client.post("/policy/check", json={**good, "action": "SEND_ANYWAY"},
                       headers=headers).status_code == 422

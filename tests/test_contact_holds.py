"""Contact hold tests.

A check used to read the record and nothing more, so two senders asking about the same
customer before either confirmed could both be told yes. An allowed contact now holds
its slot until the contact is confirmed or the hold lapses. These tests pin that down:
the second sender is refused and told why, the hold is counted once and then gives way
to the confirmed contact, it lapses on its own, and concurrent checks get one yes.
"""

import copy
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from src.clock import IST
from src.policy.check import (
    HOLD_FOR,
    SLOT_HELD_RULE,
    ConfirmedContact,
    ContactRejected,
    PolicyCheckLog,
    ProposedAction,
    check,
)
from src.policy.engine import load_policy

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)        # Tuesday, inside contact hours


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
def log(tmp_path):
    # A file, not :memory:, so that every thread in the concurrency test sees one database.
    return PolicyCheckLog(create_engine(f"sqlite:///{tmp_path / 'holds.db'}", future=True))


def rules(result) -> list[str]:
    return [r.rule_id for r in result.blocked_by]


# -- one yes per slot ---------------------------------------------------------------


def test_a_second_sender_is_refused_while_the_first_holds_the_slot(log):
    first = check(proposal(), NOW, log)

    second = check(proposal(event_id="pay_from_vendor_b"), NOW + timedelta(seconds=1), log)

    assert first.allowed
    assert rules(second) == [SLOT_HELD_RULE]
    assert first.check_id in second.blocked_by[0].reason
    assert second.history.held_contacts == 1
    assert second.history.held_until == NOW + HOLD_FOR


def test_a_hold_is_not_an_under_report(log):
    check(proposal(), NOW, log)

    second = check(proposal(), NOW, log)

    assert second.history.recorded_contacts_last_7d == 0
    assert second.history.caller_under_reported is False


def test_another_customers_hold_does_not_block(log):
    check(proposal(), NOW, log)

    assert check(proposal(customer_id="cust_2"), NOW, log).allowed


def test_confirming_the_contact_replaces_the_hold_and_counts_once(log):
    first = check(proposal(), NOW, log)
    log.confirm(contact(NOW, check_id=first.check_id), NOW)

    after = check(proposal(), NOW + timedelta(minutes=1), log)

    assert rules(after) == ["contact.min_hours_between"]
    assert after.history.held_contacts == 0
    assert after.history.recorded_contacts_last_7d == 1


def test_a_hold_lapses_if_the_contact_is_never_confirmed(log):
    check(proposal(), NOW, log)

    assert not check(proposal(), NOW + HOLD_FOR - timedelta(seconds=1), log).allowed
    assert check(proposal(), NOW + HOLD_FOR, log).allowed


def test_a_contact_moved_to_the_morning_holds_its_slot_until_then(log):
    evening = datetime(2026, 3, 10, 21, 30, tzinfo=IST)
    first = check(proposal(scheduled_at=evening.isoformat()), evening, log)
    morning = datetime(2026, 3, 11, 9, 0, tzinfo=IST)
    assert first.send_at == morning

    overnight = check(proposal(), evening + timedelta(hours=1), log)
    assert rules(overnight) == [SLOT_HELD_RULE]
    assert overnight.history.held_until == morning + HOLD_FOR


# -- what holds nothing -------------------------------------------------------------


def test_a_refused_check_holds_nothing(log):
    check(proposal(customer={"opted_out": True}), NOW, log)

    assert check(proposal(), NOW, log).allowed


def test_a_check_for_a_retry_holds_no_contact_slot(log):
    assert check(proposal(action="RETRY_NOW", channel=None, template_id=None), NOW, log).allowed

    assert check(proposal(), NOW, log).allowed


def test_without_a_record_nothing_can_be_held():
    assert check(proposal(), NOW).allowed
    assert check(proposal(), NOW).allowed


def test_a_hold_takes_one_slot_of_the_weekly_cap_not_all_of_them(log):
    # With no minimum gap, a cap of three leaves room for a second contact beside a hold,
    # and none beside two holds and a recorded contact.
    policy = copy.deepcopy(load_policy())
    policy["contact"]["min_hours_between"] = 0
    log.confirm(contact(NOW - timedelta(days=2)), NOW)

    assert check(proposal(), NOW, log, policy).allowed
    assert check(proposal(), NOW, log, policy).allowed
    third = check(proposal(), NOW, log, policy)

    assert rules(third) == [SLOT_HELD_RULE]
    assert third.history.contacts_last_7d == 3


def test_a_block_the_holds_did_not_cause_keeps_its_own_reason(log):
    check(proposal(), NOW, log)

    refused = check(proposal(customer={"opted_out": True}), NOW, log)

    assert SLOT_HELD_RULE not in rules(refused)
    assert "episode.stop_on_opt_out" in rules(refused)


# -- a check is permission for the contact it was asked about -----------------------


def test_a_check_cannot_cover_a_contact_made_after_its_hold_lapsed(log):
    allowed = check(proposal(), NOW, log)
    later = NOW + timedelta(days=1)

    with pytest.raises(ContactRejected, match="outside the window"):
        log.confirm(contact(later, check_id=allowed.check_id), later)


def test_a_check_cannot_cover_a_contact_made_before_it_was_asked(log):
    allowed = check(proposal(), NOW, log)

    with pytest.raises(ContactRejected, match="outside the window"):
        log.confirm(contact(NOW - timedelta(hours=2), check_id=allowed.check_id), NOW)


def test_a_contact_moved_to_the_morning_is_covered_when_sent_in_the_morning(log):
    evening = datetime(2026, 3, 10, 21, 30, tzinfo=IST)
    allowed = check(proposal(scheduled_at=evening.isoformat()), evening, log)
    sent = datetime(2026, 3, 11, 9, 5, tzinfo=IST)

    assert log.confirm(contact(sent, check_id=allowed.check_id), sent).checked


@pytest.mark.parametrize("change, reason", [
    ({"channel": "whatsapp"}, "was for sms"),
    ({"event_id": "pay_other"}, "different event"),
])
def test_a_check_covers_only_the_channel_and_event_it_was_asked_about(log, change, reason):
    allowed = check(proposal(), NOW, log)

    with pytest.raises(ContactRejected, match=reason):
        log.confirm(contact(NOW, check_id=allowed.check_id, **change), NOW)


# -- concurrency and the record -----------------------------------------------------


def test_senders_asking_at_the_same_instant_get_one_yes(log):
    senders = 8
    start = threading.Barrier(senders)
    answers: list[bool] = []

    def ask(i: int) -> None:
        start.wait()
        answers.append(check(proposal(event_id=f"pay_{i}"), NOW, log).allowed)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(senders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(answers) == [False] * (senders - 1) + [True]


@pytest.mark.parametrize("statement", [
    "UPDATE policy_holds SET expires_at = '2000-01-01T00:00:00+00:00'",
    "DELETE FROM policy_holds",
])
def test_a_hold_cannot_be_rewritten_or_released_by_hand(log, statement):
    check(proposal(), NOW, log)

    with pytest.raises(Exception, match="append-only"), log.engine.begin() as conn:
        conn.execute(text(statement))
    assert not check(proposal(), NOW, log).allowed

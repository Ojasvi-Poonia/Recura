"""Shadow mode tests.

Shadow mode is what a merchant runs before trusting Recura with a customer. Its two
promises are that nothing is ever executed, and that the "came back unaided" figure is
joined correctly - a wrong join there is a wrong baseline, and every later lift claim
would be measured against it.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import src.api.main as api
from src.assignment import TREATMENT, assign_arm
from src.clock import IST, VirtualClock
from src.ingest.signature import expected_signature
from src.ingest.webhook import Outcome, normalise
from src.ledger.store import Ledger
from src.models import CustomerHistory, ErrorObject, MerchantContext, RiskEvent
from src.shadow import MIN_CUSTOMERS_PER_ARM, SHADOW_ARM, ShadowRecorder, render_text

T0 = datetime(2026, 3, 10, 11, 0, tzinfo=IST)
SECRET = "whsec_shadow_test"
API_KEY = "key_shadow_test"


def failure(event_id="pay_A", amount=249900, reason="insufficient_funds",
            source_type="payment", at=T0, **kw) -> RiskEvent:
    return RiskEvent(
        event_id=event_id, merchant_id="m",
        customer_id=kw.get("customer_id", f"cust_{event_id}"),
        source_type=source_type, amount_paise=amount, observed_at=at,
        razorpay_error=ErrorObject(reason=reason) if reason else None,
        method=kw.get("method", "upi"),
        customer_history=CustomerHistory(consented_channels=("sms",)),
        merchant_context=MerchantContext(merchant_id="m"),
    )


@pytest.fixture
def recorder(tmp_path):
    return ShadowRecorder(Ledger(url=f"sqlite:///{tmp_path / 'shadow.db'}"),
                          clock=VirtualClock(T0))


@pytest.fixture
def bulk_recorder():
    """In memory, for the tests that record hundreds of failures. The on-disk fixture
    pays for a commit per write, which is right for production and slow for a test."""
    return ShadowRecorder(Ledger(url="sqlite://"), clock=VirtualClock(T0))


# -- nothing is executed ------------------------------------------------------------


def test_a_failure_is_decided_and_logged_but_never_executed(recorder):
    decision, _ = recorder.record(failure())

    rows = recorder.ledger.for_event("pay_A")

    assert len(rows) == 1 and rows[0]["arm"] == SHADOW_ARM
    assert json.loads(rows[0]["action_result"])["executed"] is False
    assert json.loads(rows[0]["decision"])["action"] == decision.action.value
    assert recorder.agent.executor.executed == []
    assert recorder.summary().executed == 0


def test_the_logged_decision_carries_the_options_it_weighed(recorder):
    recorder.record(failure())

    logged = json.loads(recorder.ledger.for_event("pay_A")[0]["decision"])

    assert len(logged["considered"]) > 1


def test_shadow_does_not_learn_from_decisions_it_never_executed(recorder):
    before = recorder.agent.model.snapshot()

    for i in range(5):
        recorder.record(failure(event_id=f"pay_{i}"))

    assert recorder.agent.model.snapshot() == before


# -- joining a failure to what settled it -------------------------------------------


def test_a_later_payment_on_the_same_order_closes_the_episode_as_paid_unaided(recorder):
    """The retry that succeeds is a different payment. The order is what they share."""
    recorder.record(failure("pay_A"), keys=("pay_A", "order_X"))

    recorder.stop(("pay_B", "order_X"), "payment_captured", T0 + timedelta(hours=3))
    summary = recorder.summary()

    assert (summary.paid_unaided, summary.paid_unaided_minor) == (1, 249900)
    assert summary.still_open == 0


def test_late_authorisation_of_the_same_payment_counts_as_paid(recorder):
    recorder.record(failure("pay_A"), keys=("pay_A",))

    recorder.stop(("pay_A",), "late_authorisation", T0 + timedelta(minutes=20))

    assert recorder.summary().paid_unaided == 1


def test_a_charge_from_before_the_failure_does_not_settle_it(recorder):
    """Last month's subscription.charged shares the subscription id with this month's
    halt. Without the time test every halted subscription reads as already paid."""
    recorder.stop(("sub_1",), "subscription_charged", T0 - timedelta(days=30))

    recorder.record(failure("sub_1", source_type="mandate"), keys=("sub_1",))
    summary = recorder.summary()

    assert (summary.paid_unaided, summary.still_open) == (0, 1)


def test_delivery_order_does_not_matter_only_event_time_does(recorder):
    """Razorpay does not guarantee order: the success can be delivered first."""
    recorder.stop(("pay_B", "order_X"), "order_paid", T0 + timedelta(minutes=5))

    recorder.record(failure("pay_A"), keys=("pay_A", "order_X"))

    assert recorder.summary().paid_unaided == 1


def test_an_unrelated_payment_closes_nothing(recorder):
    recorder.record(failure("pay_A"), keys=("pay_A", "order_X"))

    recorder.stop(("pay_Z", "order_Z"), "payment_captured", T0 + timedelta(hours=1))
    summary = recorder.summary()

    assert (summary.paid_unaided, summary.still_open) == (0, 1)


def test_a_cancellation_ends_the_episode_unpaid(recorder):
    recorder.record(failure("sub_1", source_type="mandate"), keys=("sub_1",))

    recorder.stop(("sub_1",), "subscription_cancelled", T0 + timedelta(days=2))
    summary = recorder.summary()

    assert (summary.paid_unaided, summary.ended_unpaid, summary.still_open) == (0, 1, 0)


def test_the_earliest_outcome_is_the_one_that_counts(recorder):
    recorder.record(failure("sub_1", source_type="mandate"), keys=("sub_1",))

    recorder.stop(("sub_1",), "subscription_cancelled", T0 + timedelta(days=5))
    recorder.stop(("sub_1",), "subscription_charged", T0 + timedelta(days=1))

    assert recorder.summary().paid_unaided == 1


def test_every_episode_is_in_exactly_one_outcome_bucket(recorder):
    recorder.record(failure("pay_1"), keys=("pay_1",))
    recorder.record(failure("pay_2"), keys=("pay_2",))
    recorder.record(failure("pay_3"), keys=("pay_3",))
    recorder.stop(("pay_1",), "payment_captured", T0 + timedelta(hours=1))
    recorder.stop(("pay_2",), "dispute_opened", T0 + timedelta(hours=1))

    s = recorder.summary()

    assert s.paid_unaided + s.ended_unpaid + s.still_open == s.episodes == 3
    assert sum(b.events for b in s.actions) == 3
    assert sum(b.amount_minor for b in s.actions) == s.at_risk_minor


def test_a_second_failure_for_the_same_id_is_one_episode_not_two(recorder):
    recorder.record(failure("sub_1", source_type="mandate"))
    recorder.record(failure("sub_1", source_type="mandate"))

    assert len(recorder.ledger.for_event("sub_1")) == 2      # both decisions kept
    assert recorder.summary().episodes == 1                  # one amount at risk


def test_a_failure_older_than_the_episode_horizon_is_closed_by_the_contract(recorder):
    """Shadow mode decides at the time of arrival. A webhook for a failure from months
    ago must not open an episode the contract says is already over."""
    stale = failure(at=T0 - timedelta(days=60), method="emandate", source_type="mandate",
                    reason="bank_technical_error")

    _, verdict = recorder.record(stale)

    assert "episode.max_days" in [r.rule_id for r in verdict.rules_blocked]


# -- what the summary reports -------------------------------------------------------


def test_contract_refusals_and_integration_bugs_are_reported(recorder):
    recorder.record(failure("mandate_1", source_type="mandate", method="emandate",
                            reason="bank_technical_error"))
    recorder.record(failure("pay_bug", reason="invalid_order_id"))

    s = recorder.summary()

    assert [(b.label, b.events) for b in s.blocked_rules] == [
        ("retry.pre_debit_notification_hours", 1)]
    assert [(b.label, b.events) for b in s.merchant_bugs] == [("invalid_order_id", 1)]


def test_report_says_nothing_was_sent_and_where_the_priors_come_from(recorder):
    recorder.record(failure())

    text = render_text(recorder.summary())

    assert "nothing sent" in text
    assert "synthetic benchmark" in text
    assert "floor" in text


def test_an_empty_ledger_summarises_without_dividing_by_zero(recorder):
    assert recorder.summary().episodes == 0
    assert "failures seen" in render_text(recorder.summary())


# -- the split a live test would use, checked while nobody is treated ---------------


def arm_counts(summary):
    return {a.arm: a for a in summary.arms}


def test_the_arm_is_stored_on_arrival_and_follows_the_customer(recorder):
    """Two failures from one customer must land in one arm, or the holdout is contaminated."""
    recorder.record(failure("pay_1", customer_id="cust_same"))
    recorder.record(failure("pay_2", customer_id="cust_same"))

    arms = arm_counts(recorder.summary())
    expected = assign_arm("cust_same")

    assert (arms[expected].customers, arms[expected].episodes) == (1, 2)
    assert sum(a.episodes for a in arms.values()) == 2


def test_an_assignment_once_made_is_not_rewritten_by_a_later_configuration(tmp_path):
    """Re-randomising after outcomes are visible is how a holdout gets quietly chosen."""
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    ShadowRecorder(Ledger(url=url), clock=VirtualClock(T0)).record(failure("pay_1"))
    before = arm_counts(ShadowRecorder(Ledger(url=url), clock=VirtualClock(T0)).summary())

    everyone_held_out = ShadowRecorder(Ledger(url=url), clock=VirtualClock(T0),
                                       holdout_fraction=1.0, assignment_salt="another")
    everyone_held_out.record(failure("pay_1"))

    after = arm_counts(everyone_held_out.summary())
    assert {a: c.episodes for a, c in after.items()} == {a: c.episodes
                                                         for a, c in before.items()}


def test_no_interval_is_printed_before_both_arms_have_enough_customers(recorder):
    for i in range(MIN_CUSTOMERS_PER_ARM):
        recorder.record(failure(f"pay_{i}"))

    summary = recorder.summary()

    assert summary.aa_check is None
    assert f"needs {MIN_CUSTOMERS_PER_ARM} customers in each arm" in render_text(summary)


def _many(recorder, n, pays):
    """n failures, one customer each; `pays(event)` says who pays an hour later."""
    for i in range(n):
        event = failure(f"pay_{i}")
        recorder.record(event, keys=(event.event_id,))
        if pays(event):
            recorder.stop((event.event_id,), "payment_captured", T0 + timedelta(hours=1))


def test_when_payment_is_unrelated_to_the_arm_the_check_is_consistent_with_zero(bulk_recorder):
    _many(bulk_recorder, 400, pays=lambda e: int(e.event_id.split("_")[1]) % 3 == 0)

    summary = bulk_recorder.summary()

    assert summary.aa_check is not None and summary.aa_check.consistent_with_zero
    assert summary.aa_check.ci_low_pp <= summary.aa_check.difference_pp <= summary.aa_check.ci_high_pp
    assert "consistent with zero" in render_text(summary)


def test_a_split_that_predicts_payment_is_called_out(bulk_recorder):
    """If only one arm ever pays, the assignment is not random with respect to outcome.
    That must be loud: a pilot run on such a split would report lift it did not cause."""
    _many(bulk_recorder, 400, pays=lambda e: assign_arm(e.customer_id) == TREATMENT)

    summary = bulk_recorder.summary()

    assert summary.aa_check.consistent_with_zero is False
    assert "EXCLUDES ZERO" in render_text(summary)


def test_the_same_ledger_always_prints_the_same_interval(bulk_recorder):
    _many(bulk_recorder, 300, pays=lambda e: int(e.event_id.split("_")[1]) % 2 == 0)

    assert bulk_recorder.summary().aa_check == bulk_recorder.summary().aa_check


# -- idempotency survives a restart -------------------------------------------------


def test_a_receipt_is_remembered_by_a_new_process(tmp_path):
    url = f"sqlite:///{tmp_path / 'shadow.db'}"
    first = ShadowRecorder(Ledger(url=url), clock=VirtualClock(T0))
    first.remember("evt_1")

    restarted = ShadowRecorder(Ledger(url=url), clock=VirtualClock(T0))

    assert restarted.is_duplicate("evt_1")
    assert not restarted.is_duplicate("evt_2")
    assert not restarted.is_duplicate(None)


def test_remembering_the_same_receipt_twice_is_harmless(recorder):
    recorder.remember("evt_1")
    recorder.remember("evt_1")

    assert recorder.is_duplicate("evt_1")


# -- webhook payloads carry what the join needs -------------------------------------


def hook(event="payment.failed", payment_id="pay_A", order_id="order_X",
         created=1773120000, envelope_created=None, **entity):
    body = {"id": payment_id, "order_id": order_id, "amount": 249900, "currency": "INR",
            "method": "upi", "created_at": created, **entity}
    if event == "payment.failed":
        body.setdefault("error_reason", "insufficient_funds")
    return json.dumps({"entity": "event", "event": event,
                       "payload": {"payment": {"entity": body}},
                       "created_at": envelope_created or created}).encode()


def test_a_failure_carries_its_own_id_and_the_ids_it_links_to():
    result = normalise(hook())

    assert result.outcome is Outcome.RISK_EVENT
    assert result.correlation_keys == ("pay_A", "order_X")


def test_a_stop_is_timed_by_when_it_happened_not_when_the_payment_was_created():
    result = normalise(hook("payment.captured", payment_id="pay_B",
                            created=1773120000, envelope_created=1773130000))

    assert result.outcome is Outcome.STOP
    assert result.correlation_keys == ("pay_B", "order_X")
    assert result.occurred_at == datetime.fromtimestamp(1773130000, tz=IST)


def test_oversized_or_non_string_ids_are_not_used_as_keys():
    result = normalise(hook(order_id="o" * 200, invoice_id=12345))

    assert result.correlation_keys == ("pay_A",)


# -- the API surface ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(api.WEBHOOK_SECRET_ENV, SECRET)
    monkeypatch.setenv(api.SHADOW_DB_ENV, f"sqlite:///{tmp_path / 'api_shadow.db'}")
    monkeypatch.setenv(api.API_KEY_ENV, API_KEY)
    api._recorder_for.cache_clear()
    yield TestClient(api.app)
    api._recorder_for.cache_clear()


def post(client, body, event_id):
    return client.post("/webhooks/razorpay", content=body, headers={
        "X-Razorpay-Signature": expected_signature(body, SECRET),
        "x-razorpay-event-id": event_id})


def summary(client, key=API_KEY):
    return client.get("/shadow/summary", headers={"Authorization": f"Bearer {key}"})


def test_webhook_in_shadow_mode_reports_what_it_would_do_and_that_it_did_not(client):
    reply = post(client, hook(), "evt_1").json()

    assert reply["outcome"] == "risk_event"
    assert reply["shadow"]["executed"] is False
    assert reply["shadow"]["would_do"]


def test_a_refusal_is_not_reported_to_the_caller_as_a_policy_block(client):
    """NO_ACTION on a merchant bug: the arithmetic refused, the contract stopped nothing."""
    body = hook(error_reason="invalid_order_id", error_source="business")

    shadow = post(client, body, "evt_1").json()["shadow"]

    assert shadow == {"would_do": "NO_ACTION", "blocked": False, "blocked_by": [],
                      "executed": False}


def test_failure_then_capture_on_the_same_order_reads_as_paid_unaided(client):
    post(client, hook(), "evt_1")
    post(client, hook("payment.captured", payment_id="pay_B",
                      envelope_created=1773123600), "evt_2")

    s = summary(client).json()

    assert (s["episodes"], s["paid_unaided"], s["paid_unaided_minor"]) == (1, 1, 249900)
    assert s["executed"] == 0


def test_a_redelivery_after_a_restart_is_still_a_duplicate(client):
    assert post(client, hook(), "evt_same").json()["outcome"] == "risk_event"
    api._recorder_for.cache_clear()          # a new process: nothing held in memory

    assert post(client, hook(), "evt_same").json()["outcome"] == "duplicate"
    assert summary(client).json()["episodes"] == 1


def test_an_unverified_webhook_never_reaches_the_shadow_ledger(client):
    body = hook()
    r = client.post("/webhooks/razorpay", content=body, headers={
        "X-Razorpay-Signature": "deadbeef", "x-razorpay-event-id": "evt_1"})

    assert r.status_code == 401
    assert summary(client).json()["episodes"] == 0


def test_summary_is_refused_without_the_api_key(client):
    assert summary(client, key="wrong").status_code == 401
    assert client.get("/shadow/summary").status_code == 401


def test_summary_fails_closed_when_no_api_key_is_configured(client, monkeypatch):
    monkeypatch.delenv(api.API_KEY_ENV)

    assert summary(client).status_code == 503


def test_summary_says_so_when_shadow_mode_is_off(client, monkeypatch):
    monkeypatch.delenv(api.SHADOW_DB_ENV)

    r = summary(client)

    assert r.status_code == 503 and api.SHADOW_DB_ENV in r.json()["detail"]


def test_health_reports_whether_shadow_mode_is_on(client, monkeypatch):
    assert client.get("/health").json()["shadow_mode"] is True
    monkeypatch.delenv(api.SHADOW_DB_ENV)
    assert client.get("/health").json()["shadow_mode"] is False

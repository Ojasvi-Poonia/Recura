"""Append-only ledger tests (CLAUDE.md sections 4, 10, 13)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.clock import IST
from src.models import (
    ActionResult,
    ActionType,
    BlockedRule,
    Decision,
    FailureClass,
    PolicyVerdict,
    Recoverability,
    RiskEvent,
)
from src.ledger.store import Ledger, make_entry

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=IST)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(url=f"sqlite:///{tmp_path / 'test.db'}")


def mk_event(event_id="e1"):
    return RiskEvent(event_id=event_id, merchant_id="m", customer_id="c",
                     source_type="payment", amount_paise=250000, observed_at=NOW)


def mk_entry(entry_id="en1", event_id="e1", seq=0, **kw):
    return make_entry(entry_id, event_id, kw.pop("arm", "treatment"), seq,
                      mk_event(event_id), clock_time=kw.pop("clock_time", NOW), **kw)


def test_append_and_read_back(ledger):
    ledger.append(mk_entry())
    assert ledger.count() == 1
    assert ledger.for_event("e1")[0]["entry_id"] == "en1"


def test_update_is_refused_by_the_database(ledger):
    """section 13: append-only enforced at the DB LAYER, not by convention."""
    ledger.append(mk_entry())
    with pytest.raises(Exception, match="append-only"):
        with ledger.engine.begin() as conn:
            conn.execute(text("UPDATE ledger_entries SET cost_paise = 999"))
    assert ledger.all_entries()[0]["cost_paise"] == 0


def test_delete_is_refused_by_the_database(ledger):
    ledger.append(mk_entry())
    with pytest.raises(Exception, match="append-only"):
        with ledger.engine.begin() as conn:
            conn.execute(text("DELETE FROM ledger_entries"))
    assert ledger.count() == 1


def test_immutability_survives_reconnection(tmp_path):
    """A fresh process must not get a writable ledger."""
    url = f"sqlite:///{tmp_path / 'persist.db'}"
    Ledger(url=url).append(mk_entry())
    reopened = Ledger(url=url)
    with pytest.raises(Exception, match="append-only"):
        with reopened.engine.begin() as conn:
            conn.execute(text("DELETE FROM ledger_entries"))


def test_entries_are_ordered_by_sequence_number(ledger):
    for seq in (2, 0, 1):
        ledger.append(mk_entry(entry_id=f"en{seq}", seq=seq))
    assert [r["sequence_number"] for r in ledger.for_event("e1")] == [0, 1, 2]


def test_blocked_action_is_recorded_as_evidence(ledger):
    """section 6: a blocked action is EVIDENCE, not an error. It must be in the ledger."""
    verdict = PolicyVerdict(
        allowed=False, rules_evaluated=("contact.max_per_customer_per_7d",),
        rules_blocked=(BlockedRule(rule_id="contact.max_per_customer_per_7d",
                                   reason="Customer already contacted 3 times in 7 days."),),
    )
    ledger.append(mk_entry(policy_verdict=verdict))
    stored = ledger.all_entries()[0]["policy_verdict"]
    assert "contact.max_per_customer_per_7d" in stored
    assert "already contacted" in stored


def test_decision_rationale_and_considered_are_persisted(ledger):
    """section 4: logging the runner-up EVs is how a panel verifies reasoning."""
    decision = Decision(
        event_id="e1", failure_class=FailureClass.FUNDS,
        recoverability=Recoverability.CUSTOMER_RECOVERABLE,
        root_cause="Balance short at attempt time", action=ActionType.RETRY_SCHEDULED,
        params={"scheduled_at": NOW.isoformat()}, expected_value_paise=4200,
        p_recover=0.41, confidence=0.7, rationale="Beat NUDGE on cost", considered=(),
        decided_at=NOW,
    )
    ledger.append(mk_entry(decision=decision))
    stored = ledger.all_entries()[0]["decision"]
    assert "Beat NUDGE on cost" in stored and "considered" in stored


def test_totals_group_by_arm(ledger):
    ledger.append(mk_entry(entry_id="a", event_id="e1", arm="treatment",
                           cost_paise=25, recovered_paise=250000))
    ledger.append(mk_entry(entry_id="b", event_id="e2", arm="holdout",
                           cost_paise=0, recovered_paise=100000))
    totals = ledger.totals()
    assert totals["treatment"]["cost_paise"] == 25
    assert totals["holdout"]["cost_paise"] == 0
    assert totals["holdout"]["recovered_paise"] == 100000


def test_clock_time_must_be_injected():
    """section 12: no implicit wall clock, even here."""
    with pytest.raises(ValueError):
        make_entry("x", "e1", "treatment", 0, mk_event(), clock_time=None)


def test_wall_time_and_clock_time_are_both_recorded(ledger):
    """Virtual time drives logic; wall time proves when the run actually happened."""
    ledger.append(mk_entry(clock_time=NOW + timedelta(days=5)))
    row = ledger.all_entries()[0]
    assert row["clock_time"].startswith("2026-03-07")
    assert row["wall_time"]


def test_action_result_is_persisted(ledger):
    ledger.append(mk_entry(action_result=ActionResult(
        executed=True, action=ActionType.NUDGE, provider_ref="sim_k1",
        cost_paise=25, simulated=True)))
    assert "simulated" in ledger.all_entries()[0]["action_result"]

"""Policy engine tests (CLAUDE.md section 6 and section 13).

Section 13's definition of done: "every rule in policy.yaml has a test proving it
blocks". That is enforced here by `test_every_rule_has_a_blocking_test`, which fails
if a new rule is added without a scenario - the coverage requirement cannot rot.
"""

from datetime import datetime, timedelta

import pytest

from src.clock import IST
from src.models import (
    ActionType,
    Channel,
    Decision,
    FailureClass,
    Recoverability,
)
from src.policy.engine import EpisodeState, evaluate, load_policy, rule_ids

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=IST)  # a Tuesday, inside the contact window

# Rules that modify rather than block. Covered by dedicated tests below.
MODIFYING_RULES = {"contact.quiet_hours"}


def mk_decision(action=ActionType.NUDGE, failure_class=FailureClass.FUNDS,
                recoverability=Recoverability.CUSTOMER_RECOVERABLE, **params) -> Decision:
    base = {"channel": "sms", "template_id": "tpl_recovery_01", "amount_paise": 250000}
    base.update(params)
    return Decision(
        event_id="evt_test", failure_class=failure_class, recoverability=recoverability,
        root_cause="test", action=action, params=base, expected_value_paise=1000,
        p_recover=0.4, confidence=0.8, rationale="test", considered=(), decided_at=NOW,
    )


def mk_state(**kw) -> EpisodeState:
    base = dict(
        event_id="evt_test", episode_started_at=NOW - timedelta(days=1),
        consented_channels=(Channel.SMS, Channel.EMAIL),
    )
    base.update(kw)
    return EpisodeState(**base)


# rule_id -> (decision, state) that MUST be blocked by that rule
BLOCK_SCENARIOS: dict[str, tuple] = {
    "episode.stop_on_payment": (mk_decision(), mk_state(paid=True)),
    "episode.stop_on_late_authorisation": (mk_decision(), mk_state(late_authorised=True)),
    "episode.stop_on_opt_out": (mk_decision(), mk_state(opted_out=True)),
    "episode.stop_on_dispute": (mk_decision(), mk_state(disputed=True)),
    "episode.max_days": (
        mk_decision(), mk_state(episode_started_at=NOW - timedelta(days=22))),
    "retry.forbidden_for_recoverability": (
        mk_decision(recoverability=Recoverability.MERCHANT_CONFIG), mk_state()),
    "retry.forbidden_for_classes": (
        mk_decision(action=ActionType.RETRY_NOW, failure_class=FailureClass.RISK_DECLINE),
        mk_state()),
    "retry.max_attempts_per_episode": (
        mk_decision(action=ActionType.RETRY_NOW), mk_state(attempts_made=3)),
    "retry.max_attempts_per_mandate_cycle": (
        mk_decision(action=ActionType.RETRY_NOW, source_type="mandate"),
        mk_state(attempts_this_mandate_cycle=2)),
    "retry.pre_debit_notification_hours": (
        mk_decision(action=ActionType.RETRY_NOW, source_type="mandate"), mk_state()),
    "contact.max_per_customer_per_7d": (mk_decision(), mk_state(contacts_last_7d=3)),
    "contact.min_hours_between": (
        mk_decision(), mk_state(contacts_last_7d=1, last_contact_at=NOW - timedelta(hours=1))),
    "contact.require_consent": (mk_decision(channel="whatsapp"), mk_state()),
    "contact.require_registered_template": (mk_decision(template_id=None), mk_state()),
    # Read the limits from policy.yaml so scaling the contract cannot silently
    # stop these rules being exercised.
    "merchant.daily_action_budget": (
        mk_decision(),
        mk_state(merchant_actions_today=load_policy()["merchant"]["daily_action_budget"])),
    "merchant.daily_spend_cap_paise": (
        mk_decision(),
        mk_state(merchant_spend_today_paise=load_policy()["merchant"]["daily_spend_cap_paise"],
                 action_cost_paise=1)),
    "escalation.to_human_above_paise": (mk_decision(amount_paise=6_000_000), mk_state()),
    "escalation.after_broken_promise_to_pay": (
        mk_decision(), mk_state(broken_promise_to_pay=True)),
    "escalation.max_per_day": (
        mk_decision(action=ActionType.ESCALATE_HUMAN),
        mk_state(escalations_today=load_policy()["escalation"]["max_per_day"])),
}


@pytest.mark.parametrize("rule_id", sorted(BLOCK_SCENARIOS))
def test_rule_blocks(rule_id):
    decision, state = BLOCK_SCENARIOS[rule_id]
    verdict = evaluate(decision, state, NOW)
    ids = [b.rule_id for b in verdict.rules_blocked]
    assert rule_id in ids, f"{rule_id} did not block; blocked={ids}"
    assert not verdict.allowed


@pytest.mark.parametrize("rule_id", sorted(BLOCK_SCENARIOS))
def test_every_block_carries_a_human_readable_reason(rule_id):
    """section 6: every rule returns pass/block WITH a human-readable reason."""
    decision, state = BLOCK_SCENARIOS[rule_id]
    for blocked in evaluate(decision, state, NOW).rules_blocked:
        assert len(blocked.reason) > 20, f"{blocked.rule_id}: reason too thin"
        assert blocked.reason[0].isupper() and blocked.reason.rstrip().endswith(".")


def test_every_rule_has_a_blocking_test():
    """section 13: the coverage requirement, enforced mechanically."""
    missing = set(rule_ids()) - set(BLOCK_SCENARIOS) - MODIFYING_RULES
    assert not missing, f"rules with no blocking test: {sorted(missing)}"


def test_clean_decision_is_allowed():
    verdict = evaluate(mk_decision(), mk_state(), NOW)
    assert verdict.allowed
    assert verdict.rules_blocked == ()
    assert len(verdict.rules_evaluated) == len(rule_ids())


def test_all_rules_are_always_evaluated():
    """Evaluation is total: a block does not short-circuit the rest of the contract."""
    decision, state = BLOCK_SCENARIOS["episode.stop_on_payment"]
    assert set(evaluate(decision, state, NOW).rules_evaluated) == set(rule_ids())


# --- modifying rule: quiet hours ------------------------------------------

def test_quiet_hours_shifts_rather_than_blocks():
    """A late-evening nudge is deferred to 09:00, not cancelled."""
    late = datetime(2026, 3, 10, 22, 30, tzinfo=IST)
    verdict = evaluate(mk_decision(scheduled_at=late), mk_state(), late)
    assert verdict.allowed
    shifted = verdict.modified_params["scheduled_at"]
    assert shifted.hour == 9 and shifted.day == 11


def test_early_morning_is_also_quiet():
    early = datetime(2026, 3, 10, 6, 0, tzinfo=IST)
    verdict = evaluate(mk_decision(scheduled_at=early), mk_state(), early)
    assert verdict.modified_params["scheduled_at"].hour == 9


def test_rbi_evening_bound_is_enforced_at_1900():
    """The tightened bound: 19:30 is quiet under RBI FPC even though TRAI allows it."""
    at = datetime(2026, 3, 10, 19, 30, tzinfo=IST)
    verdict = evaluate(mk_decision(scheduled_at=at), mk_state(), at)
    assert verdict.modified_params is not None, "19:30 must be treated as quiet"


def test_silent_retry_is_exempt_from_quiet_hours():
    """A gateway retry disturbs nobody, so the contact window does not apply."""
    late = datetime(2026, 3, 10, 23, 0, tzinfo=IST)
    verdict = evaluate(mk_decision(action=ActionType.RETRY_NOW, scheduled_at=late),
                       mk_state(), late)
    assert verdict.allowed and verdict.modified_params is None


# --- targeted behaviours ---------------------------------------------------

def test_escalation_is_permitted_for_merchant_config_bugs():
    """A config bug must not nudge the customer, but escalating IS correct."""
    verdict = evaluate(
        mk_decision(action=ActionType.ESCALATE_HUMAN,
                    recoverability=Recoverability.MERCHANT_CONFIG),
        mk_state(), NOW)
    ids = [b.rule_id for b in verdict.rules_blocked]
    assert "retry.forbidden_for_recoverability" not in ids


def test_switch_method_is_allowed_for_dead_instruments():
    """Switching rails is the remedy for INSTRUMENT_INVALID, not a forbidden retry."""
    verdict = evaluate(
        mk_decision(action=ActionType.SWITCH_METHOD,
                    failure_class=FailureClass.INSTRUMENT_INVALID),
        mk_state(), NOW)
    assert "retry.forbidden_for_classes" not in [b.rule_id for b in verdict.rules_blocked]


def test_pre_debit_notice_satisfied_after_24h():
    d = mk_decision(action=ActionType.RETRY_NOW, source_type="mandate")
    fresh = evaluate(d, mk_state(pre_debit_notice_sent_at=NOW - timedelta(hours=2)), NOW)
    aged = evaluate(d, mk_state(pre_debit_notice_sent_at=NOW - timedelta(hours=25)), NOW)
    assert "retry.pre_debit_notification_hours" in [b.rule_id for b in fresh.rules_blocked]
    assert "retry.pre_debit_notification_hours" not in [b.rule_id for b in aged.rules_blocked]


def test_verdict_is_immutable():
    verdict = evaluate(mk_decision(), mk_state(), NOW)
    with pytest.raises(Exception):
        verdict.allowed = False


def test_policy_yaml_has_no_unknown_top_level_sections():
    assert set(load_policy()) == {"contact", "retry", "episode", "merchant", "escalation"}


def test_evaluation_is_deterministic():
    """section 8: same inputs, same verdict, always."""
    d, s = BLOCK_SCENARIOS["contact.max_per_customer_per_7d"]
    a, b = evaluate(d, s, NOW), evaluate(d, s, NOW)
    assert a == b


def test_escalation_capacity_binds_before_the_budget_does():
    """Human review is finite; without this the optimiser escalates ~40% of events.

    The limit is read from policy.yaml so tuning capacity cannot silently break the test.
    """
    limit = load_policy()["escalation"]["max_per_day"]
    decision = mk_decision(action=ActionType.ESCALATE_HUMAN)
    assert evaluate(decision, mk_state(escalations_today=limit - 1), NOW).allowed
    assert not evaluate(decision, mk_state(escalations_today=limit), NOW).allowed


def test_escalation_capacity_does_not_block_other_actions():
    for action in (ActionType.NUDGE, ActionType.RETRY_NOW):
        verdict = evaluate(mk_decision(action=action), mk_state(escalations_today=999), NOW)
        assert "escalation.max_per_day" not in [b.rule_id for b in verdict.rules_blocked]


def test_broken_promise_still_permits_escalation():
    """The rule routes the case to a human - it must not block that route."""
    verdict = evaluate(mk_decision(action=ActionType.ESCALATE_HUMAN),
                       mk_state(broken_promise_to_pay=True), NOW)
    assert "escalation.after_broken_promise_to_pay" not in [
        b.rule_id for b in verdict.rules_blocked]


def test_intact_promise_does_not_block():
    verdict = evaluate(mk_decision(), mk_state(broken_promise_to_pay=False), NOW)
    assert "escalation.after_broken_promise_to_pay" not in [
        b.rule_id for b in verdict.rules_blocked]


def test_the_mandate_cycle_cap_does_not_apply_to_ordinary_payments():
    """A mandate cycle only exists on a mandate.

    This rule used to fire on every source type, so 4,336 of the batch's blocked
    actions - 52% of the headline "actions blocked by policy" figure - were payment and
    checkout retries stopped by a mandate-cycle limit that has no meaning for them. The
    generic ceiling is `retry.max_attempts_per_episode`; this one is mandate-specific.
    """
    for source in ("payment", "checkout", "invoice"):
        verdict = evaluate(
            mk_decision(action=ActionType.RETRY_NOW, source_type=source),
            mk_state(attempts_this_mandate_cycle=99), NOW)
        blocked = {b.rule_id for b in verdict.rules_blocked}
        assert "retry.max_attempts_per_mandate_cycle" not in blocked, (
            f"mandate-cycle cap fired on a {source} event")

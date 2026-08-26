"""The agent loop (CLAUDE.md sections 1, 11, 12).

Hand-rolled on purpose. No LangChain, no CrewAI, no LangGraph - because we need
deterministic replay and a policy gate the model cannot reach. This file is meant to be
read top to bottom in an interview.

Five steps per decision, exactly as section 1 describes:

    1 TRIAGE    is this recoverable at all?              -> Recoverability
    2 DIAGNOSE  what actually went wrong?                -> FailureClass  (LLM proposes)
    3 DECIDE    which action maximises expected value?   -> argmax EV     (math decides)
    4 GOVERN    am I permitted to do it?                 -> PolicyVerdict (policy vetoes)
    5 LEARN     did it work, and what does that change?  -> Beta update

An episode is 1-5 decisions across up to 21 virtual days, re-observing between each.

It lives at `src/agent.py` rather than in `src/decide/` on purpose: `decide/` holds the
LLM, and tests/test_invariants.py forbids anything in there from importing the policy
engine. The orchestrator is allowed to know about both; the model is not.

Outcomes arrive through an injected `observe` callback. The simulator's hidden latents
therefore never enter `src/` at all (section 9.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np

from src.act.costs import attention_cost_paise, direct_cost_paise
from src.act.provider import Downtime, SimulatedProvider, idempotency_key
from src.clock import IST
from src.decide.bandit import PropensityModel
from src.decide.ev import DecisionContext, choose, score_candidates
from src.decide.llm import ProposalSource, propose_root_cause
from src.decide.providers import LLMProvider, NullProvider
from src.act.messaging import TemplateViolation
from src.act.messaging import render as render_message
from src.ledger.store import Ledger, make_entry
from src.market import Market, get_market
from src.models import (
    ActionType,
    Channel,
    Decision,
    FailureClass,
    PolicyVerdict,
    Recoverability,
    RiskEvent,
)
from src.policy.engine import EpisodeState, evaluate
from src.taxonomy.mapping import classify

MAX_DECISIONS = 5           # section 1: "typically 1-5 decisions"
CONTACT_ACTIONS = {ActionType.NUDGE, ActionType.ESCALATE_HUMAN}
RETRY_ACTIONS = {ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED, ActionType.SWITCH_METHOD}


class Observe(Protocol):
    """Injected by the eval harness. Returns (recovered, opted_out)."""

    def __call__(self, action: ActionType, at: datetime, hours_since_event: float,
                 prior_contacts: int, sequence: int) -> tuple[bool, bool]: ...


@dataclass(frozen=True)
class Diagnosis:
    """Steps 1-2 output. A distribution over failure classes, not a single label."""

    beliefs: tuple[tuple[FailureClass, float], ...]
    recoverability: Recoverability
    root_cause: str
    confidence: float
    mapping: object
    source: ProposalSource

    @property
    def top_class(self) -> FailureClass:
        return max(self.beliefs, key=lambda pair: pair[1])[0]


@dataclass(frozen=True)
class EpisodeResult:
    event_id: str
    arm: str
    recovered_paise: int = 0
    cost_paise: int = 0
    decisions: int = 0
    actions_taken: int = 0
    actions_blocked: int = 0
    refused_negative_ev: int = 0
    escalated: bool = False
    opted_out: bool = False
    contacts: int = 0
    messages_sent: int = 0
    broken_promises: int = 0
    llm_consulted: int = 0
    llm_fallbacks: int = 0
    stop_reason: str = "exhausted"


@dataclass
class Agent:
    """One agent, one merchant, many episodes. Learning persists across episodes."""

    model: PropensityModel = field(default_factory=PropensityModel)
    llm_provider: LLMProvider = field(default_factory=NullProvider)
    executor: SimulatedProvider = field(default_factory=SimulatedProvider)
    ledger: Ledger | None = None
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))
    downtimes: tuple[Downtime, ...] = ()
    explore: bool = True                 # False = posterior mean (no-exploration ablation)
    use_llm: bool = True                 # False = ablation 4: rules only
    use_taxonomy: bool = True            # False = ablation 2: all failures identical
    use_policy: bool = True              # False = ablation 3: no policy gate
    random_chooser: bool = False         # True  = ablation 1: ignore EV entirely
    allow_network: bool = True
    policy: dict | None = None   # None = load policy.yaml
    market: Market = field(default_factory=get_market)

    # Merchant-level daily counters, carried ACROSS episodes.
    # These were previously per-episode, which meant `merchant.daily_action_budget` and
    # `daily_spend_cap_paise` could never bind - a merchant with a 500-action budget was
    # effectively running 7,992 separate budgets of 5. Requires the cohort to be walked
    # in chronological order, which eval/run_batch.py now does.
    _merchant_date: object = None
    _merchant_actions: int = 0
    _merchant_spend_paise: int = 0
    _merchant_escalations: int = 0

    # Promise-to-pay window, in hours, opened when a nudge lands without immediate
    # payment. The customer has effectively said "I will pay" by engaging; if the
    # window closes unpaid that is a BROKEN promise, which policy.yaml treats as
    # grounds for escalation. This is a named Track 03 direction and the policy rule
    # for it existed from day one with nothing to trigger it.
    promise_window_hours: float = 48.0

    # ---- step 2: DIAGNOSE -------------------------------------------------

    def _diagnose(self, event: RiskEvent) -> Diagnosis:
        """Steps 1-2: TRIAGE and DIAGNOSE. Returns a DISTRIBUTION, not a label."""
        mapping = classify(event.razorpay_error, event.source_type)

        if not self.use_taxonomy:  # ablation 2: every failure treated identically
            return Diagnosis(((FailureClass.UNKNOWN, 1.0),),
                             Recoverability.CUSTOMER_RECOVERABLE,
                             "taxonomy disabled", 0.5, mapping, ProposalSource.SKIPPED)

        certain = ((mapping.failure_class, 1.0),)
        if not self.use_llm:       # ablation 4: rules only
            return Diagnosis(certain, mapping.recoverability,
                             mapping.note or mapping.reason, 0.5, mapping,
                             ProposalSource.SKIPPED)

        result = propose_root_cause(event, mapping, provider=self.llm_provider,
                                    allow_network=self.allow_network)
        consulted = result.source in (ProposalSource.FIXTURE, ProposalSource.API)
        beliefs = result.proposal.distribution() if consulted else certain
        return Diagnosis(beliefs, mapping.recoverability, result.proposal.root_cause,
                         result.proposal.confidence, mapping, result.source)

    # ---- step 3: DECIDE ---------------------------------------------------

    def _decide(self, event: RiskEvent, now: datetime, attempts: int,
                refused: frozenset = frozenset(),
                attempts_made_so_far: int = 0) -> tuple[Decision, Diagnosis]:
        dx = self._diagnose(event)
        active = [d for d in self.downtimes if d.affects(event.method, event.bank)]
        ctx = DecisionContext(
            event=event, failure_class=dx.top_class, recoverability=dx.recoverability,
            mapping=dx.mapping, now=now, downtime_active=bool(active),
            downtime_clears_in_h=2.0 if active else 0.0, attempts_made=attempts,
            class_beliefs=dx.beliefs, market=self.market,
            steps_remaining=max(1, MAX_DECISIONS - attempts_made_so_far),
        )
        considered = score_candidates(ctx, self.model, self.rng, explore=self.explore)
        # Do not re-propose what the contract has already refused THIS episode.
        #
        # A blocked action yields no outcome, so the bandit learns nothing from it and
        # would happily propose the same forbidden retry every step. Left unfixed the
        # agent degenerates into a retry bot - CLAUDE.md section 12's explicit anti-goal -
        # burning its whole decision budget on requests the policy already denied. The
        # first refusal is still logged as evidence (section 6); only the repeats stop.
        if refused:
            survivors = [c for c in considered if c.action not in refused]
            if survivors:
                considered = survivors
        if self.random_chooser:
            # Ablation 1: pick uniformly at random. Every candidate is still priced and
            # logged, so the ledger shows exactly what EV was thrown away.
            best = considered[int(self.rng.integers(len(considered)))]
        else:
            best = choose(considered)
        runner_up = max((c for c in considered if c is not best),
                        key=lambda c: c.expected_value_paise, default=None)

        rationale = (
            f"EV {best.expected_value_paise} paise beats "
            f"{runner_up.action.value} at {runner_up.expected_value_paise}"
            if runner_up else "only candidate"
        )
        decision = Decision(
            event_id=event.event_id, failure_class=dx.top_class,
            recoverability=dx.recoverability, root_cause=dx.root_cause[:200],
            action=best.action, params=best.params,
            expected_value_paise=best.expected_value_paise, p_recover=best.p_recover,
            confidence=dx.confidence, rationale=rationale,
            considered=tuple(considered), decided_at=now,
            llm_fallback_used=(dx.source is ProposalSource.FALLBACK),
        )
        return decision, dx

    # ---- the loop ---------------------------------------------------------

    def run_episode(self, event: RiskEvent, arm: str, observe: Observe) -> EpisodeResult:
        started = event.observed_at
        margin_amount = event.amount_paise

        # HOLDOUT: no decision, no action, no contact - but observed over the SAME
        # horizon, with the SAME number of opportunities as a treated episode.
        #
        # This matters more than it looks. Observing the control arm once while the
        # treatment arm gets up to MAX_DECISIONS re-observations across 21 days hands
        # treatment extra draws on the same underlying probability, and the harness
        # reports "lift" that is pure repeated sampling. Our placebo negative control
        # caught exactly that: with every action made inert, the measured lift was
        # still +18.57pp. A customer left completely alone for three weeks also gets
        # several natural chances to pay; the counterfactual has to include them.
        if arm == "holdout":
            when = started
            for seq in range(MAX_DECISIONS):
                got_it, _ = observe(ActionType.NO_ACTION, when,
                                    (when - started).total_seconds() / 3600.0, 0, seq)
                self._log(event, arm, seq, None, None, 0,
                          margin_amount if got_it else 0, when)
                if got_it:
                    return EpisodeResult(event.event_id, arm,
                                         recovered_paise=margin_amount,
                                         decisions=seq + 1,
                                         stop_reason="recovered_unprompted")
                when = when + timedelta(hours=30)
                if (when - started).days > 21:
                    break
            return EpisodeResult(event.event_id, arm, decisions=seq + 1,
                                 stop_reason="holdout_observed")

        now = started
        history = event.customer_history
        attempts = contacts = 0
        messages_sent = 0
        promise_due_at: datetime | None = None
        broken_promises = 0
        refused_actions: set[ActionType] = set()
        cost = recovered = 0
        blocked = refused = taken = 0
        llm_consulted = llm_fallbacks = 0
        escalated = opted_out = False
        stop_reason = "exhausted"

        for seq in range(MAX_DECISIONS):
            # Re-observe: contact count is the one observable the loop itself changes.
            observed = event.model_copy(update={
                "customer_history": history.model_copy(
                    update={"contacts_last_7d": contacts})
            })

            self._roll_merchant_day(now)
            decision, dx = self._decide(observed, now, attempts,
                                        frozenset(refused_actions), seq)
            if decision.llm_fallback_used:
                llm_fallbacks += 1
            if self.use_llm:
                llm_consulted += 1

            state = EpisodeState(
                event_id=event.event_id, episode_started_at=started,
                attempts_made=attempts, attempts_this_mandate_cycle=attempts,
                contacts_last_7d=contacts,
                last_contact_at=self._last_contact_at(now, contacts),
                consented_channels=history.consented_channels, opted_out=opted_out,
                merchant_actions_today=self._merchant_actions,
                merchant_spend_today_paise=self._merchant_spend_paise,
                escalations_today=self._merchant_escalations,
                broken_promise_to_pay=(
                    promise_due_at is not None and now > promise_due_at),
                action_cost_paise=self._cost_of(decision, contacts),
            )
            # ---- step 4: GOVERN --------------------------------------------
            if self.use_policy:
                verdict = evaluate(decision, state, now, self.policy)
            else:
                # Ablation 3: no gate. Expect more actions and worse cost-per-recovery.
                verdict = PolicyVerdict(allowed=True, rules_evaluated=())

            # Quiet hours SHIFT rather than block; honour the modified send time.
            scheduled = self._scheduled_at(decision, verdict, now)

            if decision.action is ActionType.NO_ACTION:
                # Refusing is a DECISION, not an exit. Two things follow from that:
                #
                # 1. The money does not vanish. A customer we chose not to chase may
                #    still pay on their own, and that recovery belongs to the treatment
                #    arm. Recording zero here biased the comparison against ourselves.
                # 2. We still observe the outcome, so the NO_ACTION cell keeps learning.
                #    Without this its posterior sits at the Beta(1,1) mean of 0.5
                #    forever - a baseline far more optimistic than reality, against
                #    which every real action looks like a bad bet. That is what made
                #    the agent refuse 4,128 times.
                refused += 1
                got_it, _ = observe(ActionType.NO_ACTION, now,
                                    (now - started).total_seconds() / 3600.0,
                                    contacts, seq)
                self.model.update_distribution(dx.beliefs, ActionType.NO_ACTION, got_it)
                recovered_now = margin_amount if got_it else 0
                recovered += recovered_now
                self._log(event, arm, seq, decision, verdict, 0, recovered_now, now)
                if got_it:
                    stop_reason = "recovered_unprompted"
                    break
                stop_reason = "refused_negative_ev"
                now = now + timedelta(hours=30)
                if (now - started).days > 21:
                    stop_reason = "episode_expired"
                    break
                continue

            if not verdict.allowed:
                blocked += 1
                refused_actions.add(decision.action)
                # The contract stopped US, not the customer. They still have their own
                # chance to pay during this window, and the counterfactual arm gets one
                # at every step - so treatment must too, or the comparison is unfair.
                got_it, _ = observe(ActionType.NO_ACTION, now,
                                    (now - started).total_seconds() / 3600.0,
                                    contacts, seq)
                self.model.update_distribution(dx.beliefs, ActionType.NO_ACTION, got_it)
                recovered_now = margin_amount if got_it else 0
                recovered += recovered_now
                self._log(event, arm, seq, decision, verdict, 0, recovered_now, now)
                if got_it:
                    stop_reason = "recovered_unprompted"
                    break
                # A block is evidence, not a crash. Try again after a cooling-off.
                now = now + timedelta(hours=30)
                if (now - started).days > 21:
                    stop_reason = "episode_expired"
                    break
                continue

            # ---- step 5 (act) ----------------------------------------------
            action_cost = self._cost_of(decision, contacts)
            if decision.action is ActionType.NUDGE:
                # Render the ACTUAL copy through the DLT template registry. If no
                # registered template covers this diagnosis, no message goes out -
                # we do not improvise copy to fill a gap (src/act/messaging.py).
                rendered = self._render_message(event, decision, history)
                if rendered is not None:
                    self.executor.send_nudge(
                        event.event_id, Channel(decision.params.get("channel", "sms")),
                        rendered.template_key, rendered.language,
                        idempotency_key(event.event_id, seq, decision.action),
                    )
                    messages_sent += 1

            cost += action_cost
            taken += 1
            self._merchant_actions += 1
            self._merchant_spend_paise += action_cost
            if self._is_customer_contact(decision):
                contacts += 1
            if decision.action in RETRY_ACTIONS:
                attempts += 1
            if decision.action is ActionType.ESCALATE_HUMAN:
                escalated = True
                self._merchant_escalations += 1

            hours_out = (scheduled - started).total_seconds() / 3600.0
            got_it, quit = observe(decision.action, scheduled, hours_out,
                                   max(0, contacts - 1), seq)

            # Promise-to-pay: a delivered nudge that did not convert opens a window.
            if decision.action is ActionType.NUDGE:
                if got_it:
                    promise_due_at = None
                else:
                    if promise_due_at is not None and scheduled > promise_due_at:
                        broken_promises += 1
                    promise_due_at = scheduled + timedelta(hours=self.promise_window_hours)

            # ---- step 5: LEARN ---------------------------------------------
            self.model.update_distribution(dx.beliefs, decision.action, got_it)

            recovered_now = margin_amount if got_it else 0
            recovered += recovered_now
            self._log(event, arm, seq, decision, verdict, action_cost, recovered_now,
                      scheduled)

            if got_it:
                stop_reason = "recovered"
                break
            if quit:
                opted_out = True
                stop_reason = "opted_out"
                break

            now = max(scheduled, now) + timedelta(hours=6)
            if (now - started).days > 21:
                stop_reason = "episode_expired"
                break

        return EpisodeResult(
            event.event_id, arm, recovered, cost, seq + 1, taken, blocked, refused,
            escalated, opted_out, contacts, messages_sent, broken_promises,
            llm_consulted, llm_fallbacks, stop_reason,
        )

    # ---- helpers ----------------------------------------------------------

    def _roll_merchant_day(self, now: datetime) -> None:
        """Reset the merchant's daily counters when the virtual date advances."""
        day = now.date()
        if self._merchant_date != day:
            self._merchant_date = day
            self._merchant_actions = 0
            self._merchant_spend_paise = 0
            self._merchant_escalations = 0

    def _render_message(self, event: RiskEvent, decision: Decision,
                        history: CustomerHistory):
        """Fill a registered template. Returns None when none applies - never guesses."""
        try:
            return render_message(
                decision.failure_class,
                history.language,
                Channel(decision.params.get("channel", "sms")),
                {
                    "name": "Customer",
                    "amount": self.market.money(event.amount_paise),
                    "merchant": event.merchant_id,
                    "link": f"https://rzp.io/i/{event.event_id}",
                    "rail": decision.params.get("suggested_rail", "UPI"),
                    "days": str(event.days_overdue(decision.decided_at)),
                },
                source_type=event.source_type,
            )
        except TemplateViolation:
            return None

    @staticmethod
    def _is_customer_contact(decision: Decision) -> bool:
        """Does this action spend the CUSTOMER's patience?

        A nudge always does. An escalation depends on who it routes to: escalating a
        merchant integration bug goes to the merchant's own engineers and never reaches
        the customer, so it must not consume their contact budget. Escalating a genuine
        customer-recoverable case does reach them, via a human agent.
        """
        if decision.action is ActionType.NUDGE:
            return True
        if decision.action is ActionType.ESCALATE_HUMAN:
            return decision.recoverability is Recoverability.CUSTOMER_RECOVERABLE
        return False

    def _cost_of(self, decision: Decision, contacts: int) -> int:
        channel = decision.params.get("channel")
        direct = direct_cost_paise(decision.action,
                                   Channel(channel) if channel else None)
        attention = (attention_cost_paise(decision.action, contacts)
                     if self._is_customer_contact(decision) else 0)
        return direct + attention

    @staticmethod
    def _last_contact_at(now: datetime, contacts: int) -> datetime | None:
        # Contacts are spaced by the loop; policy re-checks the 24h gap itself.
        return None if contacts == 0 else now - timedelta(hours=25)

    @staticmethod
    def _scheduled_at(decision: Decision, verdict, now: datetime) -> datetime:
        raw = (verdict.modified_params or {}).get("scheduled_at") \
            or decision.params.get("scheduled_at") or now
        if isinstance(raw, str):
            raw = datetime.fromisoformat(raw)
        return raw.astimezone(IST) if raw.tzinfo else raw.replace(tzinfo=IST)

    def _log(self, event, arm, seq, decision, verdict, cost, recovered, clock_time):
        if self.ledger is None:
            return
        self.ledger.append(make_entry(
            entry_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{event.event_id}:{seq}").hex,
            event_id=event.event_id, arm=arm, sequence_number=seq,
            observed_state=event, decision=decision, policy_verdict=verdict,
            cost_paise=cost, recovered_paise=recovered, clock_time=clock_time,
        ))

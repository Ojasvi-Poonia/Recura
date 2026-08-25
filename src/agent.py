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
from typing import Callable, Protocol

import numpy as np

from src.act.costs import attention_cost_paise, direct_cost_paise
from src.act.provider import Downtime, SimulatedProvider, idempotency_key
from src.clock import IST
from src.decide.bandit import PropensityModel
from src.decide.ev import DecisionContext, choose, score_candidates
from src.decide.llm import ProposalSource, propose_root_cause
from src.decide.providers import LLMProvider, NullProvider
from src.ledger.store import Ledger, make_entry
from src.models import (
    ActionType,
    Channel,
    CustomerHistory,
    Decision,
    FailureClass,
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
    use_llm: bool = True                 # False = the rules-only ablation
    use_taxonomy: bool = True            # False = the no-taxonomy ablation
    allow_network: bool = True

    # ---- step 2: DIAGNOSE -------------------------------------------------

    def _diagnose(self, event: RiskEvent) -> tuple[FailureClass, Recoverability, str, float,
                                                   object, ProposalSource]:
        mapping = classify(event.razorpay_error)
        if not self.use_taxonomy:
            # Ablation: every failure treated identically.
            return (FailureClass.UNKNOWN, Recoverability.CUSTOMER_RECOVERABLE,
                    "taxonomy disabled", 0.5, mapping, ProposalSource.SKIPPED)

        if not self.use_llm:
            return (mapping.failure_class, mapping.recoverability,
                    mapping.note or mapping.reason, 0.5, mapping, ProposalSource.SKIPPED)

        result = propose_root_cause(event, mapping, provider=self.llm_provider,
                                    allow_network=self.allow_network)
        proposal = result.proposal

        # The LLM is consulted only where the taxonomy is blind, so when it returns a
        # confident non-UNKNOWN class it is strictly more informative than the lookup.
        consulted = result.source in (ProposalSource.FIXTURE, ProposalSource.API)
        if (consulted and proposal.suspected_failure_class is not FailureClass.UNKNOWN
                and proposal.confidence >= 0.5):
            failure_class = proposal.suspected_failure_class
        else:
            failure_class = mapping.failure_class

        return (failure_class, mapping.recoverability, proposal.root_cause,
                proposal.confidence, mapping, result.source)

    # ---- step 3: DECIDE ---------------------------------------------------

    def _decide(self, event: RiskEvent, now: datetime, attempts: int) -> Decision:
        failure_class, recoverability, root_cause, confidence, mapping, source = \
            self._diagnose(event)

        active = [d for d in self.downtimes if d.affects(event.method, event.bank)]
        ctx = DecisionContext(
            event=event, failure_class=failure_class, recoverability=recoverability,
            mapping=mapping, now=now, downtime_active=bool(active),
            downtime_clears_in_h=2.0 if active else 0.0, attempts_made=attempts,
        )
        considered = score_candidates(ctx, self.model, self.rng, explore=self.explore)
        best = choose(considered)
        runner_up = max((c for c in considered if c is not best),
                        key=lambda c: c.expected_value_paise, default=None)

        rationale = (
            f"EV {best.expected_value_paise} paise beats "
            f"{runner_up.action.value} at {runner_up.expected_value_paise}"
            if runner_up else "only candidate"
        )
        return Decision(
            event_id=event.event_id, failure_class=failure_class,
            recoverability=recoverability, root_cause=root_cause[:200],
            action=best.action, params=best.params,
            expected_value_paise=best.expected_value_paise, p_recover=best.p_recover,
            confidence=confidence, rationale=rationale,
            considered=tuple(considered), decided_at=now,
            llm_fallback_used=(source is ProposalSource.FALLBACK),
        )

    # ---- the loop ---------------------------------------------------------

    def run_episode(self, event: RiskEvent, arm: str, observe: Observe) -> EpisodeResult:
        started = event.observed_at
        margin_amount = event.amount_paise

        # HOLDOUT: observe only. No decision, no action, no contact. This is the
        # counterfactual the headline number is measured against (section 8).
        if arm == "holdout":
            recovered, _ = observe(ActionType.NO_ACTION, started, 0.0, 0, 0)
            self._log(event, arm, 0, None, None, 0,
                      margin_amount if recovered else 0, started)
            return EpisodeResult(event.event_id, arm,
                                 recovered_paise=margin_amount if recovered else 0,
                                 stop_reason="holdout_observed")

        now = started
        history = event.customer_history
        attempts = contacts = 0
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

            decision = self._decide(observed, now, attempts)
            if decision.llm_fallback_used:
                llm_fallbacks += 1
            if self.use_llm:
                llm_consulted += 1

            # ---- step 4: GOVERN --------------------------------------------
            state = EpisodeState(
                event_id=event.event_id, episode_started_at=started,
                attempts_made=attempts, attempts_this_mandate_cycle=attempts,
                contacts_last_7d=contacts,
                last_contact_at=self._last_contact_at(now, contacts),
                consented_channels=history.consented_channels, opted_out=opted_out,
                merchant_actions_today=taken,
                merchant_spend_today_paise=cost,
                action_cost_paise=self._cost_of(decision, contacts),
            )
            verdict = evaluate(decision, state, now)

            # Quiet hours SHIFT rather than block; honour the modified send time.
            scheduled = self._scheduled_at(decision, verdict, now)

            if decision.action is ActionType.NO_ACTION:
                refused += 1
                self._log(event, arm, seq, decision, verdict, 0, 0, now)
                stop_reason = "refused_negative_ev"
                break

            if not verdict.allowed:
                blocked += 1
                self._log(event, arm, seq, decision, verdict, 0, 0, now)
                # A block is evidence, not a crash. Try again after a cooling-off.
                now = now + timedelta(hours=24)
                if (now - started).days > 21:
                    stop_reason = "episode_expired"
                    break
                continue

            # ---- step 5 (act) ----------------------------------------------
            action_cost = self._cost_of(decision, contacts)
            if decision.action is ActionType.NUDGE:
                self.executor.send_nudge(
                    event.event_id, Channel(decision.params.get("channel", "sms")),
                    decision.params.get("template_id", "tpl"), history.language,
                    idempotency_key(event.event_id, seq, decision.action),
                )

            cost += action_cost
            taken += 1
            if self._is_customer_contact(decision):
                contacts += 1
            if decision.action in RETRY_ACTIONS:
                attempts += 1
            if decision.action is ActionType.ESCALATE_HUMAN:
                escalated = True

            hours_out = (scheduled - started).total_seconds() / 3600.0
            got_it, quit = observe(decision.action, scheduled, hours_out,
                                   max(0, contacts - 1), seq)

            # ---- step 5: LEARN ---------------------------------------------
            self.model.update(decision.failure_class, decision.action, got_it)

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
            escalated, opted_out, contacts, llm_consulted, llm_fallbacks, stop_reason,
        )

    # ---- helpers ----------------------------------------------------------

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

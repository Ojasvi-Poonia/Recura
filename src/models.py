"""Recura domain model (CLAUDE.md section 4).

Money is ALWAYS integer paise. Never float. Rupees appear only at display time.
All models are frozen: a Decision or LedgerEntry is a value, never mutated.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PAISE_PER_RUPEE = 100


class _Frozen(BaseModel):
    """Immutable base (rules/common/coding-style.md: never mutate in place)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class FailureClass(StrEnum):
    """What actually went wrong. Diagnosis, not triage."""

    TRANSIENT_INFRA = "TRANSIENT_INFRA"      # bank/gateway/network down -> retry soon
    FUNDS = "FUNDS"                          # no balance -> retry at replenishment
    AUTH_ABANDON = "AUTH_ABANDON"            # OTP/3DS drop -> re-engage fast
    INSTRUMENT_INVALID = "INSTRUMENT_INVALID"  # dead card/mandate -> switch method
    RISK_DECLINE = "RISK_DECLINE"            # risk decline -> do NOT retry
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"        # limit hit -> next cycle or lower amount
    UNKNOWN = "UNKNOWN"                      # conservative handling


class Recoverability(StrEnum):
    """Triage dimension (CLAUDE.md section 1, step 1). Orthogonal to FailureClass.

    Razorpay's published reason list mixes genuine customer-side payment failures
    with merchant integration bugs (`invalid_order_id`, `live_mode_not_enabled`).
    Nudging a customer because the merchant shipped a malformed request would be
    wrong, so triage is modelled separately from diagnosis.
    """

    CUSTOMER_RECOVERABLE = "CUSTOMER_RECOVERABLE"  # a customer action may recover it
    MERCHANT_CONFIG = "MERCHANT_CONFIG"            # integration bug -> escalate, never nudge
    TERMINAL = "TERMINAL"                          # nothing to recover -> close episode


class ActionType(StrEnum):
    NO_ACTION = "NO_ACTION"
    RETRY_NOW = "RETRY_NOW"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"   # params: scheduled_at
    SWITCH_METHOD = "SWITCH_METHOD"       # params: suggested_rail
    NUDGE = "NUDGE"                       # params: channel, template_id, language, scheduled_at
    ESCALATE_HUMAN = "ESCALATE_HUMAN"     # params: escalation_reason


class Channel(StrEnum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    VOICE = "voice"


Arm = Literal["treatment", "holdout"]
SourceType = Literal["payment", "checkout", "mandate", "invoice"]


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------


class ErrorObject(_Frozen):
    """Razorpay's error object, verbatim shape (CLAUDE.md section 7)."""

    code: str | None = None
    description: str | None = None
    source: str | None = None
    step: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CustomerHistory(_Frozen):
    """OBSERVABLE fields only.

    CLAUDE.md section 9.1: the agent may never read a latent field. Everything here
    is something a real merchant would already have in their own database.
    """

    prior_failed_attempts: int = 0
    prior_recoveries: int = 0
    prior_payments_total: int = 0
    contacts_last_7d: int = 0
    last_contact_at: datetime | None = None
    # Hours (IST, 0-23) at which this customer has previously paid successfully.
    successful_payment_hours: tuple[int, ...] = ()
    consented_channels: tuple[Channel, ...] = ()
    opted_out: bool = False
    language: str = "en"


class MerchantContext(_Frozen):
    merchant_id: str
    margin_bps: int = 3000  # gross margin in basis points; 3000 = 30%
    actions_today: int = 0
    spend_today_paise: int = 0


class RiskEvent(_Frozen):
    """One rupee-at-risk observation, normalised from any upstream source."""

    event_id: str
    merchant_id: str
    customer_id: str
    source_type: SourceType
    amount_paise: int = Field(ge=0)
    currency: str = "INR"
    observed_at: datetime
    razorpay_error: ErrorObject | None = None
    method: str | None = None
    bank: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    customer_history: CustomerHistory = Field(default_factory=CustomerHistory)
    merchant_context: MerchantContext | None = None


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------


class CandidateEV(_Frozen):
    """One option the agent weighed. Logged even when rejected."""

    action: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    p_recover: float = Field(ge=0.0, le=1.0)
    gross_value_paise: int
    direct_cost_paise: int
    attention_cost_paise: int
    expected_value_paise: int


class Decision(_Frozen):
    event_id: str
    failure_class: FailureClass
    recoverability: Recoverability
    root_cause: str = Field(max_length=200)  # LLM-authored
    action: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    expected_value_paise: int
    p_recover: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    considered: tuple[CandidateEV, ...]  # CLAUDE.md section 4: never optional
    decided_at: datetime
    llm_fallback_used: bool = False


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class BlockedRule(_Frozen):
    rule_id: str
    reason: str  # human-readable, shown in the ledger


class PolicyVerdict(_Frozen):
    allowed: bool
    rules_evaluated: tuple[str, ...]
    rules_blocked: tuple[BlockedRule, ...] = ()
    modified_params: dict[str, Any] | None = None  # e.g. quiet hours shifted scheduled_at


# --------------------------------------------------------------------------
# Action + ledger
# --------------------------------------------------------------------------


class ActionResult(_Frozen):
    executed: bool
    action: ActionType
    provider_ref: str | None = None
    cost_paise: int = 0
    error: str | None = None
    simulated: bool = True  # CLAUDE.md section 2: nothing real is ever sent


class LedgerEntry(_Frozen):
    entry_id: str
    event_id: str
    arm: Arm
    sequence_number: int
    observed_state: RiskEvent
    decision: Decision | None = None
    policy_verdict: PolicyVerdict | None = None
    action_result: ActionResult | None = None
    cost_paise: int = 0
    recovered_paise: int = 0
    clock_time: datetime
    wall_time: datetime


def rupees(paise: int) -> str:
    """Display helper. The ONLY place paise become rupees."""
    return f"₹{paise / PAISE_PER_RUPEE:,.2f}"

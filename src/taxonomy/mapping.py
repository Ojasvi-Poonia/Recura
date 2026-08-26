"""Razorpay `reason` -> FailureClass mapping (CLAUDE.md section 7).

Ground truth is `data/razorpay_error_reasons.csv`, transcribed from Razorpay's
published error documentation (checked 2026-08-26):
  - https://razorpay.com/docs/errors/payments/list/          (bad request + gateway)
  - https://razorpay.com/docs/payments/recurring-payments/emandate/errors/
  - https://razorpay.com/docs/errors/payment-error-parameters  (source values)

The CSV is THEIR data and is not editable by us. This file is OUR judgment:
one row per real reason, each with a class, a triage verdict, and a rationale
where the call is not obvious. Nothing here invents a reason code.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from pathlib import Path

from src.models import ErrorObject, FailureClass, Recoverability

FC = FailureClass
RC = Recoverability

REASONS_CSV = Path(__file__).resolve().parents[2] / "data" / "razorpay_error_reasons.csv"


@dataclass(frozen=True)
class ReasonMapping:
    reason: str
    failure_class: FailureClass
    recoverability: Recoverability
    min_retry_delay_hours: float | None = None  # only where Razorpay states one
    note: str = ""


# (reason, class, recoverability, min_retry_delay_hours, note)
_TABLE: tuple[tuple, ...] = (
    # -- TRANSIENT_INFRA: the rail was down or busy. The instrument is fine. ----
    ("already_declined", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, 24.0,
     "NPCI blocks duplicate mandate retries; Razorpay states a hard 24h cooldown."),
    ("authorisation_declined_by_psp", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Ambiguous: PSP downtime or a bad VPA. Classed transient because Razorpay's "
     "first suggested remedy is retry, with PSP switch only on recurrence."),
    ("bank_cutoff_in_progress", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Scheduled CBS cutoff - the single most timing-predictable failure we see."),
    ("bank_not_available", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("bank_technical_error", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("collect_request_pending", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Not a failure yet - an in-flight collect. Re-observe before acting."),
    ("deemed_transaction", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "NPCI 'deemed' state pending reconciliation; may settle without us."),
    ("duplicate_request", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, 0.5,
     "Gateway duplicate guard; Razorpay states a 30-minute cooldown."),
    ("duplicate_rrn_found", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("gateway_technical_error", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("invalid_response_from_gateway", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("issuer_technical_error", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("payment_declined_due_to_high_traffic", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Load shedding - retry timing matters more here than anywhere else."),
    ("payment_mandate_not_active", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Mandate registered but not yet activated bank-side. Wait, do not re-register."),
    ("payment_pending", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Not terminal. Re-observe; may resolve to success (see late-authorisation)."),
    ("payment_pending_approval", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Maker-checker flow at the payer's org. Contacting the customer will not help."),
    ("psp_app_not_available", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("psp_not_available", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("request_timed_out", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("server_error", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Razorpay-side. Retry is nearly free and nearly always correct."),
    ("upi_app_technical_error", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("verification_failed", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "Razorpay explicitly calls this temporary."),
    ("vpa_resolution_failed", FC.TRANSIENT_INFRA, RC.CUSTOMER_RECOVERABLE, None,
     "UPI network resolution failure, not a wrong VPA - contrast invalid_vpa."),

    # -- FUNDS: money is not there yet. Timing is the whole game. --------------
    ("insufficient_funds", FC.FUNDS, RC.CUSTOMER_RECOVERABLE, None,
     "The canonical salary-cycle case. Retry timing dominates channel choice."),
    ("funds_blocked_by_mandate", FC.FUNDS, RC.CUSTOMER_RECOVERABLE, None,
     "Balance exists but is lien-marked by another mandate. Same remedy: wait."),

    # -- AUTH_ABANDON: the customer was present and did not finish. ------------
    ("authentication_failed", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("card_number_invalid", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None,
     "Data-entry slip, not a dead card - the customer can simply retype."),
    ("incorrect_atm_pin", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_card_details", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_card_expiry_date", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_cardholder_name", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_cvv", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_otp", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_pin", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("invalid_vpa", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None,
     "Mistyped VPA. Contrast vpa_resolution_failed (network) and "
     "transaction_on_vpa_restricted (PSP block)."),
    ("mandate_creation_expired", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("mandate_creation_timeout", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("otp_attempts_exceeded", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None,
     "Issuer may temporarily lock the card; retry too fast and we burn an attempt."),
    ("otp_expired", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("payment_cancelled", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None,
     "Deliberate abandonment. High intent signal - but also high annoyance risk."),
    ("payment_collect_request_expired", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("payment_session_expired", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("payment_timed_out", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("pin_attempts_exceeded", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("pin_not_set", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("reqauth_mandate_not_acknowledged", FC.AUTH_ABANDON, RC.CUSTOMER_RECOVERABLE, None, ""),

    # -- INSTRUMENT_INVALID: this rail is dead. Retrying it is waste. ----------
    ("bank_account_invalid", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("bank_account_validation_failed", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("card_declined", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None,
     "AMBIGUOUS: a bare issuer decline may hide a risk decline. Classed as "
     "instrument because Razorpay's remedy is 'different card', but this is the "
     "likeliest mis-classification in the table - see RESULTS.md failure cases."),
    ("card_expired", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("card_not_enrolled", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("card_type_invalid", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("credit_limit_expired", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("credit_limit_inactive", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("credit_limit_not_approved", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("credit_not_permitted", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("debit_declined", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("debit_instrument_blocked", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None,
     "Blocked by issuer or by the customer themselves. Never retry the same rail."),
    ("debit_instrument_inactive", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("emi_plan_unavailable", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("incorrect_ifsc", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None,
     "Stored IFSC went stale (bank merger). Requires mandate re-registration."),
    ("international_transaction_not_allowed", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("invalid_device", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("joint_account_not_allowed", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None,
     "Banks generally allow mandates on sole-ownership accounts only."),
    ("mandate_creation_declined", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("mandate_not_active", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None,
     "Mandate revoked by customer or bank. Re-registration, never a silent retry."),
    ("psp_app_not_supported", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("psp_not_registered", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("transaction_on_vpa_restricted", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("upi_autopay_not_supported_on_psp", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("user_not_registered_for_netbanking", FC.INSTRUMENT_INVALID, RC.CUSTOMER_RECOVERABLE, None, ""),

    # -- RISK_DECLINE: do not retry. Inform or escalate. ----------------------
    ("compliance_violation", FC.RISK_DECLINE, RC.MERCHANT_CONFIG, None,
     "Compliance block - a human must look, never an automated nudge."),
    ("payment_amount_tampered", FC.RISK_DECLINE, RC.MERCHANT_CONFIG, None,
     "Integrity failure. Escalate; retrying would be actively dangerous."),
    ("payment_risk_check_failed", FC.RISK_DECLINE, RC.CUSTOMER_RECOVERABLE, None,
     "Razorpay/gateway/issuer risk decline. policy.yaml forbids retry for this class."),
    ("user_not_eligible", FC.RISK_DECLINE, RC.CUSTOMER_RECOVERABLE, None,
     "Credit eligibility decline. Not retryable, but a method switch may work."),

    # -- LIMIT_EXCEEDED: right customer, wrong amount or wrong cycle. ---------
    ("amount_less_than_minimum_amount", FC.LIMIT_EXCEEDED, RC.MERCHANT_CONFIG, None,
     "A floor, not a ceiling - but the same 'amount out of band' remedy."),
    ("credit_limit_exceeded", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("emi_greater_than_max_amount", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("mcc_amount_limit_exceeded", FC.LIMIT_EXCEEDED, RC.MERCHANT_CONFIG, None,
     "Category limit set against the merchant, not the customer."),
    ("transaction_daily_count_exceeded", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, 24.0,
     "Resets at the bank's day boundary - schedule, do not retry now."),
    ("transaction_daily_limit_exceeded", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, 24.0,
     "Razorpay states 'wait 24 hours' explicitly."),
    ("transaction_frequency_limit_exceeded", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, 24.0,
     "NPCI per-day frequency cap. Another attempt today cannot succeed."),
    ("transaction_limit_exceeded", FC.LIMIT_EXCEEDED, RC.CUSTOMER_RECOVERABLE, None,
     "Per-transaction cap. Lowering the amount or splitting can work."),

    # -- UNKNOWN but customer-side: genuinely opaque declines. ----------------
    ("credit_failed", FC.UNKNOWN, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("mandate_creation_failed", FC.UNKNOWN, RC.CUSTOMER_RECOVERABLE, None, ""),
    ("payment_declined", FC.UNKNOWN, RC.CUSTOMER_RECOVERABLE, None,
     "Razorpay: 'exact reason not communicated'. Honest UNKNOWN, not a guess."),
    ("payment_failed", FC.UNKNOWN, RC.CUSTOMER_RECOVERABLE, None,
     "No gateway code returned. The largest UNKNOWN bucket in real traffic."),

    # -- MERCHANT_CONFIG: integration bugs. Never contact the customer. -------
    ("bank_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("beneficiary_account_does_not_exist", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("beneficiary_account_dormant", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("capture_failed", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("card_network_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("collect_on_mcc_blocked", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("duplicate_refund_id", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("input_validation_failed", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_amount", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_currency", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_email", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_mobile_number", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_order_id", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_request", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("invalid_user_details", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("live_mode_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("merchant_not_activated", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("mismatch_in_transaction_details", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("mobile_number_invalid", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("order_amount_mismatch", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("order_payment_method_mismatch", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("payment_method_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("record_not_found", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("recurring_payment_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("refund_limit_crossed", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("upi_collect_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),
    ("upi_intent_not_enabled", FC.UNKNOWN, RC.MERCHANT_CONFIG, None, ""),

    # -- TERMINAL: the episode is over. -------------------------------------
    ("order_already_paid", FC.UNKNOWN, RC.TERMINAL, None,
     "STOP CONDITION. This is how late authorisation surfaces on a retry "
     "(CLAUDE.md section 7). Acting here would contact an already-paying customer."),
)

MAPPING: dict[str, ReasonMapping] = {
    row[0]: ReasonMapping(row[0], row[1], row[2], row[3], row[4]) for row in _TABLE
}

UNMAPPED_FALLBACK = ReasonMapping(
    reason="<unmapped>",
    failure_class=FailureClass.UNKNOWN,
    recoverability=Recoverability.CUSTOMER_RECOVERABLE,
    note="Reason absent from the mapping table; handled conservatively and counted.",
)

# CLAUDE.md section 13: unmapped reasons fall to UNKNOWN and are COUNTED.
_unmapped_seen: dict[str, int] = {}


# A dropped checkout carries no Razorpay error: no payment was ever attempted, so there
# is no code to map. It is nonetheless one of the best-understood failure modes there
# is - the customer was present, engaged, and left. Treating it as UNKNOWN would discard
# real information and route a re-engageable customer into conservative handling.
CHECKOUT_ABANDONED = ReasonMapping(
    reason="<checkout_abandoned>",
    failure_class=FailureClass.AUTH_ABANDON,
    recoverability=Recoverability.CUSTOMER_RECOVERABLE,
    note="Checkout dropped before a payment was attempted. No Razorpay error exists "
         "because nothing reached the gateway; intent was demonstrated and the "
         "customer can be re-engaged.",
)


# An overdue receivable carries no Razorpay error either: nobody attempted a charge, an
# invoice simply went unpaid past its terms. B2B non-payment is overwhelmingly a
# working-capital timing problem - the buyer's accounts-payable run has not reached it -
# which is the same phenomenon as FUNDS, on a corporate cycle rather than a salary one.
# Classing it UNKNOWN would throw away the strongest thing we know about it.
RECEIVABLE_OVERDUE = ReasonMapping(
    reason="<invoice_overdue>",
    failure_class=FailureClass.FUNDS,
    recoverability=Recoverability.CUSTOMER_RECOVERABLE,
    note="Invoice past its payment terms with no charge attempted. Treated as a "
         "working-capital timing problem aligned to the buyer's AP run, not as a "
         "gateway failure - there is no instrument to retry.",
)


# Razorpay's `source` field states WHO MUST ACT, and their documentation is explicit
# about it: customer -> prompt them to retry; business -> "Fix the request parameters
# before retrying"; gateway -> retry or switch method; razorpay -> retry after a delay.
#
# We mapped on `reason` alone and discarded `source` entirely, which meant discarding a
# triage signal the provider hands us for free. Found by Tier 1: the very first real
# failed payment came back as `international_transaction_not_allowed` with
# `source=business` — a merchant configuration problem our reason-only table classed as
# customer-recoverable. Our synthetic cohort could never have surfaced this, because the
# generator only ever emits `source=customer`.
MERCHANT_SOURCES = frozenset({"business"})


def refine_by_source(mapping: ReasonMapping, source: str | None) -> ReasonMapping:
    """Let Razorpay's own `source` field override our triage where it is decisive."""
    if source and source.lower() in MERCHANT_SOURCES:
        if mapping.recoverability is Recoverability.CUSTOMER_RECOVERABLE:
            return replace(
                mapping,
                recoverability=Recoverability.MERCHANT_CONFIG,
                note=(mapping.note or "") + " [source=business: Razorpay attributes this "
                     "to the merchant's integration, so it must not generate customer "
                     "contact regardless of the reason code.]",
            )
    return mapping


def classify(error: ErrorObject | None, source_type: str | None = None) -> ReasonMapping:
    """Map a Razorpay error object to our taxonomy. Never raises."""
    if (error is None or not error.reason) and source_type == "checkout":
        return CHECKOUT_ABANDONED
    if (error is None or not error.reason) and source_type == "invoice":
        return RECEIVABLE_OVERDUE
    if error is None or not error.reason:
        return UNMAPPED_FALLBACK
    hit = MAPPING.get(error.reason)
    if hit is not None:
        return refine_by_source(hit, error.source)
    _unmapped_seen[error.reason] = _unmapped_seen.get(error.reason, 0) + 1
    return UNMAPPED_FALLBACK


def unmapped_counts() -> dict[str, int]:
    """Reasons seen in traffic but missing from the table. Reported in eval."""
    return dict(_unmapped_seen)


def reset_unmapped_counts() -> None:
    _unmapped_seen.clear()


def published_reasons() -> set[str]:
    """Every reason Razorpay publishes, straight from the committed CSV."""
    with REASONS_CSV.open(encoding="utf-8") as f:
        return {row["reason"] for row in csv.DictReader(f)}

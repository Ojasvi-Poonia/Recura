"""Synthetic cohort generator.

*** FROZEN after 2026-08-27 (CLAUDE.md section 9.3). ***
Do not modify this file while tuning the agent. Any post-freeze change must be a
separate commit with a stated reason in the message.

POST-FREEZE CHANGE LOG
----------------------
2. 2026-08-26 - Checkout and invoice events no longer fabricate a gateway error.
   Reason: REALISM, not tuning. A dropped checkout never reached the gateway and an
   overdue invoice was never charged, so neither can carry a Razorpay error code -
   yet the generator attached one to every event, which made "checkout abandonment"
   and "overdue receivables" indistinguishable from payment failures. Both are named
   Track 03 directions. Invoices additionally carry `due_at`, because ageing is their
   only real signal. True-class assignment for these sources is deliberately NOISY
   (75%/70% aligned) so the inference problem is preserved rather than made trivially
   solvable by reading `source_type`.

1. 2026-08-26 - COHORT_SIZE 2,000 -> 10,000. Reason: STATISTICAL POWER, not tuning.
   The first Tier 2 run measured a +4.47pp lift with a 95% bootstrap CI of
   [-0.51, +9.42] - an interval containing zero. An a-priori two-proportion power
   calculation on that effect (4.47pp on a 27.2% baseline, 80% power, alpha=0.05)
   requires 1,631 events PER ARM; the 20% holdout held only 394, roughly a quarter of
   what is needed. The generative process below is UNCHANGED - same seed, same
   distributions, same response model. Only the sample size moved, and it moved because
   the experiment was underpowered by design, which is a design error rather than a
   result we disliked. Nothing about the agent was altered in this commit.

Design note - why this is not circular (CLAUDE.md section 9):

  The Razorpay `reason` is a NOISY EMISSION of the true underlying cause, not a
  perfect observation of it. ~78% of events emit a reason that genuinely indicates
  their true class; ~12% emit an opaque code (Razorpay itself documents
  `payment_failed` / `payment_declined` as "exact reason not communicated"); ~10%
  emit a reason belonging to a DIFFERENT class.

  This is what makes the ablation study meaningful. If the label were always
  truthful, a lookup table would be optimal, and the taxonomy and LLM ablations
  would correctly show zero contribution. Because it is noisy, an agent that also
  reads amount, contact history and hour-of-day can beat one that trusts the label.

  Latent state lives in eval/latents.py and is written to a SEPARATE file that the
  agent never opens. Observables carry no latent field, and RiskEvent forbids extras.

Usage:  make seed
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from eval.latents import LatentState
from src.clock import IST
from src.models import (
    Channel,
    CustomerHistory,
    ErrorObject,
    FailureClass,
    MerchantContext,
    RiskEvent,
)
from src.taxonomy.mapping import MAPPING
from src.models import Recoverability

FC = FailureClass

SEED = 20260826
COHORT_SIZE = 10_000  # see POST-FREEZE CHANGE LOG entry 1
HOLDOUT_FRACTION = 0.20
EPOCH = datetime(2026, 3, 1, 0, 0, tzinfo=IST)  # fixed origin; no wall clock anywhere

OUT_DIR = Path(__file__).resolve().parents[1] / "data"
COHORT_PATH = OUT_DIR / "cohort.json"       # observables - the agent reads this
LATENTS_PATH = OUT_DIR / "latents.json"     # hidden truth - only the simulator reads this

# Share of failures per TRUE class. eval/CALIBRATION.md section 2.
# TRANSIENT_INFRA is anchored to NPCI OC-149's TD:BD ratio (grade A); the interior
# split is grade C and is swept in Tier 3.
FAILURE_MIX: dict[FailureClass, float] = {
    FC.TRANSIENT_INFRA: 0.17,
    FC.FUNDS: 0.30,
    FC.AUTH_ABANDON: 0.22,
    FC.INSTRUMENT_INVALID: 0.14,
    FC.LIMIT_EXCEEDED: 0.08,
    FC.RISK_DECLINE: 0.05,
    FC.UNKNOWN: 0.04,
}

# Emission noise (chosen 2026-08-26, see CLAUDE.md decision log).
P_OPAQUE = 0.12      # emits a reason carrying no diagnostic information
P_MISLEADING = 0.10  # emits a reason that points at the WRONG class
# remaining 0.78 emits a truthful reason for the true class

# Razorpay's own documented "exact reason not communicated" codes.
OPAQUE_REASONS = ("payment_failed", "payment_declined", "credit_failed", "mandate_creation_failed")

METHODS = ("upi", "card", "netbanking", "wallet", "emandate")
METHOD_P = (0.55, 0.25, 0.10, 0.04, 0.06)  # UPI-dominant, per NPCI volume share
BANKS = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PAYTM", "YES", "IDFC")
SOURCE_TYPES = ("payment", "checkout", "mandate", "invoice")
SOURCE_TYPE_P = (0.55, 0.20, 0.15, 0.10)


def _reason_pools() -> dict[FailureClass, list[str]]:
    """Candidate reasons per class, taken from the real Razorpay mapping.

    Only CUSTOMER_RECOVERABLE reasons are emitted: a merchant integration bug is a
    different phenomenon and would pollute a recovery cohort.
    """
    pools: dict[FailureClass, list[str]] = {c: [] for c in FailureClass}
    for reason, row in MAPPING.items():
        if row.recoverability is Recoverability.CUSTOMER_RECOVERABLE:
            pools[row.failure_class].append(reason)
    for c in pools:
        pools[c].sort()  # determinism
    return pools


POOLS = _reason_pools()


def _emit_reason(rng: np.random.Generator, true_class: FailureClass) -> tuple[str, str]:
    """Return (reason, emission_kind). The agent sees only the reason."""
    roll = rng.random()
    if roll < P_OPAQUE:
        return str(rng.choice(OPAQUE_REASONS)), "opaque"
    if roll < P_OPAQUE + P_MISLEADING:
        others = [c for c in POOLS if c is not true_class and POOLS[c]]
        wrong = others[int(rng.integers(len(others)))]
        return str(rng.choice(POOLS[wrong])), "misleading"
    pool = POOLS[true_class] or list(OPAQUE_REASONS)
    return str(rng.choice(pool)), "truthful"


def generate() -> tuple[list[RiskEvent], dict[str, LatentState], list[str]]:
    rng = np.random.default_rng(SEED)
    classes = list(FAILURE_MIX)
    weights = np.array([FAILURE_MIX[c] for c in classes], dtype=float)
    weights /= weights.sum()

    events: list[RiskEvent] = []
    latents: dict[str, LatentState] = {}
    arms: list[str] = []

    for i in range(COHORT_SIZE):
        event_id = f"evt_{i:05d}"
        source_type = SOURCE_TYPES[int(rng.choice(len(SOURCE_TYPES), p=SOURCE_TYPE_P))]
        true_class = classes[int(rng.choice(len(classes), p=weights))]

        # A dropped checkout never reached the gateway; an overdue invoice was never
        # charged. Neither can carry an error code.
        no_gateway_attempt = (
            (source_type == "checkout" and rng.random() < 0.60)
            or source_type == "invoice"
        )
        # Only NON-gateway events get their truth constrained by source. A checkout
        # that did attempt payment and was declined belongs to the decline population
        # and must follow the calibrated mix, or the cited NPCI figures stop applying.
        if no_gateway_attempt:
            if source_type == "invoice" and rng.random() < 0.75:
                true_class = FC.FUNDS          # working-capital timing
            elif source_type == "checkout" and rng.random() < 0.70:
                true_class = FC.AUTH_ABANDON   # present, engaged, left

        # ---- latent truth (never observable) ----------------------------
        latent_intent = float(rng.beta(2.0, 2.0))
        liquidity_day = int(rng.choice([1, 2, 3, 5, 7, 10, 25, 28, 30]))
        # An instrument is genuinely dead mostly-but-not-only in INSTRUMENT_INVALID.
        p_dead = 0.85 if true_class is FC.INSTRUMENT_INVALID else 0.05
        instrument_dead = bool(rng.random() < p_dead)
        downtime_clears_hours = float(rng.gamma(2.0, 3.0)) if true_class is FC.TRANSIENT_INFRA else 0.0
        annoyance_threshold = int(rng.integers(1, 5))  # 1-4 contacts tolerated
        success_hour = int(rng.integers(9, 19))  # within the lawful contact window
        draws = tuple(float(x) for x in rng.random(8))

        latents[event_id] = LatentState(
            event_id=event_id,
            true_failure_class=true_class,
            latent_intent=latent_intent,
            liquidity_day=liquidity_day,
            instrument_dead=instrument_dead,
            downtime_clears_hours=downtime_clears_hours,
            annoyance_threshold=annoyance_threshold,
            success_hour=success_hour,
            draws=draws,
        )

        # ---- observables -------------------------------------------------
        reason = None if no_gateway_attempt else _emit_reason(rng, true_class)[0]
        amount_paise = int(np.clip(rng.lognormal(mean=7.4, sigma=1.25) * 100, 1000, 50_000_00))
        observed_at = EPOCH + timedelta(
            days=float(rng.integers(0, 21)), hours=float(rng.integers(0, 24)),
            minutes=float(rng.integers(0, 60)),
        )
        prior_failed = int(rng.integers(0, 4))
        prior_recoveries = int(rng.integers(0, max(1, prior_failed + 1)))

        # Observed success hours are a NOISY sample around the latent one - this is
        # the learnable timing signal, and it is deliberately imperfect.
        n_hours = int(rng.integers(0, 4))
        obs_hours = tuple(
            sorted(
                int(np.clip(success_hour + int(rng.integers(-2, 3)), 9, 18))
                for _ in range(n_hours)
            )
        )

        consent_roll = rng.random()
        consented: tuple[Channel, ...] = (Channel.EMAIL,)
        if consent_roll < 0.75:
            consented = (Channel.EMAIL, Channel.SMS)
        if consent_roll < 0.45:
            consented = (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP)

        events.append(
            RiskEvent(
                event_id=event_id,
                merchant_id="merchant_demo",
                customer_id=f"cust_{int(rng.integers(0, 1400)):05d}",
                source_type=source_type,
                amount_paise=amount_paise,
                observed_at=observed_at,
                due_at=(observed_at - timedelta(days=int(rng.integers(1, 95))))
                if source_type == "invoice" else None,
                razorpay_error=None if reason is None else ErrorObject(
                    code="BAD_REQUEST_ERROR",
                    reason=reason,
                    source="customer",
                    step="payment_authorization",
                ),
                method=METHODS[int(rng.choice(len(METHODS), p=METHOD_P))],
                bank=str(rng.choice(BANKS)),
                attempt_number=1,
                customer_history=CustomerHistory(
                    prior_failed_attempts=prior_failed,
                    prior_recoveries=prior_recoveries,
                    prior_payments_total=int(rng.integers(0, 20)),
                    contacts_last_7d=0,
                    successful_payment_hours=obs_hours,
                    consented_channels=consented,
                    opted_out=False,
                    language=str(rng.choice(["en", "hi"], p=[0.7, 0.3])),
                ),
                merchant_context=MerchantContext(merchant_id="merchant_demo"),
            )
        )
        arms.append("holdout" if rng.random() < HOLDOUT_FRACTION else "treatment")

    return events, latents, arms


def main() -> None:
    events, latents, arms = generate()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    COHORT_PATH.write_text(
        json.dumps(
            [{"arm": a, **json.loads(e.model_dump_json())} for e, a in zip(events, arms, strict=True)],
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )
    LATENTS_PATH.write_text(
        json.dumps(
            {k: {**v.__dict__, "true_failure_class": v.true_failure_class.value}
             for k, v in latents.items()},
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )

    arm_counts = Counter(arms)
    true_mix = Counter(l.true_failure_class.value for l in latents.values())
    print(f"cohort: {len(events)} events -> {COHORT_PATH.name}, {LATENTS_PATH.name}")
    print(f"arms:   treatment={arm_counts['treatment']}  holdout={arm_counts['holdout']}")
    print("true failure mix:")
    for k, v in true_mix.most_common():
        print(f"  {k:20} {v:5}  {v / len(events):6.1%}")


if __name__ == "__main__":
    main()

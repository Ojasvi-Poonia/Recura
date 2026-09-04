"""LLM root-cause synthesis (the design spec sections 1, 5, 8, 13).

The LLM's ONLY job is diagnosis: read heterogeneous signals, propose a root cause and a
suspected failure class. It never chooses an action, never sees `policy.yaml`, and never
sees a hidden latent. Those boundaries are enforced by tests/test_invariants.py.

DETERMINISM - a correction to spec §2
--------------------------------------------------
Section 2 specifies "temperature 0, pinned model string". **`temperature` no longer
exists on current Claude models** - it was removed on Opus 5 / Sonnet 5 / Fable 5 and
the 4.6+ family, and sending it returns HTTP 400. Verified against the Anthropic SDK
docs 2026-08-26.

That is fine, because temperature 0 was never a real determinism guarantee anyway
(server-side batching makes identical requests diverge). The guarantee we actually rely
on is stronger and is already required by section 8: **every response is cached to
`fixtures/` under a content-addressed key**. A committed fixture set makes `make eval`
byte-identical AND removes the API key requirement entirely, which is what lets a
Razorpay engineer clone the repo and reproduce the headline number offline.

COST / PROVIDER - bring your own key
------------------------------------
The provider is pluggable (`src/decide/providers.py`): Anthropic, Gemini (free tier), or
none. Neither key is needed to REPRODUCE results - only to regenerate `fixtures/`.

The LLM is consulted only where it can change the answer. For ~83% of events Razorpay's
reason code is diagnostic and the taxonomy lookup is already correct; spending a model
call there buys nothing. The model is called on the ~17% where the code is opaque or
maps to UNKNOWN - which is exactly where the ablation study can attribute its value.
That is cost-aware routing, and it also happens to bring a full cohort inside a free
tier: ~266 unique calls instead of 1,606.

Payloads are BANDED (amount into ranges, hour into time-of-day, counts into buckets). A
root cause does not depend on whether an amount was Rs 2,499 or Rs 2,501; the EV layer
still sees exact paise, because the money does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from src.models import ErrorObject, FailureClass, RiskEvent
from src.decide.providers import LLMProvider, NullProvider, resolve_provider
from src.taxonomy.mapping import ReasonMapping

MODEL = "claude-opus-5"          # pinned; see section 2
MAX_TOKENS = 1024
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "root_cause.md"
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

# Razorpay's own "exact reason not communicated" codes - the taxonomy is blind here.
OPAQUE_REASONS = frozenset(
    {"payment_failed", "payment_declined", "credit_failed", "mandate_creation_failed"}
)


class ProposalSource(StrEnum):
    FIXTURE = "fixture"      # replayed from the committed cache - the eval path
    API = "api"              # live call; response is written to fixtures
    FALLBACK = "fallback"    # deterministic rules; logged as a handled failure
    SKIPPED = "skipped"      # routing gate: taxonomy was already diagnostic


class ClassBelief(BaseModel):
    failure_class: FailureClass
    probability: float = Field(ge=0.0, le=1.0)


class RootCauseProposal(BaseModel):
    """What we ask the model to return. Also the fixture schema.

    `beliefs` is a DISTRIBUTION, not a label. That matters: on an opaque error code an
    honest model answers "UNKNOWN", and a single-label interface would throw that entire
    call away. A distribution lets partial information through - "probably FUNDS, maybe
    infra" is genuinely useful even when nothing is certain - and the EV layer
    marginalises over it rather than betting on one guess.
    """

    root_cause: str = Field(description="One sentence, max 200 chars, plain language.")
    beliefs: list[ClassBelief] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str

    def distribution(self) -> tuple[tuple[FailureClass, float], ...]:
        """Normalised belief. Falls back to all-UNKNOWN if the model returned nothing."""
        total = sum(max(0.0, b.probability) for b in self.beliefs)
        if total <= 0:
            return ((FailureClass.UNKNOWN, 1.0),)
        return tuple((b.failure_class, max(0.0, b.probability) / total)
                     for b in self.beliefs if b.probability > 0)

    @property
    def suspected_failure_class(self) -> FailureClass:
        """Most likely class. Kept for logging and for the no-marginalisation ablation."""
        dist = self.distribution()
        return max(dist, key=lambda pair: pair[1])[0]


@dataclass(frozen=True)
class ProposalResult:
    proposal: RootCauseProposal
    source: ProposalSource
    cache_key: str
    error: str | None = None


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _band_amount(paise: int) -> str:
    for ceiling, label in ((10_000, "<Rs100"), (50_000, "Rs100-500"),
                           (200_000, "Rs500-2k"), (1_000_000, "Rs2k-10k"),
                           (5_000_000, "Rs10k-50k")):
        if paise < ceiling:
            return label
    return ">Rs50k"


def _band_time(hour: int) -> str:
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _band_month(day: int) -> str:
    return "early" if day <= 7 else "mid" if day <= 21 else "late"


def _band_count(n: int) -> str:
    return "0" if n == 0 else "1-2" if n <= 2 else "3+"


def taxonomy_is_blind(mapping: ReasonMapping, reason: str | None) -> bool:
    """True when Razorpay's code carries no usable diagnostic signal.

    Two cases, both from Razorpay's own documentation:
      - the code is explicitly opaque ("exact reason not communicated to Razorpay")
      - the code maps to UNKNOWN in our table
    These are the only events where a model can beat the lookup, so these are the only
    events we spend a call on.
    """
    return bool(reason in OPAQUE_REASONS or mapping.failure_class is FailureClass.UNKNOWN)


def should_consult_llm(
    mapping: ReasonMapping,
    reason: str | None,
    amount_paise: int = 0,
    always_consult_above_paise: int | None = None,
) -> bool:
    """Cost-aware routing gate. Raising the amount knob trades money for coverage."""
    if taxonomy_is_blind(mapping, reason):
        return True
    if always_consult_above_paise is not None and amount_paise > always_consult_above_paise:
        return True
    return False


def observable_payload(event: RiskEvent, mapping: ReasonMapping) -> dict:
    """EXACTLY what the LLM is allowed to see.

    An explicit allow-list rather than a dump of the event, so a field added to
    RiskEvent later cannot silently leak into the prompt. Values are banded: it keeps
    the fixture cache small and stops the model anchoring on spurious precision.

    `bank` is deliberately EXCLUDED. Bank identity matters for downtime, and downtime
    is handled in the EV layer via Downtime.affects(method, bank) - which does see it.
    A single diagnostic call cannot exploit bank identity, and carrying it multiplied
    the fixture set eightfold for no diagnostic gain.
    """
    err: ErrorObject = event.razorpay_error or ErrorObject()
    h = event.customer_history
    return {
        "razorpay_reason": err.reason,
        "razorpay_source": err.source,
        "razorpay_step": err.step,
        "taxonomy_hint": mapping.failure_class.value,
        "taxonomy_note": mapping.note or None,
        "amount_band": _band_amount(event.amount_paise),
        "method": event.method,
        "source_type": event.source_type,
        "attempt_number": event.attempt_number,
        "time_of_day_ist": _band_time(event.observed_at.hour),
        "month_position": _band_month(event.observed_at.day),
        "prior_failed_attempts": _band_count(h.prior_failed_attempts),
        "prior_recoveries": _band_count(h.prior_recoveries),
        "customer_has_ever_recovered": h.prior_recoveries > 0,
    }


def cache_key(payload: dict, system_prompt: str, model: str = MODEL) -> str:
    """Content-addressed. Any change to prompt, model or payload is a new key."""
    blob = json.dumps(
        {"model": model, "system": system_prompt, "payload": payload},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


MANIFEST_NAME = "MANIFEST.json"


def _fixture_path(key: str, fixtures_dir: Path) -> Path:
    return fixtures_dir / f"{key}.json"


def write_manifest(model_id: str, fixtures_dir: Path = FIXTURES_DIR) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / MANIFEST_NAME).write_text(
        json.dumps({"model": model_id}, indent=2), encoding="utf-8")


def fixture_model_id(fixtures_dir: Path = FIXTURES_DIR) -> str | None:
    """Which model produced the committed fixture set.

    Cache keys are model-scoped, so replay must key against the model that WROTE the
    fixtures - not against whatever provider happens to be configured locally. Without
    this, a reviewer with no API key computes keys against "null", misses every
    fixture, and silently gets rules-only numbers with nothing in the output saying so.
    """
    path = fixtures_dir / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("model")
    except Exception:
        return None


def read_fixture(key: str, fixtures_dir: Path = FIXTURES_DIR) -> RootCauseProposal | None:
    path = _fixture_path(key, fixtures_dir)
    if not path.exists():
        return None
    try:
        return RootCauseProposal.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None  # a corrupt fixture must degrade, never crash the batch


def write_fixture(key: str, proposal: RootCauseProposal,
                  fixtures_dir: Path = FIXTURES_DIR) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    _fixture_path(key, fixtures_dir).write_text(
        json.dumps(proposal.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def rules_fallback(mapping: ReasonMapping, reason: str | None) -> RootCauseProposal:
    """Deterministic, no network. The no-LLM ablation runs entirely on this path."""
    described = mapping.note or mapping.reason
    return RootCauseProposal(
        root_cause=f"{reason or 'unknown reason'}: {described}"[:200],
        beliefs=[ClassBelief(failure_class=mapping.failure_class, probability=1.0)],
        confidence=0.5,
        reasoning="Deterministic taxonomy lookup; no model judgement applied.",
    )


def propose_root_cause(
    event: RiskEvent,
    mapping: ReasonMapping,
    *,
    fixtures_dir: Path = FIXTURES_DIR,
    provider: LLMProvider | None = None,
    allow_network: bool = True,
    always_consult_above_paise: int | None = None,
) -> ProposalResult:
    """Diagnose one event. Never raises - any failure degrades to the rules fallback.

    Order: routing gate -> committed fixture -> provider call (cached) -> rules.
    """
    system_prompt = load_system_prompt()
    payload = observable_payload(event, mapping)
    reason = event.razorpay_error.reason if event.razorpay_error else None

    if provider is None:
        provider = resolve_provider() if allow_network else NullProvider()
    live_model = getattr(provider, "model", provider.name)
    # Look up against the model that WROTE the fixtures; fall back to the live one when
    # there is no committed set (i.e. we are about to generate it).
    key = cache_key(payload, system_prompt, fixture_model_id(fixtures_dir) or live_model)

    # Routing gate: where the code is diagnostic, the lookup already has the answer.
    if not should_consult_llm(mapping, reason, event.amount_paise,
                              always_consult_above_paise):
        return ProposalResult(rules_fallback(mapping, reason), ProposalSource.SKIPPED, key)

    cached = read_fixture(key, fixtures_dir)
    if cached is not None:
        return ProposalResult(cached, ProposalSource.FIXTURE, key)

    if isinstance(provider, NullProvider) or not allow_network:
        return ProposalResult(rules_fallback(mapping, reason), ProposalSource.FALLBACK, key,
                              error="no fixture and no provider")

    try:
        proposal = provider.diagnose(
            system_prompt, json.dumps(payload, sort_keys=True, indent=2), RootCauseProposal
        )
        proposal = proposal.model_copy(update={"root_cause": proposal.root_cause[:200]})
        write_fixture(key, proposal, fixtures_dir)
        return ProposalResult(proposal, ProposalSource.API, key)
    except Exception as exc:  # section 13: invalid output falls back AND is logged
        return ProposalResult(
            rules_fallback(mapping, reason), ProposalSource.FALLBACK, key,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )

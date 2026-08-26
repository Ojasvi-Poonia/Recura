"""LLM diagnosis-layer tests (CLAUDE.md sections 1, 8, 13)."""

from datetime import datetime

import pytest

from src.clock import IST
from src.decide import llm
from src.decide import providers as P
from src.models import CustomerHistory, ErrorObject, FailureClass, RiskEvent
from src.taxonomy.mapping import classify

NOW = datetime(2026, 3, 10, 14, 0, tzinfo=IST)

LATENT_NAMES = {
    "latent_intent", "true_failure_class", "instrument_dead", "liquidity_day",
    "annoyance_threshold", "success_hour", "draws", "downtime_clears_hours",
}


def mk(reason="payment_failed", **kw):
    err = ErrorObject(reason=reason, source="gateway", step="payment_authorization")
    event = RiskEvent(
        event_id="e1", merchant_id="m", customer_id="c", source_type="payment",
        amount_paise=kw.pop("amount_paise", 349900), observed_at=NOW,
        razorpay_error=err, method="card", bank="HDFC",
        customer_history=CustomerHistory(prior_failed_attempts=2, prior_recoveries=1),
        **kw,
    )
    return event, classify(err)


class FakeProvider:
    """Records what it was asked; returns a fixed proposal."""

    name = "fake"
    model = "fake-model-1"

    def __init__(self, boom: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.boom = boom

    def diagnose(self, system_prompt, user_content, schema):
        self.calls.append((system_prompt, user_content))
        if self.boom:
            raise RuntimeError(self.boom)
        return schema(root_cause="Opaque gateway decline on a repeat card attempt.",
                      suspected_failure_class=FailureClass.TRANSIENT_INFRA,
                      confidence=0.42, reasoning="Opaque code; customer recovered before.")


# --- what the model is allowed to see --------------------------------------

def test_bank_is_excluded_from_the_diagnostic_payload():
    """Bank identity belongs to the downtime path (EV layer), not to diagnosis."""
    assert "bank" not in llm.observable_payload(*mk())


def test_payload_is_an_explicit_allowlist():
    assert set(llm.observable_payload(*mk())) == {
        "razorpay_reason", "razorpay_source", "razorpay_step", "taxonomy_hint",
        "taxonomy_note", "amount_band", "method", "source_type",
        "attempt_number", "time_of_day_ist", "month_position",
        "prior_failed_attempts", "prior_recoveries", "customer_has_ever_recovered",
    }


def test_payload_carries_no_latent_field():
    """section 9.1: the agent - including the LLM - never reads a latent."""
    assert not (LATENT_NAMES & set(llm.observable_payload(*mk())))


def test_payload_carries_no_pii_and_no_exact_amount():
    """No PII (section 2); amounts are banded so the model cannot anchor on precision."""
    payload = llm.observable_payload(*mk(amount_paise=349900))
    blob = str(payload).lower()
    for leak in ("email", "@", "phone", "349900"):
        assert leak not in blob
    assert payload["amount_band"] == "Rs2k-10k"


def test_prompt_never_mentions_policy():
    """section 1: the LLM never sees policy.yaml and cannot reason about it."""
    prompt = llm.load_system_prompt().lower()
    for term in ("policy.yaml", "quiet hours", "consent", "budget", "escalat"):
        assert term not in prompt


def test_prompt_forbids_recommending_actions():
    assert "do not recommend an action" in llm.load_system_prompt().lower()


# --- routing gate -----------------------------------------------------------

@pytest.mark.parametrize("reason", ["payment_failed", "payment_declined",
                                    "credit_failed", "mandate_creation_failed"])
def test_opaque_reasons_consult_the_model(reason):
    event, mapping = mk(reason=reason)
    assert llm.should_consult_llm(mapping, reason, event.amount_paise)


@pytest.mark.parametrize("reason", ["insufficient_funds", "card_expired",
                                    "incorrect_otp", "bank_technical_error"])
def test_diagnostic_reasons_skip_the_model(reason):
    """section 8 cost: no model call where the lookup already has the answer."""
    event, mapping = mk(reason=reason)
    assert not llm.should_consult_llm(mapping, reason, event.amount_paise)


def test_skipped_events_use_the_taxonomy_and_make_no_call(tmp_path):
    provider = FakeProvider()
    result = llm.propose_root_cause(*mk(reason="insufficient_funds"),
                                    fixtures_dir=tmp_path, provider=provider)
    assert result.source is llm.ProposalSource.SKIPPED
    assert provider.calls == []
    assert result.proposal.suspected_failure_class is FailureClass.FUNDS


def test_high_value_override_forces_consultation(tmp_path):
    event, mapping = mk(reason="insufficient_funds", amount_paise=9_000_000)
    assert llm.should_consult_llm(mapping, "insufficient_funds", event.amount_paise,
                                  always_consult_above_paise=5_000_000)


# --- caching / determinism --------------------------------------------------

def test_cache_key_is_stable_and_model_scoped():
    event, mapping = mk()
    system = llm.load_system_prompt()
    payload = llm.observable_payload(event, mapping)
    assert llm.cache_key(payload, system, "m1") == llm.cache_key(payload, system, "m1")
    assert llm.cache_key(payload, system, "m1") != llm.cache_key(payload, system, "m2")


def test_banding_collapses_near_identical_events():
    """Rs 3,499.00 and Rs 3,501.00 are the same diagnostic question."""
    system = llm.load_system_prompt()
    a = llm.cache_key(llm.observable_payload(*mk(amount_paise=349900)), system)
    b = llm.cache_key(llm.observable_payload(*mk(amount_paise=350100)), system)
    assert a == b


def test_fixture_roundtrip(tmp_path):
    proposal = llm.RootCauseProposal(
        root_cause="Bank was briefly unavailable.",
        suspected_failure_class=FailureClass.TRANSIENT_INFRA,
        confidence=0.7, reasoning="Gateway source with a transient code.")
    llm.write_fixture("abc123", proposal, tmp_path)
    assert llm.read_fixture("abc123", tmp_path) == proposal


def test_corrupt_fixture_degrades_rather_than_crashing(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert llm.read_fixture("bad", tmp_path) is None


def test_fixture_hit_needs_no_provider(tmp_path):
    """section 8: `make eval` runs offline from committed fixtures, no key required."""
    event, mapping = mk()
    key = llm.cache_key(llm.observable_payload(event, mapping),
                        llm.load_system_prompt(), "fake-model-1")
    llm.write_fixture(key, llm.RootCauseProposal(
        root_cause="Cached diagnosis.", suspected_failure_class=FailureClass.FUNDS,
        confidence=0.6, reasoning="from fixture"), tmp_path)
    provider = FakeProvider()
    result = llm.propose_root_cause(event, mapping, fixtures_dir=tmp_path,
                                    provider=provider)
    assert result.source is llm.ProposalSource.FIXTURE
    assert provider.calls == []


# --- fallback behaviour -----------------------------------------------------

def test_no_provider_falls_back(tmp_path):
    result = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path,
                                    provider=P.NullProvider())
    assert result.source is llm.ProposalSource.FALLBACK


def test_fallback_is_deterministic(tmp_path):
    a = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path, provider=P.NullProvider())
    b = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path, provider=P.NullProvider())
    assert a.proposal == b.proposal


def test_provider_error_is_handled_and_logged(tmp_path):
    """section 13: invalid LLM output falls back to rules AND is logged."""
    result = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path,
                                    provider=FakeProvider(boom="upstream exploded"))
    assert result.source is llm.ProposalSource.FALLBACK
    assert "upstream exploded" in result.error


def test_successful_call_is_written_to_fixtures(tmp_path):
    result = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path, provider=FakeProvider())
    assert result.source is llm.ProposalSource.API
    assert llm.read_fixture(result.cache_key, tmp_path) is not None


def test_root_cause_is_truncated_to_200_chars(tmp_path):
    class Long(FakeProvider):
        def diagnose(self, s, u, schema):
            return schema(root_cause="x" * 500,
                          suspected_failure_class=FailureClass.FUNDS,
                          confidence=0.5, reasoning="r")
    result = llm.propose_root_cause(*mk(), fixtures_dir=tmp_path, provider=Long())
    assert len(result.proposal.root_cause) == 200


# --- provider adapters ------------------------------------------------------

def test_provider_resolution_prefers_explicit_choice(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert P.resolve_provider("gemini").name == "gemini"
    assert P.resolve_provider("anthropic").name == "anthropic"
    assert P.resolve_provider("null").name == "null"


def test_gemini_reads_a_comma_separated_key_ring(monkeypatch):
    """Free-tier quotas are per key, so several keys extend the daily ceiling."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "k1, k2 ,k3")
    assert P.GeminiProvider().keys == ["k1", "k2", "k3"]


def test_gemini_rotates_off_an_exhausted_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    provider = P.GeminiProvider(pace=False)
    assert provider.active_key_index == 0
    assert provider._rotate() is True
    assert provider.active_key_index == 1
    assert provider._rotate() is False       # ring exhausted


@pytest.mark.parametrize("message,expected", [
    ("429 RESOURCE_EXHAUSTED: quota exceeded", True),
    ("403 PERMISSION_DENIED: API key invalid", True),
    ("400 INVALID_ARGUMENT: bad schema", False),
])
def test_only_quota_and_auth_errors_trigger_rotation(message, expected):
    """A malformed request is our bug - burning a second key on it helps nobody."""
    assert P._should_rotate(RuntimeError(message)) is expected


def test_unknown_provider_is_rejected():
    with pytest.raises(P.ProviderUnavailable):
        P.resolve_provider("openai")


def test_resolution_falls_back_to_null_without_keys(monkeypatch):
    for var in ("RECURA_LLM_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert P.resolve_provider().name == "null"


def test_bring_your_own_key_selects_gemini(monkeypatch):
    monkeypatch.delenv("RECURA_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert P.resolve_provider().name == "gemini"


def test_gemini_without_a_key_raises_provider_unavailable(monkeypatch):
    for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(P.ProviderUnavailable):
        P.GeminiProvider()


def test_anthropic_model_is_pinned():
    assert P.ANTHROPIC_MODEL == "claude-opus-5"


def test_no_temperature_is_sent_to_anthropic():
    """`temperature` was removed on current Claude models and returns HTTP 400."""
    sent = {}

    class Recording:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                body = llm.RootCauseProposal(
                    root_cause="ok", suspected_failure_class=FailureClass.FUNDS,
                    confidence=0.5, reasoning="r").model_dump_json()
                block = type("B", (), {"type": "text", "text": body})()
                return type("R", (), {"content": [block]})()

    P.AnthropicProvider(_client=Recording()).diagnose("sys", "{}", llm.RootCauseProposal)
    assert "temperature" not in sent and "top_p" not in sent
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_effort_is_configurable_for_cost():
    """Effort drives thinking tokens, which dominate the fixture-set bill."""
    sent = {}

    class Recording:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                body = llm.RootCauseProposal(
                    root_cause="ok", suspected_failure_class=FailureClass.FUNDS,
                    confidence=0.5, reasoning="r").model_dump_json()
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": body})()]})()

    P.AnthropicProvider(effort="low", _client=Recording()).diagnose(
        "sys", "{}", llm.RootCauseProposal)
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_anthropic_without_a_key_raises_provider_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(P.ProviderUnavailable):
        P.AnthropicProvider()._ensure()


def test_fixtures_from_different_models_do_not_collide():
    """A three-tier provider ablation needs each model's fixtures kept separate."""
    event, mapping = mk()
    payload = llm.observable_payload(event, mapping)
    system = llm.load_system_prompt()
    keys = {llm.cache_key(payload, system, m)
            for m in ("claude-opus-5", "gemini-2.5-flash", "null")}
    assert len(keys) == 3


@pytest.mark.parametrize("message,transient", [
    ("ReadError: [Errno 54] Connection reset by peer", True),
    ("httpx.ConnectTimeout: timed out", True),
    ("503 Service temporarily unavailable", True),
    ("429 RESOURCE_EXHAUSTED: quota exceeded", False),
    ("400 INVALID_ARGUMENT: bad schema", False),
])
def test_transient_network_errors_are_distinguished_from_quota(message, transient):
    """A connection reset is not a spent quota - retry the key, do not burn the next one."""
    assert P._is_transient(RuntimeError(message)) is transient


def test_checkout_and_receivable_events_do_not_consult_the_model():
    """Neither is ambiguous: a dropped checkout is AUTH_ABANDON, an overdue invoice is
    a working-capital problem. Sending them to a model wastes quota and, worse, would
    have them diagnosed as UNKNOWN by a caller that forgot the source type."""
    from src.taxonomy.mapping import classify as cls
    for source in ("checkout", "invoice"):
        mapping = cls(None, source_type=source)
        assert not llm.should_consult_llm(mapping, None, 500_000)

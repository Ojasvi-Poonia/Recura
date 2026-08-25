"""Cohort generator credibility invariants (CLAUDE.md section 9).

These tests exist because the biggest risk to this project is fooling ourselves
with our own synthetic data. Each one maps to a numbered invariant in section 9.
"""

import json
from pathlib import Path

import pytest

from eval import generate_cohort as g
from eval.latents import BASELINE_RECOVERY
from src.models import FailureClass, RiskEvent
from src.taxonomy.mapping import MAPPING

LATENT_FIELDS = {
    "latent_intent", "true_failure_class", "instrument_dead", "liquidity_day",
    "annoyance_threshold", "success_hour", "draws", "downtime_clears_hours",
}


@pytest.fixture(scope="module")
def cohort():
    return g.generate()


def test_generation_is_deterministic(cohort):
    """section 8: same seed -> byte-identical output, every time."""
    a_events, a_lat, a_arms = cohort
    b_events, b_lat, b_arms = g.generate()
    assert [e.model_dump_json() for e in a_events] == [e.model_dump_json() for e in b_events]
    assert a_arms == b_arms
    assert {k: v.draws for k, v in a_lat.items()} == {k: v.draws for k, v in b_lat.items()}


def test_observables_carry_no_latent_field(cohort):
    """section 9.1: the agent sees only observables."""
    events, _, _ = cohort
    for e in events[:200]:
        payload = json.loads(e.model_dump_json())
        assert not (LATENT_FIELDS & set(payload)), payload


def test_risk_event_rejects_latent_smuggling():
    """extra='forbid' makes leakage a hard error, not a convention."""
    events, _, _ = g.generate()
    payload = json.loads(events[0].model_dump_json())
    payload["latent_intent"] = 0.9
    with pytest.raises(Exception):
        RiskEvent(**payload)


def test_baseline_recovery_is_non_zero_for_every_class():
    """section 9.2: a zero baseline makes the holdout comparison meaningless."""
    for cls in FailureClass:
        assert BASELINE_RECOVERY[cls] > 0.0, cls


def test_holdout_split_is_close_to_target(cohort):
    _, _, arms = cohort
    holdout = arms.count("holdout") / len(arms)
    assert 0.17 <= holdout <= 0.23, holdout


def test_true_failure_mix_matches_calibration(cohort):
    """The generated mix must match the documented, cited mix."""
    _, latents, _ = cohort
    n = len(latents)
    for cls, target in g.FAILURE_MIX.items():
        actual = sum(1 for l in latents.values() if l.true_failure_class is cls) / n
        assert abs(actual - target) < 0.03, f"{cls}: {actual:.3f} vs {target}"


def test_emitted_reasons_are_all_real_razorpay_codes(cohort):
    """section 7: never invent a reason code."""
    events, _, _ = cohort
    for e in events:
        assert e.razorpay_error.reason in MAPPING, e.razorpay_error.reason


def test_label_is_genuinely_unreliable(cohort):
    """The anti-circularity property. If this passes trivially, the ablations are rigged.

    A naive lookup on the reason code must be wrong often enough that reading
    amount / history / hour can beat it.
    """
    events, latents, _ = cohort
    wrong = sum(
        1 for e in events
        if MAPPING[e.razorpay_error.reason].failure_class
        is not latents[e.event_id].true_failure_class
    )
    rate = wrong / len(events)
    assert 0.15 <= rate <= 0.30, f"label error rate {rate:.1%} outside the designed band"


def test_amounts_are_positive_integers(cohort):
    events, _, _ = cohort
    for e in events:
        assert isinstance(e.amount_paise, int) and e.amount_paise > 0


def test_generator_is_frozen():
    """section 9.3: the freeze is declared in the file itself, not just in a doc."""
    src = Path(g.__file__).read_text(encoding="utf-8")
    assert "FROZEN" in src and "section 9.3" in src


def test_batch_walks_the_cohort_chronologically():
    """Learning must be causally honest: an outcome can only inform later decisions."""
    from eval.run_batch import load_cohort
    times = [event.observed_at for event, _ in load_cohort()]
    assert times == sorted(times)

"""Tier 3 sweep and policy-replay tests (CLAUDE.md sections 8, 10)."""

import pytest

from eval import generate_cohort as gen
from eval import latents as lat
from eval.replay import VARIANTS, apply_patch
from eval.sweep import SWEEP, parameterisation
from src.policy.engine import load_policy


# --- sweep ------------------------------------------------------------------

def test_parameterisation_restores_every_constant():
    """The frozen generator must be byte-identical after a sweep runs."""
    before_baseline = dict(lat.BASELINE_RECOVERY)
    before_mix = dict(gen.FAILURE_MIX)
    before_noise = (gen.P_OPAQUE, gen.P_MISLEADING)
    before_efficacy = lat.NUDGE_INTENT_WEIGHT

    with parameterisation(SWEEP[-1]):
        assert dict(gen.FAILURE_MIX) != before_mix   # it really did change

    assert dict(lat.BASELINE_RECOVERY) == before_baseline
    assert dict(gen.FAILURE_MIX) == before_mix
    assert (gen.P_OPAQUE, gen.P_MISLEADING) == before_noise
    assert lat.NUDGE_INTENT_WEIGHT == before_efficacy


def test_parameterisation_restores_even_after_an_error():
    before = dict(gen.FAILURE_MIX)
    with pytest.raises(RuntimeError):
        with parameterisation(SWEEP[-1]):
            raise RuntimeError("boom")
    assert dict(gen.FAILURE_MIX) == before


def test_sweep_covers_both_directions():
    """An envelope needs a pessimistic AND an optimistic edge, or it is not a bound."""
    scales = [p.baseline_scale for p in SWEEP]
    assert max(scales) > 1.0 and min(scales) < 1.0


def test_sweep_varies_every_grade_c_assumption():
    """CALIBRATION.md marks these as assumptions; each must actually be swept."""
    assert any(p.baseline_scale != 1.0 for p in SWEEP)
    assert any(p.efficacy_scale != 1.0 for p in SWEEP)
    assert any(p.failure_mix for p in SWEEP)
    assert any(p.p_opaque is not None for p in SWEEP)


def test_every_parameterisation_states_why_it_exists():
    for p in SWEEP:
        assert len(p.rationale) > 30, p.label


def test_sweep_baseline_is_the_committed_parameterisation():
    base = SWEEP[0]
    assert base.baseline_scale == 1.0 and base.efficacy_scale == 1.0
    assert base.failure_mix is None


# --- replay -----------------------------------------------------------------

def test_patch_does_not_mutate_the_committed_policy():
    original = load_policy()
    snapshot = original["contact"]["max_per_customer_per_7d"]
    patched = apply_patch(original, {"contact.max_per_customer_per_7d": 99})
    assert patched["contact"]["max_per_customer_per_7d"] == 99
    assert original["contact"]["max_per_customer_per_7d"] == snapshot


def test_patch_reaches_nested_keys():
    patched = apply_patch(load_policy(), {"contact.quiet_hours.start": "21:00"})
    assert patched["contact"]["quiet_hours"]["start"] == "21:00"


def test_every_replay_patch_targets_a_real_policy_key():
    """A typo in a dotted path would silently answer the wrong question."""
    policy = load_policy()
    for variant in VARIANTS:
        for dotted in variant.patch:
            node = policy
            for key in dotted.split("."):
                assert key in node, f"{variant.label}: no such policy key {dotted}"
                node = node[key]


def test_first_variant_is_the_shipped_contract():
    assert VARIANTS[0].patch == {}


def test_every_variant_states_the_question_it_answers():
    for variant in VARIANTS:
        assert variant.question.endswith("?"), variant.label

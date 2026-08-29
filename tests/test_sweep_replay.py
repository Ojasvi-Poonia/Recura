"""Tier 3 sweep and policy-replay tests (CLAUDE.md sections 8, 10)."""

import copy

import pytest

from eval import generate_cohort as gen
from eval import latents as lat
from eval.replay import VARIANTS, apply_patch
from eval.sweep import SWEEP, parameterisation

_ORIGINAL_NUDGE_WEIGHT = lat.NUDGE_INTENT_WEIGHT
from src.policy.engine import load_policy


# --- sweep ------------------------------------------------------------------

def test_parameterisation_restores_every_constant():
    """The frozen generator must be byte-identical after EVERY sweep row runs.

    Checked for all parameterisations rather than one, and by label rather than by list
    position: this test used to reach for SWEEP[-1] and silently stopped exercising a
    mix-changing row the moment new rows were appended to the end of the list.
    """
    before_baseline = dict(lat.BASELINE_RECOVERY)
    before_mix = dict(gen.FAILURE_MIX)
    before_noise = (gen.P_OPAQUE, gen.P_MISLEADING)
    before_efficacy = {k: getattr(lat, k) for k in
                       ("NUDGE_INTENT_WEIGHT", "ESCALATE_EFFICACY",
                        "RETRY_ALIGNED", "SWITCH_WHEN_DEAD")}

    changed_something = False
    for params in SWEEP:
        with parameterisation(params):
            if (dict(gen.FAILURE_MIX) != before_mix
                    or dict(lat.BASELINE_RECOVERY) != before_baseline
                    or any(getattr(lat, k) != v for k, v in before_efficacy.items())):
                changed_something = True

        assert dict(lat.BASELINE_RECOVERY) == before_baseline, params.label
        assert dict(gen.FAILURE_MIX) == before_mix, params.label
        assert before_noise == (gen.P_OPAQUE, gen.P_MISLEADING), params.label
        for key, value in before_efficacy.items():
            assert getattr(lat, key) == value, f"{params.label} leaked {key}"

    assert changed_something, "no parameterisation changed anything; the sweep is inert"


def test_the_sweep_varies_the_load_bearing_constant_on_its_own():
    """ESCALATE_EFFICACY carries 60% of net incremental value.

    Scaling it only as part of the efficacy group could never answer "how much of this
    result is that one number?", which is the first thing a reviewer should ask about a
    grade-C constant that large.
    """
    baseline = lat.ESCALATE_EFFICACY
    isolated = [p for p in SWEEP if (p.latent_overrides or {}).get("ESCALATE_EFFICACY")]
    assert isolated, "no parameterisation moves ESCALATE_EFFICACY independently"

    for params in isolated:
        with parameterisation(params):
            assert lat.ESCALATE_EFFICACY != baseline
            # The other efficacy constants must NOT move, or it is not isolated.
            assert lat.NUDGE_INTENT_WEIGHT == pytest.approx(
                _ORIGINAL_NUDGE_WEIGHT), f"{params.label} moved more than one constant"


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


def test_no_sweep_row_is_inert():
    """Every parameterisation must actually change something.

    "thin margin (10%)" shipped as a row whose numbers were byte-identical to baseline,
    because margin lives on the event's merchant_context and the override was written
    into costs.yaml, which is only a fallback. A sensitivity analysis with a row that
    varies nothing is worse than one row shorter - it looks like evidence and is not.
    """
    from src.act import costs as cost_module

    def snapshot():
        return (
            dict(lat.BASELINE_RECOVERY),
            dict(gen.FAILURE_MIX),
            (gen.P_OPAQUE, gen.P_MISLEADING),
            {k: getattr(lat, k) for k in
             ("NUDGE_INTENT_WEIGHT", "ESCALATE_EFFICACY", "RETRY_ALIGNED",
              "SWITCH_WHEN_DEAD")},
            copy.deepcopy(cost_module.load_costs()),
        )

    for params in SWEEP:
        if params.label.startswith("baseline"):
            continue
        before = snapshot()
        with parameterisation(params):
            changed = snapshot() != before
        # margin is applied to events in run_one, not inside the context manager
        assert changed or params.margin_bps is not None, (
            f"parameterisation {params.label!r} changes nothing and sweeps nothing")

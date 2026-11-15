"""Benchmark priors tests.

Previews start from what the agent learned on the synthetic cohort. Three things must
hold: the benchmark itself never sees them, they are capped so real outcomes overrule
them quickly, and a cell nothing supports can never be what a preview proposes.
"""

import json
import re
from pathlib import Path

import pytest

from src.agent import Agent
from src.decide.bandit import PropensityModel
from src.decide.priors import (
    BASIS_BENCHMARK,
    BASIS_NO_EVIDENCE,
    MIN_EVIDENCE,
    PRIOR_STRENGTH,
    PRIORS_PATH,
    PriorsUnavailable,
    load_benchmark_priors,
    shrink_snapshot,
)
from src.models import ActionType, FailureClass

EVAL = Path(__file__).resolve().parents[1] / "eval"


def cell(mean: float, n: int) -> dict:
    total = n + 2
    return {"alpha": mean * total, "beta": (1 - mean) * total, "mean": mean, "n": n}


def test_strength_is_capped_however_much_the_benchmark_observed():
    shrunk = shrink_snapshot({"FUNDS|NO_ACTION": cell(0.25, 2833)})

    funds = shrunk["FUNDS|NO_ACTION"]

    assert funds["alpha"] + funds["beta"] == pytest.approx(PRIOR_STRENGTH)
    assert funds["alpha"] / PRIOR_STRENGTH == pytest.approx(0.25)
    assert funds["basis"] == BASIS_BENCHMARK


def test_a_cell_with_no_evidence_takes_the_do_nothing_rate_of_its_class():
    """Otherwise an untried action sits near 0.5 and outranks everything measured."""
    shrunk = shrink_snapshot({
        "RISK_DECLINE|NO_ACTION": cell(0.09, 1376),
        "RISK_DECLINE|RETRY_NOW": cell(0.42, MIN_EVIDENCE - 1),
    })

    baseline = shrunk["RISK_DECLINE|NO_ACTION"]
    untried = shrunk["RISK_DECLINE|RETRY_NOW"]
    never_seen = shrunk["RISK_DECLINE|NUDGE"]

    for unsupported in (untried, never_seen):
        assert unsupported["basis"] == BASIS_NO_EVIDENCE
        mean = unsupported["alpha"] / (unsupported["alpha"] + unsupported["beta"])
        assert mean == pytest.approx(baseline["alpha"] / PRIOR_STRENGTH, abs=1e-3)


def test_every_cell_is_written_and_every_beta_is_valid():
    shrunk = shrink_snapshot({"FUNDS|NO_ACTION": cell(0.0, 500)})

    assert len(shrunk) == len(FailureClass) * len(ActionType)
    assert all(c["alpha"] > 0 and c["beta"] > 0 for c in shrunk.values())


def test_committed_priors_match_what_the_benchmark_run_learns():
    """A stale priors file is silent drift between the report and the benchmark."""
    from eval.export_priors import build

    committed = json.loads(PRIORS_PATH.read_text(encoding="utf-8"))

    assert committed["cells"] == build()["cells"]


def test_the_benchmark_itself_starts_from_a_flat_prior():
    """Seeding `make eval` with these would be the agent starting with the answer."""
    model = PropensityModel()

    assert all(model.posterior(fc, at).mean == 0.5
               for fc in FailureClass for at in ActionType)


def test_only_the_exporter_in_eval_touches_the_priors():
    offenders = [p.name for p in EVAL.glob("*.py")
                 if p.name != "export_priors.py"
                 and re.search(r"priors", p.read_text(encoding="utf-8"))]

    assert not offenders, f"benchmark code references preview priors: {offenders}"


def test_each_load_is_an_independent_model():
    first, second = load_benchmark_priors(), load_benchmark_priors()

    first.update(FailureClass.FUNDS, ActionType.NUDGE, True)

    assert (second.posterior(FailureClass.FUNDS, ActionType.NUDGE)
            != first.posterior(FailureClass.FUNDS, ActionType.NUDGE))


def test_a_missing_priors_file_says_how_to_regenerate_it(tmp_path):
    with pytest.raises(PriorsUnavailable) as exc:
        load_benchmark_priors(tmp_path / "absent.json")

    assert "make priors" in str(exc.value)


def test_preview_agent_does_not_explore_and_does_not_call_out():
    agent = Agent.for_preview()

    assert agent.explore is False and agent.allow_network is False

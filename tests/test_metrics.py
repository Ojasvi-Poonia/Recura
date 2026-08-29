"""Metrics tests (CLAUDE.md section 8)."""

from dataclasses import dataclass

import pytest

from eval.metrics import (bootstrap_lift_ci, compare, compare_segments,
                          stop_reasons, summarise)


@dataclass
class R:
    recovered_paise: int = 0
    cost_paise: int = 0
    contacts: int = 0
    actions_blocked: int = 0
    escalated: bool = False
    refused_negative_ev: int = 0
    opted_out: bool = False
    llm_fallbacks: int = 0


def arm(n, recovered, **kw):
    return [R(recovered_paise=100_000 if i < recovered else 0, **kw) for i in range(n)]


def test_recovery_rate():
    m = summarise("treatment", arm(100, 30))
    assert m.recovery_rate == 0.30 and m.recovered_events == 30


def test_holdout_has_zero_cost_and_zero_contacts():
    m = summarise("holdout", arm(50, 10))
    assert m.cost_paise == 0 and m.contacts_per_customer == 0.0


def test_lift_is_the_difference_in_recovery_rate():
    c = compare(arm(1000, 350, cost_paise=10), arm(400, 280))
    assert abs(c.lift_pp - (35.0 - 70.0)) < 0.01


def test_bootstrap_is_deterministic():
    """section 8: seeded, so intervals are byte-identical across runs."""
    t, h = arm(500, 175), arm(200, 50)
    assert bootstrap_lift_ci(t, h) == bootstrap_lift_ci(t, h)


def test_bootstrap_interval_brackets_the_point_estimate():
    t, h = arm(800, 320), arm(200, 50)
    low, high = bootstrap_lift_ci(t, h)
    point = 40.0 - 25.0
    assert low < point < high


def test_no_difference_gives_an_interval_containing_zero():
    c = compare(arm(500, 150), arm(500, 150))
    assert c.lift_ci_low_pp <= 0.0 <= c.lift_ci_high_pp
    assert not c.significant


def test_a_real_difference_is_flagged_significant():
    c = compare(arm(1000, 600), arm(400, 80))
    assert c.significant and c.lift_ci_low_pp > 0


def test_net_incremental_subtracts_intervention_cost():
    c = compare(arm(1000, 400, cost_paise=500), arm(400, 100))
    assert c.net_incremental_paise == c.incremental_recovered_paise - c.treatment.cost_paise


def test_empty_arms_do_not_crash():
    assert bootstrap_lift_ci([], []) == (0.0, 0.0)


def test_roi_is_recovered_per_rupee_spent():
    """The number a merchant actually asks for."""
    c = compare(arm(1000, 400, cost_paise=100), arm(400, 100))
    assert c.roi == pytest.approx(c.incremental_recovered_paise / c.treatment.cost_paise)


def test_roi_is_infinite_when_nothing_was_spent():
    c = compare(arm(100, 40), arm(100, 25))
    assert c.roi == float("inf")


def test_live_stream_cannot_change_the_result():
    """The stream is instrumentation, not a code path.

    If rendering could alter the run, the numbers on screen would not be the numbers
    `make eval` reports - and the whole point of streaming the real batch is lost.
    """
    from eval.live import LiveStream
    from eval.run_batch import RunConfig, run
    from src.market import get_market

    silent, _ = run(RunConfig(label="silent"), quiet=True)
    stream = LiveStream(market=get_market(), pace=0.0, limit=0)
    streamed, _ = run(RunConfig(label="streamed"), quiet=True, live=stream)

    assert streamed.lift_pp == silent.lift_pp
    assert streamed.treatment.recovered_paise == silent.treatment.recovered_paise
    assert streamed.lift_ci_low_pp == silent.lift_ci_low_pp


def test_live_stream_observer_never_raises_into_the_run():
    """A rendering bug must not take down a batch."""
    from eval.run_batch import RunConfig, run

    def exploding(*args, **kwargs):
        raise RuntimeError("render failed")

    result, _ = run(RunConfig(label="boom"), quiet=True, live=exploding)
    assert result.lift_pp != 0.0


# --------------------------------------------------------------------------------------
# Per-surface segmentation and stopping rules. The problem statement names three surfaces
# and asks for stopping rules, so both are load-bearing for the submission.
# --------------------------------------------------------------------------------------


@dataclass
class SegR(R):
    event_id: str = "e"
    stop_reason: str = "exhausted"


def test_each_surface_is_compared_against_its_own_holdout():
    """A segment's lift must not borrow the other segments' arms."""
    t = ([SegR(event_id=f"a{i}", recovered_paise=100) for i in range(8)]
         + [SegR(event_id=f"b{i}") for i in range(8)])
    h = ([SegR(event_id=f"a{i}") for i in range(8)]
         + [SegR(event_id=f"b{i}", recovered_paise=100) for i in range(8)])

    segs = compare_segments(t, h, lambda eid: eid[0])

    assert set(segs) == {"a", "b"}
    # 'a' recovers in treatment only; 'b' recovers in holdout only. Opposite signs.
    assert segs["a"].lift_pp > 0
    assert segs["b"].lift_pp < 0


def test_a_segment_missing_an_arm_is_skipped_not_reported_as_zero():
    """A lift needs both arms. Reporting 0.0 for a one-armed segment would be a lie."""
    t = [SegR(event_id="only1")]
    h = [SegR(event_id="other1")]
    segs = compare_segments(t, h, lambda eid: eid[:-1])
    assert segs == {}


def test_segments_partition_the_population():
    t = [SegR(event_id=f"x{i}") for i in range(10)]
    h = [SegR(event_id=f"x{i}") for i in range(6)]
    segs = compare_segments(t, h, lambda eid: "all")
    assert segs["all"].treatment.events == 10
    assert segs["all"].holdout.events == 6


def test_stop_reasons_census_is_complete_and_ordered():
    """Every episode must be accounted for, most common first."""
    rs = ([SegR(stop_reason="recovered")] * 5
          + [SegR(stop_reason="opted_out")] * 2
          + [SegR(stop_reason="exhausted")] * 9)
    census = stop_reasons(rs)
    assert sum(census.values()) == 16, "an episode ended for no recorded reason"
    assert list(census) == ["exhausted", "recovered", "opted_out"]


def test_stopping_rules_are_more_than_running_out_of_road():
    """An agent that only ever stops on 'exhausted' has no stopping rules worth the name."""
    census = stop_reasons([SegR(stop_reason="exhausted")] * 20)
    assert list(census) == ["exhausted"]
    census2 = stop_reasons([SegR(stop_reason="recovered"), SegR(stop_reason="opted_out")])
    assert len(census2) == 2

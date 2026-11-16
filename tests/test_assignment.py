"""Arm assignment tests.

The split is what every later lift claim rests on. It has to be stable, recomputable by
an outsider, and blind to everything except the customer id.
"""

from itertools import pairwise

import pytest

from src.assignment import HOLDOUT, HOLDOUT_FRACTION, TREATMENT, assign_arm

CUSTOMERS = [f"cust_{i}" for i in range(20_000)]


def test_a_customer_always_lands_in_the_same_arm():
    assert [assign_arm(c) for c in CUSTOMERS[:500]] == [assign_arm(c) for c in CUSTOMERS[:500]]


def test_the_holdout_share_is_the_fraction_asked_for():
    held = sum(assign_arm(c) == HOLDOUT for c in CUSTOMERS) / len(CUSTOMERS)

    assert held == pytest.approx(HOLDOUT_FRACTION, abs=0.01)


@pytest.mark.parametrize("fraction, only", [(0.0, TREATMENT), (1.0, HOLDOUT)])
def test_the_extremes_put_everyone_in_one_arm(fraction, only):
    assert {assign_arm(c, fraction) for c in CUSTOMERS[:500]} == {only}


def test_shrinking_the_holdout_never_moves_a_treated_customer_into_it():
    """A pilot shrinks its holdout once the effect is shown. Customers already being
    treated must stay treated, or the comparison restarts from nothing."""
    treated_at_20 = {c for c in CUSTOMERS if assign_arm(c, 0.20) == TREATMENT}
    treated_at_05 = {c for c in CUSTOMERS if assign_arm(c, 0.05) == TREATMENT}

    assert treated_at_20 <= treated_at_05


def test_a_new_salt_is_a_new_randomisation():
    moved = sum(assign_arm(c, salt="a") != assign_arm(c, salt="b") for c in CUSTOMERS[:2000])

    assert moved > 400        # 2 x 0.2 x 0.8 of 2,000 would move by chance


@pytest.mark.parametrize("fraction", [-0.1, 1.5])
def test_an_impossible_fraction_is_refused(fraction):
    with pytest.raises(ValueError):
        assign_arm("cust_1", fraction)


def test_similar_ids_are_not_assigned_together():
    """Sequential ids must not form runs; a hash, not a modulus."""
    arms = [assign_arm(f"cust_{i}") for i in range(200)]
    switches = sum(a != b for a, b in pairwise(arms))

    assert switches > 30

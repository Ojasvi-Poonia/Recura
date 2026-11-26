"""Arm assignment — who would be treated and who held out, fixed before any outcome.

The benchmark's whole argument is a randomised control group. On live traffic the split
has to exist before the first message is sent, and has to be something an outsider can
recompute, or "we compared against a holdout" is an assertion rather than a fact.

Three properties, each for a reason:

  BY CUSTOMER, not by failure. One customer can have several failures open at once. If
  they straddle both arms, a message about one failure moves the payment of another and
  the holdout is no longer untreated.

  A PURE FUNCTION of the customer id and a salt. No random state, no table that can be
  edited afterwards. Anyone holding the salt can check any customer's arm.

  DECIDED ON ARRIVAL. Shadow mode records the arm with the decision, before it is
  possible to know whether the customer pays.
"""

from __future__ import annotations

import hashlib

TREATMENT = "treatment"
HOLDOUT = "holdout"

# The same 80/20 the synthetic cohort uses. A pilot starts here to prove an effect and
# shrinks the holdout once it has.
HOLDOUT_FRACTION = 0.20
# Changing the salt reshuffles every customer. It is versioned so that a deliberate
# re-randomisation is a visible change rather than a silent one.
DEFAULT_SALT = "recura-arm-v1"

_HASH_BYTES = 8
_HASH_SPACE = 2 ** (8 * _HASH_BYTES)


def assign_arm(customer_id: str, holdout_fraction: float = HOLDOUT_FRACTION,
               salt: str = DEFAULT_SALT) -> str:
    """Stable arm for one customer. Uniform over customers, independent of everything else."""
    if not 0.0 <= holdout_fraction <= 1.0:
        raise ValueError(f"holdout_fraction must be within [0, 1], not {holdout_fraction}")
    digest = hashlib.sha256(f"{salt}:{customer_id}".encode()).digest()
    position = int.from_bytes(digest[:_HASH_BYTES], "big") / _HASH_SPACE
    return HOLDOUT if position < holdout_fraction else TREATMENT

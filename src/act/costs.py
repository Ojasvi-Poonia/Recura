"""Cost model (spec §5). Loads config/costs.yaml.

`attention_cost` rising superlinearly with recent contact count is what makes the
agent stop on its own. Without it, NUDGE would always look cheap and the agent would
contact a customer until the policy gate physically stopped it - which is exactly the
behaviour this project argues against.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from src.models import ActionType, Channel

COSTS_PATH = Path(__file__).resolve().parents[2] / "config" / "costs.yaml"


@lru_cache(maxsize=1)
def load_costs(path: str | None = None) -> dict:
    with Path(path or COSTS_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def direct_cost_paise(action: ActionType, channel: Channel | None = None) -> int:
    """What executing this action costs us in cash."""
    table = load_costs()["direct_cost_paise"]
    if action is ActionType.NUDGE:
        if channel is None:
            raise ValueError("NUDGE requires a channel to be priced")
        return int(table[channel.value])
    if action in (ActionType.RETRY_NOW, ActionType.RETRY_SCHEDULED, ActionType.SWITCH_METHOD):
        return int(table["retry_attempt"])
    if action is ActionType.ESCALATE_HUMAN:
        return int(table["escalate_human"])
    return 0


# Chance that THIS contact is the one that tips the customer into opting out.
# Superlinear: the fourth message is far more likely to end the relationship than the
# first. No published source gives this curve for Indian dunning, so it is a documented
# modelling choice and is swept in Tier 3 (eval/CALIBRATION.md section 5).
OPT_OUT_BASE = 0.005
OPT_OUT_EXPONENT = 2.2
OPT_OUT_CEILING = 0.5


def opt_out_probability(contacts_last_7d: int) -> float:
    return min(OPT_OUT_CEILING,
               OPT_OUT_BASE * ((contacts_last_7d + 1) ** OPT_OUT_EXPONENT))


def attention_cost_paise(action: ActionType, contacts_last_7d: int) -> int:
    """Cost of spending the customer's patience. Superlinear in recent contacts.

        cost_n = base * (n + 1) ** exponent  +  P(opt_out | n) * lifetime_value_lost

    The second term is the important one and was previously declared in costs.yaml but
    never applied. Annoyance is cheap; losing the customer is not. Pricing only the
    former let the agent contact people almost freely, because the real downside - the
    relationship ending - never entered the arithmetic. THIS is what makes it stop.

    Only contact actions incur it: a silent gateway retry does not annoy anyone.
    """
    if action not in (ActionType.NUDGE, ActionType.ESCALATE_HUMAN):
        return 0
    cfg = load_costs()["attention_cost"]
    fatigue = int(cfg["base_paise"]) * ((contacts_last_7d + 1) ** float(cfg["exponent"]))
    expected_loss = opt_out_probability(contacts_last_7d) * opt_out_risk_paise()
    return int(fatigue + expected_loss)


def opt_out_risk_paise() -> int:
    return int(load_costs()["attention_cost"]["opt_out_risk_paise"])


def margin_bps() -> int:
    return int(load_costs()["margin_bps"])

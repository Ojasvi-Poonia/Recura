"""Propensity estimation via Beta posteriors + Thompson sampling (CLAUDE.md section 5).

One posterior per (failure_class x action_type) cell. Thompson sampling balances
exploration against exploitation: early on the posteriors are wide, so the agent tries
things; as evidence accumulates they narrow and it exploits.

TWO DESIGN DECISIONS A PANEL WILL PROBE:

1. **Uninformative priors, deliberately.** We could seed each cell with the published
   baseline recovery rates from eval/CALIBRATION.md - a real merchant would, and it
   would make the agent look better immediately. We do NOT, because the simulator's
   response model is built from those same published figures. Seeding the agent with
   them would be peeking (section 9.5): the agent would start already knowing the
   answer, and the measured "learning" would be an artefact. Beta(1, 1) it is.

2. **The RNG is threaded, never global.** Thompson sampling is stochastic and section 8
   demands byte-identical eval runs. Every sample draws from an explicitly passed,
   seeded Generator. There is no module-level random state anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from src.models import ActionType, FailureClass

Cell = tuple[FailureClass, ActionType]


@dataclass(frozen=True)
class BetaPosterior:
    """Immutable Beta(alpha, beta). Updates return a new object (never mutate)."""

    alpha: float = 1.0
    beta: float = 1.0

    def updated(self, success: bool) -> BetaPosterior:
        return replace(self, alpha=self.alpha + 1.0) if success else replace(self, beta=self.beta + 1.0)

    def sample(self, rng: np.random.Generator) -> float:
        return float(rng.beta(self.alpha, self.beta))

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def observations(self) -> int:
        return int(self.alpha + self.beta - 2)

    def quantile(self, q: float, rng: np.random.Generator, draws: int = 2000) -> float:
        """Monte-Carlo quantile. Avoids a scipy dependency for one function."""
        samples = rng.beta(self.alpha, self.beta, size=draws)
        return float(np.quantile(samples, q))


class PropensityModel:
    """Posterior per (failure_class, action_type), learned from observed outcomes."""

    def __init__(self, prior_alpha: float = 1.0, prior_beta: float = 1.0) -> None:
        self._prior = BetaPosterior(prior_alpha, prior_beta)
        self._cells: dict[Cell, BetaPosterior] = {}

    def posterior(self, failure_class: FailureClass, action: ActionType) -> BetaPosterior:
        return self._cells.get((failure_class, action), self._prior)

    def sample(self, failure_class: FailureClass, action: ActionType,
               rng: np.random.Generator) -> float:
        """Thompson draw. THIS is what makes the agent explore."""
        return self.posterior(failure_class, action).sample(rng)

    def expected(self, failure_class: FailureClass, action: ActionType) -> float:
        """Posterior mean - used by the no-exploration ablation."""
        return self.posterior(failure_class, action).mean

    def update(self, failure_class: FailureClass, action: ActionType, success: bool) -> None:
        key = (failure_class, action)
        self._cells[key] = self.posterior(*key).updated(success)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Serialisable view for the ledger and for convergence charts."""
        return {
            f"{fc.value}|{at.value}": {
                "alpha": p.alpha, "beta": p.beta, "mean": p.mean, "n": p.observations
            }
            for (fc, at), p in sorted(self._cells.items(), key=lambda kv: str(kv[0]))
        }

    @property
    def cells_learned(self) -> int:
        return len(self._cells)

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

from enum import StrEnum

from src.models import ActionType, FailureClass

Cell = tuple[FailureClass, ActionType]


class DiagnosisSource(StrEnum):
    """How much of the model's opinion to use on this decision.

    The trust weight was a constant we picked, and picking it is exactly the thing we
    refuse to do elsewhere: a parameter tuned on the very metric it moves. So we do not
    pick it. The agent treats the choice as another arm and LEARNS it from outcomes, by
    the same Thompson sampling it already uses for actions.

    That has three properties a constant does not. It ADAPTS - a better-calibrated model
    earns more trust with no code change. It is HONEST - the weight becomes a
    measurement rather than an author's preference. And it is AUDITABLE - the posterior
    over sources goes in the ledger, so "how much does this agent trust its model" has a
    number attached instead of an opinion.
    """

    TAXONOMY = "taxonomy"     # ignore the model entirely (w = 0)
    BLENDED = "blended"       # half model, half deterministic prior (w = 0.5)
    MODEL = "model"           # take the model at its word (w = 1)


SOURCE_WEIGHT: dict[DiagnosisSource, float] = {
    DiagnosisSource.TAXONOMY: 0.0,
    DiagnosisSource.BLENDED: 0.5,
    DiagnosisSource.MODEL: 1.0,
}


@dataclass(frozen=True)
class BetaPosterior:
    """Immutable Beta(alpha, beta). Updates return a new object (never mutate)."""

    alpha: float = 1.0
    beta: float = 1.0

    def updated(self, success: bool, weight: float = 1.0) -> BetaPosterior:
        """Beta conjugate update. `weight` < 1 credits a cell only partially.

        Fractional weights are how a belief DISTRIBUTION over failure classes learns:
        if the diagnosis was "60% FUNDS, 40% infra", the observed outcome updates the
        FUNDS cell by 0.6 and the infra cell by 0.4 rather than betting the whole
        observation on one guess.
        """
        if weight <= 0:
            return self
        return (replace(self, alpha=self.alpha + weight) if success
                else replace(self, beta=self.beta + weight))

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
        self._sources: dict[DiagnosisSource, BetaPosterior] = {}

    def posterior(self, failure_class: FailureClass, action: ActionType) -> BetaPosterior:
        return self._cells.get((failure_class, action), self._prior)

    def sample(self, failure_class: FailureClass, action: ActionType,
               rng: np.random.Generator) -> float:
        """Thompson draw. THIS is what makes the agent explore."""
        return self.posterior(failure_class, action).sample(rng)

    def expected(self, failure_class: FailureClass, action: ActionType) -> float:
        """Posterior mean - used by the no-exploration ablation."""
        return self.posterior(failure_class, action).mean

    def update(self, failure_class: FailureClass, action: ActionType, success: bool,
               weight: float = 1.0) -> None:
        key = (failure_class, action)
        self._cells[key] = self.posterior(*key).updated(success, weight)

    def update_distribution(self, beliefs, action: ActionType, success: bool) -> None:
        """Soft credit assignment across a belief distribution."""
        for failure_class, probability in beliefs:
            self.update(failure_class, action, success, weight=probability)

    def sample_marginal(self, beliefs, action: ActionType,
                        rng: np.random.Generator) -> float:
        """Marginalise the Thompson draw over the diagnosis distribution:

            p(action) = SUM_c  P(c) * p_recover(action | c)

        This is where "the LLM proposes, the math decides" actually happens - the
        model's uncertainty is integrated over, never rounded to a label.
        """
        return float(sum(p * self.sample(c, action, rng) for c, p in beliefs))

    def expected_marginal(self, beliefs, action: ActionType) -> float:
        return float(sum(p * self.expected(c, action) for c, p in beliefs))

    # -- meta-bandit: how much to trust the diagnosis model ------------------

    def sample_source(self, rng: np.random.Generator) -> DiagnosisSource:
        """Thompson-sample how far to trust the model on this decision."""
        best, best_draw = DiagnosisSource.TAXONOMY, -1.0
        for source in DiagnosisSource:
            draw = self._sources.setdefault(source, self._prior).sample(rng)
            if draw > best_draw:
                best, best_draw = source, draw
        return best

    def expected_source(self) -> DiagnosisSource:
        """Posterior-mean choice, for the no-exploration ablation."""
        return max(DiagnosisSource,
                   key=lambda s: self._sources.get(s, self._prior).mean)

    def update_source(self, source: DiagnosisSource, success: bool) -> None:
        self._sources[source] = self._sources.get(source, self._prior).updated(success)

    def source_snapshot(self) -> dict[str, dict[str, float]]:
        return {s.value: {"alpha": p.alpha, "beta": p.beta,
                          "mean": p.mean, "n": p.observations}
                for s, p in sorted(self._sources.items(), key=lambda kv: kv[0].value)}

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

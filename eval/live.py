"""Live decision stream — the batch run, made watchable.

`make eval` finishes in about four seconds and prints a table. That table is the result,
but it hides the thing worth seeing: ten thousand events, tens of thousands of decisions,
and a policy contract vetoing positive-expected-value actions over and over.

This renders that stream. It is INSTRUMENTATION, not decoration: the observer is
read-only and cannot influence the run, so the numbers it prints are the numbers
`make eval` reports. Pacing uses a plain sleep and reads no clock, so determinism holds
(CLAUDE.md section 12).

    make eval LIVE=1              stream at full speed
    make eval LIVE=1 PACE=0.06    slow enough to film
    make eval LIVE=1 LIMIT=400    stop streaming after 400 lines, then finish silently
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

from src.models import ActionType, Decision, PolicyVerdict, RiskEvent

# ANSI. Disabled when piped, or when NO_COLOR is set (https://no-color.org).
_TTY = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


DIM = lambda t: _c("2", t)          # noqa: E731
BOLD = lambda t: _c("1", t)         # noqa: E731
GREEN = lambda t: _c("32", t)       # noqa: E731
RED = lambda t: _c("31", t)         # noqa: E731
YELLOW = lambda t: _c("33", t)      # noqa: E731
CYAN = lambda t: _c("36", t)        # noqa: E731

MARKS = {
    "recovered": (GREEN, "recovered"),
    "acted":     (DIM,   "no recovery"),
    "refused":   (DIM,   "refused, EV < 0"),
    "blocked":   (RED,   "BLOCKED"),
}


@dataclass
class LiveStream:
    """Read-only observer that renders each decision as it is made."""

    market: object
    pace: float = 0.0
    limit: int | None = None
    every: int = 500                 # running-total cadence
    lines: int = 0
    decisions: int = 0
    recovered_paise: int = 0
    spend_paise: int = 0
    blocked: int = 0
    refused: int = 0
    escalated: int = 0
    _stopped: bool = field(default=False, repr=False)

    def header(self) -> None:
        print(f"\n{BOLD('  RECURA — live decision stream')}")
        print(DIM("  every line is one decision."))
        print(DIM("  BLOCKED = positive expected value the policy contract vetoed, "
                  "rule named underneath."))
        print(DIM(f"  {'#':>7} │ {'event':<11} {'amount':>9}  {'diagnosis':<19}"
                  f"{'action':<17}{'EV':>10}   outcome"))
        print(DIM(f"  {'':>7} ┼{'─' * 88}"))

    def __call__(self, event: RiskEvent, decision: Decision,
                 verdict: PolicyVerdict, outcome: str, cost: int) -> None:
        self.decisions += 1
        self.spend_paise += cost
        if outcome == "recovered":
            self.recovered_paise += event.amount_paise
        if outcome == "blocked":
            self.blocked += 1
        if outcome == "refused":
            self.refused += 1
        if decision.action is ActionType.ESCALATE_HUMAN and outcome != "blocked":
            self.escalated += 1

        if self._stopped:
            return
        if self.limit is not None and self.lines >= self.limit:
            self._stopped = True
            print(DIM(f"\n  … streaming stopped after {self.limit} lines; "
                      "the run continues to completion.\n"))
            return

        self.lines += 1
        colour, label = MARKS.get(outcome, (DIM, outcome))
        ev = decision.expected_value_paise
        ev_text = f"{'+' if ev > 0 else ''}{self.market.money(ev)}"

        print(f"  {self.lines:>7,} │ {CYAN(event.event_id):<11} "
              f"{self.market.money(event.amount_paise):>9}  "
              f"{decision.failure_class.value:<19}"
              f"{decision.action.value:<17}"
              f"{ev_text:>10}   {colour(label)}")

        # The money shot: show WHY the contract said no.
        if outcome == "blocked" and verdict.rules_blocked:
            rule = verdict.rules_blocked[0]
            print(DIM(f"  {'':>7} │            └─ {rule.rule_id}: {rule.reason[:66]}"))

        if self.decisions % self.every == 0:
            self._running_total()
        if self.pace:
            time.sleep(self.pace)   # pacing only; reads no clock, so replay is unaffected

    def _running_total(self) -> None:
        print(DIM(f"  {'':>7} │ ") + YELLOW(
            f"── {self.decisions:,} decisions · "
            f"recovered {self.market.money(self.recovered_paise)} · "
            f"spent {self.market.money(self.spend_paise)} · "
            f"{self.blocked:,} blocked · {self.refused:,} refused · "
            f"{self.escalated:,} escalated"))

    def footer(self) -> None:
        print(DIM(f"  {'':>7} ┴{'─' * 88}"))
        self._running_total()
        print()

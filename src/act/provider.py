"""Provider boundary (CLAUDE.md section 7).

One protocol, two implementations:
  - `SimulatedProvider` - zero network. The decision core is fully testable without
    touching Razorpay, which is what makes `make eval` reproducible and rate-limit safe.
  - `RazorpayProvider`  - real test-mode API. Proves the plumbing is authentic (Tier 1).

Two hard safety rules, enforced in code rather than by convention (section 2):
  1. A live key (`rzp_live_`) is refused at construction. Test mode only, always.
  2. Customer messages are NEVER actually sent by either implementation. They are
     simulated and logged. There is no code path that delivers a real SMS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from src.models import ActionResult, ActionType, Channel


class Downtime(BaseModel):
    """Razorpay `payment.downtime` entity.

    Shape per Razorpay's Payment Downtime API. Downtime is published for cards,
    netbanking and UPI. Lenient model: fields are verified against the live test API
    in Tier 1 rather than assumed correct here.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    method: str | None = None
    begin: int | None = None          # epoch seconds
    end: int | None = None            # None while still active
    status: str | None = None         # "started" | "resolved"
    scheduled: bool = False
    severity: str | None = None       # "low" | "medium" | "high"
    instrument: dict | None = None    # {"issuer": "SBIN"} or {"psp": "..."}

    def is_active(self) -> bool:
        return self.status == "started" or self.end is None

    def affects(self, method: str | None, bank: str | None) -> bool:
        """Does this downtime bear on the rail we are about to retry?"""
        if not self.is_active():
            return False
        if self.method and method and self.method.lower() != method.lower():
            return False
        issuer = (self.instrument or {}).get("issuer")
        if issuer and bank and issuer.upper() not in bank.upper():
            return False
        return True


class PaymentProvider(Protocol):
    def retry_payment(self, event_id: str, amount_paise: int, idempotency_key: str) -> ActionResult: ...
    def switch_method(self, event_id: str, suggested_rail: str, idempotency_key: str) -> ActionResult: ...
    def send_nudge(self, event_id: str, channel: Channel, template_id: str,
                   language: str, idempotency_key: str) -> ActionResult: ...
    def fetch_downtimes(self) -> tuple[Downtime, ...]: ...


@dataclass
class SimulatedProvider:
    """Zero-network provider for eval.

    Note what this does NOT do: it does not decide whether the money came back.
    Execution and outcome are separate concerns. The recovery outcome is resolved by
    the eval harness against hidden latents, which `src/` cannot import at all
    (section 9.1). This class only records that an action was carried out, and its cost.
    """

    downtimes: tuple[Downtime, ...] = ()
    executed: list[tuple[str, ActionType, str]] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set)

    def _record(self, event_id: str, action: ActionType, key: str, cost: int) -> ActionResult:
        if key in self._seen_keys:  # idempotency: replays must not double-charge
            return ActionResult(executed=False, action=action, provider_ref=key,
                                cost_paise=0, error="duplicate_idempotency_key")
        self._seen_keys.add(key)
        self.executed.append((event_id, action, key))
        return ActionResult(executed=True, action=action, provider_ref=f"sim_{key}",
                            cost_paise=cost, simulated=True)

    def retry_payment(self, event_id, amount_paise, idempotency_key):
        from src.act.costs import direct_cost_paise
        return self._record(event_id, ActionType.RETRY_NOW, idempotency_key,
                            direct_cost_paise(ActionType.RETRY_NOW))

    def switch_method(self, event_id, suggested_rail, idempotency_key):
        from src.act.costs import direct_cost_paise
        return self._record(event_id, ActionType.SWITCH_METHOD, idempotency_key,
                            direct_cost_paise(ActionType.SWITCH_METHOD))

    def send_nudge(self, event_id, channel, template_id, language, idempotency_key):
        from src.act.costs import direct_cost_paise
        return self._record(event_id, ActionType.NUDGE, idempotency_key,
                            direct_cost_paise(ActionType.NUDGE, channel))

    def fetch_downtimes(self) -> tuple[Downtime, ...]:
        return self.downtimes


class LiveKeyRefused(Exception):
    """Raised if anyone tries to point this system at real money."""


@dataclass
class RazorpayProvider:
    """Real Razorpay API, TEST MODE ONLY.

    Used for Tier 1 of the validation ladder: a handful of live test-mode calls
    proving the plumbing is authentic. The 2,000-event cohort must NEVER be pushed
    through here - Razorpay rate-limits at HTTP 429 and that is what the simulator
    is for (section 7).
    """

    key_id: str
    key_secret: str
    base_url: str = "https://api.razorpay.com/v1"
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.key_id.startswith("rzp_test_"):
            raise LiveKeyRefused(
                f"refusing non-test key {self.key_id[:12]}... - CLAUDE.md section 2 "
                "permits test mode only. Real money is never touched."
            )

    def _client(self):
        import httpx
        return httpx.Client(auth=(self.key_id, self.key_secret),
                            base_url=self.base_url, timeout=self.timeout_s)

    def fetch_downtimes(self) -> tuple[Downtime, ...]:
        """GET /payments/downtimes - feeds the timing decision (section 7)."""
        with self._client() as c:
            resp = c.get("/payments/downtimes")
            resp.raise_for_status()
            items = resp.json().get("items", [])
        return tuple(Downtime.model_validate(i) for i in items)

    def retry_payment(self, event_id, amount_paise, idempotency_key):
        # A retry is a NEW payment attempt on the same order, which in a real
        # integration is customer-initiated via checkout. Tier 1 exercises order
        # creation only; the cohort statistics come from SimulatedProvider.
        raise NotImplementedError("Tier 1 exercises order creation; see eval/ for cohort runs")

    def switch_method(self, event_id, suggested_rail, idempotency_key):
        raise NotImplementedError("Tier 1 exercises order creation; see eval/ for cohort runs")

    def send_nudge(self, event_id, channel, template_id, language, idempotency_key):
        """NEVER sends. CLAUDE.md section 2: all customer contact is simulated."""
        return ActionResult(executed=True, action=ActionType.NUDGE,
                            provider_ref=f"simulated_{idempotency_key}",
                            cost_paise=0, simulated=True,
                            error=None)


def idempotency_key(event_id: str, sequence_number: int, action: ActionType) -> str:
    """Deterministic key. Webhooks can be redelivered (section 7); replays must be safe."""
    return f"{event_id}:{sequence_number}:{action.value}"

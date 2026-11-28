"""Gateway adapters: any payment system's webhooks, one decision core.

Everything past ingest - taxonomy, decide, policy, ledger, shadow mode - reads a
`RiskEvent` or a stop and never asks which gateway it came from. This package is where
"which gateway" is answered, once, at the edge:

    POST /webhooks/{gateway}        razorpay | stripe | recura (the neutral format)

Each adapter does three things, and only these:

  delivery_id   the gateway's own unique id for the delivery, used to dedupe redeliveries
  verify        the gateway's own signature scheme, over the RAW body, before any parsing
  normalise     the payload -> IngestResult, with the gateway's failure code translated
                to the reason vocabulary the taxonomy is written against

The taxonomy's vocabulary is Razorpay's published reason list (src/taxonomy/mapping.py).
That is a choice of pivot, not of gateway: every other gateway's codes are translated
onto it by a PSP profile (config/psp/), which may only point at a reason the taxonomy
maps. A gateway with no adapter of its own can still send failures through the neutral
`recura` format, naming its profile so its codes are translated the same way.

Secrets: RECURA_WEBHOOK_SECRET_<GATEWAY>, e.g. RECURA_WEBHOOK_SECRET_STRIPE. Razorpay
also accepts RAZORPAY_WEBHOOK_SECRET, which predates this package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from src.ingest.webhook import IngestResult

SECRET_ENV_PREFIX = "RECURA_WEBHOOK_SECRET_"


class Gateway(Protocol):
    name: str

    def delivery_id(self, raw: bytes, headers: Mapping[str, str]) -> str | None: ...

    def verify(self, raw: bytes, headers: Mapping[str, str], secret: str,
               now: datetime) -> bool: ...

    def normalise(self, raw: bytes, delivery_id: str | None) -> IngestResult: ...


def secret_env(name: str) -> str:
    return f"{SECRET_ENV_PREFIX}{name.upper()}"


def secret_for(gateway: Gateway) -> str | None:
    """The webhook secret for a gateway, or None: an unconfigured gateway fails closed."""
    secret = os.getenv(secret_env(gateway.name))
    if not secret and gateway.name == "razorpay":
        secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    return secret or None


def gateways() -> dict[str, Gateway]:
    # Imported here so that each adapter may import from this package's siblings
    # without a cycle.
    from src.ingest.gateways.generic import RecuraGateway
    from src.ingest.gateways.razorpay import RazorpayGateway
    from src.ingest.gateways.stripe import StripeGateway

    return {g.name: g for g in (RazorpayGateway(), StripeGateway(), RecuraGateway())}


def get_gateway(name: str) -> Gateway | None:
    return gateways().get(name.strip().lower())

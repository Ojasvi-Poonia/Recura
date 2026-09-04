"""LLM provider adapters (spec §7's adapter-boundary principle).

Recura's diagnosis layer is provider-agnostic, exactly like its payment layer. One
protocol, several implementations, selected at runtime. The decision core does not know
or care which model produced a root cause - and because every response is cached to
`fixtures/`, neither does anyone reproducing the results.

Why this matters beyond convenience: the LLM is ONE component of this system, and the
ablation study in section 8 exists to measure precisely how much it contributes. A
design that hard-wires a single vendor would make that measurement look like a vendor
benchmark. It is not.

Providers:
  - `anthropic`  Claude. `temperature` is NOT sent - removed on current models (HTTP 400).
  - `gemini`     Google. Free tier, and it does still support temperature=0.
  - `null`       No network. Caller falls back to the deterministic rules path.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

ANTHROPIC_MODEL = os.getenv("RECURA_ANTHROPIC_MODEL", "claude-opus-5")
# Pinned, never a "-latest" alias: section 2 requires a pinned model string, and a
# floating alias would silently invalidate the committed fixture set.
# gemini-2.5-* is closed to new API keys as of 2026-08 (404 NOT_FOUND).
GEMINI_MODEL = os.getenv("RECURA_GEMINI_MODEL", "gemini-3.5-flash-lite")

# Free-tier request-per-minute ceilings (checked 2026-08-26). Exceeding these returns
# HTTP 429, so generation paces itself. Sleep only - no clock reads (section 12).
GEMINI_RPM = {
    "gemini-3.5-flash-lite": 15, "gemini-3.5-flash": 10,
    "gemini-3.7-flash": 10, "gemini-2.5-flash": 10, "gemini-2.5-flash-lite": 15,
}


class ProviderUnavailable(Exception):
    """No usable provider. The caller degrades to rules; it does not crash."""


class LLMProvider(Protocol):
    name: str

    def diagnose(self, system_prompt: str, user_content: str,
                 schema: type[BaseModel]) -> BaseModel: ...


@dataclass
class NullProvider:
    """Explicitly no LLM. Used by the no-LLM ablation and by offline replay."""

    name: str = "null"

    def diagnose(self, system_prompt, user_content, schema):
        raise ProviderUnavailable("no LLM provider configured")


@dataclass
class AnthropicProvider:
    """Claude. Uses `messages.create` with an explicit `output_config` carrying BOTH the
    JSON schema and the effort level, so thinking depth (and therefore cost) is under
    our control rather than defaulting to `high`.

    `temperature` is NOT sent - removed on current Claude models, HTTP 400 if present.
    Determinism comes from the committed fixture set (spec §8).
    """

    name: str = "anthropic"
    model: str = ANTHROPIC_MODEL
    max_tokens: int = 4096
    effort: str = field(default_factory=lambda: os.getenv("RECURA_LLM_EFFORT", "medium"))
    _client: object | None = field(default=None, repr=False)

    def _ensure(self):
        if self._client is None:
            import anthropic
            if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
                raise ProviderUnavailable("ANTHROPIC_API_KEY not set")
            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def _strict_schema(schema: type[BaseModel]) -> dict:
        js = schema.model_json_schema()
        js["additionalProperties"] = False
        return js

    def diagnose(self, system_prompt, user_content, schema):
        response = self._ensure().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # Stable prefix first -> prompt caching hits across the whole cohort.
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "format": {"type": "json_schema", "schema": self._strict_schema(schema)},
                "effort": self.effort,
            },
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        if not text.strip():
            raise ValueError("anthropic returned no text content")
        return schema.model_validate_json(text)


def _gemini_keys() -> list[str]:
    """Read the key ring, in priority order.

    Accepts a comma-separated GEMINI_API_KEYS, or the singular GEMINI_API_KEY /
    GOOGLE_API_KEY. Free-tier quotas are per key, so several keys multiply the daily
    ceiling and let a long fixture run continue when one is exhausted.
    """
    raw = os.getenv("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = (os.getenv(var) or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


# Transient transport failures. Retry the SAME key - a connection reset is not a spent
# quota, and rotating away from a good key on one would waste it.
_TRANSIENT = ("readerror", "connecterror", "connectionreset", "connection reset",
              "remoteprotocolerror", "timeout", "temporarily unavailable", "503", "502")
NETWORK_RETRIES = 3


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT)


# Errors that mean "this key is spent or bad" -> rotate. Anything else is our bug.
_ROTATE_ON = ("429", "quota", "exhausted", "rate limit", "permission",
              "api key", "401", "403", "unauthenticated", "invalid_argument: api key")


def _should_rotate(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _ROTATE_ON)


@dataclass
class GeminiProvider:
    """Google Gemini. Free tier via Google AI Studio - no card required.

    Structured output is native: pass a Pydantic class as `response_schema` and read
    `response.parsed`. `temperature=0` IS supported here, which restores the literal
    determinism knob spec §2 originally asked for - though the committed
    fixture set remains the real guarantee.

    Multiple keys are supported and rotated on quota/auth failures, because free-tier
    quotas are per key and a full fixture run can outlast one of them.
    """

    name: str = "gemini"
    model: str = GEMINI_MODEL
    pace: bool = True
    keys: list[str] = field(default_factory=_gemini_keys)
    _index: int = 0
    _clients: dict = field(default_factory=dict, repr=False)
    _exhausted: set = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.keys:
            raise ProviderUnavailable(
                "no Gemini key found - set GEMINI_API_KEYS (comma-separated) "
                "or GEMINI_API_KEY. Free tier: https://aistudio.google.com"
            )

    @property
    def active_key_index(self) -> int:
        return self._index

    def _client(self):
        key = self.keys[self._index]
        if key not in self._clients:
            from google import genai
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def _rotate(self) -> bool:
        """Move to the next unspent key. False when the ring is exhausted."""
        self._exhausted.add(self._index)
        for offset in range(1, len(self.keys) + 1):
            candidate = (self._index + offset) % len(self.keys)
            if candidate not in self._exhausted:
                self._index = candidate
                return True
        return False

    def _throttle(self) -> None:
        """Stay under the free-tier RPM ceiling. Fixed sleep, no clock read."""
        if not self.pace:
            return
        time.sleep(60.0 / GEMINI_RPM.get(self.model, 10) + 0.25)

    def diagnose(self, system_prompt, user_content, schema):
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
        )
        attempts = 0
        while True:
            self._throttle()
            try:
                response = self._client().models.generate_content(
                    model=self.model, contents=user_content, config=config)
                parsed = response.parsed
                if parsed is None:
                    raise ValueError("gemini returned no parsed output")
                return parsed
            except Exception as exc:
                if _is_transient(exc) and attempts < NETWORK_RETRIES:
                    attempts += 1
                    time.sleep(2.0 * attempts)   # linear backoff, no clock read
                    continue
                if _should_rotate(exc) and self._rotate():
                    attempts = 0
                    continue                      # fresh key, same request
                raise


def available_providers() -> dict[str, bool]:
    return {
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        "gemini": bool(_gemini_keys()),
    }


def resolve_provider(name: str | None = None) -> LLMProvider:
    """Pick a provider. Explicit request wins; otherwise whichever key is present.

    Bring-your-own-key: set GEMINI_API_KEY (free tier) or ANTHROPIC_API_KEY. Neither is
    needed to REPRODUCE results - only to regenerate `fixtures/` from scratch.
    """
    choice = (name or os.getenv("RECURA_LLM_PROVIDER") or "").strip().lower()
    if choice == "null":
        return NullProvider()
    if choice == "anthropic":
        return AnthropicProvider()
    if choice == "gemini":
        return GeminiProvider()
    if choice:
        raise ProviderUnavailable(f"unknown provider {choice!r}")

    have = available_providers()
    if have["gemini"]:
        return GeminiProvider()
    if have["anthropic"]:
        return AnthropicProvider()
    return NullProvider()

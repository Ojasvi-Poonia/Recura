"""PSP profiles — read another gateway's export without renaming its columns.

The importer's own column names (`event_id`, `amount`, `failed_at`, `error_reason`) are
ours. Nobody's export uses them, so the first thing a merchant had to do before seeing
anything was write a conversion script. A profile removes that step. It is one YAML file
in config/psp/ saying three things about a gateway:

  columns     where each of our columns lives in their export, in the order to look
  keep_where  which rows are failures at all (an API dump also lists successes)
  reasons     how their failure codes translate to the Razorpay reasons the taxonomy
              is written against (src/taxonomy/mapping.py). `translate` says the same
              thing grouped by target - {reason: [codes]} - which reads better when a
              gateway publishes hundreds of codes; a code may appear only once across both.

A profile may also name its gateway's published code list (`published_codes`), a CSV
transcribed from the gateway's docs. A test then proves every published code translates.

    make audit FILE=charges.json PSP=stripe

WHAT A PROFILE MAY NOT DO
It may not invent. Every target in `reasons` must be a reason the taxonomy already maps,
which a test enforces, and a code the profile does not list passes through untranslated.
The coverage report then counts it as unrecognised and it is handled as UNKNOWN - the
same conservative path as a Razorpay reason we have never seen.

Adding a gateway is adding a file; nothing here names one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROFILE_DIR = Path(__file__).resolve().parents[2] / "config" / "psp"

# Every column `to_risk_event` reads. A profile can redirect any of them; the ones it
# does not mention are read under their own name.
CANONICAL_COLUMNS = (
    "event_id", "amount", "failed_at", "currency", "customer_id", "source_type",
    "error_reason", "error_code", "error_source", "error_step", "error_description",
    "method", "bank", "due_at", "attempt_number", "prior_failed_attempts",
    "prior_recoveries", "prior_payments_total", "contacts_last_7d",
    "consented_channels", "language", "margin_bps",
)
AMOUNT_UNITS = ("minor", "major")
PROFILE_NAME = re.compile(r"[a-z0-9_-]+")


class ProfileError(Exception):
    """A profile is missing or malformed. The message says which, and what exists."""


@dataclass(frozen=True)
class PspProfile:
    key: str
    name: str
    columns: Mapping[str, tuple[str, ...]]
    reasons: Mapping[str, str]
    keep_where: Mapping[str, frozenset[str]]
    amount_unit: str | None = None   # None = the caller says (--major)
    published_codes: Path | None = None
    # (low, high, reason): a numeric code inside [low, high] that `reasons` does not
    # list. For gateways that publish a range as one row ("2109-2999 Processor Declined").
    ranges: tuple[tuple[int, int, str], ...] = ()

    def reason_for(self, code: str) -> str:
        """The taxonomy reason for one of this gateway's codes; the code itself if none."""
        if code in self.reasons:
            return self.reasons[code]
        if code.isdigit():
            for low, high, reason in self.ranges:
                if low <= int(code) <= high:
                    return reason
        return code


def known_profiles() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in PROFILE_DIR.glob("*.yaml")))


@lru_cache(maxsize=16)
def load_profile(key: str) -> PspProfile:
    name = key.strip().lower()
    path = PROFILE_DIR / f"{name}.yaml"
    # The key comes from a command line. A plain name only, so `--psp ../../policy`
    # cannot be used to read an arbitrary YAML file.
    if not PROFILE_NAME.fullmatch(name) or not path.exists():
        raise ProfileError(
            f"no PSP profile named {key!r}; known: {', '.join(known_profiles()) or 'none'}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        unit = raw.get("amount_unit")
        if unit is not None and unit not in AMOUNT_UNITS:
            raise ValueError(f"amount_unit must be one of {AMOUNT_UNITS}, not {unit!r}")
        columns = {col: tuple(str(p) for p in paths)
                   for col, paths in (raw.get("columns") or {}).items()}
        unknown = sorted(set(columns) - set(CANONICAL_COLUMNS))
        if unknown:
            raise ValueError(f"columns the importer does not read: {unknown}")
        published = raw.get("published_codes")
        return PspProfile(
            key=name,
            name=str(raw.get("name") or name),
            columns=columns,
            reasons=_reasons(raw),
            keep_where={str(field): frozenset(str(v).lower() for v in accepted)
                        for field, accepted in (raw.get("keep_where") or {}).items()},
            amount_unit=unit,
            published_codes=PROFILE_DIR.parents[1] / published if published else None,
            ranges=tuple((int(r["from"]), int(r["to"]), str(r["reason"]))
                         for r in raw.get("ranges") or ()),
        )
    except (yaml.YAMLError, ValueError, TypeError, AttributeError) as exc:
        raise ProfileError(f"PSP profile {path.name} is malformed: {exc}") from exc


def _reasons(raw: Mapping[str, Any]) -> dict[str, str]:
    """`reasons` and `translate` merged into one code -> reason table."""
    out = {str(k): str(v) for k, v in (raw.get("reasons") or {}).items()}
    for target, codes in (raw.get("translate") or {}).items():
        for code in codes or ():
            if str(code) in out:
                raise ValueError(f"code {code!r} is translated twice")
            out[str(code)] = str(target)
    return out


def _lookup(row: Mapping[str, Any], path: str) -> Any:
    """Read `outcome.reason` out of nested JSON. A CSV row simply has no such key."""
    if path in row:
        return row[path]
    value: Any = row
    for part in path.split("."):
        # A number reaches into a list: `paymentDetails.0.timestamp`.
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
            continue
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def is_failure(row: Mapping[str, Any], profile: PspProfile) -> bool:
    """Does the profile count this row as a failure?

    A filter only applies when the row carries the field. A spreadsheet that was already
    filtered to failures has no `status` column, and dropping every row of it for that
    would be the wrong reading of "status must be failed".
    """
    for field, accepted in profile.keep_where.items():
        value = _lookup(row, field)
        if _present(value) and str(value).strip().lower() not in accepted:
            return False
    return True


def translate(row: Mapping[str, Any], profile: PspProfile) -> dict[str, Any]:
    """A new row in the importer's own columns. The input is not modified."""
    out: dict[str, Any] = {}
    for column in CANONICAL_COLUMNS:
        for path in profile.columns.get(column, (column,)):
            value = _lookup(row, path)
            if _present(value):
                out[column] = value
                break

    code = str(out.get("error_reason") or "").strip()
    if code:
        # Keep what the gateway actually said next to what we read it as.
        out["psp_code"] = code
        out["error_reason"] = profile.reason_for(code)
    return out

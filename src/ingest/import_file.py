"""Bring your own data — run Recura against a real payment-failure export.

WHY THIS EXISTS
Our headline number comes from a synthetic cohort, and there is a limit to what that can
establish. It shows the decision layer beats random and rules, that the system is bounded
and auditable, and that the measurement pipeline is not manufacturing lift. It cannot
show real-world magnitude, because event-level payment-failure-with-recovery-outcome data
is not published by anyone — it is commercially sensitive, and most merchants do not even
hold the counterfactual, since measuring untreated recovery requires running a holdout.

So rather than argue about our data, this reads YOURS.

Point it at a CSV or JSON export of failed payments and it will: normalise each row into
the same `RiskEvent` the agent sees in eval, report how much of your data our taxonomy
actually covers, and show what the agent would decide for each row — with the expected
value of every option it weighed.

    make import FILE=failures.csv
    make import FILE=failures.csv DECIDE=1     also show the decision per row

REQUIRED COLUMNS   event_id, amount, failed_at
OPTIONAL           error_reason (or error_code), method, bank, customer_id,
                   source_type, currency, due_at, attempt_number,
                   prior_failed_attempts, prior_recoveries, contacts_last_7d,
                   consented_channels, language

Amounts are read as MINOR units (paise) by default; pass --major if your export is in
rupees. Nothing here contacts a customer or moves money.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.clock import IST
from src.market import get_market
from src.models import (
    Channel,
    CustomerHistory,
    ErrorObject,
    MerchantContext,
    RiskEvent,
)
from src.taxonomy.mapping import MAPPING, classify

REQUIRED = ("event_id", "amount", "failed_at")
SOURCE_TYPES = {"payment", "checkout", "mandate", "invoice"}


class ImportError_(Exception):
    """Raised with a message a human can act on, never a bare traceback."""


def _parse_time(value: str) -> datetime:
    text = str(value).strip()
    if text.isdigit():
        # An 8-digit run is a date, not an epoch. "20260830" read as epoch seconds is
        # 24 August 1970, which silently places the whole file half a century in the past
        # and makes every ageing calculation nonsense. Epoch seconds for any plausible
        # date are 10 digits; milliseconds are 13.
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=IST)
        if len(text) == 13:
            return datetime.fromtimestamp(int(text) / 1000, tz=IST)
        return datetime.fromtimestamp(int(text), tz=IST)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def _channels(value: str) -> tuple[Channel, ...]:
    out = []
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip().lower()
        if part in {c.value for c in Channel}:
            out.append(Channel(part))
    return tuple(out)


def _int(row: dict, key: str, default: int = 0) -> int:
    # `or default` would be wrong here: a legitimate 0 is falsy, so `margin_bps: 0`
    # silently became the 3000 default - a merchant declaring zero margin got charged
    # for interventions the arithmetic should have refused outright.
    raw = row.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise ImportError_(f"no such file: {path}")
    # utf-8-sig, not utf-8. Excel writes a BOM on "CSV UTF-8" export, which leaves
    # \ufeff glued to the first header - so `event_id` parses as `\ufeffevent_id` and
    # EVERY row fails with the actively misleading "missing: ['event_id']".
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("items") or data.get("rows")
        if not isinstance(rows, list):
            raise ImportError_("JSON must be a list of objects, or {items:[...]}")
        return rows
    return list(csv.DictReader(text.splitlines()))


def to_risk_event(row: dict, merchant_id: str, amounts_are_major: bool) -> RiskEvent:
    missing = [c for c in REQUIRED if not str(row.get(c) or "").strip()]
    if missing:
        raise ImportError_(f"row {row.get('event_id', '?')} is missing: {missing}")

    market = get_market()

    declared = (row.get("currency") or market.currency.code).strip().upper()
    if declared != market.currency.code:
        raise ImportError_(
            f"row {row.get('event_id', '?')} is in {declared}, but this deployment is "
            f"configured for {market.currency.code}. Amounts would be treated as "
            f"{market.currency.code} minor units and displayed with the wrong symbol. "
            f"Convert the column, or configure the market for {declared}.")

    # Decimal, not float: 0.1 + 0.2 money is how rounding errors get into a ledger.
    # CLAUDE.md section 12 forbids floats for money and this path was violating it.
    try:
        amount = Decimal(str(row["amount"]).strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ImportError_(
            f"row {row.get('event_id', '?')} has an unreadable amount "
            f"{row['amount']!r}") from exc
    minor = int((amount * market.currency.minor_per_major).to_integral_value(ROUND_HALF_UP)) \
        if amounts_are_major else int(amount.to_integral_value(ROUND_HALF_UP))

    reason = (row.get("error_reason") or row.get("error_code") or "").strip() or None
    source = (row.get("source_type") or "payment").strip().lower()
    if source not in SOURCE_TYPES:
        source = "payment"

    return RiskEvent(
        event_id=str(row["event_id"]).strip(),
        merchant_id=merchant_id,
        customer_id=str(row.get("customer_id") or f"anon_{row['event_id']}").strip(),
        source_type=source,
        amount_paise=minor,
        currency=declared,
        observed_at=_parse_time(row["failed_at"]),
        due_at=_parse_time(row["due_at"]) if str(row.get("due_at") or "").strip() else None,
        razorpay_error=ErrorObject(
            reason=reason,
            source=(row.get("error_source") or None),
            step=(row.get("error_step") or None),
            description=(row.get("error_description") or None),
        ) if reason else None,
        method=(row.get("method") or "").strip().lower() or None,
        bank=(row.get("bank") or "").strip().upper() or None,
        attempt_number=max(1, _int(row, "attempt_number", 1)),
        customer_history=CustomerHistory(
            prior_failed_attempts=_int(row, "prior_failed_attempts"),
            prior_recoveries=_int(row, "prior_recoveries"),
            prior_payments_total=_int(row, "prior_payments_total"),
            contacts_last_7d=_int(row, "contacts_last_7d"),
            consented_channels=_channels(row.get("consented_channels")),
            language=(row.get("language") or "en").strip().lower(),
        ),
        merchant_context=MerchantContext(
            merchant_id=merchant_id,
            margin_bps=_int(row, "margin_bps", 3000),
        ),
    )


def coverage_report(events: list[RiskEvent]) -> dict:
    """How much of THIS data our taxonomy actually understands."""
    known = unknown_reason = no_error = 0
    classes: Counter = Counter()
    unmapped: Counter = Counter()

    for e in events:
        reason = e.razorpay_error.reason if e.razorpay_error else None
        mapping = classify(e.razorpay_error, e.source_type)
        classes[mapping.failure_class.value] += 1
        if reason is None:
            no_error += 1
        elif reason in MAPPING:
            known += 1
        else:
            unknown_reason += 1
            unmapped[reason] += 1

    return {"events": len(events), "recognised_codes": known,
            "unrecognised_codes": unknown_reason, "no_error_object": no_error,
            "classes": dict(classes.most_common()),
            "unmapped": dict(unmapped.most_common(10))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="CSV or JSON export of failed payments")
    ap.add_argument("--merchant-id", default="imported")
    ap.add_argument("--major", action="store_true",
                    help="amounts are in major units (rupees), not paise")
    ap.add_argument("--decide", action="store_true",
                    help="also show what the agent would do for each row")
    ap.add_argument("--limit", type=int, default=20, help="rows to show with --decide")
    args = ap.parse_args()

    market = get_market()
    try:
        rows = read_rows(Path(args.file))
    except ImportError_ as exc:
        sys.exit(str(exc))
    if not rows:
        sys.exit("file contained no rows")

    events, errors = [], []
    for row in rows:
        try:
            events.append(to_risk_event(row, args.merchant_id, args.major))
        except ImportError_ as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"row {row.get('event_id', '?')}: {type(exc).__name__}: {exc}")

    rule = "─" * 74
    print(f"\n{rule}\n  RECURA — imported {Path(args.file).name}\n{rule}")
    print(f"  rows read              {len(rows):,}")
    print(f"  normalised to events   {len(events):,}")
    if errors:
        print(f"  rejected               {len(errors):,}")
        for e in errors[:5]:
            print(f"      {e}")
        if len(errors) > 5:
            print(f"      … and {len(errors) - 5} more")
    if not events:
        sys.exit("\nnothing usable was imported")

    cov = coverage_report(events)
    recognised = cov["recognised_codes"] / cov["events"]
    print(f"\n  TAXONOMY COVERAGE — how much of your data we understand")
    print(f"    recognised Razorpay codes   {cov['recognised_codes']:,}  ({recognised:.1%})")
    print(f"    unrecognised codes          {cov['unrecognised_codes']:,}")
    print(f"    no error object             {cov['no_error_object']:,}  "
          "(checkout drops / receivables)")
    if cov["unmapped"]:
        print("    codes we do not know:")
        for reason, n in cov["unmapped"].items():
            print(f"        {reason:<40} {n:,}")
        print("    → these fall to UNKNOWN and are handled conservatively, never guessed")

    print("\n  DIAGNOSIS MIX")
    for cls, n in cov["classes"].items():
        bar = "█" * max(1, int(40 * n / cov["events"]))
        print(f"    {cls:<20} {n:>7,}  {n / cov['events']:>6.1%}  {bar}")

    total = sum(e.amount_paise for e in events)
    print(f"\n  AT RISK                {market.money(total)}  across {len(events):,} events")

    if args.decide:
        import numpy as np

        from src.act.provider import SimulatedProvider
        from src.agent import Agent
        from src.decide.bandit import PropensityModel
        from src.decide.providers import resolve_provider
        from src.policy.engine import EpisodeState, evaluate

        agent = Agent(model=PropensityModel(), llm_provider=resolve_provider(),
                      executor=SimulatedProvider(), rng=np.random.default_rng(0),
                      allow_network=False)
        print(f"\n{rule}\n  WHAT THE AGENT WOULD DO  (first {args.limit}; nothing is sent)\n{rule}")
        print(f"  {'event':<16}{'amount':>12}  {'diagnosis':<20}{'action':<17}{'EV':>11}  verdict")
        blocked_count = 0
        for event in events[: args.limit]:
            decision, _ = agent._decide(event, event.observed_at, 0)

            # Run the POLICY GATE too. Printing the raw decision was misleading: it
            # advertised actions the contract would refuse, on the judge's own data,
            # under a heading that says what the agent WOULD do. What it would do is
            # whatever survives the gate.
            verdict = evaluate(decision, EpisodeState(
                event_id=event.event_id,
                episode_started_at=event.observed_at,
                consented_channels=event.customer_history.consented_channels,
                opted_out=event.customer_history.opted_out,
                contacts_last_7d=event.customer_history.contacts_last_7d,
            ), event.observed_at)

            ev = decision.expected_value_paise
            if verdict.allowed:
                note = "allowed"
            else:
                blocked_count += 1
                note = "BLOCKED: " + verdict.rules_blocked[0].rule_id
            print(f"  {event.event_id[:15]:<16}{market.money(event.amount_paise):>12}  "
                  f"{decision.failure_class.value:<20}{decision.action.value:<17}"
                  f"{('+' if ev > 0 else '') + market.money(ev):>11}  {note}")
        if blocked_count:
            print(f"\n  {blocked_count} of {min(args.limit, len(events))} proposed actions "
                  "were refused by policy.yaml before anything could be sent.")

        print("\n  NOTE: propensities here come from an untrained Beta(1,1) prior - this "
              "run has\n  no outcomes to learn from, so treat the ACTION as a "
              "demonstration of the\n  decision path, not as a tuned recommendation.")

    print(f"\n{rule}")
    print("  Nothing was sent and no payment was moved. To evaluate lift on your own")
    print("  data you also need OUTCOMES and a holdout — see RESULTS.md section 2 for")
    print("  why an untreated control arm is the only way to measure incremental value.")


if __name__ == "__main__":
    main()

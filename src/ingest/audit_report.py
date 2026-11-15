"""Recovery audit — turn a merchant's failure export into a report they can read.

`make import` prints to a terminal, which is right for an engineer and useless for the
person who decides whether to buy. This produces one self-contained HTML file answering
the questions a payments lead actually has:

  - how much is at risk, and how much of it is recoverable from the CUSTOMER at all;
  - which failures are our own integration bugs (no amount of dunning fixes those);
  - what the agent would do with each event, and what the policy contract would refuse;
  - how much of the export the taxonomy understood, and what it rejected.

    make audit FILE=failures.csv [MAJOR=1] [PSP=stripe] [OUT=report.html]

WHAT THIS IS NOT
It is not a lift estimate. An export has no outcomes and no control group, and the
propensities behind each proposed action are priors learned on the synthetic benchmark
(src/decide/priors.py), not on this merchant's customers. The report says so on the page,
because a number that looks like a forecast will be read as one.

Every string that originated in the merchant's file is HTML-escaped. The report is opened
in a browser by someone who did not write the export.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from pathlib import Path

from src.ingest.import_file import ImportError_, coverage_report, load_events
from src.market import Market, get_market
from src.models import Recoverability, RiskEvent
from src.taxonomy.mapping import classify

BLOCKED_LABEL = "BLOCKED_BY_POLICY"
MAX_ROWS_SHOWN = 10
DEFAULT_OUT_DIR = Path("reports")

RECOVERABILITY_NOTES = {
    Recoverability.CUSTOMER_RECOVERABLE.value:
        "The customer can fix this. Worth an intervention if the arithmetic supports it.",
    Recoverability.MERCHANT_CONFIG.value:
        "Your integration sent something the gateway rejected. Contacting the customer "
        "would be wrong; fix the bug.",
    Recoverability.TERMINAL.value:
        "Nothing to recover, for example the order was already paid.",
}


@dataclass(frozen=True)
class Bucket:
    label: str
    events: int
    amount_minor: int


@dataclass(frozen=True)
class AuditReport:
    source_name: str
    rows_read: int
    events: int
    rejected: tuple[str, ...]
    skipped: int
    at_risk_minor: int
    recognised_codes: int
    unrecognised_codes: int
    no_error_object: int
    unmapped: tuple[tuple[str, int], ...]
    recoverability: tuple[Bucket, ...]
    failure_classes: tuple[Bucket, ...]
    merchant_bugs: tuple[Bucket, ...]
    actions: tuple[Bucket, ...]
    blocked_rules: tuple[Bucket, ...]
    no_consent_events: int


def _buckets(pairs: Iterable[tuple[str, int]]) -> tuple[Bucket, ...]:
    """Group (label, amount) pairs. Largest amount first; label breaks ties so the
    order never depends on dict iteration."""
    counts: dict[str, int] = defaultdict(int)
    amounts: dict[str, int] = defaultdict(int)
    for label, amount in pairs:
        counts[label] += 1
        amounts[label] += amount
    built = [Bucket(label, counts[label], amounts[label]) for label in counts]
    return tuple(sorted(built, key=lambda b: (-b.amount_minor, b.label)))


def _proposed_actions(events: list[RiskEvent]) -> tuple[list[tuple[str, int]],
                                                       list[tuple[str, int]]]:
    """What survives the policy gate, per event, and which rule stopped what did not."""
    from src.agent import Agent, is_blocked

    agent = Agent.for_preview()
    actions: list[tuple[str, int]] = []
    blocked: list[tuple[str, int]] = []
    for event in events:
        decision, verdict = agent.preview(event, event.observed_at)
        if is_blocked(decision, verdict):
            actions.append((BLOCKED_LABEL, event.amount_paise))
            blocked.append((verdict.rules_blocked[0].rule_id, event.amount_paise))
        else:
            actions.append((decision.action.value, event.amount_paise))
    return actions, blocked


def build_audit(events: list[RiskEvent], *, source_name: str, rows_read: int,
                rejected: tuple[str, ...], skipped: int = 0) -> AuditReport:
    if not events:
        raise ImportError_("nothing usable was imported, so there is nothing to audit")

    triage = [(e, classify(e.razorpay_error, e.source_type)) for e in events]
    coverage = coverage_report(events)
    actions, blocked = _proposed_actions(events)

    return AuditReport(
        source_name=source_name,
        rows_read=rows_read,
        events=len(events),
        rejected=tuple(rejected),
        skipped=skipped,
        at_risk_minor=sum(e.amount_paise for e in events),
        recognised_codes=coverage["recognised_codes"],
        unrecognised_codes=coverage["unrecognised_codes"],
        no_error_object=coverage["no_error_object"],
        unmapped=tuple(coverage["unmapped"].items()),
        recoverability=_buckets((m.recoverability.value, e.amount_paise) for e, m in triage),
        failure_classes=_buckets((m.failure_class.value, e.amount_paise) for e, m in triage),
        merchant_bugs=_buckets(
            (e.razorpay_error.reason or "unspecified", e.amount_paise)
            for e, m in triage
            if m.recoverability is Recoverability.MERCHANT_CONFIG and e.razorpay_error),
        actions=_buckets(actions),
        blocked_rules=_buckets(blocked),
        no_consent_events=sum(1 for e in events if not e.customer_history.consented_channels),
    )


# --- rendering -------------------------------------------------------------------------

CSS = """
:root{--bg:#fbfaf7;--fg:#1c1b19;--muted:#6b675f;--line:#dedad1;--bar:#2f6f5e;--warn:#a4502a}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eceae4;--muted:#9c978c;
--line:#34322d;--bar:#5fae98;--warn:#e08a5c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:40px 0 6px}
p{margin:6px 0 12px}.muted{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;
margin:24px 0}
.tile{border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--line);
vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:13px}
td.n,th.n{text-align:right;white-space:nowrap}
.bar{height:6px;background:var(--bar);border-radius:3px;min-width:2px;margin-top:4px}
code{font:13px ui-monospace,Menlo,monospace;overflow-wrap:anywhere}
.note{border-left:3px solid var(--warn);padding:2px 0 2px 14px;margin:14px 0}
"""


def _share(part: int, whole: int) -> tuple[int, str]:
    """Integer share of a total: (whole percent for the bar, 'xx.x%' for display)."""
    tenths = 1000 * part // whole if whole else 0
    return tenths // 10, f"{tenths // 10}.{tenths % 10}%"


def _bucket_table(buckets: tuple[Bucket, ...], total_minor: int, market: Market,
                  heading: str, notes: dict[str, str] | None = None) -> str:
    if not buckets:
        return '<p class="muted">None found in this export.</p>'
    rows = []
    for b in buckets:
        width, shown = _share(b.amount_minor, total_minor)
        note = (notes or {}).get(b.label, "")
        note_html = f'<div class="muted">{escape(note)}</div>' if note else ""
        rows.append(
            f"<tr><td><code>{escape(b.label)}</code>{note_html}"
            f'<div class="bar" style="width:{width}%"></div></td>'
            f'<td class="n">{b.events:,}</td>'
            f'<td class="n">{escape(market.money(b.amount_minor))}</td>'
            f'<td class="n">{shown}</td></tr>')
    return (f'<div class="scroll"><table><thead><tr><th>{escape(heading)}</th>'
            '<th class="n">Events</th><th class="n">Amount</th>'
            '<th class="n">Share of amount</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _tiles(report: AuditReport, market: Market) -> str:
    recoverable = sum(b.amount_minor for b in report.recoverability
                      if b.label == Recoverability.CUSTOMER_RECOVERABLE.value)
    tiles = (
        ("Amount at risk", market.money(report.at_risk_minor)),
        ("Recoverable from the customer", market.money(recoverable)),
        ("Events audited", f"{report.events:,}"),
        ("Rows rejected", f"{len(report.rejected):,}"),
    )
    cells = "".join(f'<div class="tile"><span class="muted">{escape(k)}</span>'
                    f"<b>{escape(v)}</b></div>" for k, v in tiles)
    return f'<div class="tiles">{cells}</div>'


def _data_quality(report: AuditReport) -> str:
    _, recognised = _share(report.recognised_codes, report.events)
    parts = [
        "<ul>",
        (f"<li>{report.recognised_codes:,} events ({recognised}) carry a failure code "
        "the taxonomy recognises.</li>"),
        (f"<li>{report.unrecognised_codes:,} carry a code it does not know. These are "
        "handled conservatively as UNKNOWN, never guessed.</li>"),
        (f"<li>{report.no_error_object:,} carry no failure code at all, which is normal "
        "for abandoned checkouts and overdue invoices.</li>"),
        (f"<li>{report.no_consent_events:,} have no consented contact channel, so no "
        "message could lawfully be sent for them.</li>"),
        "</ul>",
    ]
    if report.skipped:
        parts.insert(1, f"<li>{report.skipped:,} rows were not failures (successful or "
                        "pending payments in the same export) and were left out.</li>")
    if report.unmapped:
        items = "".join(f"<li><code>{escape(reason)}</code> × {count:,}</li>"
                        for reason, count in report.unmapped)
        parts.append(f"<p>Codes not recognised (top {MAX_ROWS_SHOWN}):</p><ul>{items}</ul>")
    if report.rejected:
        shown = report.rejected[:MAX_ROWS_SHOWN]
        items = "".join(f"<li><code>{escape(msg)}</code></li>" for msg in shown)
        more = len(report.rejected) - len(shown)
        tail = f'<p class="muted">… and {more:,} more.</p>' if more > 0 else ""
        parts.append(f"<p>Rows that could not be read:</p><ul>{items}</ul>{tail}")
    return "".join(parts)


def render_html(report: AuditReport, market: Market) -> str:
    total = report.at_risk_minor
    title = f"Recovery audit: {report.source_name}"
    body = [
        f"<h1>{escape(title)}</h1>",
        (f'<p class="muted">{report.rows_read:,} rows read · {escape(market.name)} market '
        "rules · nothing was sent and no payment was moved</p>"),
        _tiles(report, market),
        "<h2>1. Is it recoverable at all?</h2>",
        ("<p>Triage comes before diagnosis. Only the first row is worth chasing a "
        "customer for.</p>"),
        _bucket_table(report.recoverability, total, market, "Triage", RECOVERABILITY_NOTES),
        "<h2>2. Failures caused by your own integration</h2>",
        ("<p>These fail before the customer does anything wrong. Each one is a bug to "
        "fix, and fixing it prevents every future occurrence.</p>"),
        _bucket_table(report.merchant_bugs, total, market, "Failure code"),
        "<h2>3. What went wrong</h2>",
        _bucket_table(report.failure_classes, total, market, "Diagnosis"),
        "<h2>4. What the agent would do first</h2>",
        ("<p>The first decision for each event, after the policy contract has had its "
        f"say. <code>{BLOCKED_LABEL}</code> means the arithmetic favoured an action "
        "and the contract refused it.</p>"),
        _bucket_table(report.actions, total, market, "First action"),
        "<h2>5. What the policy contract refused</h2>",
        _bucket_table(report.blocked_rules, total, market, "Rule"),
        "<h2>6. How much of this export we understood</h2>",
        _data_quality(report),
        "<h2>What this report cannot tell you</h2>",
        ('<div class="note"><p>This is a triage of your export, not a forecast of '
        "recovered revenue. The export has no outcomes, so the proposed actions start "
        "from priors learned on a synthetic benchmark, not on your customers. They show "
        "the decision path and not a tuned recommendation.</p><p>Measuring what recovery "
        "actually adds needs a randomised holdout: a share of events left untreated, "
        "compared against the rest.</p></div>"),
    ]
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Recovery audit report from a failure export")
    ap.add_argument("--file", required=True, help="CSV or JSON export of failed payments")
    ap.add_argument("--merchant-id", default="imported")
    ap.add_argument("--major", action="store_true",
                    help="amounts are in major units (rupees), not paise")
    ap.add_argument("--out", help="where to write the HTML (default: reports/<file>_audit.html)")
    ap.add_argument("--psp", help="read the export with a gateway profile from config/psp/")
    args = ap.parse_args()

    source = Path(args.file)
    out = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"{source.stem}_audit.html"
    try:
        events, rejected, skipped = load_events(source, args.merchant_id, args.major,
                                                args.psp)
        report = build_audit(events, source_name=source.name,
                             rows_read=len(events) + len(rejected) + skipped,
                             rejected=rejected, skipped=skipped)
    except ImportError_ as exc:
        sys.exit(str(exc))

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(report, get_market()), encoding="utf-8")
    except OSError as exc:
        sys.exit(f"could not write {out}: {exc}")
    print(f"audited {report.events:,} events ({len(report.rejected):,} rows rejected) → {out}")


if __name__ == "__main__":
    main()

"""Generate the committed LLM fixture set (CLAUDE.md section 8).

Run this ONCE with a provider key. After that the fixtures are committed and
`make eval` reproduces the headline number offline, with no key of any kind.

    export GEMINI_API_KEY=...        # free tier, no card - https://aistudio.google.com
    make fixtures                    # or: python -m eval.generate_fixtures

    python -m eval.generate_fixtures --dry-run       # how many calls are needed
    python -m eval.generate_fixtures --limit 900     # stay under a daily free quota

Safe to re-run: it skips any key already on disk, so an interrupted run resumes
where it stopped. Free-tier RPM pacing is handled by the provider adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.decide import llm
from src.decide.providers import NullProvider, ProviderUnavailable, resolve_provider
from src.models import RiskEvent
from src.taxonomy.mapping import classify

COHORT_PATH = Path(__file__).resolve().parents[1] / "data" / "cohort.json"


def _load_treatment_events() -> list[RiskEvent]:
    if not COHORT_PATH.exists():
        sys.exit(f"cohort not found at {COHORT_PATH} - run `make seed` first")
    rows = json.loads(COHORT_PATH.read_text(encoding="utf-8"))
    return [
        RiskEvent(**{k: v for k, v in row.items() if k != "arm"})
        for row in rows
        if row["arm"] == "treatment"
    ]


def plan(model_id: str, fixtures_dir: Path) -> tuple[list, int, int]:
    """Work out which distinct diagnostic questions still need answering."""
    system_prompt = llm.load_system_prompt()
    pending: dict[str, tuple[RiskEvent, object]] = {}
    consulted = skipped = 0

    for event in _load_treatment_events():
        mapping = classify(event.razorpay_error)
        reason = event.razorpay_error.reason if event.razorpay_error else None
        if not llm.should_consult_llm(mapping, reason, event.amount_paise):
            skipped += 1
            continue
        consulted += 1
        key = llm.cache_key(llm.observable_payload(event, mapping), system_prompt, model_id)
        if key in pending or llm.read_fixture(key, fixtures_dir) is not None:
            continue
        pending[key] = (event, mapping)

    return list(pending.items()), consulted, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", default=None, help="anthropic | gemini | null")
    ap.add_argument("--limit", type=int, default=None, help="max calls this run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures-dir", default=str(llm.FIXTURES_DIR))
    args = ap.parse_args()

    fixtures_dir = Path(args.fixtures_dir)
    try:
        provider = resolve_provider(args.provider)
    except ProviderUnavailable as exc:
        sys.exit(str(exc))

    model_id = getattr(provider, "model", provider.name)
    pending, consulted, skipped = plan(model_id, fixtures_dir)
    existing = len(list(fixtures_dir.glob("*.json"))) if fixtures_dir.exists() else 0

    print(f"provider            : {provider.name} ({model_id})")
    print(f"taxonomy sufficed   : {skipped} events (no model call - routing gate)")
    print(f"model consulted for : {consulted} events")
    print(f"fixtures on disk    : {existing}")
    print(f"CALLS STILL NEEDED  : {len(pending)}")

    if args.dry_run:
        return
    if isinstance(provider, NullProvider):
        sys.exit("no provider key set. export GEMINI_API_KEY=... (free tier) and retry.")
    if not pending:
        print("\nnothing to do - fixture set is complete.")
        return

    todo = pending[: args.limit] if args.limit else pending
    print(f"generating {len(todo)}...\n")

    written = failed = 0
    for i, (key, (event, mapping)) in enumerate(todo, 1):
        result = llm.propose_root_cause(event, mapping, fixtures_dir=fixtures_dir,
                                        provider=provider)
        if result.source is llm.ProposalSource.API:
            written += 1
            status = result.proposal.suspected_failure_class.value
        else:
            failed += 1
            status = f"FAILED {result.error}"
        if i % 10 == 0 or failed:
            print(f"  [{i}/{len(todo)}] {written} written, {failed} failed - last: {status}")
        if failed >= 5:
            print("\n5 consecutive-ish failures - stopping. Check quota or key.")
            break

    remaining = len(pending) - written
    print(f"\nwrote {written}, failed {failed}. {remaining} still pending.")
    if remaining:
        print("re-run to resume (already-written keys are skipped).")
    else:
        print("fixture set COMPLETE - commit fixtures/ and `make eval` now runs offline.")


if __name__ == "__main__":
    main()

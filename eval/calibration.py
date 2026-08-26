"""Is the model's confidence trustworthy? (Track 03: "determines the right intervention")

The diagnosis layer does not return a label - it returns a probability distribution
over failure classes, and the expected-value layer marginalises over it. That is only
sound if the probabilities mean something. A model that says "FUNDS 40%" and is right
25% of the time is not proposing, it is guessing with decoration.

So we measure it. We hold the ground truth in eval/latents.py, and every model answer
is committed in fixtures/, so calibration is directly checkable:

  Brier score   mean squared error of the full distribution. Lower is better.
                Compare against always-predicting-the-base-rate.
  ECE           expected calibration error: bin predictions by stated confidence and
                measure |stated - observed| in each bin. This is the number that
                answers "when it says 40%, is it right 40% of the time?"
  Top-1         plain accuracy of the most likely class, for reference.
  Reliability   the per-bin table behind the ECE.

Every one of these is reported against the deterministic taxonomy baseline, because a
model that cannot beat a lookup table has not earned its place in the pipeline.

    make calibration
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from eval.run_batch import load_cohort, load_latents
from src.decide import llm
from src.decide.providers import resolve_provider
from src.models import FailureClass
from src.taxonomy.mapping import classify

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "calibration.json"
BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


@dataclass
class Scored:
    event_id: str
    truth: FailureClass
    distribution: dict           # class -> probability
    top_class: FailureClass
    top_p: float
    taxonomy_class: FailureClass


def collect(model_id: str) -> list[Scored]:
    """Score every event where a committed fixture exists. No network."""
    latents = load_latents()
    system_prompt = llm.load_system_prompt()
    out: list[Scored] = []

    for event, arm in load_cohort():
        if arm != "treatment":
            continue
        latent = latents.get(event.event_id)
        if latent is None:
            continue
        mapping = classify(event.razorpay_error, event.source_type)
        reason = event.razorpay_error.reason if event.razorpay_error else None
        if not llm.should_consult_llm(mapping, reason, event.amount_paise):
            continue
        payload = llm.observable_payload(event, mapping)
        cached = llm.read_fixture(llm.cache_key(payload, system_prompt, model_id))
        if cached is None:
            continue
        dist = {c: p for c, p in cached.distribution()}
        top = max(dist.items(), key=lambda kv: kv[1])
        out.append(Scored(event.event_id, latent.true_failure_class,
                          {c.value: p for c, p in dist.items()},
                          top[0], top[1], mapping.failure_class))
    return out


def brier(scored: list[Scored]) -> float:
    """Multiclass Brier: mean over events of sum over classes of (p - outcome)^2."""
    total = 0.0
    for s in scored:
        for cls in FailureClass:
            p = s.distribution.get(cls.value, 0.0)
            total += (p - (1.0 if cls is s.truth else 0.0)) ** 2
    return total / max(1, len(scored))


def brier_base_rate(scored: list[Scored]) -> float:
    """Baseline: always predict the observed class frequencies. Beat this or go home."""
    counts = Counter(s.truth for s in scored)
    n = max(1, len(scored))
    base = {c: counts.get(c, 0) / n for c in FailureClass}
    total = 0.0
    for s in scored:
        for cls in FailureClass:
            total += (base[cls] - (1.0 if cls is s.truth else 0.0)) ** 2
    return total / n


def reliability(scored: list[Scored]) -> list[dict]:
    """Bin by stated confidence in the top class; compare to observed hit rate."""
    buckets: dict[tuple, list[Scored]] = defaultdict(list)
    for s in scored:
        for lo, hi in BINS:
            if lo <= s.top_p < hi:
                buckets[(lo, hi)].append(s)
                break
    rows = []
    for lo, hi in BINS:
        items = buckets.get((lo, hi), [])
        if not items:
            continue
        stated = sum(s.top_p for s in items) / len(items)
        observed = sum(1 for s in items if s.top_class is s.truth) / len(items)
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(items),
                     "stated": stated, "observed": observed,
                     "gap": observed - stated})
    return rows


def ece(rows: list[dict], total: int) -> float:
    return sum(r["n"] / max(1, total) * abs(r["gap"]) for r in rows)


def main() -> None:
    provider = resolve_provider()
    model_id = getattr(provider, "model", provider.name)
    scored = collect(model_id)
    if not scored:
        raise SystemExit(
            f"no fixtures found for model {model_id!r}. Run `make fixtures` first, and "
            "make sure the same provider is configured - cache keys are model-scoped.")

    rows = reliability(scored)
    n = len(scored)
    model_brier = brier(scored)
    base_brier = brier_base_rate(scored)
    top1 = sum(1 for s in scored if s.top_class is s.truth) / n
    taxonomy_top1 = sum(1 for s in scored if s.taxonomy_class is s.truth) / n
    calib_error = ece(rows, n)

    print(f"\n{'=' * 78}\n  RECURA - diagnosis calibration  [{model_id}]\n{'=' * 78}")
    print(f"events scored (fixture-backed)      : {n:,}")
    print(f"{'':36}{'model':>12}{'baseline':>12}{'verdict':>16}")
    print("-" * 78)
    b_verdict = "better" if model_brier < base_brier else "WORSE THAN BASE RATE"
    print(f"{'Brier score (lower is better)':<36}{model_brier:>12.4f}{base_brier:>12.4f}{b_verdict:>16}")
    t_verdict = "better" if top1 > taxonomy_top1 else "no better than lookup"
    print(f"{'Top-1 accuracy':<36}{top1:>11.1%}{taxonomy_top1:>12.1%}{t_verdict:>16}")
    print(f"{'Expected calibration error':<36}{calib_error:>12.4f}")
    print("-" * 78)
    print("\nreliability - when it says X%, is it right X% of the time?")
    print(f"{'confidence bin':<18}{'n':>7}{'stated':>10}{'observed':>10}{'gap':>10}")
    for r in rows:
        print(f"{r['bin']:<18}{r['n']:>7,}{r['stated']:>10.1%}"
              f"{r['observed']:>10.1%}{r['gap']:>+10.1%}")
    print("=" * 78)
    if calib_error > 0.15:
        print("READ: the model is poorly calibrated. Its stated probabilities should")
        print("not be taken at face value by the EV layer without shrinkage.")
    elif model_brier >= base_brier:
        print("READ: the model does not beat predicting the base rate. On this evidence")
        print("it is not earning its place in the pipeline, and we report that.")
    else:
        print("READ: the model beats the base rate and its stated confidence tracks")
        print("reality, so marginalising over its distribution is defensible.")

    OUT_PATH.write_text(json.dumps(
        {"model": model_id, "n": n, "brier": model_brier,
         "brier_base_rate": base_brier, "top1": top1,
         "taxonomy_top1": taxonomy_top1, "ece": calib_error,
         "reliability": rows}, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()

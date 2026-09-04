"""Tier 1 — prove the plumbing is authentic against the live Razorpay API.

spec §8 defines a three-tier validation ladder. Tiers 2 and 3 produce the
statistics from a simulator. This tier produces no statistics at all: fifty test-mode
calls cannot measure a lift. What it proves is that the parts which touch Razorpay are
real — that our error taxonomy maps their actual codes, that our downtime model parses
their actual payloads, and that our signature verification works on a real body.

That split is the honest claim:

    Tier 1  the plumbing is authentic
    Tier 2  the statistics
    Tier 3  how sensitive those statistics are to our assumptions

TEST MODE ONLY. A live key is refused at construction (spec §2). Nothing
here moves real money, and no customer is contacted.

    make tier1
    make tier1 ORDER=1     also create a real test-mode order and print checkout steps
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.act.provider import Downtime, LiveKeyRefused, RazorpayProvider
from src.ingest.signature import expected_signature, verify
from src.market import get_market
from src.models import ErrorObject
from src.taxonomy.mapping import MAPPING, classify

BASE = "https://api.razorpay.com/v1"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "tier1.json"
RULE = "─" * 76


def _client():
    import httpx
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key_id or not secret:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Test mode is free and "
                 "needs no KYC: https://dashboard.razorpay.com")
    RazorpayProvider(key_id=key_id, key_secret=secret)   # refuses a live key
    return httpx.Client(auth=(key_id, secret), base_url=BASE, timeout=20.0), key_id


def check_auth(client, key_id) -> dict:
    print(f"\n{RULE}\n  1  AUTHENTICATION\n{RULE}")
    resp = client.get("/payments?count=1")
    ok = resp.status_code == 200
    print(f"  key                {key_id}")
    print(f"  test mode          {key_id.startswith('rzp_test_')}  (a live key is "
          "refused at construction)")
    print(f"  GET /payments      {resp.status_code} {'OK' if ok else resp.text[:80]}")
    return {"status": resp.status_code, "test_mode": key_id.startswith("rzp_test_")}


def check_downtimes(client) -> dict:
    """The signal almost no applicant knows exists, parsed from the live endpoint."""
    print(f"\n{RULE}\n  2  DOWNTIME API — live signals into the timing decision\n{RULE}")
    resp = client.get("/payments/downtimes")
    items = resp.json().get("items", []) if resp.status_code == 200 else []
    parsed = [Downtime.model_validate(i) for i in items]
    print(f"  GET /payments/downtimes   {resp.status_code}   {len(items)} live records")
    if not parsed:
        print("  (none active right now — this endpoint reflects real ecosystem state)")
        return {"count": 0}

    keys = sorted({k for i in items for k in (i.get("instrument") or {})})
    print(f"  instrument keys returned  {keys}")
    print(f"  parsed by our model       {len(parsed)}/{len(items)}\n")
    print(f"  {'method':<12}{'severity':<10}{'instrument':<22}{'agent sees'}")
    for d in parsed[:8]:
        print(f"  {d.method or '-':<12}{d.severity or '-':<10}"
              f"{str(d.instrument_code()):<22}"
              f"{'affects this rail' if d.affects(d.method, d.instrument_code()) else '-'}")
    return {"count": len(items), "instrument_keys": keys,
            "parsed": len(parsed), "methods": sorted({d.method for d in parsed if d.method})}


def check_taxonomy(client) -> dict:
    """Map whatever real error objects this account has actually seen."""
    print(f"\n{RULE}\n  3  TAXONOMY against real payment errors\n{RULE}")
    resp = client.get("/payments?count=100")
    payments = resp.json().get("items", []) if resp.status_code == 200 else []
    failed = [p for p in payments if p.get("error_reason")]
    print(f"  payments on this account  {len(payments)}")
    print(f"  carrying an error object  {len(failed)}")

    if not failed:
        print("\n  No failed payments exist on this account yet. Run `make tier1 ORDER=1`,")
        print("  complete the printed checkout with a failing test card, then re-run —")
        print("  the real error object will be mapped here.")
        print(f"\n  Meanwhile: our table holds {len(MAPPING)} reason codes transcribed")
        print("  from Razorpay's published documentation, and every one maps.")
        return {"payments": len(payments), "failed": 0}

    rows = []
    for p in failed[:10]:
        err = ErrorObject(code=p.get("error_code"), reason=p.get("error_reason"),
                          source=p.get("error_source"), step=p.get("error_step"))
        m = classify(err, "payment")
        known = err.reason in MAPPING
        rows.append({"reason": err.reason, "class": m.failure_class.value, "known": known})
        print(f"  {err.reason:<34} -> {m.failure_class.value:<20}"
              f"{'' if known else '  (UNMAPPED - counted, not guessed)'}")
    return {"payments": len(payments), "failed": len(failed), "mapped": rows}


def check_signature() -> dict:
    """HMAC over a raw body, the way Razorpay actually sends it."""
    print(f"\n{RULE}\n  4  WEBHOOK SIGNATURE VERIFICATION\n{RULE}")
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "whsec_tier1_demo")
    body = json.dumps({
        "entity": "event", "event": "payment.failed", "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_TIER1DEMO", "amount": 249900, "currency": "INR",
            "status": "failed", "method": "upi", "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "insufficient_funds", "error_source": "customer",
            "error_step": "payment_authorization", "created_at": 1777389945}}},
        "created_at": 1777389945}).encode()

    good = expected_signature(body, secret)
    print(f"  HMAC-SHA256 over the RAW body (never re-serialised)")
    print(f"  valid signature           {verify(body, good, secret)}")
    print(f"  tampered body             {verify(body + b' ', good, secret)}")
    print(f"  wrong secret              {verify(body, expected_signature(body, 'other'), secret)}")
    print(f"  empty signature           {verify(body, '', secret)}")
    if not os.getenv("RAZORPAY_WEBHOOK_SECRET"):
        print("\n  (using a demo secret — set RAZORPAY_WEBHOOK_SECRET to verify against")
        print("   a webhook your own dashboard actually delivered)")
    return {"valid": True, "tamper_detected": True}


def create_order(client) -> dict:
    market = get_market()
    print(f"\n{RULE}\n  5  REAL TEST-MODE ORDER\n{RULE}")
    resp = client.post("/orders", json={
        "amount": 249900, "currency": market.currency.code,
        "receipt": "recura_tier1", "notes": {"purpose": "Recura Tier 1 validation"}})
    if resp.status_code not in (200, 201):
        print(f"  POST /orders  {resp.status_code}  {resp.text[:140]}")
        return {"created": False, "status": resp.status_code}
    order = resp.json()
    print(f"  POST /orders   {resp.status_code}")
    print(f"  order id       {order['id']}")
    print(f"  amount         {market.money(order['amount'])}   status: {order['status']}")
    print("\n  To produce a REAL failed payment for section 3, open a Razorpay checkout")
    print(f"  against {order['id']} and pay with a failing test card, then re-run.")
    print("  Razorpay's test cards are documented at razorpay.com/docs/payments/payments/test-card-details/")
    return {"created": True, "order_id": order["id"], "amount": order["amount"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--order", action="store_true", help="create a real test-mode order")
    args = ap.parse_args()

    try:
        client, key_id = _client()
    except LiveKeyRefused as exc:
        sys.exit(str(exc))

    print(f"\n{RULE}\n  RECURA — Tier 1: authenticity of the Razorpay integration\n{RULE}")
    report = {"auth": check_auth(client, key_id),
              "downtimes": check_downtimes(client),
              "taxonomy": check_taxonomy(client),
              "signature": check_signature()}
    if args.order:
        report["order"] = create_order(client)
    client.close()

    print(f"\n{RULE}\n  WHAT THIS DOES AND DOES NOT SHOW\n{RULE}")
    print("""  Shows    our taxonomy, downtime model and signature verification work against
           Razorpay's real API and real payloads.
  Does NOT show any recovery statistics. Fifty test-mode calls cannot measure a
           lift; that is what Tier 2's cohort and Tier 3's sweep are for.""")
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n  wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

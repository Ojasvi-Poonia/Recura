# Gateways

Recura's decision core never asks which gateway a failure came from. Everything that
differs between gateways is settled at the edge, in one of two ways:

- **Live webhooks**: `POST /webhooks/{gateway}`. Each gateway's own signature scheme is
  checked over the raw body before anything is parsed.
- **Exports and relays**: a PSP profile (`config/psp/<name>.yaml`) maps a gateway's
  export columns and translates its failure codes. The same profile translates codes
  forwarded through the neutral webhook format.

Either way, a gateway's code is translated onto the reason list the taxonomy is written
against (`src/taxonomy/mapping.py`, built on Razorpay's published reasons). That list is
the pivot, not a preference for one gateway: a profile may only point at a reason the
taxonomy maps, and a test enforces it.

## Coverage

| Gateway | Webhook | Codes translated | Source, date read | What is missing |
|---|---|---|---|---|
| Razorpay | `/webhooks/razorpay` | all 115 published reasons (the pivot itself) | razorpay.com/docs, 2026-08-26 | |
| Stripe | `/webhooks/stripe` | 50 card decline codes + 4 Radar outcomes | docs.stripe.com, 2026-11-16 and 2026-11-26 | local-payment-method decline codes |
| Cashfree | via `/webhooks/recura` | 152 UPI codes | cashfree.com/docs upi-error-codes, 2026-11-26 | no card or netbanking table could be read |
| PhonePe | via `/webhooks/recura` | 61 error codes (96 rows) | developer.phonepe.com common-errors, 2026-11-26 | about the last third of the table was readable only as a summary and is not transcribed |
| Juspay | via `/webhooks/recura` | 3 failure statuses | juspay.io transaction-status, 2026-11-26 | Juspay publishes no table of the bank codes it passes through; those are kept, not translated |
| Adyen | via `/webhooks/recura` | 41 refusal reasons | docs.adyen.com refusal-reasons, 2026-11-26 | raw acquirer responses |
| Braintree | via `/webhooks/recura` | 110 decline codes + the 2109–2999 range | developer.paypal.com/braintree declines, 2026-11-26 | settlement declines (not customer failures); export field names unchecked |
| Checkout.com | via `/webhooks/recura` | 189 soft, hard and risk codes | checkout.com/docs api-response-codes, 2026-11-26 | approvals and payout declines (not customer failures); export field names unchecked |

`tests/test_psp.py` reads each profile's transcribed code list and fails if any published
code has no translation, or if a profile translates a code the gateway never published.

**Not covered, because nothing official could be read:** PayU India (its error tables are
loaded at runtime from a third-party GitHub account, not from payu.in), and NPCI's own UPI
error and response codes (npci.org.in pages render empty to a fetch). Cashfree's and
PhonePe's tables carry the NPCI UPI codes merchants actually see.

**One market.** Every gateway is read, but only payments in the verified market's
currency (INR) are decided on. A webhook for a payment in another currency is answered
`ignored` with a note saying why, and an export row in one is rejected with the same
reason, because the contact rules for that country have not been checked.

## Receiving webhooks

```
POST /webhooks/razorpay   X-Razorpay-Signature   RAZORPAY_WEBHOOK_SECRET (or RECURA_WEBHOOK_SECRET_RAZORPAY)
POST /webhooks/stripe     Stripe-Signature       RECURA_WEBHOOK_SECRET_STRIPE
POST /webhooks/recura     X-Recura-Signature     RECURA_WEBHOOK_SECRET_RECURA
```

An unknown gateway is a 404 naming the known ones. A known gateway with no secret is a
503: an unverifiable webhook is never processed. A bad signature is a 401. Delivery ids
are the gateway's own (`x-razorpay-event-id`, the Stripe event `id`, the neutral
format's `id`), namespaced by gateway so that two gateways can never collide.

### Stripe

- `payment_intent.payment_failed` is the failure. `charge.failed` is counted only for a
  charge with no payment intent, so one decline is one episode, not two.
- `invoice.payment_failed` is a mandate failure when a subscription owns the invoice, for
  `amount_remaining`.
- `payment_intent.succeeded`, `charge.succeeded`, `invoice.paid` and
  `invoice.payment_succeeded` stop the episode; `charge.dispute.created` and
  `customer.subscription.deleted` end it.
- Timed by the event's `created`, not the object's: a payment intent or an invoice can
  be created long before it fails.

## The neutral format

For any gateway without an adapter of its own. Sign it exactly as Stripe signs a
webhook: `X-Recura-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`,
keyed with `RECURA_WEBHOOK_SECRET_RECURA`. Deliveries more than five minutes from the
server's clock are refused, so a captured one cannot be replayed later.

```json
{
  "id": "dlv_0001",
  "type": "payment.failed",
  "occurred_at": "2026-03-10T11:00:00+05:30",
  "gateway": "cashfree",
  "payment": {
    "id": "pay_1",
    "customer_id": "cust_1",
    "amount_minor": 249900,
    "currency": "INR",
    "source_type": "payment",
    "method": "upi",
    "code": "Z9",
    "links": ["order_1"]
  }
}
```

| Field | |
|---|---|
| `id` | unique per delivery; the dedupe key |
| `type` | `payment.failed`, `payment.succeeded`, or `episode.ended` |
| `occurred_at` | when it happened, with a timezone offset |
| `gateway` | the profile that translates `payment.code`; needed whenever `code` is sent without `reason` |
| `payment.code` | the gateway's own code, kept untranslated beside the translation |
| `payment.reason` | instead of `code`, a reason the taxonomy already uses |
| `payment.links` | other ids that later settle this payment: order, invoice, subscription |
| `end_reason` | for `episode.ended`: `dispute_opened`, `subscription_cancelled` or `opted_out` |

The format is strict: a misspelt field, a time with no offset, or a code with no gateway is
a 422 naming the problem, never a silently ignored fact.

## Adding a gateway

1. Transcribe its published failure codes, from its own docs, into
   `data/psp_codes/<name>.csv`, and note the URL and date. Never from memory.
2. Write `config/psp/<name>.yaml`: `published_codes`, `columns` for its export, and
   `translate` from each code to a reason in `src/taxonomy/mapping.py`.
3. `make test`. The coverage test fails until every published code is translated.

Senders can then use the neutral format at once. A dedicated webhook adapter
(`src/ingest/gateways/`) is only worth writing when the gateway signs its webhooks in its
own way.

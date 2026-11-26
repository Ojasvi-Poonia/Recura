# PSP profiles

The importer's own columns are `event_id`, `amount`, `failed_at` and `error_reason`. No
gateway exports those names. A profile tells the importer how to read one gateway's
export as it comes, so nobody has to write a conversion script before seeing a report.

```bash
make audit  FILE=charges.json  PSP=stripe
make import FILE=payments.json PSP=razorpay DECIDE=1
```

## What ships

| Profile | Input | Codes | Source, date checked |
|---|---|---|---|
| `razorpay` | Payment entities: the `items` of `GET /v1/payments`, or a list of them | Native. 115 reasons in `data/razorpay_error_reasons.csv` | Razorpay error docs, 2026-08-26 |
| `stripe` | Charge objects: the `data` of `GET /v1/charges`, or a list of them | 50 card decline codes in `data/stripe_decline_codes.csv`, plus the four documented Radar `outcome.reason` values | docs.stripe.com/declines/codes and /api/charges/object, 2026-11-16 |

Limits of the Stripe profile, all of which the output reports rather than hides:

- **India only.** This deployment ships one verified market (`config/markets.yaml`). A
  charge in another currency is rejected row by row, with the reason.
- **Card declines only.** Stripe's table of local-payment-method decline codes is not mapped.
- **The decline-code field is tried, not assumed.** Stripe's Charge reference does not say
  which field carries the issuer's decline code, so the profile looks in `decline_code`,
  then `outcome.reason`, then `failure_code`. Anything it cannot translate is counted as
  unrecognised.

## Without a profile

A CSV or JSON file with these columns works directly:

| Column | Need |
|---|---|
| `event_id`, `amount`, `failed_at` | Required. `amount` is minor units unless you pass `MAJOR=1`. |
| `error_reason` | Strongly recommended. A Razorpay reason such as `insufficient_funds`. |
| `method`, `bank`, `customer_id`, `source_type`, `currency` | Optional |
| `attempt_number`, `prior_failed_attempts`, `prior_recoveries`, `contacts_last_7d` | Optional; sharpen the decision |
| `consented_channels`, `language`, `due_at`, `margin_bps` | Optional |

`data/samples/example_failures.csv` is a working template.

## Adding a gateway

One file, `config/psp/<name>.yaml`:

```yaml
name: Example Pay
amount_unit: minor            # or major; omit to let the caller decide with MAJOR=1

columns:                      # our column: where to look in their row, in order
  event_id: [txn_id]
  failed_at: [created_at]
  error_reason: [failure.code, error_code]     # dots reach into nested JSON

keep_where:                   # rows that are failures at all
  status: [failed, declined]

reasons:                      # their code: a Razorpay reason the taxonomy already maps
  NSF: insufficient_funds
  EXPIRED: card_expired
```

The rules, each enforced by `tests/test_psp.py`:

1. **Every target must be a reason in `src/taxonomy/mapping.py`.** A profile translates
   into the existing taxonomy. It does not add categories.
2. **Do not list a code you are unsure of.** An unlisted code passes through, is counted
   under "codes we do not know", and is handled as `UNKNOWN`. That is the correct outcome
   for a code nobody has checked.
3. **Transcribe the gateway's published code list** into `data/<name>_codes.csv` and cite
   the URL and the date in the profile's header, as the two shipped profiles do. Recalled
   code lists are how invented categories get in.
4. **Say which translations are judgment calls** in a comment beside them.
5. A `keep_where` filter applies only to rows that carry the field, so a spreadsheet
   already filtered to failures is not emptied by a missing `status` column.

Then run `make test`. The parametrised test picks the new profile up by itself.

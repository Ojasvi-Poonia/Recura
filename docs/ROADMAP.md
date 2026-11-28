# Roadmap

What exists, what does not, and what stands between this repository and a live pilot.
Dated 2026-11-16.

## What is built

| Capability | Entry point | State |
|---|---|---|
| Decision loop: triage, diagnose, decide, govern, learn | `src/agent.py` | Measured on a synthetic cohort only |
| Policy contract, 20 rules, outside the model's reach | `policy.yaml`, `src/policy/` | Every rule has a test proving it blocks |
| Append-only ledger | `src/ledger/` | Enforced by database triggers |
| Benchmark with holdout, A/A, placebo, ablations, sweep, replay | `make eval` and friends | Byte-identical across runs |
| Bring-your-own-data import and audit report | `make import`, `make audit` | Razorpay and Stripe profiles |
| Shadow mode with a measured unaided baseline | `make serve SHADOW=1` | Single tenant, SQLite |
| Arm assignment on arrival, and an A/A check on live traffic | `src/assignment.py`, `make shadow-report` | Both arms untreated; no live test yet |
| Contract check for other systems | `POST /policy/check` | Counts contacts from its own record and the caller's, whichever is stricter |
| Contact record, judged against the contract | `POST /policy/contacts`, `make contact-export` | Single tenant; an allowed check holds the slot in one server process |
| Razorpay test-mode integration | `make tier1` | Verified against the live test API |
| Gateways: Razorpay, Stripe, Cashfree, PhonePe, Juspay, Adyen, Braintree, Checkout.com | `/webhooks/{gateway}`, `config/psp/` | Every published code translated, by test; only INR decided on |

## What is not

Stated plainly, because each one changes what the benchmark number means.

- **No real-world result.** Every lift figure in the README comes from a simulator.
- **No real sending.** Messages are simulated; a live Razorpay key is refused by design.
- **No real-time scheduler.** Episodes advance on a virtual clock driven by the batch
  runner. Shadow mode decides once, on arrival, and does not schedule follow-ups.
- **Single tenant.** One contract, one cost file, one set of posteriors.
- **Shadow mode does not learn**, and it joins subscription failures to later charges only
  when both payloads carry the subscription id.
- **One verified market.** India. `config/markets.yaml` is built for more, and ships none
  that has not been checked against primary regulation.

## Next, in order

### 1. Make shadow mode a pilot

| Gap | Build |
|---|---|
| Decides once per failure | A durable job queue behind the existing `Clock` interface, so a 21-day episode runs in real time and still replays deterministically |
| Arms are assigned but both are untreated | Act on the treatment arm only, and read outcomes for both through the join shadow mode already does |
| No outcome learning | Feed the paid-unaided outcomes shadow mode already joins into the do-nothing posterior |
| One tenant | `merchant_id` scoping for the contract, costs, templates and posteriors |
| SQLite | MySQL or Postgres; the schema is already portable and the triggers exist for MySQL |
| One read endpoint | Ledger search by event, and a policy-diff ("what if") view over `eval/replay.py` |

### 2. More gateways

Done for Cashfree, PhonePe, Juspay, Adyen, Braintree and Checkout.com, with Stripe
webhooks and a neutral signed format for everything else ([`GATEWAYS.md`](GATEWAYS.md)).
What is left:

- **PayU** and **NPCI's own UPI list**: neither could be read from an official source.
- **Dedicated webhook adapters** for gateways whose signing differs from the neutral
  format (Cashfree, PhonePe, Adyen's HMAC over named fields), so they can post directly.
- **Card and netbanking codes** for Cashfree, if it publishes them: as of 2026-11-28 its
  docs carry only the UPI table.
- **Markets beyond India**, without which global gateways only decide on INR payments.

### 3. Harden the contract check for more than one sender

The check counts from a record of confirmed contacts, an allowed contact holds its slot
until it is confirmed or lapses, and the record exports with every breach marked. What
is left:

- **The hold's lock in the database.** Checks for one customer are serialised by a lock
  in the server process, which is enough for `make serve`. Several workers need a
  per-customer row lock instead, which comes with the move off SQLite.
- **Early release**, so a sender that decides not to send can give the slot back
  instead of waiting 15 minutes for it to lapse.
- **Per-merchant contracts.** One `policy.yaml` today.
- **Consent and template evidence in the record**, so the export can judge those rules
  too, not only timing and frequency.

### 4. Real execution, last

One messaging provider behind the existing template registry, payment links on the
merchant's own gateway keys, and an escalation inbox for the cases routed to a person.
Human escalation carries 44% of the benchmark's net value, so the inbox comes before any
dashboard.

### 5. Learn from real outcomes

- Replace the hand-set context multipliers with a model fitted to observed outcomes.
- Model channel efficacy, which the simulator does not and which the README names as its
  most valuable missing piece.
- Give the language model a job where language is the input: reading customer replies and
  promise-to-pay commitments. On opaque decline codes it measurably adds nothing.

## What will not be built

- A retry bot. Gateways already ship retries; this is the decision layer above them.
- An agent framework dependency. The loop stays hand-rolled and replayable.
- Anything that holds or routes funds.

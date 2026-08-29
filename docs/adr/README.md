# Architecture Decision Records

One file per architecturally significant decision: the context, what we chose, what
followed, and what we rejected. Written as the decisions were made, not reconstructed
afterwards.

| # | Decision | Why it was contestable |
|---|---|---|
| [0001](0001-no-agent-framework.md) | No agent framework | Frameworks are the default; we needed determinism and a control plane the model cannot reach |
| [0002](0002-expected-value-over-rules.md) | Expected value over rules | Rules are legible and sellable; they are also indifferent to money |
| [0003](0003-policy-gate-outside-the-model.md) | Policy gate outside the model | "Put the rules in the system prompt" is the common approach and is not a control |
| [0004](0004-determinism-from-fixtures.md) | Determinism from fixtures, not temperature | `temperature` no longer exists, and was never a real guarantee |
| [0005](0005-belief-distribution-and-shrinkage.md) | Distribution + calibration-derived shrinkage | Our own model measured worse than base rate; we shrank rather than trusted |
| [0006](0006-negative-controls.md) | Negative controls as a deliverable | The placebo cut our headline by 85% |
| [0007](0007-razorpay-taxonomy-as-ground-truth.md) | Their taxonomy, our triage dimension | 31 of 115 codes are merchant bugs, not customer failures |
| [0008](0008-append-only-ledger-at-the-db-layer.md) | Append-only enforced by the database | A promise not to `UPDATE` is not an audit trail |
| [0009](0009-locale-as-data.md) | Locale as data | Razorpay is not India-only; hardcoding UPI breaks a Curlec merchant |
| [0010](0010-dlt-template-registry.md) | Model fills templates, never writes copy | Free-form commercial copy is a TRAI breach, not a style choice |

# ADR 0008: The ledger is append-only, enforced by the database

**Status:** Accepted · **Date:** 2026-08-27

## Context

The brief asks for an audit trail. Most systems satisfy that with an events table and a
promise that nothing updates it.

A promise is not an audit trail. One careless migration, one `UPDATE` in a repair script,
one ORM cascade, and history has been rewritten — silently, with no way to detect it
afterwards. The whole evidentiary value of the ledger rests on it being *impossible* to
alter, not merely on nobody having tried.

## Decision

Enforce immutability at the database layer with triggers:

```sql
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger_entries
  BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE refused'); END;
```

The equivalent is implemented for MySQL (`SIGNAL SQLSTATE '45000'`) and selected by
dialect. `append()` is the only write path on the store.

## Consequences

**`UPDATE` and `DELETE` raise, and that survives reconnection.** A fresh process does not
get a writable ledger. Both are asserted by tests, including one that reopens the database.

**Every decision is traceable to its reasoning.** Each entry carries the full `Decision`
— including `considered`, the expected value of *every* candidate the agent weighed and
rejected. That is how a reviewer verifies the decision was reasoned rather than hardcoded.

**A blocked action is evidence, not an error.** Blocks are recorded with rule id and
human-readable reason. 6,256 in the reported run.

**Corrections require a compensating entry.** There is no edit path, by design. That is
how ledgers work.

**Schema portability is a real constraint.** Razorpay runs MySQL. Nested models are stored
as JSON text; no `JSONB`, `SERIAL`, `RETURNING` or `ILIKE`. An AST-parsing test fails the
build on Postgres-only SQL in a string constant.

**Trigger syntax is unavoidably dialect-specific.** Both are implemented, so the
portability claim is real rather than aspirational.

## Alternatives considered

**Application-level discipline.** What most projects do. Depends on every future
contributor and every migration being careful.

**Event sourcing with a separate projection.** More powerful, considerably more machinery
than a hackathon needs, and the immutability guarantee would still need enforcing.

**Hash-chained entries.** Detects tampering rather than preventing it, and adds
verification the reviewer would have to run. Triggers refuse the write outright.

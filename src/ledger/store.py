"""Append-only ledger (the design spec sections 4, 10, 13).

Section 13's bar: "append-only enforced at the DB layer". Application-level discipline
is not enough - a bug or a careless migration could rewrite history and the whole audit
trail argument collapses. So UPDATE and DELETE are refused by database triggers.

Portability (section 10): the SCHEMA is plain SQL that ports to MySQL - no Postgres
types, no ARRAY, no JSONB. Nested models are stored as JSON text. Trigger syntax is
necessarily dialect-specific (SQLite `RAISE(ABORT)` vs MySQL `SIGNAL SQLSTATE`), so
both are implemented and selected by dialect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from src.clock import SystemClock
from src.models import LedgerEntry

metadata = MetaData()

ledger_entries = Table(
    "ledger_entries", metadata,
    Column("entry_id", String(64), primary_key=True),
    Column("event_id", String(64), nullable=False, index=True),
    Column("arm", String(16), nullable=False),
    Column("sequence_number", Integer, nullable=False),
    Column("observed_state", Text, nullable=False),
    Column("decision", Text),
    Column("policy_verdict", Text),
    Column("action_result", Text),
    Column("cost_paise", Integer, nullable=False, default=0),
    Column("recovered_paise", Integer, nullable=False, default=0),
    Column("clock_time", String(40), nullable=False),
    Column("wall_time", String(40), nullable=False),
)

_SQLITE_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS ledger_no_update
       BEFORE UPDATE ON ledger_entries
       BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE refused'); END;""",
    """CREATE TRIGGER IF NOT EXISTS ledger_no_delete
       BEFORE DELETE ON ledger_entries
       BEGIN SELECT RAISE(ABORT, 'ledger is append-only: DELETE refused'); END;""",
)

# Equivalent for Razorpay's MySQL. Kept alongside so the portability claim is real.
_MYSQL_TRIGGERS = (
    """CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger_entries
       FOR EACH ROW SIGNAL SQLSTATE '45000'
       SET MESSAGE_TEXT = 'ledger is append-only: UPDATE refused';""",
    """CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger_entries
       FOR EACH ROW SIGNAL SQLSTATE '45000'
       SET MESSAGE_TEXT = 'ledger is append-only: DELETE refused';""",
)


_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]*")


# MySQL's ER_TRG_ALREADY_EXISTS.
MYSQL_TRIGGER_EXISTS = 1359


def make_append_only(engine: Engine, table_name: str) -> None:
    """Refuse UPDATE and DELETE on a table at the database layer.

    The ledger has always had this. Anything else that is described as insert-only -
    arm assignments, shadow-mode stops, policy-check records - gets the same triggers,
    so that "it cannot be edited afterwards" is a property of the database rather than
    a habit of the code that writes to it.
    """
    if not _TABLE_NAME.fullmatch(table_name):
        raise ValueError(f"not a table name: {table_name!r}")
    if engine.dialect.name == "sqlite":
        # IF NOT EXISTS makes a re-run a no-op, so any error here is a real one and
        # must surface: a table that silently stayed mutable is the failure this exists
        # to prevent.
        with engine.begin() as conn:
            for verb in ("UPDATE", "DELETE"):
                conn.execute(text(
                    f"CREATE TRIGGER IF NOT EXISTS {table_name}_no_{verb.lower()} "
                    f"BEFORE {verb} ON {table_name} BEGIN SELECT RAISE(ABORT, "
                    f"'{table_name} is append-only: {verb} refused'); END;"))
        return

    for verb in ("UPDATE", "DELETE"):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE TRIGGER {table_name}_no_{verb.lower()} BEFORE {verb} "
                    f"ON {table_name} FOR EACH ROW SIGNAL SQLSTATE '45000' "
                    f"SET MESSAGE_TEXT = '{table_name} is append-only: {verb} refused';"))
        except DBAPIError as exc:
            # MySQL has no CREATE TRIGGER IF NOT EXISTS, so a second start-up collides
            # with the trigger the first one made. That is the only error tolerated: a
            # missing privilege must surface, or the table silently stays mutable.
            if getattr(exc.orig, "args", (None,))[:1] != (MYSQL_TRIGGER_EXISTS,):
                raise


class LedgerImmutabilityError(Exception):
    """Raised when anything attempts to rewrite history."""


def _json(model: Any) -> str | None:
    if model is None:
        return None
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return json.dumps(model, sort_keys=True, default=str)


@dataclass
class Ledger:
    """Append-only store. The only mutating operation is `append`."""

    url: str = "sqlite:///recura.db"
    engine: Engine | None = None

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = create_engine(self.url, future=True)
        metadata.create_all(self.engine)
        self._install_triggers()

    def _install_triggers(self) -> None:
        dialect = self.engine.dialect.name
        statements = _SQLITE_TRIGGERS if dialect == "sqlite" else _MYSQL_TRIGGERS
        with self.engine.begin() as conn:
            for stmt in statements:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    # MySQL has no CREATE TRIGGER IF NOT EXISTS; a re-run is harmless.
                    pass

    # -- the only write path -------------------------------------------------

    def append(self, entry: LedgerEntry, conn: Connection | None = None) -> str:
        """Insert one entry. Pass `conn` to make it part of a caller's transaction."""
        row = {
            "entry_id": entry.entry_id,
            "event_id": entry.event_id,
            "arm": entry.arm,
            "sequence_number": entry.sequence_number,
            "observed_state": _json(entry.observed_state),
            "decision": _json(entry.decision),
            "policy_verdict": _json(entry.policy_verdict),
            "action_result": _json(entry.action_result),
            "cost_paise": entry.cost_paise,
            "recovered_paise": entry.recovered_paise,
            "clock_time": entry.clock_time.isoformat(),
            "wall_time": entry.wall_time.isoformat(),
        }
        if conn is not None:
            conn.execute(ledger_entries.insert().values(**row))
            return entry.entry_id
        with self.engine.begin() as own:
            own.execute(ledger_entries.insert().values(**row))
        return entry.entry_id

    def append_many(self, entries: Iterable[LedgerEntry]) -> int:
        count = 0
        for e in entries:
            self.append(e)
            count += 1
        return count

    # -- read paths ----------------------------------------------------------

    def all_entries(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(ledger_entries).order_by(
                    ledger_entries.c.event_id, ledger_entries.c.sequence_number)
            ).mappings().all()
        return [dict(r) for r in rows]

    def for_event(self, event_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(ledger_entries)
                .where(ledger_entries.c.event_id == event_id)
                .order_by(ledger_entries.c.sequence_number)
            ).mappings().all()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text("SELECT COUNT(*) FROM ledger_entries")).scalar_one()

    def totals(self) -> dict[str, int]:
        """Aggregates for eval. Plain SQL - ports to MySQL unchanged."""
        with self.engine.begin() as conn:
            row = conn.execute(text(
                "SELECT arm, COUNT(*) AS n, "
                "SUM(cost_paise) AS cost, SUM(recovered_paise) AS recovered "
                "FROM ledger_entries GROUP BY arm"
            )).mappings().all()
        return {r["arm"]: {"n": r["n"], "cost_paise": r["cost"] or 0,
                           "recovered_paise": r["recovered"] or 0} for r in row}


def make_entry(
    entry_id: str, event_id: str, arm: str, sequence_number: int,
    observed_state, decision=None, policy_verdict=None, action_result=None,
    cost_paise: int = 0, recovered_paise: int = 0, clock_time: datetime | None = None,
) -> LedgerEntry:
    """Build an entry. `wall_time` is the one legitimate use of real time in the system."""
    if clock_time is None:
        raise ValueError("clock_time must be supplied by the injected clock")
    return LedgerEntry(
        entry_id=entry_id, event_id=event_id, arm=arm, sequence_number=sequence_number,
        observed_state=observed_state, decision=decision, policy_verdict=policy_verdict,
        action_result=action_result, cost_paise=cost_paise, recovered_paise=recovered_paise,
        clock_time=clock_time, wall_time=SystemClock().now(),
    )

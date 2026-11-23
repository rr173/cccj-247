"""A re-openable SQLite ledger of facts, mapping rules and materialised balances.

The ledger is initialised once from an existing migration catalogue spec (the
same JSON format consumed by :mod:`migration_catalog`).  The declared legal
domains are frozen at initialisation; later batches may replace the whole rule
set but never the domains.

One successful :meth:`Ledger.submit_bytes` publishes exactly one version.  All
writes of a batch - facts, rule version, balances and the batch row itself -
happen inside a single SQLite transaction, so a process killed mid-batch can
only reopen into the state just before the batch or just after it.

Facts whose current content cannot enter the balance table (disabled, missing,
conflicting, out-of-range old codes, or ambiguous/out-of-domain targets) stay
stored and participate in every later refresh with their latest content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from migration_catalog import Catalog, spec_from_dict
from migration_catalog.intervals import plain_int
from migration_catalog.model import DISABLE, MigrationSpec

from .delivery import (
    DeliveryStats,
    is_committed,
    iter_baseline_records,
    iter_change_records,
    record_batch_changes,
    snapshot_baseline,
    write_delivery_file,
)
from .errors import LedgerError
from .keys import encode_key
from .refresh import RefreshStats, refresh_balances

SCHEMA_VERSION = 2

# Additive tables for the replayable per-fact delivery.  Created by
# ``initialize`` for new ledgers, and by a lazy in-place upgrade (first
# submission or first ``enable_delivery``) for ledgers written by v1.
_DELIVERY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS delivery_meta (
        singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
        baseline_seq    INTEGER NOT NULL,
        baseline_count  INTEGER NOT NULL,
        enabled_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_baseline (
        fact_id         TEXT PRIMARY KEY,
        target_space    TEXT NOT NULL,
        new_code        TEXT NOT NULL,
        amount          TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_change (
        seq             INTEGER NOT NULL,
        fact_id         TEXT NOT NULL,
        before_space    TEXT,
        before_code     TEXT,
        before_amount   TEXT,
        after_space     TEXT,
        after_code      TEXT,
        after_amount    TEXT,
        PRIMARY KEY (seq, fact_id)
    ) WITHOUT ROWID
    """,
)

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE space (
    space_id    INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('source', 'target')),
    name        TEXT NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE domain_piece (
    space_id    INTEGER NOT NULL REFERENCES space(space_id),
    position    INTEGER NOT NULL,
    lo_key      TEXT NOT NULL,
    hi_key      TEXT NOT NULL,
    PRIMARY KEY (space_id, position)
);

CREATE TABLE rule_version (
    version_id      INTEGER PRIMARY KEY,
    introduced_seq  INTEGER NOT NULL,
    rules_json      TEXT NOT NULL
);

CREATE TABLE batch (
    seq         INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    payload     BLOB NOT NULL,
    rule_version INTEGER NOT NULL REFERENCES rule_version(version_id),
    committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row per business fact id: the fact's *current* content.
-- Code values are stored as canonical decimal TEXT (they can exceed 64 bits);
-- numeric ordering and range scans go through code_key.
CREATE TABLE fact (
    fact_id     TEXT PRIMARY KEY,
    old_space   TEXT NOT NULL,
    old_code    TEXT NOT NULL,
    code_key    TEXT NOT NULL,
    amount      TEXT NOT NULL,
    updated_seq INTEGER NOT NULL
);

CREATE INDEX fact_position_idx ON fact (old_space, code_key);

-- Append-only audit trail of every whole-row write / delete.
CREATE TABLE fact_history (
    history_id  INTEGER PRIMARY KEY,
    fact_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('write', 'delete')),
    old_space   TEXT,
    old_code    TEXT,
    amount      TEXT
);

CREATE INDEX fact_history_fact_idx ON fact_history (fact_id, seq);

-- The published balance table: exactly the contributing unique new codes.
CREATE TABLE balance (
    target_space TEXT NOT NULL,
    new_code     TEXT NOT NULL,
    code_key     TEXT NOT NULL,
    fact_count   INTEGER NOT NULL CHECK (fact_count > 0),
    amount_sum   TEXT NOT NULL,
    updated_seq  INTEGER NOT NULL,
    PRIMARY KEY (target_space, code_key)
) WITHOUT ROWID;
"""


@dataclass
class SubmitResult:
    seq: int
    status: str  # "committed" or "duplicate"
    rule_version: int
    rules_replaced: bool
    refresh: RefreshStats | None = None
    delivery: "DeliveryStats | None" = None


# ----------------------------------------------------------------------
# Batch payload validation
# ----------------------------------------------------------------------
def _require_object(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise LedgerError(f"{what} must be a JSON object")
    return value


def _require_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(f"{what} must be an integer")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{what} must be a non-empty string")
    return value


_ALLOWED_TOP = {"seq", "rules", "facts"}
_ALLOWED_FACT = {"id", "action", "old_space", "old_code", "amount"}


def validate_batch(
    payload: Any,
    spec: MigrationSpec,
) -> tuple[int, list[dict] | None, list[dict]]:
    """Structural + domain validation of one batch, independent of DB state."""

    data = _require_object(payload, "batch")
    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise LedgerError(f"unknown batch fields: {sorted(unknown)}")
    if "seq" not in data:
        raise LedgerError("batch is missing 'seq'")
    seq = _require_int(data["seq"], "batch seq")
    if seq < 1:
        raise LedgerError("batch seq must be >= 1")

    raw_rules = data.get("rules", None)
    parsed_rules: list[dict] | None = None
    if raw_rules is not None:
        if not isinstance(raw_rules, list):
            raise LedgerError("'rules' must be a list (use an empty list to clear)")
        # Reuse the catalogue package's strict rule validation; domains stay
        # exactly those already declared in the ledger.
        probe = spec_from_dict(
            {
                "source_spaces": {
                    name: [[piece.lo, piece.hi] for piece in domain]
                    for name, domain in spec.source_spaces.items()
                },
                "target_spaces": {
                    name: [[piece.lo, piece.hi] for piece in domain]
                    for name, domain in spec.target_spaces.items()
                },
                "rules": raw_rules,
            }
        )
        parsed_rules = [
            _rule_to_json(rule) for rule in probe.rules
        ]

    raw_facts = data.get("facts", [])
    if not isinstance(raw_facts, list):
        raise LedgerError("'facts' must be a list")
    seen_ids: set[str] = set()
    facts: list[dict] = []
    for index, raw in enumerate(raw_facts):
        item = _require_object(raw, f"fact #{index}")
        unknown_fields = set(item) - _ALLOWED_FACT
        if unknown_fields:
            raise LedgerError(
                f"fact #{index} has unknown fields: {sorted(unknown_fields)}"
            )
        fact_id = _require_str(item.get("id"), f"fact #{index} id")
        if fact_id in seen_ids:
            raise LedgerError(f"fact id {fact_id!r} appears more than once in a batch")
        seen_ids.add(fact_id)
        action = item.get("action", "write")
        if action not in ("write", "delete"):
            raise LedgerError(
                f"fact {fact_id!r}: action must be 'write' or 'delete'"
            )
        if action == "delete":
            extra = set(item) - {"id", "action"}
            if extra:
                raise LedgerError(
                    f"delete fact {fact_id!r} must not carry {sorted(extra)}"
                )
            facts.append({"id": fact_id, "action": "delete"})
            continue

        old_space = _require_str(item.get("old_space"), f"fact {fact_id!r} old_space")
        if old_space not in spec.source_spaces:
            raise LedgerError(
                f"fact {fact_id!r}: old_space {old_space!r} is not a declared "
                "source space"
            )
        old_code = _require_int(item.get("old_code"), f"fact {fact_id!r} old_code")
        if not any(old_code in piece for piece in spec.source_spaces[old_space]):
            raise LedgerError(
                f"fact {fact_id!r}: old_code {plain_int(old_code)} is outside the "
                f"declared legal domain of {old_space!r}"
            )
        amount = _require_int(item.get("amount"), f"fact {fact_id!r} amount")
        facts.append(
            {
                "id": fact_id,
                "action": "write",
                "old_space": old_space,
                "old_code": old_code,
                "amount": amount,
            }
        )
    return seq, parsed_rules, facts


def _rule_to_json(rule) -> dict:
    data = {
        "id": rule.id,
        "source_space": rule.source_space,
        "source_range": [rule.source_range.lo, rule.source_range.hi],
        "priority": rule.priority,
    }
    if rule.action == DISABLE:
        data["action"] = "disable"
    else:
        data["target_space"] = rule.target_space
        data["direction"] = rule.shift.direction
        data["offset"] = rule.shift.offset
    return data


# ----------------------------------------------------------------------
# Storage adapter (only point/range queries, never full scans of facts)
# ----------------------------------------------------------------------
class _Storage:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def current_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) FROM batch").fetchone()
        return row[0] if row[0] is not None else 0

    def get_fact_by_id(self, fact_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT fact_id, old_space, old_code, code_key, amount, updated_seq "
            "FROM fact WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        return None if row is None else _fact_row(row)

    def facts_in_range(self, space: str, lo: int, hi: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT old_code FROM fact "
            "WHERE old_space = ? AND code_key BETWEEN ? AND ?",
            (space, encode_key(lo), encode_key(hi)),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def facts_at(self, space: str, code: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT fact_id, old_space, old_code, code_key, amount, updated_seq "
            "FROM fact WHERE old_space = ? AND code_key = ?",
            (space, encode_key(code)),
        ).fetchall()
        return [_fact_row(row) for row in rows]

    def balances_in_range(self, target_space: str, lo: int, hi: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT target_space, new_code, code_key, fact_count, amount_sum, updated_seq "
            "FROM balance WHERE target_space = ? AND code_key BETWEEN ? AND ?",
            (target_space, encode_key(lo), encode_key(hi)),
        ).fetchall()
        return [_balance_row(row) for row in rows]

    def delete_balance(self, target_space: str, new_code: int) -> None:
        self.conn.execute(
            "DELETE FROM balance WHERE target_space = ? AND code_key = ?",
            (target_space, encode_key(new_code)),
        )

    def upsert_balance(
        self,
        target_space: str,
        new_code: int,
        fact_count: int,
        amount_sum: int,
        updated_seq: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO balance
                (target_space, new_code, code_key, fact_count, amount_sum, updated_seq)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (target_space, code_key) DO UPDATE SET
                new_code = excluded.new_code,
                fact_count = excluded.fact_count,
                amount_sum = excluded.amount_sum,
                updated_seq = excluded.updated_seq
            """,
            (
                target_space,
                plain_int(new_code),
                encode_key(new_code),
                fact_count,
                plain_int(amount_sum),
                updated_seq,
            ),
        )


def _fact_row(row: sqlite3.Row) -> dict:
    return {
        "id": row[0],
        "old_space": row[1],
        "old_code": int(row[2]),
        "code_key": row[3],
        "amount": int(row[4]),
        "updated_seq": row[5],
    }


def _balance_row(row: sqlite3.Row) -> dict:
    return {
        "target_space": row[0],
        "new_code": int(row[1]),
        "code_key": row[2],
        "fact_count": row[3],
        "amount_sum": int(row[4]),
        "updated_seq": row[5],
    }


# ----------------------------------------------------------------------
# Ledger lifecycle
# ----------------------------------------------------------------------
class Ledger:
    def __init__(self, path: str | Path, conn: sqlite3.Connection, spec: MigrationSpec):
        self.path = Path(path)
        self.conn = conn
        self.spec = spec
        self._storage = _Storage(conn)
        self.last_refresh: RefreshStats | None = None

    # -- opening / initialising -----------------------------------------
    @classmethod
    def initialize(cls, path: str | Path, spec: MigrationSpec) -> "Ledger":
        """Create an empty ledger database from a catalogue spec."""

        path = Path(path)
        if path.exists():
            raise LedgerError(f"database already exists at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
            for statement in _DELIVERY_DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('catalog_spec', ?)",
                (json.dumps(_spec_json(spec), sort_keys=True),),
            )
            _persist_spaces(conn, spec)
            # Rule version 0: whatever rules the catalogue spec initially had.
            conn.execute(
                "INSERT INTO rule_version (version_id, introduced_seq, rules_json) "
                "VALUES (0, 0, ?)",
                (json.dumps([_rule_to_json(rule) for rule in spec.rules]),),
            )
            conn.commit()
        except Exception:
            conn.close()
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            raise
        return cls(path, conn, spec)

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        """Re-open a previously committed ledger; all durable state is in SQLite."""

        path = Path(path)
        if not path.exists():
            raise LedgerError(f"no ledger database at {path}")
        conn = sqlite3.connect(str(path))
        conn.row_factory = None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        # v1 ledgers are upgraded lazily: opening one stays read-only, and the
        # additive delivery tables are created the first time delivery is
        # enabled.  Until then a v1 ledger behaves exactly as before.
        if version is None or int(version[0]) not in (1, SCHEMA_VERSION):
            conn.close()
            raise LedgerError("incompatible or missing schema_version")
        raw = conn.execute(
            "SELECT value FROM meta WHERE key = 'catalog_spec'"
        ).fetchone()
        spec = spec_from_dict(json.loads(raw[0]))
        return cls(path, conn, spec)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- versions / catalogues ------------------------------------------
    def head_seq(self) -> int:
        return self._storage.current_seq()

    def current_rule_version(self) -> int:
        row = self.conn.execute(
            "SELECT rule_version FROM batch ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row is not None else 0

    def _rules_json_of(self, version_id: int) -> list[dict]:
        row = self.conn.execute(
            "SELECT rules_json FROM rule_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return json.loads(row[0])

    def _catalog(self, rules_json: list[dict]) -> Catalog:
        return Catalog(
            spec_from_dict(
                {
                    "source_spaces": _spec_json(self.spec)["source_spaces"],
                    "target_spaces": _spec_json(self.spec)["target_spaces"],
                    "rules": rules_json,
                }
            )
        )

    # -- submission ------------------------------------------------------
    def submit_bytes(
        self,
        payload_bytes: bytes,
        precommit_hook: Callable[[int], None] | None = None,
    ) -> SubmitResult:
        """Validate and atomically commit one raw JSON batch.

        Re-posting the exact same bytes for an already committed seq is a
        no-op success; any other protocol violation raises :class:`LedgerError`
        and leaves the committed state untouched.
        """

        if not isinstance(payload_bytes, (bytes, bytearray)):
            raise LedgerError("batch payload must be raw bytes")
        payload_bytes = bytes(payload_bytes)
        digest = hashlib.sha256(payload_bytes).hexdigest()
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError(f"batch is not valid UTF-8 JSON: {exc}") from exc

        seq, parsed_rules, facts = validate_batch(payload, self.spec)

        # All protocol checks happen before any write.
        head = self.head_seq()
        existing = self.conn.execute(
            "SELECT sha256 FROM batch WHERE seq = ?", (seq,)
        ).fetchone()
        if existing is not None:
            if existing[0] == digest:
                return SubmitResult(
                    seq=seq,
                    status="duplicate",
                    rule_version=self.current_rule_version(),
                    rules_replaced=False,
                )
            raise LedgerError(
                f"batch seq {seq} already committed with different content"
            )
        if seq != head + 1:
            raise LedgerError(
                f"expected next batch seq {head + 1}, got {seq} (no gaps allowed)"
            )

        # Deletes of unknown ids are input errors and abort before the txn.
        for item in facts:
            if item["action"] == "delete" and self._storage.get_fact_by_id(item["id"]) is None:
                raise LedgerError(
                    f"cannot delete unknown fact id {item['id']!r}"
                )

        old_version = self.current_rule_version()
        rules_replaced = parsed_rules is not None
        old_catalog = self._catalog(self._rules_json_of(old_version))
        new_rules_json = parsed_rules if rules_replaced else self._rules_json_of(old_version)
        new_catalog = self._catalog(new_rules_json)
        named_ids = {item["id"] for item in facts}

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            # A v1 ledger is upgraded lazily, in the same transaction as this
            # batch, so an unenabled old ledger writes no delivery rows.
            self._ensure_delivery_tables()
            # Capture where named facts live before any write, so the balance
            # rows they used to contribute to are recomputed as well, and so
            # the delivery log can pair pre-batch and post-batch detail.
            prior_positions: set[tuple[str, int]] = set()
            prior_rows: dict[str, dict | None] = {}
            for item in facts:
                previous = self._storage.get_fact_by_id(item["id"])
                prior_rows[item["id"]] = previous
                if previous is not None:
                    prior_positions.add(
                        (previous["old_space"], previous["old_code"])
                    )

            new_version = old_version
            if rules_replaced:
                new_version = old_version + 1
                self.conn.execute(
                    "INSERT INTO rule_version (version_id, introduced_seq, rules_json) "
                    "VALUES (?, ?, ?)",
                    (new_version, seq, json.dumps(new_rules_json)),
                )

            for item in facts:
                if item["action"] == "delete":
                    self.conn.execute("DELETE FROM fact WHERE fact_id = ?", (item["id"],))
                    self.conn.execute(
                        "INSERT INTO fact_history "
                        "(fact_id, seq, action, old_space, old_code, amount) "
                        "VALUES (?, ?, 'delete', NULL, NULL, NULL)",
                        (item["id"], seq),
                    )
                    continue
                self.conn.execute(
                    """
                    INSERT INTO fact (fact_id, old_space, old_code, code_key, amount, updated_seq)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (fact_id) DO UPDATE SET
                        old_space = excluded.old_space,
                        old_code = excluded.old_code,
                        code_key = excluded.code_key,
                        amount = excluded.amount,
                        updated_seq = excluded.updated_seq
                    """,
                    (
                        item["id"],
                        item["old_space"],
                        plain_int(item["old_code"]),
                        encode_key(item["old_code"]),
                        plain_int(item["amount"]),
                        seq,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO fact_history "
                    "(fact_id, seq, action, old_space, old_code, amount) "
                    "VALUES (?, ?, 'write', ?, ?, ?)",
                    (
                        item["id"],
                        seq,
                        item["old_space"],
                        plain_int(item["old_code"]),
                        plain_int(item["amount"]),
                    ),
                )

            examined_positions: set[tuple[str, int]] = set()
            stats = refresh_balances(
                self._storage,
                old_catalog,
                new_catalog,
                named_fact_ids=named_ids,
                prior_positions=prior_positions,
                rules_changed=rules_replaced,
                seq=seq,
                position_sink=examined_positions,
            )

            delivery_stats: DeliveryStats | None = None
            if self._delivery_enabled():
                # Read under the same IMMEDIATE write lock that guards the
                # enabling snapshot: a batch is either entirely before ``S``
                # (only in the baseline) or entirely after it (only changes).
                # Runs against the already-written facts but the same
                # pre-batch / post-batch catalogues the refresh used, inside
                # this transaction: a batch can never be half delivered.
                delivery_stats = record_batch_changes(
                    self.conn,
                    self._storage,
                    old_catalog,
                    new_catalog,
                    prior_rows=prior_rows,
                    named_ids=named_ids,
                    rules_changed=rules_replaced,
                    affected_positions=examined_positions,
                    seq=seq,
                )

            self.conn.execute(
                "INSERT INTO batch (seq, sha256, payload, rule_version) "
                "VALUES (?, ?, ?, ?)",
                (seq, digest, payload_bytes, new_version),
            )
            if precommit_hook is not None:
                precommit_hook(seq)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        self.last_refresh = stats
        return SubmitResult(
            seq=seq,
            status="committed",
            rule_version=new_version,
            rules_replaced=rules_replaced,
            refresh=stats,
            delivery=delivery_stats,
        )

    def submit_file(self, path: str | Path, **kwargs) -> SubmitResult:
        return self.submit_bytes(Path(path).read_bytes(), **kwargs)

    # -- reads -----------------------------------------------------------
    def get_balance(self, target_space: str, new_code: int) -> dict | None:
        if target_space not in self.spec.target_spaces:
            raise LedgerError(f"unknown target space {target_space!r}")
        if not any(new_code in piece for piece in self.spec.target_spaces[target_space]):
            raise LedgerError(
                f"new_code {plain_int(new_code)} is outside the declared legal "
                f"domain of {target_space!r}"
            )
        row = self.conn.execute(
            "SELECT target_space, new_code, code_key, fact_count, amount_sum, updated_seq "
            "FROM balance WHERE target_space = ? AND code_key = ?",
            (target_space, encode_key(new_code)),
        ).fetchone()
        if row is None:
            return None
        result = _balance_row(row)
        result.pop("code_key")
        result["head_seq"] = self.head_seq()
        return result

    def list_balances(self, target_space: str | None = None) -> list[dict]:
        if target_space is not None and target_space not in self.spec.target_spaces:
            raise LedgerError(f"unknown target space {target_space!r}")
        if target_space is None:
            rows = self.conn.execute(
                "SELECT target_space, new_code, code_key, fact_count, amount_sum, updated_seq "
                "FROM balance ORDER BY target_space, code_key"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT target_space, new_code, code_key, fact_count, amount_sum, updated_seq "
                "FROM balance WHERE target_space = ? ORDER BY code_key",
                (target_space,),
            ).fetchall()
        head = self.head_seq()
        result = []
        for row in rows:
            item = _balance_row(row)
            item.pop("code_key")
            item["head_seq"] = head
            result.append(item)
        return result

    def get_fact(self, fact_id: str) -> dict | None:
        row = self._storage.get_fact_by_id(fact_id)
        if row is None:
            return None
        row.pop("code_key")
        return row

    def list_committed_seqs(self) -> list[int]:
        return [row[0] for row in self.conn.execute("SELECT seq FROM batch ORDER BY seq")]

    def batch_payload(self, seq: int) -> bytes | None:
        row = self.conn.execute(
            "SELECT payload FROM batch WHERE seq = ?", (seq,)
        ).fetchone()
        return None if row is None else row[0]

    # -- replayable delivery --------------------------------------------
    def _delivery_meta(self):
        """Return the delivery singleton row, creating the tables if absent.

        A v1 ledger is upgraded here, first-use only; the schema_version bump
        and the enabling snapshot share the same transaction below.
        """

        try:
            return self.conn.execute(
                "SELECT baseline_seq, baseline_count, enabled_at "
                "FROM delivery_meta WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None

    def _delivery_enabled(self) -> bool:
        return self._delivery_meta() is not None

    def _ensure_delivery_tables(self) -> None:
        """Create the additive delivery tables (upgrading a v1 ledger).

        Safe to call inside the caller's write transaction; a no-op for an
        already-upgraded ledger and for every batch thereafter.
        """

        if self._schema_version() >= SCHEMA_VERSION:
            return
        for statement in _DELIVERY_DDL:
            self.conn.execute(statement)
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )

    def _schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def delivery_enabled(self) -> bool:
        """Whether the baseline snapshot has been taken on this ledger."""

        return self._delivery_enabled()

    def delivery_start_seq(self) -> int:
        """``S``: the committed batch the baseline snapshot was taken at."""

        meta = self._delivery_meta()
        if meta is None:
            raise LedgerError(
                "delivery is not enabled; call enable_delivery() first"
            )
        return meta[0]

    def enable_delivery(self) -> dict:
        """Enable delivery once and snapshot the baseline.

        The starting batch is the ledger's committed head at the moment this
        first succeeds (``0`` for an empty ledger).  The snapshot is persisted,
        so reopening the ledger or calling this again returns the identical
        ``S`` and content instead of a fresh view of the current facts.

        On a ledger with no facts and no commits the snapshot is empty, so
        enabling and later re-enabling simply retains ``S = 0``.
        """

        meta = self._delivery_meta()
        if meta is not None:
            return {
                "start_seq": meta[0],
                "baseline_facts": meta[1],
                "already_enabled": True,
            }

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            # Upgrade a v1 ledger in the same transaction that takes the
            # snapshot, then re-read under the write lock so a concurrent
            # first enable (or a batch committing right now) cannot produce
            # two snapshots.
            self._ensure_delivery_tables()
            meta = self._delivery_meta()
            if meta is not None:
                self.conn.rollback()
                return {
                    "start_seq": meta[0],
                    "baseline_facts": meta[1],
                    "already_enabled": True,
                }
            start_seq = self.head_seq()
            # Rules at the end of ``start_seq`` define what is booked; no
            # later batch may slip into the snapshot because both this read
            # and the snapshot happen under one write lock, and the batch
            # transaction captures its delivery rows sequentially.
            catalog = self._catalog(self._rules_json_of(self.current_rule_version()))
            count = snapshot_baseline(self.conn, catalog)
            self.conn.execute(
                "INSERT INTO delivery_meta "
                "(singleton, baseline_seq, baseline_count, enabled_at) "
                "VALUES (1, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (start_seq, count),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "start_seq": start_seq,
            "baseline_facts": count,
            "already_enabled": False,
        }

    def export_baseline(self, path: str | Path) -> dict:
        """Write the baseline file for ``S`` to ``path`` atomically."""

        meta = self._delivery_meta()
        if meta is None:
            raise LedgerError(
                "delivery is not enabled; call enable_delivery() first"
            )
        start_seq, baseline_count, _ = meta
        header = {
            "kind": "baseline",
            "start_seq": start_seq,
            "fact_count": baseline_count,
        }
        return write_delivery_file(
            path,
            header,
            iter_baseline_records(self.conn),
            {"fact_count": baseline_count},
        )

    def export_changes(self, low: int, high: int, path: str | Path) -> dict:
        """Write the change file for ``(low, high]`` to ``path`` atomically.

        Requires ``S <= low < high`` and ``high`` already committed.  Reposting
        the same interval after newer batches commit yields byte-identical
        content, because only batches inside the fixed interval are read.
        """

        meta = self._delivery_meta()
        if meta is None:
            raise LedgerError(
                "delivery is not enabled; call enable_delivery() first"
            )
        start_seq = meta[0]
        if isinstance(low, bool) or not isinstance(low, int):
            raise LedgerError("interval low must be an integer")
        if isinstance(high, bool) or not isinstance(high, int):
            raise LedgerError("interval high must be an integer")
        if low < start_seq:
            raise LedgerError(
                f"interval low {low} is before the delivery start {start_seq}"
            )
        if low >= high:
            raise LedgerError(
                f"interval must satisfy low < high, got ({low}, {high}]"
            )
        if not is_committed(self.conn, high):
            head = self.head_seq()
            raise LedgerError(
                f"upper bound {high} is not a committed batch (head is {head})"
            )

        header = {"kind": "changes", "start_seq": start_seq, "low": low, "high": high}
        return write_delivery_file(
            path,
            header,
            iter_change_records(self.conn, low, high),
            {"low": low, "high": high, "last_seq": high},
        )


def _spec_json(spec: MigrationSpec) -> dict:
    return {
        "source_spaces": {
            name: [[piece.lo, piece.hi] for piece in domain]
            for name, domain in spec.source_spaces.items()
        },
        "target_spaces": {
            name: [[piece.lo, piece.hi] for piece in domain]
            for name, domain in spec.target_spaces.items()
        },
        "rules": [_rule_to_json(rule) for rule in spec.rules],
    }


def _persist_spaces(conn: sqlite3.Connection, spec: MigrationSpec) -> None:
    for kind, spaces in (
        ("source", spec.source_spaces),
        ("target", spec.target_spaces),
    ):
        for name, domain in spaces.items():
            cur = conn.execute(
                "INSERT INTO space (kind, name) VALUES (?, ?)", (kind, name)
            )
            space_id = cur.lastrowid
            for position, piece in enumerate(domain):
                conn.execute(
                    "INSERT INTO domain_piece (space_id, position, lo_key, hi_key) "
                    "VALUES (?, ?, ?, ?)",
                    (space_id, position, encode_key(piece.lo), encode_key(piece.hi)),
                )

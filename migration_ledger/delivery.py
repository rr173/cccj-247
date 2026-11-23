"""Replayable per-fact delivery: snapshotted baseline + per-batch changes.

The SQLite ledger only materialises *current* balances, which is not enough for
a downstream warehouse that has to reproduce the booked detail at the end of a
batch.  Two facts moving between target codes can leave every balance row
unchanged while the individual attribution has changed.

This module keeps an append-only, per-batch delivery log:

``delivery_baseline``
    A **snapshot** taken the first time delivery is enabled (at the committed
    batch ``S``): one row per booked fact - id, target space, new code and
    integer amount - ordered by id when exported.  The snapshot is stored, not
    recomputed, so reopening the ledger or re-enabling delivery keeps the same
    ``S`` and the same content even after newer batches arrive.

``delivery_change``
    One row per ``(batch, fact id)`` whose *delivered detail* actually changed
    in that batch.  Both sides are row-valued: ``None`` means "not booked".
    The "before" side is the fact content and rules at the end of the previous
    batch, the "after" side the content and rules at the end of this batch.  A
    batch that only changes rule *evidence* (or otherwise leaves every booked
    fact's target space / new code / amount untouched) writes no row at all.

Change rows are computed inside the same SQLite transaction as the batch, from
the same symbolic catalogue diff the balance refresh uses, so an interrupted,
failed or byte-identically replayed batch can never publish extra delivery.

Files are NDJSON: one JSON object per line, framed by a start record and an end
record that carries the record counts and the sha256 of everything before it.
They are written to a temporary file in the destination directory, fsynced and
atomically renamed, so a failed export cannot truncate a previously published
delivery.  Codes and amounts are decimal strings, so values wider than 64 bits
round-trip losslessly.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from migration_catalog import Catalog
from migration_catalog.core import SRC_MAPPED, TGT_UNIQUE
from migration_catalog.intervals import plain_int

from .errors import LedgerError

FORMAT = "migration-ledger-delivery/1"

# Baseline snapshot rows are inserted in chunks while streaming the fact table;
# only this many rows are ever held in memory at once.
_BASELINE_CHUNK = 1000


# ----------------------------------------------------------------------
# Delivered detail
# ----------------------------------------------------------------------
def delivered_detail(
    catalog: Catalog,
    old_space: str,
    old_code: int,
    amount: int,
) -> tuple[str, int, int] | None:
    """What the downstream warehouse should store for one fact.

    A fact is booked iff, at the end of the batch, its old code is a valid
    ``mapped`` source position and its new code is reverse-``unique`` over the
    declared legal domains.  The delivered detail is
    ``(target_space, new_code, amount)``; ``None`` means "not booked".
    """

    hit = catalog.source_lookup(old_space, old_code)
    if hit.status != SRC_MAPPED or hit.target_space is None or hit.target is None:
        return None
    reverse = catalog.target_lookup(hit.target_space, hit.target)
    if reverse.status != TGT_UNIQUE:
        return None
    return (hit.target_space, hit.target, amount)


def _detail_record(detail: tuple[str, int, int] | None) -> dict | None:
    if detail is None:
        return None
    target_space, new_code, amount = detail
    return {
        "target_space": target_space,
        "new_code": plain_int(new_code),
        "amount": plain_int(amount),
    }


def _detail_columns(
    detail: tuple[str, int, int] | None,
) -> tuple[str | None, str | None, str | None]:
    if detail is None:
        return (None, None, None)
    return (
        detail[0],
        plain_int(detail[1]),
        plain_int(detail[2]),
    )


@dataclass
class DeliveryStats:
    """Instrumentation for one committed batch's delivery work."""

    facts_examined: int = 0
    changes: int = 0
    rules_changed: bool = False

    def as_dict(self) -> dict:
        return {
            "facts_examined": self.facts_examined,
            "changes": self.changes,
            "rules_changed": self.rules_changed,
        }


# ----------------------------------------------------------------------
# Change capture (runs inside the batch transaction)
# ----------------------------------------------------------------------
def record_batch_changes(
    conn,
    storage,
    old_catalog: Catalog,
    new_catalog: Catalog,
    prior_rows: dict[str, dict | None],
    named_ids: set[str],
    rules_changed: bool,
    affected_positions: Iterable[tuple[str, int]],
    seq: int,
) -> DeliveryStats:
    """Insert this batch's per-fact before/after rows, if any detail changed.

    ``prior_rows`` holds the pre-batch content of every fact named by the
    batch (``None`` when the id did not exist).  When rules changed, every fact
    at a source position the symbolic diff marked as affected is examined too;
    those facts keep their content, only their interpretation changed.  When
    rules did not change, no unrelated fact can change its delivered detail, so
    only the named ids are touched.
    """

    stats = DeliveryStats(rules_changed=rules_changed)

    def compare_and_store(
        fact_id: str,
        before_row: dict | None,
        after_row: dict | None,
    ) -> None:
        before = (
            None
            if before_row is None
            else delivered_detail(
                old_catalog,
                before_row["old_space"],
                before_row["old_code"],
                before_row["amount"],
            )
        )
        after = (
            None
            if after_row is None
            else delivered_detail(
                new_catalog,
                after_row["old_space"],
                after_row["old_code"],
                after_row["amount"],
            )
        )
        if before == after:
            return
        before_space, before_code, before_amount = _detail_columns(before)
        after_space, after_code, after_amount = _detail_columns(after)
        conn.execute(
            """
            INSERT INTO delivery_change
                (seq, fact_id,
                 before_space, before_code, before_amount,
                 after_space, after_code, after_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                fact_id,
                before_space,
                before_code,
                before_amount,
                after_space,
                after_code,
                after_amount,
            ),
        )
        stats.changes += 1

    # Named facts come first: they are the only ones whose stored content can
    # change.  Their pre-batch and post-batch content are both known exactly.
    seen: set[str] = set()
    for fact_id in sorted(named_ids):
        seen.add(fact_id)
        stats.facts_examined += 1
        compare_and_store(
            fact_id,
            prior_rows.get(fact_id),
            storage.get_fact_by_id(fact_id),
        )

    # When rules changed, facts at positions the symbolic diff marked as
    # affected may be re-attributed even though no batch named them: their
    # content is unchanged, but their pre-batch and post-batch interpretation
    # under the two catalogues may differ.  Facts at every other position
    # cannot change and are never read.  Position queries use the
    # (old_space, code_key) index, never a full scan.
    if rules_changed:
        for space, code in sorted(affected_positions):
            for row in storage.facts_at(space, code):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                stats.facts_examined += 1
                compare_and_store(row["id"], row, row)

    return stats


# ----------------------------------------------------------------------
# Snapshot (first enable only)
# ----------------------------------------------------------------------
def snapshot_baseline(conn, catalog: Catalog) -> int:
    """Store the booked detail of every fact currently in the ledger.

    Reads the whole fact table in primary-key chunks (allowed only for the
    first enable) and inserts only booked facts.  Must run inside the enabling
    transaction.
    """

    last_id = ""
    count = 0
    while True:
        rows = conn.execute(
            "SELECT fact_id, old_space, old_code, amount FROM fact "
            "WHERE fact_id > ? ORDER BY fact_id LIMIT ?",
            (last_id, _BASELINE_CHUNK),
        ).fetchall()
        if not rows:
            break
        for fact_id, old_space, old_code, amount in rows:
            detail = delivered_detail(catalog, old_space, int(old_code), int(amount))
            if detail is None:
                continue
            conn.execute(
                "INSERT INTO delivery_baseline "
                "(fact_id, target_space, new_code, amount) VALUES (?, ?, ?, ?)",
                (
                    fact_id,
                    detail[0],
                    plain_int(detail[1]),
                    plain_int(detail[2]),
                ),
            )
            count += 1
        last_id = rows[-1][0]
    return count


# ----------------------------------------------------------------------
# Streaming readers
# ----------------------------------------------------------------------
def iter_baseline_records(conn) -> Iterator[dict]:
    """Yield baseline fact records in id order, one SQLite row at a time."""

    cursor = conn.execute(
        "SELECT fact_id, target_space, new_code, amount "
        "FROM delivery_baseline ORDER BY fact_id"
    )
    for fact_id, target_space, new_code, amount in cursor:
        yield {
            "type": "fact",
            "id": fact_id,
            "target_space": target_space,
            "new_code": new_code,
            "amount": amount,
        }


def committed_seq_range(conn, low_exclusive: int, high_inclusive: int) -> Iterator[int]:
    """Committed batch numbers in ``(low, high]``, streamed in order.

    Only the ``batch`` primary-key index is touched; payloads are never read.
    """

    cursor = conn.execute(
        "SELECT seq FROM batch WHERE seq > ? AND seq <= ? ORDER BY seq",
        (low_exclusive, high_inclusive),
    )
    for row in cursor:
        yield row[0]


def is_committed(conn, seq: int) -> bool:
    row = conn.execute("SELECT 1 FROM batch WHERE seq = ?", (seq,)).fetchone()
    return row is not None


def iter_change_records(conn, low_exclusive: int, high_inclusive: int) -> Iterator[dict]:
    """Yield batch markers and change records for ``(low, high]``.

    Every committed batch in the range yields one ``batch`` marker even when it
    has no change rows, so a consumer can always advance its checkpoint.  Batch
    rows are streamed in id order; never the whole range in memory.
    """

    for seq in committed_seq_range(conn, low_exclusive, high_inclusive):
        change_count = conn.execute(
            "SELECT COUNT(*) FROM delivery_change WHERE seq = ?", (seq,)
        ).fetchone()[0]
        yield {"type": "batch", "seq": seq, "changes": change_count}
        cursor = conn.execute(
            "SELECT fact_id, "
            "       before_space, before_code, before_amount, "
            "       after_space, after_code, after_amount "
            "FROM delivery_change WHERE seq = ? ORDER BY fact_id",
            (seq,),
        )
        for (
            fact_id,
            before_space,
            before_code,
            before_amount,
            after_space,
            after_code,
            after_amount,
        ) in cursor:
            before = (
                None
                if before_space is None
                else {
                    "target_space": before_space,
                    "new_code": before_code,
                    "amount": before_amount,
                }
            )
            after = (
                None
                if after_space is None
                else {
                    "target_space": after_space,
                    "new_code": after_code,
                    "amount": after_amount,
                }
            )
            yield {"type": "change", "id": fact_id, "before": before, "after": after}


# ----------------------------------------------------------------------
# Atomic file writer / verifying reader
# ----------------------------------------------------------------------
def _dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def write_delivery_file(
    path: str | Path,
    header: dict,
    body: Iterable[dict],
    trailer: dict,
) -> dict:
    """Write one framed NDJSON delivery atomically and return a summary.

    All records are streamed; the sha256 is accumulated while writing.  On any
    failure the temporary file is removed and any file already at ``path`` is
    left exactly as it was.
    """

    target = Path(path)
    if target.exists() and target.is_dir():
        raise LedgerError(f"delivery path {target} is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    records = 0
    batches = 0
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            def emit(record: dict) -> None:
                line = (_dumps(record) + "\n").encode("utf-8")
                handle.write(line)
                digest.update(line)

            emit({"type": "delivery_start", "format": FORMAT, **header})
            for record in body:
                emit(record)
                kind = record.get("type")
                if kind in ("fact", "change"):
                    records += 1
                elif kind == "batch":
                    batches += 1
            end = dict(trailer)
            end.update({"type": "delivery_end", "sha256": digest.hexdigest()})
            handle.write((_dumps(end) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_directory(target.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    summary = {"path": str(target), "records": records}
    summary.update({k: v for k, v in header.items() if k != "format"})
    summary.update(trailer)
    if header.get("kind") == "changes":
        summary["batches"] = batches
    return summary


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_delivery_file(path: str | Path) -> Iterator[dict]:
    """Yield every record of a delivery file, verifying its end marker.

    A truncated file (or one whose body was altered) raises :class:`LedgerError`
    instead of being silently consumed as if it were complete.
    """

    source = Path(path)
    digest = hashlib.sha256()
    started = False
    with source.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                raise LedgerError(f"{source}: truncated delivery file")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{source}: invalid JSON line: {exc}") from exc
            if not isinstance(record, dict) or "type" not in record:
                raise LedgerError(f"{source}: delivery line is not a typed record")
            if record["type"] == "delivery_end":
                if not started:
                    raise LedgerError(f"{source}: end marker before start marker")
                if record.get("sha256") != digest.hexdigest():
                    raise LedgerError(f"{source}: delivery checksum mismatch")
                yield record
                return
            if record["type"] == "delivery_start":
                if started:
                    raise LedgerError(f"{source}: duplicate start marker")
                if record.get("format") != FORMAT:
                    raise LedgerError(f"{source}: unsupported delivery format")
                started = True
            digest.update(raw)
            yield record
    raise LedgerError(f"{source}: truncated delivery file (no end marker)")


def apply_records(records: Iterable[dict]) -> tuple[int, dict[str, dict]]:
    """Fold delivery records into ``{fact_id: detail}``, checking continuity.

    This is the reference consumer used by the example and tests: it applies a
    baseline and/or changes to an in-memory map keyed by fact id.
    """

    state: dict[str, dict] = {}
    seq: int | None = None
    for record in records:
        kind = record.get("type")
        if kind == "fact":
            fact_id = record["id"]
            if fact_id in state:
                raise LedgerError(f"baseline repeats fact id {fact_id!r}")
            state[fact_id] = {
                "target_space": record["target_space"],
                "new_code": record["new_code"],
                "amount": record["amount"],
            }
        elif kind == "batch":
            seq = record["seq"]
        elif kind == "change":
            fact_id = record["id"]
            before = detail_from_record(record["before"])
            after = detail_from_record(record["after"])
            current = state.get(fact_id)
            if before is None:
                if current is not None:
                    raise LedgerError(
                        f"change for {fact_id!r} says it was not booked, but it is"
                    )
            elif current != before:
                raise LedgerError(
                    f"change for {fact_id!r} does not match the restored detail"
                )
            if after is None:
                state.pop(fact_id, None)
            else:
                state[fact_id] = dict(after)
    return (seq if seq is not None else 0), state


def detail_from_record(record: dict | None) -> dict | None:
    """Validate one JSON detail record (strings already lossless) or ``None``."""

    if record is None:
        return None
    if set(record) != {"target_space", "new_code", "amount"}:
        raise LedgerError(f"malformed detail record: {record!r}")
    return record


__all__ = [
    "FORMAT",
    "DeliveryStats",
    "apply_records",
    "committed_seq_range",
    "delivered_detail",
    "detail_from_record",
    "is_committed",
    "iter_baseline_records",
    "iter_change_records",
    "iter_delivery_file",
    "record_batch_changes",
    "snapshot_baseline",
    "write_delivery_file",
]

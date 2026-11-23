"""Streaming reader for the warehouse key manifest (one JSON object per line).

Each line is either a *participating* record::

    {"id": "R1", "current": {"space": "modern", "code": "1000001"},
     "unit": "billing", "old": {"space": "legacy", "code": "1"}}

or a *reserved placeholder* that only occupies its current key::

    {"id": "KEEP-1", "current": {"space": "modern", "code": "1000020"}}

Rules enforced here (all are manifest errors, reported with line numbers):

* every line is one JSON object with only the keys above;
* ``id`` is a non-empty string, unique across the file;
* ``current``/``old`` are ``{"space": <non-empty string>, "code": <decimal
  string>}``; codes are decimal strings so values beyond 64 bits survive;
* ``unit`` and ``old`` appear together or not at all;
* the current key ``(space, code)`` is unique across the file.

Rows are inserted into a temporary SQLite database as they are read, so the
manifest is never held in memory.  Codes are canonicalised with Python's
arbitrary-precision integers (``"-0"`` and ``"0"`` are the same key).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .errors import PlanError

_CODE_RE = re.compile(r"-?[0-9]+")

SCHEMA = """
CREATE TABLE records (
  line INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  cur_space TEXT NOT NULL,
  cur_code TEXT NOT NULL,
  unit TEXT,
  old_space TEXT,
  old_code TEXT,
  state TEXT,
  tgt_space TEXT,
  tgt_code TEXT,
  block TEXT
);
CREATE UNIQUE INDEX records_cur ON records(cur_space, cur_code);
CREATE INDEX records_unit ON records(unit);
CREATE INDEX records_state ON records(state);
CREATE INDEX records_tgt ON records(tgt_space, tgt_code);
"""


def parse_code(value, what: str) -> str:
    """Validate a decimal-string code and return its canonical form."""

    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise PlanError(f"{what} must be a decimal string, got {value!r}")
    return str(int(value))  # arbitrary precision; canonicalises "-0"/"007"


def _parse_key(obj, what: str) -> tuple[str, str]:
    if not isinstance(obj, dict):
        raise PlanError(f"{what} must be an object with 'space' and 'code'")
    unknown = set(obj) - {"space", "code"}
    if unknown:
        raise PlanError(f"{what} has unknown fields: {sorted(unknown)}")
    for required in ("space", "code"):
        if required not in obj:
            raise PlanError(f"{what} is missing {required!r}")
    space = obj["space"]
    if not isinstance(space, str) or not space:
        raise PlanError(f"{what} space must be a non-empty string")
    return space, parse_code(obj["code"], f"{what} code")


def parse_line(text: str, line_no: int):
    """Parse one manifest line into a record tuple."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(f"line {line_no}: invalid JSON: {exc}") from exc

    def fail(message: str) -> PlanError:
        return PlanError(f"line {line_no}: {message}")

    if not isinstance(data, dict):
        raise fail("manifest line must be a JSON object")
    unknown = set(data) - {"id", "current", "unit", "old"}
    if unknown:
        raise fail(f"unknown fields: {sorted(unknown)}")
    for required in ("id", "current"):
        if required not in data:
            raise fail(f"missing {required!r}")

    record_id = data["id"]
    if not isinstance(record_id, str) or not record_id:
        raise fail("id must be a non-empty string")
    try:
        cur_space, cur_code = _parse_key(data["current"], "current")
    except PlanError as exc:
        raise fail(str(exc)) from exc

    has_unit = "unit" in data
    has_old = "old" in data
    if has_unit != has_old:
        raise fail("'unit' and 'old' must appear together or not at all")
    if not has_unit:
        return record_id, cur_space, cur_code, None, None, None

    unit = data["unit"]
    if not isinstance(unit, str) or not unit:
        raise fail("unit must be a non-empty string")
    try:
        old_space, old_code = _parse_key(data["old"], "old")
    except PlanError as exc:
        raise fail(str(exc)) from exc
    return record_id, cur_space, cur_code, unit, old_space, old_code


def load_manifest(path: str, conn: sqlite3.Connection) -> str:
    """Stream the manifest into the records table; return its sha256."""

    sha = hashlib.sha256()
    insert = (
        "INSERT INTO records(line, id, cur_space, cur_code, unit, old_space,"
        " old_code) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    with open(path, "rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            sha.update(raw)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PlanError(f"line {line_no}: not valid UTF-8") from exc
            if not text.strip():
                raise PlanError(f"line {line_no}: blank line")
            record = parse_line(text.strip(), line_no)
            try:
                conn.execute(insert, (line_no, *record))
            except sqlite3.IntegrityError:
                raise _duplicate_error(conn, line_no, record) from None
    conn.commit()
    return sha.hexdigest()


def _duplicate_error(conn: sqlite3.Connection, line_no: int, record) -> PlanError:
    record_id, cur_space, cur_code = record[0], record[1], record[2]
    row = conn.execute(
        "SELECT line FROM records WHERE id = ?", (record_id,)
    ).fetchone()
    if row is not None:
        return PlanError(
            f"line {line_no}: duplicate record id {record_id!r} "
            f"(first seen on line {row[0]})"
        )
    row = conn.execute(
        "SELECT line, id FROM records WHERE cur_space = ? AND cur_code = ?",
        (cur_space, cur_code),
    ).fetchone()
    if row is not None:
        return PlanError(
            f"line {line_no}: duplicate current key {cur_space}:{cur_code} "
            f"(already used by record {row[1]!r} on line {row[0]})"
        )
    return PlanError(f"line {line_no}: duplicate record")  # pragma: no cover

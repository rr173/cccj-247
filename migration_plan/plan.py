"""Rekey rollout planning: which release units can publish, in which atomic
batches, and which must be remediated first.

Inputs
------
* a candidate catalogue (the existing migration spec, compiled to
  :class:`migration_catalog.Catalog`);
* a warehouse key manifest streamed into a temporary SQLite database by
  :mod:`migration_plan.manifest`;
* ``k``, the maximum number of business units in one atomic batch.

Semantics (strictly per the warehouse rules)
--------------------------------------------
* A participating row gets a target only when its original old code has a
  valid ``mapped`` entry in the candidate catalogue **and** the target is
  reverse-``unique`` over every legal old code.  A row whose target equals
  its current key is kept as-is and never frees that key.
* A unit with any row that cannot get a legal target cannot publish.
* If several moving records claim the same target key, every applicant's
  unit leaves this release; the key is never reallocated to one applicant
  just because another drops out for a different reason.
* For the remaining moves: a free key is usable; an occupied key is usable
  only when the occupier's unit frees it by publishing earlier or in the
  same atomic batch.  Reserved placeholders, kept rows and units that
  cannot publish never free keys.
* Batches are the strongly connected components of the "needs the key
  freed by" graph: only mutually dependent units share a batch.  A
  component larger than ``k`` cannot publish, and neither can the units
  that need its keys.

Memory: the full manifest and every record's move detail live only in the
temporary SQLite database; release units and inter-unit dependencies are
kept in memory, which the warehouse rules explicitly allow.
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import tempfile
from collections import deque
from dataclasses import dataclass

from migration_catalog import Catalog
from migration_catalog.core import SRC_MAPPED, TGT_UNIQUE

from .errors import PlanError
from .manifest import SCHEMA, load_manifest

# Row states stored in records.state.
PLACEHOLDER = "placeholder"  # no unit/old: occupies its current key forever
KEPT = "kept"  # target == current key: retained as-is, never frees the key
MOVE = "move"  # has a legal target different from the current key
BLOCKED = "blocked"  # no legal target; the whole unit cannot publish

_CHUNK = 500


@dataclass
class UnitInfo:
    name: str
    total: int = 0
    moves: int = 0
    kept: int = 0
    blocked_rows: int = 0
    business: bool = False  # some member row has no legal target
    contention: bool = False  # some member claims a contested target key
    occupied: bool = False  # some member needs a key that is never freed
    oversize: bool = False  # atomic group larger than k


class Planner:
    """Computes the rollout plan for one catalogue + manifest + k."""

    def __init__(self, catalog: Catalog, k: int, work_dir: str | None = None):
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise PlanError(f"k must be a positive integer, got {k!r}")
        self.catalog = catalog
        self.k = k
        self._tmp = tempfile.TemporaryDirectory(
            prefix="migration-plan-", dir=work_dir
        )
        self.conn = sqlite3.connect(os.path.join(self._tmp.name, "plan.db"))
        self.conn.executescript(SCHEMA)
        self.manifest_sha256 = ""
        self.units: dict[str, UnitInfo] = {}
        # deps[u][v] = one witness that u needs a key freed by v.
        self.deps: dict[str, dict[str, dict]] = {}
        self.dependers: dict[str, set[str]] = {}
        self.blocked: set[str] = set()
        self.oversize_groups: dict[str, list[str]] = {}
        self.sccs: list[list[str]] = []
        self.batches: list[list[str]] = []

    def __enter__(self) -> "Planner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Phase 0: ingest
    # ------------------------------------------------------------------
    def load(self, manifest_path: str) -> str:
        self.manifest_sha256 = load_manifest(manifest_path, self.conn)
        return self.manifest_sha256

    # ------------------------------------------------------------------
    # Phase 1: resolve every participating row against the catalogue
    # ------------------------------------------------------------------
    def resolve_rows(self) -> None:
        self.conn.execute(
            "UPDATE records SET state = ? WHERE unit IS NULL", (PLACEHOLDER,)
        )
        last = 0
        while True:
            rows = self.conn.execute(
                "SELECT line, old_space, old_code, cur_space, cur_code "
                "FROM records WHERE unit IS NOT NULL AND line > ? "
                "ORDER BY line LIMIT ?",
                (last, _CHUNK),
            ).fetchall()
            if not rows:
                break
            updates = [
                (*self._resolve(ospace, ocode, cspace, ccode), line)
                for line, ospace, ocode, cspace, ccode in rows
            ]
            self.conn.executemany(
                "UPDATE records SET state = ?, tgt_space = ?, tgt_code = ?,"
                " block = ? WHERE line = ?",
                updates,
            )
            last = rows[-1][0]
        self.conn.commit()

    def _resolve(self, old_space, old_code_text, cur_space, cur_code_text):
        if old_space not in self.catalog.spec.source_spaces:
            return (
                BLOCKED,
                None,
                None,
                _block_detail(
                    "old_code_unmapped",
                    catalog_status="unknown_source_space",
                    detail=(
                        f"source space {old_space!r} is not declared in the"
                        " candidate catalog"
                    ),
                ),
            )
        hit = self.catalog.source_lookup(old_space, int(old_code_text))
        if hit.status != SRC_MAPPED:
            return (
                BLOCKED,
                None,
                None,
                _block_detail(
                    "old_code_unmapped",
                    catalog_status=hit.status,
                    detail=hit.reason,
                    rule_ids=list(hit.rule_ids),
                ),
            )
        target_space, target = hit.target_space, hit.target
        reverse = self.catalog.target_lookup(target_space, target)
        if reverse.status != TGT_UNIQUE:
            return (
                BLOCKED,
                None,
                None,
                _block_detail(
                    "target_not_unique",
                    target={"space": target_space, "code": str(target)},
                    reverse_status=reverse.status,
                    witness=[
                        {
                            "source_space": item.source_space,
                            "code": str(item.code),
                            "rule_ids": list(item.rule_ids),
                        }
                        for item in reverse.witness
                    ],
                ),
            )
        preimage = reverse.preimages[0]
        if preimage.source_space != old_space or preimage.code != int(old_code_text):
            raise AssertionError("catalog reverse lookup disagrees with forward")
        target_text = str(target)
        if target_space == cur_space and target_text == cur_code_text:
            return KEPT, target_space, target_text, None
        return MOVE, target_space, target_text, None

    # ------------------------------------------------------------------
    # Phase 2: per-unit aggregation (units may live in memory)
    # ------------------------------------------------------------------
    def aggregate_units(self) -> None:
        query = (
            "SELECT unit, state, COUNT(*) FROM records "
            "WHERE unit IS NOT NULL GROUP BY unit, state"
        )
        for unit, state, count in self.conn.execute(query):
            info = self.units.setdefault(unit, UnitInfo(unit))
            info.total += count
            if state == MOVE:
                info.moves = count
            elif state == KEPT:
                info.kept = count
            elif state == BLOCKED:
                info.blocked_rows = count
        for info in self.units.values():
            info.business = info.blocked_rows > 0

    # ------------------------------------------------------------------
    # Phase 3: target contention across *all* move requests
    # ------------------------------------------------------------------
    def find_contentions(self) -> None:
        self.conn.execute(
            "CREATE TABLE contested AS SELECT tgt_space, tgt_code FROM records"
            " WHERE state = ? GROUP BY tgt_space, tgt_code HAVING COUNT(*) > 1",
            (MOVE,),
        )
        self.conn.execute(
            "CREATE INDEX contested_key ON contested(tgt_space, tgt_code)"
        )
        query = (
            "SELECT DISTINCT r.unit FROM records r JOIN contested c"
            " ON c.tgt_space = r.tgt_space AND c.tgt_code = r.tgt_code"
            " WHERE r.state = ?"
        )
        for (unit,) in self.conn.execute(query, (MOVE,)):
            self.units[unit].contention = True
        self.conn.commit()

    # ------------------------------------------------------------------
    # Phase 4: who occupies each wanted target key right now
    # ------------------------------------------------------------------
    def find_occupancy(self) -> None:
        self.conn.execute(
            "CREATE TABLE direct_blocks ("
            " move_id TEXT, tgt_space TEXT, tgt_code TEXT,"
            " occupier_id TEXT, occupier_kind TEXT, occupier_unit TEXT,"
            " unit TEXT)"
        )
        cursor = self.conn.execute(
            "SELECT m.id, m.unit, m.tgt_space, m.tgt_code,"
            "       o.id, o.unit, o.state "
            "FROM records m JOIN records o"
            "  ON o.cur_space = m.tgt_space AND o.cur_code = m.tgt_code "
            "WHERE m.state = ? ORDER BY m.line",
            (MOVE,),
        )
        direct = []
        for move_id, unit, tspace, tcode, occ_id, occ_unit, occ_state in cursor:
            if occ_state in (PLACEHOLDER, KEPT):
                # Reserved placeholders and retained rows never free a key.
                self.units[unit].occupied = True
                kind = "placeholder" if occ_state == PLACEHOLDER else "retained"
                direct.append(
                    (move_id, tspace, tcode, occ_id, kind, occ_unit, unit)
                )
            elif occ_state in (BLOCKED, MOVE):
                # The occupier's unit must free the key first (or share the
                # batch); whether that blocks the applicant is decided once
                # the final blocked set is known.
                self._add_dep(unit, occ_unit, move_id, tspace, tcode, occ_id)
            else:  # pragma: no cover - states are exhaustive
                raise AssertionError(f"unknown occupier state {occ_state!r}")
        self.conn.executemany("INSERT INTO direct_blocks VALUES (?,?,?,?,?,?,?)", direct)
        self.conn.commit()

    def _add_dep(self, unit, other, move_id, tspace, tcode, occ_id) -> None:
        if unit == other:
            return  # key swaps inside one unit are internal to its batch
        edges = self.deps.setdefault(unit, {})
        if other not in edges:
            edges[other] = {
                "id": move_id,
                "target": {"space": tspace, "code": tcode},
                "occupied_by": {"id": occ_id, "unit": other},
            }
            self.dependers.setdefault(other, set()).add(unit)

    # ------------------------------------------------------------------
    # Phase 5: blocked-set fixpoint, atomic groups, batch order
    # ------------------------------------------------------------------
    def propagate(self) -> None:
        self.blocked = {
            name
            for name, info in self.units.items()
            if info.business or info.contention or info.occupied
        }
        self._closure()

    def _closure(self) -> None:
        queue = deque(self.blocked)
        while queue:
            unit = queue.popleft()
            for depender in self.dependers.get(unit, ()):
                if depender not in self.blocked:
                    self.blocked.add(depender)
                    queue.append(depender)

    def finalize(self) -> None:
        self.sccs = _strongly_connected(self.units, self.deps)
        for scc in self.sccs:
            if len(scc) <= self.k:
                continue
            for unit in scc:
                self.units[unit].oversize = True
                self.oversize_groups[unit] = scc
                self.blocked.add(unit)
            self._closure()

    def plan_batches(self) -> None:
        surviving = [u for u in self.units if u not in self.blocked]
        scc_of = {}
        for index, scc in enumerate(self.sccs):
            for unit in scc:
                scc_of[unit] = index
        survivors = {scc_of[u] for u in surviving}
        successors: dict[int, set[int]] = {index: set() for index in survivors}
        indegree = {index: 0 for index in survivors}
        for unit in surviving:
            for other in self.deps.get(unit, ()):
                before, after = scc_of[other], scc_of[unit]
                if before != after and after in survivors and before in survivors:
                    if after not in successors[before]:
                        successors[before].add(after)
                        indegree[after] += 1

        heap = [(tuple(self.sccs[i]), i) for i in survivors if indegree[i] == 0]
        heapq.heapify(heap)
        self.batches = []
        while heap:
            _, index = heapq.heappop(heap)
            self.batches.append(self.sccs[index])
            for nxt in successors[index]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(heap, (tuple(self.sccs[nxt]), nxt))
        if len(self.batches) != len(survivors):  # pragma: no cover
            raise AssertionError("dependency cycle among atomic groups")

    def plan(self) -> None:
        self.resolve_rows()
        self.aggregate_units()
        self.find_contentions()
        self.find_occupancy()
        self.propagate()
        self.finalize()
        self.plan_batches()

    # ------------------------------------------------------------------
    # Output (streamed; move details are read back from SQLite in order)
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        counts = {"records": 0, "placeholders": 0, "moves": 0, "kept": 0,
                  "blocked_records": 0, "participants": 0}
        for state, count in self.conn.execute(
            "SELECT state, COUNT(*) FROM records GROUP BY state"
        ):
            counts["records"] += count
            if state == PLACEHOLDER:
                counts["placeholders"] = count
            elif state == MOVE:
                counts["moves"] = count
            elif state == KEPT:
                counts["kept"] = count
            elif state == BLOCKED:
                counts["blocked_records"] = count
        counts["participants"] = counts["records"] - counts["placeholders"]
        return {
            **counts,
            "units": len(self.units),
            "publishable_units": len(self.units) - len(self.blocked),
            "blocked_units": len(self.blocked),
            "batches": len(self.batches),
        }

    def emit(self, out, meta: dict | None = None) -> None:
        write = out.write
        write("{\n")
        write('  "status": "ok",\n')
        write(f'  "k": {self.k},\n')
        for key, value in (meta or {}).items():
            write(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},\n")
        write(f'  "summary": {json.dumps(self.summary(), ensure_ascii=False)},\n')
        self._emit_batches(out)
        write(",\n")
        self._emit_blocked(out)
        write("\n}\n")

    def _emit_batches(self, out) -> None:
        out.write('  "batches": [')
        for number, scc in enumerate(self.batches, 1):
            out.write(",\n" if number > 1 else "\n")
            out.write("    {\n")
            out.write(f'      "batch": {number},\n')
            out.write('      "units": [')
            for index, unit in enumerate(scc):
                out.write(",\n" if index else "\n")
                self._emit_unit(out, unit)
            out.write("\n      ]\n")
            out.write("    }")
        out.write("\n  ]" if self.batches else "]")

    def _emit_unit(self, out, unit: str) -> None:
        info = self.units[unit]
        depends = sorted(self.deps.get(unit, ()))
        out.write("        {\n")
        out.write(f'          "unit": {json.dumps(unit, ensure_ascii=False)},\n')
        out.write(f'          "depends_on": {json.dumps(depends, ensure_ascii=False)},\n')
        out.write(f'          "kept": {info.kept},\n')
        out.write('          "moves": [')
        first = True
        for row in self.conn.execute(
            "SELECT id, cur_space, cur_code, tgt_space, tgt_code FROM records"
            " WHERE unit = ? AND state = ? ORDER BY id",
            (unit, MOVE),
        ):
            move = {
                "id": row[0],
                "current": {"space": row[1], "code": row[2]},
                "target": {"space": row[3], "code": row[4]},
            }
            out.write(("," if not first else "") + "\n            ")
            out.write(json.dumps(move, ensure_ascii=False))
            first = False
        out.write("]" if first else "\n          ]")
        out.write("\n        }")

    def _emit_blocked(self, out) -> None:
        out.write('  "blocked_units": [')
        for index, unit in enumerate(sorted(self.blocked)):
            out.write(",\n" if index else "\n")
            out.write("        {\n")
            out.write(f'          "unit": {json.dumps(unit, ensure_ascii=False)},\n')
            out.write('          "reasons": [')
            first = True
            for reason in self._reasons(unit):
                out.write(("," if not first else "") + "\n            ")
                out.write(json.dumps(reason, ensure_ascii=False))
                first = False
            out.write("]" if first else "\n          ]")
            out.write("\n        }")
        out.write("\n  ]" if self.blocked else "]")

    def _reasons(self, unit: str):
        # 1. Member rows whose old code has no legal usable mapping.
        for row in self.conn.execute(
            "SELECT id, old_space, old_code, block FROM records"
            " WHERE unit = ? AND state = ? ORDER BY id",
            (unit, BLOCKED),
        ):
            detail = json.loads(row[3])
            kind = detail.pop("kind")
            yield {
                "kind": kind,
                "id": row[0],
                "old": {"space": row[1], "code": row[2]},
                **detail,
            }
        # 2. Contested target keys (all applicants listed, whichever unit).
        for tspace, tcode in self.conn.execute(
            "SELECT DISTINCT r.tgt_space, r.tgt_code FROM records r"
            " JOIN contested c ON c.tgt_space = r.tgt_space"
            " AND c.tgt_code = r.tgt_code"
            " WHERE r.unit = ? AND r.state = ? ORDER BY 1, 2",
            (unit, MOVE),
        ):
            applicants = [
                {"id": rid, "unit": applicant}
                for rid, applicant in self.conn.execute(
                    "SELECT id, unit FROM records WHERE state = ?"
                    " AND tgt_space = ? AND tgt_code = ? ORDER BY unit, id",
                    (MOVE, tspace, tcode),
                )
            ]
            yield {
                "kind": "target_contention",
                "target": {"space": tspace, "code": tcode},
                "applicants": applicants,
            }
        # 3. Target keys held by placeholders or retained rows (never freed).
        for row in self.conn.execute(
            "SELECT move_id, tgt_space, tgt_code, occupier_id, occupier_kind,"
            " occupier_unit FROM direct_blocks WHERE unit = ? ORDER BY move_id",
            (unit,),
        ):
            occupied_by = {"id": row[3], "kind": row[4]}
            if row[5] is not None:
                occupied_by["unit"] = row[5]
            yield {
                "kind": "target_occupied",
                "id": row[0],
                "target": {"space": row[1], "code": row[2]},
                "occupied_by": occupied_by,
            }
        # 4. Keys that only a blocked unit could free (follow the chain in
        #    that unit's own entry of "blocked_units").
        for other in sorted(self.deps.get(unit, ())):
            if other in self.blocked:
                yield {
                    "kind": "depends_on_blocked_unit",
                    "unit": other,
                    "via": self.deps[unit][other],
                }
        # 5. The atomic group itself exceeds k.
        if unit in self.oversize_groups:
            members = self.oversize_groups[unit]
            yield {
                "kind": "batch_too_large",
                "size": len(members),
                "k": self.k,
                "members": members,
            }


def _block_detail(kind: str, **fields) -> str:
    return json.dumps({"kind": kind, **fields}, ensure_ascii=False)


def _strongly_connected(units, deps) -> list[list[str]]:
    """Iterative Tarjan over the unit dependency graph (deterministic)."""

    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for root in sorted(units):
        if root in index_of:
            continue
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(sorted(deps.get(root, ()))))]
        while work:
            node, edges = work[-1]
            descended = False
            for nxt in edges:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(deps.get(nxt, ())))))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                result.append(sorted(scc))
    return result

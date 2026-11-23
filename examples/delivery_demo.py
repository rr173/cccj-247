#!/usr/bin/env python3
"""Replayable per-fact delivery walkthrough.

Builds a fresh ledger, enables delivery at an existing committed batch, posts a
mix of corrections / moves / rule changes, then exports a baseline and a change
interval and *replays* them into a downstream in-memory detail table.

The key point is the batch where two facts swap target codes: the balance rows
are byte-for-byte unchanged, yet the replay shows each fact's new attribution.

Run from the repository root:

    python3 -I examples/delivery_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_catalog import spec_from_dict  # noqa: E402
from migration_ledger import (  # noqa: E402
    Ledger,
    apply_records,
    iter_delivery_file,
)

RUN_DIR = ROOT / "examples" / "delivery_run"
DB_PATH = RUN_DIR / "ledger.db"
BIG = 2**200


def catalogue_spec() -> dict:
    return {
        "description": "Delivery demo: two old spaces, one new space.",
        "source_spaces": {"legacy": [[0, 20], [BIG, BIG + 9]], "aux": [[80, 99]]},
        "target_spaces": {"modern": [[1000, 1019], [BIG + 1000, BIG + 1009]]},
        "rules": [
            {"id": "core", "source_space": "legacy", "source_range": [0, 20],
             "priority": 10, "target_space": "modern", "direction": 1,
             "offset": 1000},
            {"id": "wide", "source_space": "legacy", "source_range": [BIG, BIG + 9],
             "priority": 10, "target_space": "modern", "direction": 1,
             "offset": 1000},
        ],
    }


# The rule set in force at batch 4: legacy:5 and legacy:6 swap their targets,
# leaving every aggregate balance exactly as it was.
SWAP_RULES = [
    {"id": "core-a", "source_space": "legacy", "source_range": [0, 4],
     "priority": 10, "target_space": "modern", "direction": 1, "offset": 1000},
    {"id": "core-b", "source_space": "legacy", "source_range": [7, 20],
     "priority": 10, "target_space": "modern", "direction": 1, "offset": 1000},
    {"id": "swap-5", "source_space": "legacy", "source_range": [5, 5],
     "priority": 20, "target_space": "modern", "direction": 1, "offset": 1001},
    {"id": "swap-6", "source_space": "legacy", "source_range": [6, 6],
     "priority": 20, "target_space": "modern", "direction": 1, "offset": 999},
    {"id": "wide", "source_space": "legacy", "source_range": [BIG, BIG + 9],
     "priority": 10, "target_space": "modern", "direction": 1, "offset": 1000},
]


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok - {label}")


def write_batch(name: str, payload: dict) -> bytes:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    (RUN_DIR / name).write_bytes(data)
    return data


def facts(ledger: Ledger) -> dict[str, dict]:
    rows = ledger.conn.execute(
        "SELECT fact_id, old_space, old_code, amount FROM fact ORDER BY fact_id"
    ).fetchall()
    return {
        fact_id: {"old_space": old_space, "old_code": int(old_code),
                  "amount": int(amount)}
        for fact_id, old_space, old_code, amount in rows
    }


def balances(ledger: Ledger) -> dict[int, tuple[int, int]]:
    return {
        row["new_code"]: (row["fact_count"], row["amount_sum"])
        for row in ledger.list_balances("modern")
    }


def restore(paths: list[Path]) -> dict[str, dict]:
    records: list[dict] = []
    for path in paths:
        records.extend(iter_delivery_file(path))
    return apply_records(records)[1]


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for stale in RUN_DIR.iterdir():
        stale.unlink()

    print("1. Seed a ledger *before* delivery is enabled (batches 1-3)")
    ledger = Ledger.initialize(DB_PATH, spec_from_dict(catalogue_spec()))
    ledger.submit_bytes(write_batch("batch_001.json", {"seq": 1, "facts": [
        {"id": "A", "old_space": "legacy", "old_code": 5, "amount": 100},
        {"id": "B", "old_space": "legacy", "old_code": 6, "amount": 100},
        {"id": "C", "old_space": "legacy", "old_code": 7, "amount": 0},
        {"id": "W", "old_space": "legacy", "old_code": BIG + 3, "amount": BIG + 7},
    ]}))
    ledger.submit_bytes(write_batch("batch_002.json", {"seq": 2, "facts": [
        {"id": "D", "old_space": "legacy", "old_code": 8, "amount": -25},
        {"id": "C", "old_space": "legacy", "old_code": 7, "amount": 0},
    ]}))
    ledger.submit_bytes(write_batch("batch_003.json", {"seq": 3, "facts": [
        {"id": "E", "old_space": "aux", "old_code": 90, "amount": 9},
    ]}))
    check("head is 3 before enabling", ledger.head_seq() == 3)

    print("\n2. Enable delivery: baseline is snapshotted at the current head (S=3)")
    result = ledger.enable_delivery()
    check("start seq is 3", result["start_seq"] == 3)
    check("baseline holds the five booked facts", result["baseline_facts"] == 5)

    print("\n3. Batches after S: an amount correction, then a silent rule swap")
    ledger.submit_bytes(write_batch("batch_004.json", {"seq": 4, "facts": [
        {"id": "D", "old_space": "legacy", "old_code": 8, "amount": -7},
    ]}))
    check("correction is visible in the balance table",
          balances(ledger)[1008] == (1, -7))
    before_swap = balances(ledger)
    ledger.submit_bytes(
        write_batch("batch_005.json", {"seq": 5, "rules": SWAP_RULES, "facts": []})
    )
    after_swap = balances(ledger)
    check("balance table is unchanged by the rule swap", before_swap == after_swap)
    ledger.submit_bytes(write_batch("batch_006.json", {"seq": 6, "facts": [
        {"id": "F", "old_space": "legacy", "old_code": 9, "amount": 42},
    ]}))
    check("head is 6", ledger.head_seq() == 6)

    print("\n4. Re-export the baseline after newer batches: same S, same bytes")
    baseline_a = RUN_DIR / "baseline.ndjson"
    baseline_b = RUN_DIR / "baseline-again.ndjson"
    ledger.export_baseline(baseline_a)
    ledger.export_baseline(baseline_b)
    check("baseline is byte-identical on re-export",
          baseline_a.read_bytes() == baseline_b.read_bytes())

    print("\n5. Downstream asks for (3, 6]: one file, every batch identified")
    changes = RUN_DIR / "changes-3-6.ndjson"
    summary = ledger.export_changes(3, 6, changes)
    check("change file covers three batches", summary["batches"] == 3)
    check("four facts changed (D, the A/B swap, and the new F)",
          summary["records"] == 4)
    markers = [
        (record["seq"], record["changes"])
        for record in iter_delivery_file(changes)
        if record["type"] == "batch"
    ]
    check("each batch appears in order, empty batches included",
          markers == [(4, 1), (5, 2), (6, 1)])

    print("\n6. Replay baseline + changes into a downstream detail table")
    restored = restore([baseline_a, changes])
    check("deleted fact E is gone", "E" not in restored)
    check("A/B swapped exactly (balance table could not show this)",
          (restored["A"]["new_code"], restored["B"]["new_code"]) == ("1006", "1005"))
    check("D carries the corrected amount", restored["D"]["amount"] == "-7")
    check("F was added with its own detail", restored["F"]["amount"] == "42")
    check("zero-amount fact C is still delivered", restored["C"]["amount"] == "0")
    check("wide fact W round-trips losslessly",
          restored["W"]["new_code"] == str(BIG + 1003)
          and restored["W"]["amount"] == str(BIG + 7))

    print("\n7. The restored table equals the ledger's own booked detail")
    # Independently recompute what should be booked from the final facts and
    # the final rule set, then compare with the replay.
    from migration_catalog import Catalog
    from migration_ledger.delivery import delivered_detail

    final_rules = SWAP_RULES
    catalog = Catalog(spec_from_dict({
        "source_spaces": catalogue_spec()["source_spaces"],
        "target_spaces": catalogue_spec()["target_spaces"],
        "rules": final_rules,
    }))
    expected = {}
    for fact_id, row in facts(ledger).items():
        detail = delivered_detail(
            catalog, row["old_space"], row["old_code"], row["amount"]
        )
        if detail is not None:
            expected[fact_id] = {
                "target_space": detail[0],
                "new_code": str(detail[1]),
                "amount": str(detail[2]),
            }
    check("replay matches recomputation", restored == expected)
    check("the ledger's own stored facts back this up",
          set(expected) == {"A", "B", "C", "D", "F", "W"})

    print("\n8. A fixed interval stays byte-identical after new batches commit")
    first = RUN_DIR / "changes-3-6-first.ndjson"
    first.write_bytes(changes.read_bytes())
    ledger.submit_bytes(write_batch("batch_007.json", {"seq": 7, "facts": [
        {"id": "G", "old_space": "legacy", "old_code": 10, "amount": 1},
    ]}))
    again = RUN_DIR / "changes-3-6-again.ndjson"
    ledger.export_changes(3, 6, again)
    check("re-export of the same interval is byte-identical",
          first.read_bytes() == again.read_bytes())
    wider = RUN_DIR / "changes-6-7.ndjson"
    ledger.export_changes(6, 7, wider)
    check("a newer batch only appears in a wider interval",
          [r["seq"] for r in iter_delivery_file(wider) if r["type"] == "batch"] == [7])

    print("\n9. Invalid intervals are rejected without touching the last good file")
    guard = RUN_DIR / "guarded.ndjson"
    guard.write_bytes(b"previous complete delivery\n")
    for low, high in [(2, 6), (5, 5), (3, 99)]:
        try:
            ledger.export_changes(low, high, guard)
        except ValueError:
            pass
        else:
            raise AssertionError(f"FAILED: accepted ({low}, {high}]")
    check("last complete delivery preserved",
          guard.read_bytes() == b"previous complete delivery\n")

    ledger.close()

    print("\n10. Reopen: the same S and the same baseline content come back")
    ledger = Ledger.open(DB_PATH)
    reopened = ledger.enable_delivery()
    check("re-enable returns the original S", reopened["start_seq"] == 3)
    check("re-enable is idempotent", reopened["already_enabled"])
    reopened_baseline = RUN_DIR / "baseline-reopen.ndjson"
    ledger.export_baseline(reopened_baseline)
    check("baseline after reopen is byte-identical",
          baseline_a.read_bytes() == reopened_baseline.read_bytes())

    print("\nCommands used (all from the repository root):")
    print(f"  python3 -I bin/migration-ledger delivery-enable {DB_PATH}")
    print(f"  python3 -I bin/migration-ledger delivery-baseline {DB_PATH} out/baseline.ndjson")
    print(f"  python3 -I bin/migration-ledger delivery-changes {DB_PATH} out/changes.ndjson "
          "--low 3 --high 6")
    ledger.close()
    print("\nALL DELIVERY DEMO CHECKS PASSED")


if __name__ == "__main__":
    main()

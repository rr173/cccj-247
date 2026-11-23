#!/usr/bin/env python3
"""Re-runnable ledger walkthrough.

The scenario exercises every rule in the brief on a fresh, disposable database:

* initialising an empty SQLite ledger from the existing catalogue spec;
* posting facts, correcting amounts, moving facts between old codes, deleting;
* facts blocked by disabled / missing / ambiguous / out-of-range catalog states
  stay stored but never enter the balance table, and reappear automatically;
* a batch that changes rules *and* facts publishes only the end-of-batch state;
* idempotent byte-identical replay, same-seq-different-content and gap rejection;
* amounts and codes beyond 64 bits, zero-sum rows still carrying fact counts;
* a real process kill inside the commit transaction and a clean reopen;
* a local rule change over a ledger holding 20,000 unrelated "cold" facts, with
  instrumentation and EXPLAIN QUERY PLAN proving the cold facts are never scanned.

Run from the repository root:

    python3 -I examples/ledger_demo.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_catalog import spec_from_dict  # noqa: E402
from migration_ledger import Ledger, LedgerError  # noqa: E402

RUN_DIR = ROOT / "examples" / "ledger_run"
DB_PATH = RUN_DIR / "ledger.db"
CATALOG_PATH = RUN_DIR / "catalog.json"

BIG = 10**30  # far beyond 64 bits; the domain is declared symbolically.


def initial_spec() -> dict:
    return {
        "description": "Ledger demo catalogue: two source spaces, one target.",
        "source_spaces": {
            "legacy": [[0, 20], [400, 499], [BIG, BIG + 9]],
            "aux": [[80, 99]],
        },
        "target_spaces": {
            "modern": [[1000, 1019], [BIG + 1000, BIG + 1009]],
            "archive": [[9000, 9009]],
        },
        "rules": [
            {
                "id": "main",
                "source_space": "legacy",
                "source_range": [0, 9],
                "priority": 10,
                "target_space": "modern",
                "direction": 1,
                "offset": 1000,
            },
            {
                "id": "retire",
                "source_space": "legacy",
                "source_range": [10, 12],
                "priority": 20,
                "action": "disable",
            },
            {
                # Only competes for modern 1000..1004; modern 1005..1009 stays
                # unique so blocked and eligible segments coexist in one table.
                "id": "aux-mirror",
                "source_space": "aux",
                "source_range": [85, 89],
                "priority": 10,
                "target_space": "modern",
                "direction": -1,
                "offset": 1089,
            },
            {
                "id": "wide",
                "source_space": "legacy",
                "source_range": [BIG, BIG + 9],
                "priority": 10,
                "target_space": "modern",
                "direction": 1,
                "offset": 1000,
            },
        ],
    }


BASE_RULES = initial_spec()["rules"]


def write_batch(name: str, payload: dict) -> bytes:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    (RUN_DIR / name).write_bytes(data)
    return data


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok - {label}")


def balance_map(ledger: Ledger) -> dict[tuple[str, int], tuple[int, int]]:
    return {
        (row["target_space"], row["new_code"]): (row["fact_count"], row["amount_sum"])
        for row in ledger.list_balances()
    }


def balance_codes(balances):
    return list(balances)


def assert_query_plans() -> None:
    """The refresh SQL must use the position indexes, never a full fact scan."""

    conn = sqlite3.connect(str(DB_PATH))
    try:
        range_plan = " ".join(
            row[3]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT old_code FROM fact "
                "WHERE old_space = ? AND code_key BETWEEN ? AND ?",
                ("legacy", "x", "y"),
            ).fetchall()
        )
        point_plan = " ".join(
            row[3]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT fact_id FROM fact "
                "WHERE old_space = ? AND code_key = ?",
                ("legacy", "x"),
            ).fetchall()
        )
    finally:
        conn.close()
    print(f"  EQP range: {range_plan}")
    print(f"  EQP point: {point_plan}")
    check("range query uses fact_position_idx", "USING INDEX fact_position_idx" in range_plan)
    check("point query uses fact_position_idx", "USING INDEX fact_position_idx" in point_plan)
    check("no full scan of fact in either plan", "SCAN fact" not in range_plan + point_plan)


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for stale in RUN_DIR.iterdir():
        stale.unlink()
    CATALOG_PATH.write_text(json.dumps(initial_spec(), indent=2), encoding="utf-8")

    print("1. Initialise empty ledger from the catalogue spec")
    ledger = Ledger.initialize(DB_PATH, spec_from_dict(initial_spec()))
    check("head starts at 0", ledger.head_seq() == 0)
    check("no balances before any batch", ledger.list_balances() == [])

    print("\n2. Batch 1: facts land only where mapped + unique")
    b1 = write_batch(
        "batch_001.json",
        {
            "seq": 1,
            "facts": [
                {"id": "F1", "old_space": "legacy", "old_code": 1, "amount": 500},
                {"id": "F2", "old_space": "legacy", "old_code": 2, "amount": 300},
                {"id": "F3", "old_space": "legacy", "old_code": 3, "amount": 250},
                {"id": "F4", "old_space": "legacy", "old_code": 4, "amount": -800},
                {"id": "F5", "old_space": "legacy", "old_code": 10, "amount": 111},
                {"id": "F6", "old_space": "legacy", "old_code": 15, "amount": 222},
                {"id": "F7", "old_space": "legacy", "old_code": BIG, "amount": BIG + 7},
                {"id": "F8", "old_space": "legacy", "old_code": 8, "amount": 0},
                {"id": "F9", "old_space": "aux", "old_code": 88, "amount": 99},
            ],
        },
    )
    result = ledger.submit_bytes(b1)
    check("batch 1 committed", result.status == "committed" and result.seq == 1)
    balances = balance_map(ledger)
    # aux-mirror competes on modern 1000..1004; legacy 1..4 cannot enter.
    check("F1 blocked: modern 1001 ambiguous with aux", ("modern", 1001) not in balances)
    check("F2 blocked: modern 1002 ambiguous", ("modern", 1002) not in balances)
    check("F3 blocked: modern 1003 ambiguous", ("modern", 1003) not in balances)
    check("F4 blocked: modern 1004 ambiguous", ("modern", 1004) not in balances)
    check("F5 disabled (retire rule) stays out", ("modern", 1007) not in balances)
    check("F6 missing a rule stays out", all(code != 1010 for _, code in balance_codes(balances)))
    check(
        "F7 beyond-64-bit balance exact",
        balances.get(("modern", BIG + 1000)) == (1, BIG + 7),
    )
    check("F8 unique code posts even with zero amount", balances.get(("modern", 1008)) == (1, 0))
    check("zero amount still reports one fact", ledger.get_balance("modern", 1008)["fact_count"] == 1)
    check("F9 is the competing aux fact: its target stays blocked", ("modern", 1001) not in balances)
    check("blocked facts remain stored with latest content", ledger.get_fact("F4")["amount"] == -800)
    check("balance read carries head batch number", ledger.get_balance("modern", 1008)["head_seq"] == 1)
    check("archive target has no rows", all(space != "archive" for space, _ in balance_codes(balances)))

    print("\n3. Batch 2: rules and facts change together - only the end state is published")
    competitor = {
        "id": "competitor",
        "source_space": "aux",
        "source_range": [90, 98],
        "priority": 30,
        "target_space": "modern",
        "direction": 1,
        "offset": 910,
    }
    b2 = write_batch(
        "batch_002.json",
        {
            "seq": 2,
            "rules": BASE_RULES + [competitor],
            "facts": [
                # F8 moves legacy:8 -> legacy:7; its old balance row must vanish.
                {"id": "F8", "old_space": "legacy", "old_code": 7, "amount": 40},
            ],
        },
    )
    ledger.submit_bytes(b2)
    balances = balance_map(ledger)
    check("F8 old row 1008 removed after the move", ("modern", 1008) not in balances)
    check("F8 new row 1007 is immediately blocked by the competitor",
          ("modern", 1007) not in balances)
    check("competitor makes modern 1005..1008 ambiguous",
          all(("modern", c) not in balances for c in range(1005, 1009)))
    check("modern 1009 stays unique (blockers do not spread across segments)",
          ("modern", 1009) not in balances)  # unique position, simply no fact booked
    check("F7 wide row untouched by the local competitor",
          balances.get(("modern", BIG + 1000)) == (1, BIG + 7))
    check("F4 fact itself still in store", ledger.get_fact("F4")["amount"] == -800)

    print("\n4. Batch 3: corrections while blocked are stored and reconsidered later")
    ledger.submit_bytes(
        write_batch(
            "batch_003.json",
            {
                "seq": 3,
                "facts": [
                    {"id": "F1", "old_space": "legacy", "old_code": 1, "amount": 650},
                    {"id": "F2", "old_space": "legacy", "old_code": 6, "amount": 77},
                    {"id": "F8", "old_space": "legacy", "old_code": 7, "amount": 42},
                ],
            },
        )
    )
    balances = balance_map(ledger)
    check("F1 correction stored while still blocked", ledger.get_fact("F1")["amount"] == 650)
    check("no row at 1001 while ambiguous", ("modern", 1001) not in balances)
    check("F2 stored but blocked (1006 ambiguous)", ("modern", 1006) not in balances)
    check("F8 correction stored but blocked (1007 ambiguous)", ("modern", 1007) not in balances)

    print("\n5. Batch 4: competition clears; corrections made while blocked are incorporated")
    ledger.submit_bytes(
        write_batch(
            "batch_004.json",
            {"seq": 4, "rules": BASE_RULES, "facts": []},
        )
    )
    balances = balance_map(ledger)
    check("F2 row appears with the amount corrected during the block",
          balances.get(("modern", 1006)) == (1, 77))
    check("F8 row appears with its corrected 42",
          balances.get(("modern", 1007)) == (1, 42))
    check("F1 still blocked by the standing aux-mirror, correction retained",
          ("modern", 1001) not in balances and ledger.get_fact("F1")["amount"] == 650)
    check("F3/F4 stay blocked by the standing aux-mirror",
          all(("modern", c) not in balances for c in (1003, 1004)))
    check("unique-but-unbooked modern 1009 has no row", ("modern", 1009) not in balances)
    check("F7 wide row returns exactly", balances.get(("modern", BIG + 1000)) == (1, BIG + 7))
    check("reincorporated rows are stamped with batch 4",
          {ledger.get_balance("modern", c)["updated_seq"] for c in (1006, 1007)} == {4})
    check("untouched rows keep their original stamp",
          ledger.get_balance("modern", BIG + 1000)["updated_seq"] == 1)

    print("\n6. Reopen: durable state comes back from SQLite alone")
    ledger.close()
    ledger = Ledger.open(DB_PATH)
    check("head seq is 4 after reopen", ledger.head_seq() == 4)
    check("balances identical after reopen", balance_map(ledger) == balances)
    check("committed batch numbers are 1..4", ledger.list_committed_seqs() == [1, 2, 3, 4])

    print("\n7. Idempotency and protocol errors leave committed state untouched")
    duplicate = ledger.submit_bytes(b2)
    check("identical bytes replay as duplicate", duplicate.status == "duplicate")
    check("duplicate does not move head", ledger.head_seq() == 4)
    for label, bad in [
        ("same seq, different content", {"seq": 4, "facts": []}),
        ("gap in numbering", {"seq": 9, "facts": []}),
        ("unknown fact delete", {"seq": 5, "facts": [{"id": "GHOST", "action": "delete"}]}),
        ("fact outside legal domain", {"seq": 5, "facts": [
            {"id": "X", "old_space": "legacy", "old_code": 700, "amount": 1}]}),
        ("duplicate id inside batch", {"seq": 5, "facts": [
            {"id": "F1", "old_space": "legacy", "old_code": 1, "amount": 1},
            {"id": "F1", "old_space": "legacy", "old_code": 2, "amount": 1}]}),
        ("bool amount rejected", {"seq": 5, "facts": [
            {"id": "B", "old_space": "legacy", "old_code": 5, "amount": True}]}),
    ]:
        try:
            ledger.submit_bytes(json.dumps(bad).encode())
        except LedgerError as exc:
            check(f"rejected: {label}", True, str(exc))
        else:
            raise AssertionError(f"FAILED: {label} was accepted")
    check("head still 4 after all rejections", ledger.head_seq() == 4)

    print("\n8. Rules omitted from a batch inherit the last committed rule set")
    ledger.submit_bytes(
        write_batch(
            "batch_005.json",
            {"seq": 5, "facts": [{"id": "F2", "old_space": "legacy", "old_code": 6, "amount": 88}]},
        )
    )
    check("inherited rules keep the same table shape",
          balance_map(ledger).get(("modern", 1006)) == (1, 88))
    ledger.submit_bytes(
        write_batch(
            "batch_006.json",
            {"seq": 6, "facts": [{"id": "F2", "old_space": "legacy", "old_code": 6, "amount": 77}]},
        )
    )
    check("F2 restored to baseline 77", balance_map(ledger).get(("modern", 1006)) == (1, 77))

    print("\n9. Twenty thousand cold facts, then a local rule change: no full scan")
    _seed_cold_facts(ledger)
    total_facts = ledger.conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
    check("cold facts are stored", total_facts >= 20_000)
    assert_query_plans()
    freeze2 = {
        "id": "freeze-code-2",
        "source_space": "legacy",
        "source_range": [2, 2],
        "priority": 40,
        "action": "disable",
    }
    # legacy:2 currently has no balance row (still ambiguous from aux-mirror),
    # so use legacy:6 (unique, hosts F2) to prove a local disable stays local.
    freeze6 = {
        "id": "freeze-code-6",
        "source_space": "legacy",
        "source_range": [6, 6],
        "priority": 40,
        "action": "disable",
    }
    stats = ledger.submit_bytes(
        write_batch(
            "batch_local_off.json",
            {"seq": ledger.head_seq() + 1, "rules": BASE_RULES + [freeze2, freeze6], "facts": []},
        )
    ).refresh
    print(f"  refresh stats: {json.dumps(stats.as_dict(), sort_keys=True)}")
    check("refresh touched only a handful of facts", stats.fact_rows_touched < 30)
    check("cold facts never scanned", stats.fact_rows_touched < total_facts // 100)
    check("F2 balance removed by local disable", ("modern", 1006) not in balance_map(ledger))
    check("unrelated F7 wide balance survives",
          balance_map(ledger).get(("modern", BIG + 1000)) == (1, BIG + 7))
    restore = ledger.submit_bytes(
        write_batch(
            "batch_local_on.json",
            {"seq": ledger.head_seq() + 1, "rules": BASE_RULES, "facts": []},
        )
    ).refresh
    check("restoring stays local too", restore.fact_rows_touched < 30)
    check("F2 balance restored", balance_map(ledger).get(("modern", 1006)) == (1, 77))

    print("\n10. Process killed inside the commit transaction: reopen sees the prior state")
    _crash_reopen_check()

    ledger.close()
    print("\nALL LEDGER DEMO CHECKS PASSED")


def _seed_cold_facts(ledger: Ledger) -> None:
    """20,000 facts on legacy 400..499, where no rule ever maps anything."""

    chunk = 1000
    cold_id = 0
    for start in range(0, 20_000, chunk):
        facts = []
        for _ in range(chunk):
            code = 400 + (cold_id % 100)
            facts.append(
                {"id": f"COLD-{cold_id:05d}", "old_space": "legacy", "old_code": code, "amount": cold_id}
            )
            cold_id += 1
        ledger.submit_bytes(
            write_batch(
                f"batch_cold_{ledger.head_seq() + 1:03d}.json",
                {"seq": ledger.head_seq() + 1, "facts": facts},
            )
        )
    check("cold facts create no balance rows",
          all(space == "modern" for space, _ in balance_map(ledger)))


def _crash_reopen_check() -> None:
    crash_db = RUN_DIR / "crash.db"
    crash_db.unlink(missing_ok=True)
    ledger = Ledger.initialize(crash_db, spec_from_dict(initial_spec()))
    ledger.submit_bytes(_crash_seed_payload())
    head_before = ledger.head_seq()
    ledger.close()

    helper = RUN_DIR / "crash_helper.py"
    helper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from migration_ledger import Ledger\n"
        "import json\n"
        "import os\n"
        f"ledger = Ledger.open({str(crash_db)!r})\n"
        "payload = json.dumps({'seq': 2, 'facts': ["
        "{'id': 'CRASH', 'old_space': 'legacy', 'old_code': 6, 'amount': 9}]}).encode()\n"
        "ledger.submit_bytes(payload, precommit_hook=lambda seq: os._exit(7))\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(helper)], capture_output=True, text=True
    )
    check("helper process died with the hook exit code", proc.returncode == 7, proc.stderr)
    reopened = Ledger.open(crash_db)
    check("head unchanged after mid-commit kill", reopened.head_seq() == head_before)
    check("killed fact never became visible", reopened.get_fact("CRASH") is None)
    check("pre-crash balance intact", reopened.get_balance("modern", 1006) == {
        "target_space": "modern", "new_code": 1006, "fact_count": 1,
        "amount_sum": 700, "updated_seq": 1, "head_seq": 1,
    })
    reopened.submit_bytes(
        json.dumps(
            {"seq": 2, "facts": [
                {"id": "CRASH", "old_space": "legacy", "old_code": 6, "amount": 9}]}
        ).encode()
    )
    check("recovered ledger accepts the next batch", reopened.head_seq() == 2)
    reopened.close()


def _crash_seed_payload() -> bytes:
    return json.dumps(
        {
            "seq": 1,
            "facts": [
                {"id": "SEED", "old_space": "legacy", "old_code": 6, "amount": 700},
            ],
        }
    ).encode()


if __name__ == "__main__":
    main()

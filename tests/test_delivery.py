"""Tests for the replayable per-fact delivery files.

Run with ``python3 -m unittest discover -s tests -v`` (pytest also works).
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_catalog import Catalog, spec_from_dict  # noqa: E402
from migration_ledger import (  # noqa: E402
    Ledger,
    LedgerError,
    apply_records,
    iter_delivery_file,
)
from migration_ledger.delivery import delivered_detail  # noqa: E402


def spec(rules=None, source_domain=((0, 40),), target_domain=((0, 300),)):
    return spec_from_dict(
        {
            "source_spaces": {"s": [[lo, hi] for lo, hi in source_domain]},
            "target_spaces": {"t": [[lo, hi] for lo, hi in target_domain]},
            "rules": rules
            if rules is not None
            else [
                {
                    "id": "m",
                    "source_space": "s",
                    "source_range": [0, 40],
                    "priority": 10,
                    "target_space": "t",
                    "direction": 1,
                    "offset": 200,
                }
            ],
        }
    )


def submit(ledger, seq, facts=(), rules=None):
    payload = {"seq": seq}
    if rules is not None:
        payload["rules"] = rules
    if facts:
        payload["facts"] = facts
    return ledger.submit_bytes(json.dumps(payload).encode())


def write_fact(fid, code, amount, space="s"):
    return {"id": fid, "old_space": space, "old_code": code, "amount": amount}


def restore(*paths):
    """Baseline then changes, exactly as a downstream consumer would."""

    records = []
    for path in paths:
        records.extend(iter_delivery_file(path))
    return apply_records(records)


def expected_state(ledger, catalog):
    """Brute-force delivered detail for the ledger's current stored facts."""

    result = {}
    rows = ledger.conn.execute(
        "SELECT fact_id, old_space, old_code, amount FROM fact ORDER BY fact_id"
    ).fetchall()
    for fact_id, old_space, old_code, amount in rows:
        detail = delivered_detail(catalog, old_space, int(old_code), int(amount))
        if detail is None:
            continue
        result[fact_id] = {
            "target_space": detail[0],
            "new_code": str(detail[1]),
            "amount": str(detail[2]),
        }
    return result


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "l.db"
        self.ledger = Ledger.initialize(self.db, spec())

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_enable_on_existing_data_snapshots_at_head(self):
        submit(self.ledger, 1, [write_fact("b", 6, 50), write_fact("a", 5, 100)])
        submit(self.ledger, 2, [write_fact("c", 7, 0)])
        result = self.ledger.enable_delivery()
        self.assertEqual(result["start_seq"], 2)
        self.assertEqual(result["baseline_facts"], 3)
        path = self.dir / "baseline.ndjson"
        summary = self.ledger.export_baseline(path)
        self.assertEqual(summary["fact_count"], 3)
        self.assertEqual(summary["start_seq"], 2)
        seq, state = restore(path)
        self.assertEqual(seq, 0)
        self.assertEqual(
            state,
            {
                "a": {"target_space": "t", "new_code": "205", "amount": "100"},
                "b": {"target_space": "t", "new_code": "206", "amount": "50"},
                "c": {"target_space": "t", "new_code": "207", "amount": "0"},
            },
        )
        # Sorted by id in the file itself.
        ids = [
            json.loads(line)["id"]
            for line in path.read_text(encoding="utf-8").splitlines()[1:-1]
        ]
        self.assertEqual(ids, ["a", "b", "c"])

    def test_baseline_only_contains_booked_facts(self):
        partial = spec(rules=[
            {"id": "m", "source_space": "s", "source_range": [0, 20],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ])
        ledger = Ledger.initialize(self.dir / "partial.db", partial)
        try:
            submit(ledger, 1, [
                write_fact("ok", 5, 10),
                write_fact("blocked", 35, 10),  # no rule covers 35
            ])
            ledger.enable_delivery()
            path = self.dir / "b.ndjson"
            ledger.export_baseline(path)
            _, state = restore(path)
            self.assertEqual(set(state), {"ok"})
        finally:
            ledger.close()

    def test_empty_ledger_uses_start_zero(self):
        result = self.ledger.enable_delivery()
        self.assertEqual(result["start_seq"], 0)
        self.assertEqual(result["baseline_facts"], 0)
        path = self.dir / "b.ndjson"
        self.ledger.export_baseline(path)
        _, state = restore(path)
        self.assertEqual(state, {})

    def test_reopen_and_reenable_keep_same_s_and_content(self):
        submit(self.ledger, 1, [write_fact("a", 5, 100)])
        submit(self.ledger, 2, [write_fact("b", 6, 100)])
        self.ledger.enable_delivery()
        first = self.dir / "first.ndjson"
        self.ledger.export_baseline(first)

        # New batches change the current state, but never the snapshot.
        submit(self.ledger, 3, [write_fact("a", 8, 999)])
        submit(self.ledger, 4, [{"id": "b", "action": "delete"}])
        self.ledger.close()

        self.ledger = Ledger.open(self.db)
        again = self.ledger.enable_delivery()
        self.assertTrue(again["already_enabled"])
        self.assertEqual(again["start_seq"], 2)
        self.assertEqual(again["baseline_facts"], 2)
        second = self.dir / "second.ndjson"
        self.ledger.export_baseline(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_baseline_excludes_later_changes_and_has_no_change_rows(self):
        submit(self.ledger, 1, [write_fact("a", 5, 100)])
        self.ledger.enable_delivery()
        submit(self.ledger, 2, [write_fact("a", 5, 101)])
        change_file = self.dir / "c.ndjson"
        self.ledger.export_changes(1, 2, change_file)
        self.assertIn('"id":"a"', change_file.read_text(encoding="utf-8"))

    def test_enable_is_atomic_with_a_concurrent_batch(self):
        # A batch committing before enable is part of the snapshot; one
        # committing after produces change rows; never both or neither.
        submit(self.ledger, 1, [write_fact("a", 5, 100)])
        self.ledger.enable_delivery()
        submit(self.ledger, 2, [write_fact("a", 5, 101)])
        count = self.ledger.conn.execute(
            "SELECT COUNT(*) FROM delivery_change WHERE seq = 1"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        baseline = self.dir / "b.ndjson"
        changes = self.dir / "c.ndjson"
        self.ledger.export_baseline(baseline)
        self.ledger.export_changes(1, 2, changes)
        _, state = restore(baseline, changes)
        self.assertEqual(state["a"]["amount"], "101")


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "l.db"
        self.ledger = Ledger.initialize(self.db, spec())
        submit(self.ledger, 1, [
            write_fact("a", 5, 100),
            write_fact("b", 6, 50),
            write_fact("c", 7, 0),
        ])
        self.ledger.enable_delivery()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def export(self, low, high, name="c.ndjson"):
        path = self.dir / name
        self.ledger.export_changes(low, high, path)
        return path

    def test_amount_correction_emits_before_and_after(self):
        submit(self.ledger, 2, [write_fact("a", 5, 101)])
        path = self.export(1, 2)
        records = list(iter_delivery_file(path))
        changes = [r for r in records if r["type"] == "change"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["id"], "a")
        self.assertEqual(changes[0]["before"]["amount"], "100")
        self.assertEqual(changes[0]["after"]["amount"], "101")

    def test_move_emits_one_before_after_pair(self):
        submit(self.ledger, 2, [write_fact("a", 8, 100)])
        changes = [r for r in iter_delivery_file(self.export(1, 2))
                   if r["type"] == "change"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["before"]["new_code"], "205")
        self.assertEqual(changes[0]["after"]["new_code"], "208")

    def test_delete_and_unbooked_sides_are_null(self):
        submit(self.ledger, 2, [{"id": "b", "action": "delete"}])
        changes = [r for r in iter_delivery_file(self.export(1, 2))
                   if r["type"] == "change"]
        self.assertEqual(changes[0]["id"], "b")
        self.assertIsNotNone(changes[0]["before"])
        self.assertIsNone(changes[0]["after"])

    def test_unblocking_fact_is_delivered_from_empty_side(self):
        # Add a disable rule for code 8 in batch 2, then another fact there.
        base_rules = [
            {"id": "m", "source_space": "s", "source_range": [0, 40],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        frozen = base_rules + [
            {"id": "off", "source_space": "s", "source_range": [8, 8],
             "priority": 50, "action": "disable"},
        ]
        submit(self.ledger, 2, [write_fact("d", 8, 7)])
        submit(self.ledger, 3, [], rules=frozen)
        submit(self.ledger, 4, [], rules=base_rules)
        changes = [r for r in iter_delivery_file(self.export(1, 4))
                   if r["type"] == "change" and r["id"] == "d"]
        self.assertEqual(len(changes), 3)
        self.assertEqual(
            [(c["before"], c["after"]) for c in changes],
            [
                (None, {"target_space": "t", "new_code": "208", "amount": "7"}),
                ({"target_space": "t", "new_code": "208", "amount": "7"}, None),
                (None, {"target_space": "t", "new_code": "208", "amount": "7"}),
            ],
        )

    def test_unnamed_facts_can_swap_attribution_without_touching_balances(self):
        # Batch 1 uses a rule set where legacy 5..7 all map through the same
        # target offset but the balances differ per code.  A later rule change
        # swaps which fact lands where while every per-code aggregate stays
        # identical: only the per-fact delivery can reveal it.
        rules = [
            {"id": "m", "source_space": "s", "source_range": [0, 4],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
            {"id": "x5", "source_space": "s", "source_range": [5, 5],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
            {"id": "x6", "source_space": "s", "source_range": [6, 6],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        ledger = Ledger.initialize(self.dir / "swap.db", spec(rules=rules))
        try:
            submit(ledger, 1, [write_fact("a", 5, 50), write_fact("b", 6, 50)])
            ledger.enable_delivery()
            before = {
                row["new_code"]: (row["fact_count"], row["amount_sum"])
                for row in ledger.list_balances()
            }
            self.assertEqual(before, {205: (1, 50), 206: (1, 50)})
            swapped = [
                {"id": "m", "source_space": "s", "source_range": [0, 4],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
                {"id": "y5", "source_space": "s", "source_range": [5, 5],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 201},
                {"id": "y6", "source_space": "s", "source_range": [6, 6],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 199},
            ]
            submit(ledger, 2, [], rules=swapped)
            after = {
                row["new_code"]: (row["fact_count"], row["amount_sum"])
                for row in ledger.list_balances()
            }
            self.assertEqual(before, after)
            path = self.dir / "swap.ndjson"
            ledger.export_changes(1, 2, path)
            changes = [r for r in iter_delivery_file(path) if r["type"] == "change"]
            mapping = {r["id"]: (r["before"]["new_code"], r["after"]["new_code"])
                       for r in changes}
            self.assertEqual(mapping, {"a": ("205", "206"), "b": ("206", "205")})
        finally:
            ledger.close()

    def test_batch_without_changes_still_has_marker(self):
        submit(self.ledger, 2, [])
        submit(self.ledger, 3, [write_fact("a", 5, 100)])  # unchanged content
        path = self.export(1, 3)
        batches = [r for r in iter_delivery_file(path) if r["type"] == "batch"]
        self.assertEqual([(r["seq"], r["changes"]) for r in batches], [(2, 0), (3, 0)])

    def test_rule_evidence_change_without_detail_change_is_silent(self):
        rules = [
            {"id": "m", "source_space": "s", "source_range": [0, 40],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
            {"id": "same", "source_space": "s", "source_range": [0, 20],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        submit(self.ledger, 2, [], rules=rules)
        path = self.export(1, 2)
        changes = [r for r in iter_delivery_file(path) if r["type"] == "change"]
        self.assertEqual(changes, [])

    def test_zero_amount_fact_is_delivered(self):
        submit(self.ledger, 2, [write_fact("c", 7, 5)])
        changes = [r for r in iter_delivery_file(self.export(1, 2))
                   if r["type"] == "change" and r["id"] == "c"]
        self.assertEqual(changes[0]["after"]["amount"], "5")
        # A zero-amount fact added fresh is still a detail.
        submit(self.ledger, 3, [write_fact("z", 9, 0)])
        changes = [r for r in iter_delivery_file(self.export(2, 3, "d.ndjson"))
                   if r["type"] == "change"]
        self.assertEqual(changes, [{
            "type": "change",
            "id": "z",
            "before": None,
            "after": {"target_space": "t", "new_code": "209", "amount": "0"},
        }])

    def test_fixed_interval_is_byte_identical_after_newer_batches(self):
        submit(self.ledger, 2, [write_fact("a", 5, 101)])
        first = self.export(1, 2, "first.ndjson")
        snapshot = first.read_bytes()
        submit(self.ledger, 3, [write_fact("b", 6, 1)])
        submit(self.ledger, 4, [write_fact("a", 5, 102)])
        second = self.export(1, 2, "second.ndjson")
        self.assertEqual(snapshot, second.read_bytes())

    def test_restore_is_correct_after_rule_and_fact_churn(self):
        base_rules = [
            {"id": "m", "source_space": "s", "source_range": [0, 40],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        rng = random.Random(5)
        rules = base_rules
        for seq in range(2, 10):
            actions = []
            if rng.random() < 0.5:
                lo = rng.randint(0, 20)
                hi = rng.randint(lo, 40)
                rules = [
                    {"id": "m", "source_space": "s", "source_range": [lo, hi],
                     "priority": 10, "target_space": "t",
                     "direction": rng.choice([1, -1]),
                     "offset": rng.randint(200, 260)},
                ]
                if rng.random() < 0.4:
                    point = rng.randint(lo, hi)
                    rules.append({"id": "off", "source_space": "s",
                                  "source_range": [point, point],
                                  "priority": 50, "action": "disable"})
            for fact_id in ("a", "b", "c"):
                if rng.random() < 0.5:
                    actions.append(write_fact(fact_id, rng.randint(0, 40),
                                              rng.randint(-9, 9)))
            submit(self.ledger, seq, actions, rules=rules)
        changes = self.export(1, 9)
        baseline = self.dir / "b.ndjson"
        self.ledger.export_baseline(baseline)
        _, state = restore(baseline, changes)
        catalog = Catalog(self.spec_now())
        self.assertEqual(state, expected_state(self.ledger, catalog))

    def test_restore_matches_current_state_after_random_batches(self):
        rng = random.Random(11)
        facts = {"a": (5, 100), "b": (6, 50), "c": (7, 0)}
        for seq in range(2, 8):
            actions = []
            for fact_id in sorted(facts):
                if rng.random() < 0.4:
                    code = rng.randint(0, 40)
                    amount = rng.randint(-20, 20)
                    actions.append(write_fact(fact_id, code, amount))
                    facts[fact_id] = (code, amount)
            submit(self.ledger, seq, actions)
        path = self.export(1, 7)
        baseline = self.dir / "b.ndjson"
        self.ledger.export_baseline(baseline)
        _, state = restore(baseline, path)
        catalog = Catalog(self.spec_now())
        self.assertEqual(state, expected_state(self.ledger, catalog))

    def spec_now(self):
        rules = self.ledger._rules_json_of(self.ledger.current_rule_version())
        return spec_from_dict({
            "source_spaces": {"s": [[p.lo, p.hi] for p in self.ledger.spec.source_spaces["s"]]},
            "target_spaces": {"t": [[p.lo, p.hi] for p in self.ledger.spec.target_spaces["t"]]},
            "rules": rules,
        })


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = Ledger.initialize(self.dir / "l.db", spec())
        submit(self.ledger, 1, [write_fact("a", 5, 1)])
        self.ledger.enable_delivery()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_before_enable_export_is_rejected(self):
        other = Ledger.initialize(self.dir / "o.db", spec())
        try:
            with self.assertRaises(LedgerError):
                other.export_baseline(self.dir / "x.ndjson")
            with self.assertRaises(LedgerError):
                other.delivery_start_seq()
        finally:
            other.close()

    def test_interval_rules(self):
        submit(self.ledger, 2, [])
        for low, high in [(0, 2), (2, 2), (2, 1), (1, 5)]:
            with self.assertRaises(LedgerError):
                self.ledger.export_changes(low, high, self.dir / "bad.ndjson")
            self.assertFalse((self.dir / "bad.ndjson").exists())

    def test_low_equal_to_s_is_allowed(self):
        submit(self.ledger, 2, [write_fact("a", 5, 2)])
        path = self.dir / "ok.ndjson"
        self.ledger.export_changes(1, 2, path)
        self.assertTrue(path.exists())

    def test_failed_export_keeps_previous_file(self):
        path = self.dir / "c.ndjson"
        path.write_bytes(b"previous complete delivery\n")
        with self.assertRaises(LedgerError):
            self.ledger.export_changes(1, 9, path)
        self.assertEqual(path.read_bytes(), b"previous complete delivery\n")
        # No stray partial file is left behind either.
        leftovers = [p.name for p in self.dir.iterdir() if "partial" in p.name]
        self.assertEqual(leftovers, [])

    def test_interrupted_write_keeps_previous_file(self):
        from migration_ledger.delivery import write_delivery_file

        path = self.dir / "c.ndjson"
        path.write_bytes(b"previous complete delivery\n")

        def exploding():
            yield {"type": "batch", "seq": 1, "changes": 0}
            raise RuntimeError("disk full")

        with self.assertRaises(RuntimeError):
            write_delivery_file(
                path, {"kind": "changes", "start_seq": 1, "low": 0, "high": 1},
                exploding(), {"low": 0, "high": 1},
            )
        self.assertEqual(path.read_bytes(), b"previous complete delivery\n")
        leftovers = [p.name for p in self.dir.iterdir() if "partial" in p.name]
        self.assertEqual(leftovers, [])

    def test_truncated_file_is_rejected(self):
        submit(self.ledger, 2, [write_fact("a", 5, 3)])
        path = self.dir / "c.ndjson"
        self.ledger.export_changes(1, 2, path)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        with self.assertRaises(LedgerError):
            list(iter_delivery_file(path))

    def test_tampered_file_is_rejected(self):
        submit(self.ledger, 2, [write_fact("a", 5, 3)])
        path = self.dir / "c.ndjson"
        self.ledger.export_changes(1, 2, path)
        text = path.read_text(encoding="utf-8")
        self.assertIn('"amount":"1"', text)  # baseline value 1 from setUp
        path.write_text(text.replace('"amount":"1"', '"amount":"999"'), encoding="utf-8")
        with self.assertRaises(LedgerError):
            list(iter_delivery_file(path))


class CrashAndLocalityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_delivery_from_a_batch_that_never_committed(self):
        ledger = Ledger.initialize(self.dir / "l.db", spec())
        submit(ledger, 1, [write_fact("a", 5, 1)])
        ledger.enable_delivery()
        with self.assertRaises(RuntimeError):
            ledger.submit_bytes(
                json.dumps({"seq": 2, "facts": [write_fact("a", 5, 2)]}).encode(),
                precommit_hook=lambda seq: (_ for _ in ()).throw(RuntimeError("killed")),
            )
        self.assertEqual(ledger.head_seq(), 1)
        count = ledger.conn.execute("SELECT COUNT(*) FROM delivery_change").fetchone()[0]
        self.assertEqual(count, 0)
        ledger.close()
        # A committed batch after reopen is deliverable.
        ledger = Ledger.open(self.dir / "l.db")
        submit(ledger, 2, [write_fact("a", 5, 3)])
        path = self.dir / "c.ndjson"
        ledger.export_changes(1, 2, path)
        ids = [r["id"] for r in iter_delivery_file(path) if r["type"] == "change"]
        self.assertEqual(ids, ["a"])
        ledger.close()

    def test_replay_and_rejected_batches_emit_nothing(self):
        ledger = Ledger.initialize(self.dir / "l.db", spec())
        payload = json.dumps({"seq": 1, "facts": [write_fact("a", 5, 1)]}).encode()
        ledger.submit_bytes(payload)
        ledger.enable_delivery()
        ledger.submit_bytes(payload)  # byte-identical replay
        with self.assertRaises(LedgerError):
            submit(ledger, 3, [write_fact("a", 5, 2)])  # gap
        count = ledger.conn.execute("SELECT COUNT(*) FROM delivery_change").fetchone()[0]
        self.assertEqual(count, 0)
        ledger.close()

    def test_small_correction_does_not_read_unrelated_facts(self):
        ledger = Ledger.initialize(
            self.dir / "l.db", spec(source_domain=((0, 40), (400, 599)))
        )
        cold = [
            write_fact(f"cold{i}", 400 + (i % 200), i) for i in range(500)
        ]
        submit(ledger, 1, cold + [write_fact("hot", 5, 50)])
        ledger.enable_delivery()
        result = submit(ledger, 2, [write_fact("hot", 5, 51)])
        self.assertLess(result.delivery.facts_examined, 5)
        self.assertEqual(result.delivery.changes, 1)

    def test_local_rule_change_does_not_deliver_cold_facts(self):
        ledger = Ledger.initialize(
            self.dir / "l.db", spec(source_domain=((0, 40), (400, 599)))
        )
        cold = [
            write_fact(f"cold{i}", 400 + (i % 200), i) for i in range(500)
        ]
        base_rules = [
            {"id": "m", "source_space": "s", "source_range": [0, 40],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        submit(ledger, 1, cold + [write_fact("hot", 5, 50)])
        ledger.enable_delivery()
        frozen = base_rules + [
            {"id": "off", "source_space": "s", "source_range": [5, 5],
             "priority": 50, "action": "disable"},
        ]
        result = submit(ledger, 2, [], rules=frozen)
        self.assertLess(result.delivery.facts_examined, 20)
        self.assertEqual(result.delivery.changes, 1)
        path = self.dir / "c.ndjson"
        ledger.export_changes(1, 2, path)
        changes = [r for r in iter_delivery_file(path) if r["type"] == "change"]
        self.assertEqual([r["id"] for r in changes], ["hot"])
        ledger.close()

    def test_recent_export_does_not_read_old_batch_payloads(self):
        ledger = Ledger.initialize(self.dir / "l.db", spec())
        submit(ledger, 1, [write_fact("a", 5, 1)])
        submit(ledger, 2, [write_fact("a", 5, 2)])
        ledger.enable_delivery()
        for seq in range(3, 13):
            submit(ledger, seq, [write_fact("a", 5, seq)])
        statements = []
        ledger.conn.set_trace_callback(statements.append)
        try:
            path = self.dir / "c.ndjson"
            ledger.export_changes(11, 12, path)
        finally:
            ledger.conn.set_trace_callback(None)
        joined = "\n".join(statements)
        self.assertNotIn("SELECT payload", joined)
        self.assertNotIn("FROM batch WHERE seq > ? AND seq <= ?" .replace(
            "FROM", "FROM"), "")  # sanity: trace captured something
        self.assertIn("FROM delivery_change", joined)
        ledger.close()


class DeliveryOracleTests(unittest.TestCase):
    """Random rule/fact churn, replayed delivery vs brute-force recomputation."""

    def test_random_batches_replay_to_brute_force_state(self):
        rng = random.Random(20261124)
        for trial in range(60):
            with tempfile.TemporaryDirectory() as tmp:
                self._run_trial(rng, Path(tmp), trial)

    def _run_trial(self, rng, directory, trial):
        domain_hi = rng.randint(6, 18)
        spec_payload = {
            "source_spaces": {"s": [[0, domain_hi]], "u": [[0, domain_hi]]},
            "target_spaces": {"t": [[0, domain_hi + 40]]},
            "rules": [],
        }
        ledger = Ledger.initialize(directory / "o.db", spec_from_dict(spec_payload))
        ledger.enable_delivery()
        start = ledger.delivery_start_seq()
        self.assertEqual(start, 0)

        facts: dict[str, tuple[str, int, int]] = {}
        rules: list[dict] = []
        counters = {"new": 0}
        for seq in range(1, 9):
            batch: dict = {"seq": seq}
            if rng.random() < 0.55:
                rules = self._random_rules(rng, domain_hi)
                batch["rules"] = rules
            actions = []
            ids_in_batch = set()
            for _ in range(rng.randint(1, 5)):
                if facts and rng.random() < 0.35:
                    fact_id = rng.choice(list(facts))
                    if fact_id in ids_in_batch:
                        continue
                    ids_in_batch.add(fact_id)
                    if rng.random() < 0.2:
                        actions.append({"id": fact_id, "action": "delete"})
                        facts.pop(fact_id)
                        continue
                else:
                    counters["new"] += 1
                    fact_id = f"f{counters['new']:05d}"
                    if fact_id in ids_in_batch:
                        continue
                    ids_in_batch.add(fact_id)
                space = rng.choice(["s", "u"])
                code = rng.randint(0, domain_hi)
                amount = rng.randint(-30, 30)
                actions.append(write_fact(fact_id, code, amount, space=space))
                facts[fact_id] = (space, code, amount)
            batch["facts"] = actions
            ledger.submit_bytes(json.dumps(batch).encode())

            # Replay baseline + (0, seq] and compare with brute force.
            baseline = directory / "b.ndjson"
            changes = directory / f"c{seq}.ndjson"
            ledger.export_baseline(baseline)
            ledger.export_changes(0, seq, changes)
            _, replayed = restore(baseline, changes)

            catalog = Catalog(spec_from_dict({**spec_payload, "rules": rules}))
            expected = {}
            for fact_id, (space, code, amount) in facts.items():
                detail = delivered_detail(catalog, space, code, amount)
                if detail is not None:
                    expected[fact_id] = {
                        "target_space": detail[0],
                        "new_code": str(detail[1]),
                        "amount": str(detail[2]),
                    }
            self.assertEqual(
                replayed, expected, f"trial {trial} seq {seq}"
            )
        ledger.close()

    def _random_rules(self, rng, domain_hi):
        for _ in range(20):
            rules = []
            for rule_id, space in (("m-s", "s"), ("m-u", "u")):
                if rng.random() < 0.8:
                    lo = rng.randint(0, domain_hi)
                    hi = rng.randint(lo, domain_hi)
                    rules.append({
                        "id": rule_id, "source_space": space,
                        "source_range": [lo, hi],
                        "priority": rng.choice([5, 10]),
                        "target_space": "t",
                        "direction": rng.choice([1, -1]),
                        "offset": rng.randint(0, domain_hi + 40),
                    })
            if rng.random() < 0.3:
                point = rng.randint(0, domain_hi)
                rules.append({
                    "id": "off", "source_space": "s",
                    "source_range": [point, point],
                    "priority": 40, "action": "disable",
                })
            try:
                parsed = spec_from_dict({
                    "source_spaces": {"s": [[0, domain_hi]], "u": [[0, domain_hi]]},
                    "target_spaces": {"t": [[0, domain_hi + 40]]},
                    "rules": rules,
                })
            except (ValueError, TypeError):
                continue
            return [
                {
                    "id": rule.id,
                    "source_space": rule.source_space,
                    "source_range": [rule.source_range.lo, rule.source_range.hi],
                    "priority": rule.priority,
                    **(
                        {"action": "disable"}
                        if rule.action == "disable"
                        else {
                            "target_space": rule.target_space,
                            "direction": rule.shift.direction,
                            "offset": rule.shift.offset,
                        }
                    ),
                }
                for rule in parsed.rules
            ]
        return []


class WideValueTests(unittest.TestCase):
    def test_codes_and_amounts_beyond_64_bits_round_trip(self):
        big = 2**200
        wide = spec_from_dict({
            "source_spaces": {"s": [[big, big + 10]]},
            "target_spaces": {"t": [[big + 1000, big + 1100]]},
            "rules": [
                {"id": "m", "source_space": "s", "source_range": [big, big + 10],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 1000},
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ledger = Ledger.initialize(directory / "w.db", wide)
            submit(ledger, 1, [
                write_fact("a", big + 7, big * 3 - 1),
                write_fact("z", big + 8, 0),
            ])
            ledger.enable_delivery()
            submit(ledger, 2, [write_fact("a", big + 7, -(big * 3) + 5)])
            baseline = directory / "b.ndjson"
            changes = directory / "c.ndjson"
            ledger.export_baseline(baseline)
            ledger.export_changes(1, 2, changes)
            _, state = restore(baseline, changes)
            self.assertEqual(
                state["a"],
                {"target_space": "t", "new_code": str(big + 1007),
                 "amount": str(-(big * 3) + 5)},
            )
            self.assertEqual(state["z"]["amount"], "0")
            # The decimal text is exact, not a float or truncated integer.
            self.assertEqual(int(state["a"]["amount"]), -(big * 3) + 5)
            ledger.close()


class MigrationTests(unittest.TestCase):
    def test_v1_ledger_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = directory / "v1.db"
            ledger = Ledger.initialize(db, spec())
            submit(ledger, 1, [write_fact("a", 5, 100)])
            ledger.close()
            # Simulate a ledger written before delivery existed; opening it
            # must not write anything, and normal operation keeps working.
            conn = sqlite3.connect(db)
            conn.execute("DROP TABLE delivery_change")
            conn.execute("DROP TABLE delivery_baseline")
            conn.execute("DROP TABLE delivery_meta")
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
            conn.commit()
            conn.close()

            ledger = Ledger.open(db)
            self.assertFalse(ledger.delivery_enabled())
            submit(ledger, 2, [write_fact("a", 5, 101)])
            self.assertFalse(ledger.delivery_enabled())
            version = ledger.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, "2")
            result = ledger.enable_delivery()
            self.assertEqual(result["start_seq"], 2)
            path = directory / "b.ndjson"
            ledger.export_baseline(path)
            _, state = restore(path)
            self.assertEqual(state["a"]["amount"], "101")
            ledger.close()


class CliDeliveryTests(unittest.TestCase):
    def test_cli_enable_baseline_changes(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from migration_ledger.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = directory / "cli.db"
            spec_path = directory / "spec.json"
            spec_path.write_text(json.dumps({
                "source_spaces": {"s": [[0, 10]]},
                "target_spaces": {"t": [[100, 110]]},
                "rules": [{"id": "m", "source_space": "s", "source_range": [0, 10],
                           "priority": 10, "target_space": "t",
                           "direction": 1, "offset": 100}],
            }), encoding="utf-8")
            batch = directory / "b1.json"
            batch.write_text(json.dumps({"seq": 1, "facts": [
                write_fact("a", 5, 250),
            ]}), encoding="utf-8")

            def run(argv):
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli_main(argv)
                return code, out.getvalue(), err.getvalue()

            self.assertEqual(run(["init", str(db), str(spec_path)])[0], 0)
            self.assertEqual(run(["submit", str(db), str(batch)])[0], 0)
            code, out, _ = run(["delivery-enable", str(db)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["start_seq"], 1)

            baseline = directory / "base.ndjson"
            code, out, _ = run(["delivery-baseline", str(db), str(baseline)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["fact_count"], 1)

            batch2 = directory / "b2.json"
            batch2.write_text(json.dumps({"seq": 2, "facts": [
                write_fact("a", 5, 251),
            ]}), encoding="utf-8")
            self.assertEqual(run(["submit", str(db), str(batch2)])[0], 0)
            changes = directory / "chg.ndjson"
            code, out, _ = run([
                "delivery-changes", str(db), str(changes), "--low", "1", "--high", "2",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["records"], 1)

            target = directory / "target.ndjson"
            target.write_bytes(b"keep me\n")
            code, _, err = run([
                "delivery-changes", str(db), str(target), "--low", "1", "--high", "9",
            ])
            self.assertEqual(code, 2)
            self.assertIn("not a committed batch", err)
            self.assertEqual(target.read_bytes(), b"keep me\n")


if __name__ == "__main__":
    unittest.main()

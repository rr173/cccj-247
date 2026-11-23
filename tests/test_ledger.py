"""Tests for the SQLite migration ledger.

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
sys.path.insert(0, str(ROOT / "tests"))

import reference  # noqa: E402  (the brute-force catalogue oracle)
from migration_catalog import Catalog, spec_from_dict  # noqa: E402
from migration_ledger import Ledger, LedgerError  # noqa: E402
from migration_ledger.keys import decode_key, encode_key  # noqa: E402


def simple_spec(rules=None, source_domain=((0, 40),), target_domain=((200, 260),)):
    payload = {
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
    return spec_from_dict(payload)


class KeyTests(unittest.TestCase):
    def test_round_trip_and_order(self):
        values = [
            -(10**100), -100, -11, -10, -9, -1, 0, 1, 9, 10, 11, 99, 100,
            2**200, 10**100,
        ]
        keys = [encode_key(v) for v in values]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([decode_key(k) for k in keys], values)

    def test_random_ordering(self):
        rng = random.Random(7)
        values = [rng.randint(-(10**60), 10**60) for _ in range(500)]
        rebuilt = [decode_key(k) for k in sorted(encode_key(v) for v in values)]
        self.assertEqual(rebuilt, sorted(values))

    def test_bool_rejected(self):
        with self.assertRaises(TypeError):
            encode_key(True)


class LedgerCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = Ledger.initialize(self.db, simple_spec())

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def submit(self, seq, facts=(), rules=None):
        payload: dict = {"seq": seq}
        if rules is not None:
            payload["rules"] = rules
        if facts:
            payload["facts"] = facts
        return self.ledger.submit_bytes(json.dumps(payload).encode())

    def test_empty_ledger_has_no_balances_and_zero_head(self):
        self.assertEqual(self.ledger.head_seq(), 0)
        self.assertEqual(self.ledger.list_balances(), [])
        self.assertIsNone(self.ledger.get_balance("t", 205))

    def test_basic_balance_and_head_seq(self):
        self.submit(1, [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
            {"id": "b", "old_space": "s", "old_code": 6, "amount": -25},
        ])
        self.assertEqual(self.ledger.get_balance("t", 205), {
            "target_space": "t", "new_code": 205, "fact_count": 1,
            "amount_sum": 100, "updated_seq": 1, "head_seq": 1,
        })
        self.assertEqual(self.ledger.get_balance("t", 206)["amount_sum"], -25)

    def test_multiple_facts_same_code_count_and_exact_sum(self):
        self.submit(1, [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
            {"id": "b", "old_space": "s", "old_code": 5, "amount": -100},
            {"id": "c", "old_space": "s", "old_code": 5, "amount": 0},
        ])
        row = self.ledger.get_balance("t", 205)
        self.assertEqual(row["fact_count"], 3)
        self.assertEqual(row["amount_sum"], 0)

    def test_amount_correction_uses_latest_value(self):
        self.submit(1, [{"id": "a", "old_space": "s", "old_code": 5, "amount": 100}])
        self.submit(2, [{"id": "a", "old_space": "s", "old_code": 5, "amount": 30}])
        self.assertEqual(self.ledger.get_balance("t", 205)["amount_sum"], 30)
        self.assertEqual(self.ledger.get_fact("a")["updated_seq"], 2)

    def test_move_between_old_codes_recomputes_both_rows(self):
        self.submit(1, [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
            {"id": "b", "old_space": "s", "old_code": 6, "amount": 7},
        ])
        self.submit(2, [{"id": "a", "old_space": "s", "old_code": 6, "amount": 100}])
        self.assertIsNone(self.ledger.get_balance("t", 205))
        row = self.ledger.get_balance("t", 206)
        self.assertEqual((row["fact_count"], row["amount_sum"]), (2, 107))

    def test_delete_removes_fact_and_balance(self):
        self.submit(1, [{"id": "a", "old_space": "s", "old_code": 5, "amount": 100}])
        self.submit(2, [{"id": "a", "action": "delete"}])
        self.assertIsNone(self.ledger.get_balance("t", 205))
        self.assertIsNone(self.ledger.get_fact("a"))
        history = self.ledger.conn.execute(
            "SELECT action FROM fact_history WHERE fact_id = 'a' ORDER BY seq"
        ).fetchall()
        self.assertEqual([row[0] for row in history], ["write", "delete"])

    def test_delete_unknown_is_input_error(self):
        with self.assertRaises(LedgerError):
            self.submit(1, [{"id": "ghost", "action": "delete"}])
        self.assertEqual(self.ledger.head_seq(), 0)

    def test_duplicate_id_in_batch_rejected(self):
        with self.assertRaises(LedgerError):
            self.submit(1, [
                {"id": "a", "old_space": "s", "old_code": 5, "amount": 1},
                {"id": "a", "old_space": "s", "old_code": 6, "amount": 1},
            ])

    def test_out_of_domain_fact_rejected(self):
        with self.assertRaises(LedgerError):
            self.submit(1, [{"id": "a", "old_space": "s", "old_code": 99, "amount": 1}])
        with self.assertRaises(LedgerError):
            self.ledger.get_balance("t", 999)


class AmbiguityAndBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        two_sources = {
            "source_spaces": {"s": [[0, 20]], "u": [[0, 20]]},
            "target_spaces": {"t": [[0, 400]]},
            "rules": [
                {"id": "s-map", "source_space": "s", "source_range": [0, 20],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
            ],
        }
        self.base = two_sources
        self.ledger = Ledger.initialize(self.db, spec_from_dict(two_sources))

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_uniqueness_is_declared_not_booked(self):
        # No fact in u yet; s:5 books uniquely.
        self.ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
        ]}).encode())
        self.assertEqual(self.ledger.get_balance("t", 205)["amount_sum"], 100)
        # A rule in u starts competing; the booked row must disappear even
        # though u still has no facts.
        rules = self.base["rules"] + [
            {"id": "u-map", "source_space": "u", "source_range": [0, 20],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        self.ledger.submit_bytes(json.dumps({"seq": 2, "rules": rules, "facts": []}).encode())
        self.assertIsNone(self.ledger.get_balance("t", 205))
        # Facts remain stored; corrections during the blocked window are kept.
        self.ledger.submit_bytes(json.dumps({"seq": 3, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 333},
        ]}).encode())
        # Competition clears; corrected fact is incorporated.
        self.ledger.submit_bytes(json.dumps({"seq": 4, "rules": self.base["rules"],
                                             "facts": []}).encode())
        self.assertEqual(self.ledger.get_balance("t", 205)["amount_sum"], 333)

    def test_blocking_in_other_segment_does_not_spread(self):
        rules = self.base["rules"] + [
            # Only competes on t 200..209 (u 0..9 -> t 200..209).
            {"id": "u-part", "source_space": "u", "source_range": [0, 9],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 200},
        ]
        self.ledger.submit_bytes(json.dumps({"seq": 1, "rules": rules, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
            {"id": "b", "old_space": "s", "old_code": 15, "amount": 200},
        ]}).encode())
        self.assertIsNone(self.ledger.get_balance("t", 205))
        self.assertEqual(self.ledger.get_balance("t", 215)["amount_sum"], 200)

    def test_disabled_and_missing_positions_never_book(self):
        spec = {
            "source_spaces": {"s": [[0, 10]]},
            "target_spaces": {"t": [[100, 110]]},
            "rules": [
                {"id": "m", "source_space": "s", "source_range": [0, 4],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 100},
                {"id": "off", "source_space": "s", "source_range": [5, 6],
                 "priority": 20, "action": "disable"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.db"
            ledger = Ledger.initialize(path, spec_from_dict(spec))
            ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
                {"id": "ok", "old_space": "s", "old_code": 1, "amount": 5},
                {"id": "disabled", "old_space": "s", "old_code": 5, "amount": 5},
                {"id": "missing", "old_space": "s", "old_code": 8, "amount": 5},
            ]}).encode())
            self.assertEqual(ledger.get_balance("t", 101)["amount_sum"], 5)
            self.assertIsNone(ledger.get_balance("t", 105))
            self.assertIsNone(ledger.get_balance("t", 108))
            self.assertIsNotNone(ledger.get_fact("disabled"))
            ledger.close()

    def test_stale_unique_rows_removed_when_preimage_identity_shifts(self):
        """Regression: contiguous unique target segments whose per-code preimage
        changes across rule versions must not leave stale balance rows."""

        base = {
            "source_spaces": {"s": [[0, 20]]},
            "target_spaces": {"t": [[0, 70]]},
        }
        old_rules = [
            {"id": "m", "source_space": "s", "source_range": [2, 12],
             "priority": 10, "target_space": "t", "direction": -1, "offset": 31},
        ]
        new_rules = [
            {"id": "m", "source_space": "s", "source_range": [11, 13],
             "priority": 10, "target_space": "t", "direction": 1, "offset": 9},
            {"id": "rival", "source_space": "s", "source_range": [10, 12],
             "priority": 5, "target_space": "t", "direction": 1, "offset": 6},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger.initialize(
                Path(tmp) / "r.db", spec_from_dict({**base, "rules": old_rules})
            )
            # s:10 -> t:21 and s:9 -> t:22 under the old descending rule.
            ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
                {"id": "a", "old_space": "s", "old_code": 10, "amount": 36},
                {"id": "b", "old_space": "s", "old_code": 9, "amount": -49},
            ]}).encode())
            self.assertEqual(ledger.get_balance("t", 21)["amount_sum"], 36)
            self.assertEqual(ledger.get_balance("t", 22)["amount_sum"], -49)
            ledger.submit_bytes(json.dumps(
                {"seq": 2, "rules": new_rules, "facts": []}
            ).encode())
            # Under the new rules s:9/s:10 have no valid mapping; stale rows go.
            self.assertIsNone(ledger.get_balance("t", 21))
            self.assertIsNone(ledger.get_balance("t", 22))
            # Facts themselves remain stored for later reconsideration.
            self.assertEqual(ledger.get_fact("a")["old_code"], 10)
            ledger.close()

    def test_disable_then_reenable_locally_removes_and_restores_row(self):
        rules = self.base["rules"]
        with_disable = rules + [
            {"id": "freeze", "source_space": "s", "source_range": [5, 5],
             "priority": 50, "action": "disable"},
        ]
        self.ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
        ]}).encode())
        self.ledger.submit_bytes(json.dumps({"seq": 2, "rules": with_disable,
                                             "facts": []}).encode())
        self.assertIsNone(self.ledger.get_balance("t", 205))
        self.ledger.submit_bytes(json.dumps({"seq": 3, "rules": rules,
                                             "facts": []}).encode())
        self.assertEqual(self.ledger.get_balance("t", 205)["amount_sum"], 100)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = Ledger.initialize(self.db, simple_spec())

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_byte_identical_replay_is_idempotent(self):
        payload = json.dumps({"seq": 1, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 1},
        ]}).encode()
        first = self.ledger.submit_bytes(payload)
        second = self.ledger.submit_bytes(payload)
        self.assertEqual(first.status, "committed")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(self.ledger.head_seq(), 1)
        self.assertEqual(self.ledger.list_committed_seqs(), [1])

    def test_same_seq_different_content_rejected(self):
        self.ledger.submit_bytes(json.dumps({"seq": 1, "facts": []}).encode())
        with self.assertRaises(LedgerError):
            self.ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
                {"id": "a", "old_space": "s", "old_code": 5, "amount": 1},
            ]}).encode())

    def test_gap_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.submit_bytes(json.dumps({"seq": 2, "facts": []}).encode())

    def test_invalid_json_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.submit_bytes(b"{not json")

    def test_rules_omitted_inherit_previous_set(self):
        rules = self.base_rules()
        self.ledger.submit_bytes(json.dumps({"seq": 1, "rules": rules, "facts": []}).encode())
        self.assertEqual(self.ledger.current_rule_version(), 1)
        self.ledger.submit_bytes(json.dumps({"seq": 2, "facts": []}).encode())
        self.assertEqual(self.ledger.current_rule_version(), 1)
        self.assertEqual(self.ledger.head_seq(), 2)

    def test_empty_rules_list_is_explicit_clear_not_inherit(self):
        self.ledger.submit_bytes(json.dumps({"seq": 1, "rules": [], "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 1},
        ]}).encode())
        self.assertEqual(self.ledger.current_rule_version(), 1)
        self.assertEqual(self.ledger.list_balances(), [])
        self.assertIsNotNone(self.ledger.get_fact("a"))

    def test_reopen_sees_committed_state(self):
        self.ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
            {"id": "a", "old_space": "s", "old_code": 5, "amount": 100},
        ]}).encode())
        before = self.ledger.list_balances()
        self.ledger.close()
        reopened = Ledger.open(self.db)
        self.assertEqual(reopened.head_seq(), 1)
        self.assertEqual(reopened.list_balances(), before)
        reopened.close()

    @staticmethod
    def base_rules():
        return [
            {"id": "m", "source_space": "s", "source_range": [0, 40], "priority": 10,
             "target_space": "t", "direction": 1, "offset": 200},
        ]


class WideIntegerTests(unittest.TestCase):
    def test_codes_and_amounts_beyond_64_bits(self):
        big = 2**200
        spec = spec_from_dict({
            "source_spaces": {"s": [[big, big + 100]]},
            "target_spaces": {"t": [[big + 1000, big + 1100]]},
            "rules": [
                {"id": "m", "source_space": "s", "source_range": [big, big + 100],
                 "priority": 10, "target_space": "t", "direction": 1, "offset": 1000},
            ],
        })
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger.initialize(Path(tmp) / "w.db", spec)
            ledger.submit_bytes(json.dumps({"seq": 1, "facts": [
                {"id": "a", "old_space": "s", "old_code": big + 7, "amount": big * 3 - 1},
                {"id": "b", "old_space": "s", "old_code": big + 7, "amount": -(big * 3) + 5},
            ]}).encode())
            row = ledger.get_balance("t", big + 1007)
            self.assertEqual(row["fact_count"], 2)
            self.assertEqual(row["amount_sum"], 4)
            ledger.close()


class LocalRefreshTests(unittest.TestCase):
    def test_cold_facts_outside_affected_ranges_are_not_scanned(self):
        spec = simple_spec(source_domain=((0, 20), (400, 599)))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger.initialize(Path(tmp) / "c.db", spec)
            # 200 cold facts on 400..599 where no rule maps anything.
            cold = [
                {"id": f"c{i}", "old_space": "s", "old_code": 400 + (i % 200),
                 "amount": i}
                for i in range(200)
            ]
            ledger.submit_bytes(json.dumps({"seq": 1, "facts": cold + [
                {"id": "hot", "old_space": "s", "old_code": 5, "amount": 50},
            ]}).encode())
            disable = [
                {"id": "m", "source_space": "s", "source_range": [0, 40], "priority": 10,
                 "target_space": "t", "direction": 1, "offset": 200},
                {"id": "freeze", "source_space": "s", "source_range": [5, 5],
                 "priority": 50, "action": "disable"},
            ]
            stats = ledger.submit_bytes(
                json.dumps({"seq": 2, "rules": disable, "facts": []}).encode()
            ).refresh
            self.assertLess(stats.fact_rows_touched, 20)
            self.assertIsNone(ledger.get_balance("t", 205))
            # The refresh SQL never uses a full scan of the fact table.
            plan = "\n".join(
                row[3] for row in ledger.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT DISTINCT old_code FROM fact "
                    "WHERE old_space = ? AND code_key BETWEEN ? AND ?",
                    ("s", encode_key(400), encode_key(599)),
                )
            )
            self.assertIn("USING INDEX fact_position_idx", plan)
            self.assertNotIn("SCAN fact", plan)
            ledger.close()


class OracleCrossCheckTests(unittest.TestCase):
    """Cross-check ledger balances against brute-force catalogue enumeration."""

    def test_random_batches_match_brute_force(self):
        rng = random.Random(20261123)
        for trial in range(150):
            with tempfile.TemporaryDirectory() as tmp:
                self._run_trial(rng, Path(tmp) / "o.db", trial)

    def _run_trial(self, rng, path, trial):
        domain_hi = rng.randint(8, 20)
        target_hi = domain_hi + 25
        spec = spec_from_dict({
            "source_spaces": {"s": [[0, domain_hi]]},
            "target_spaces": {"t": [[0, target_hi + 50]]},
            "rules": [],
        })
        ledger = Ledger.initialize(path, spec)
        facts: dict[str, tuple[int, int]] = {}
        rules: list[dict] = []
        seq = 0
        new_id_counter = 0
        for step in range(8):
            batch: dict = {"seq": seq + 1}
            if rng.random() < 0.45:
                rules = self._random_rules(rng, domain_hi, target_hi)
                batch["rules"] = rules
            fact_actions = []
            batch_ids: set[str] = set()
            for _ in range(rng.randint(1, 5)):
                if facts and rng.random() < 0.3:
                    fact_id = rng.choice(list(facts))
                    if fact_id in batch_ids:
                        continue
                    batch_ids.add(fact_id)
                    if rng.random() < 0.2:
                        fact_actions.append({"id": fact_id, "action": "delete"})
                        facts.pop(fact_id)
                    else:
                        code = rng.randint(0, domain_hi)
                        amount = rng.randint(-50, 50)
                        fact_actions.append(
                            {"id": fact_id, "old_space": "s",
                             "old_code": code, "amount": amount}
                        )
                        facts[fact_id] = (code, amount)
                else:
                    new_id_counter += 1
                    fact_id = f"f{new_id_counter:05d}"
                    batch_ids.add(fact_id)
                    code = rng.randint(0, domain_hi)
                    amount = rng.randint(-50, 50)
                    fact_actions.append(
                        {"id": fact_id, "old_space": "s",
                         "old_code": code, "amount": amount}
                    )
                    facts[fact_id] = (code, amount)
            batch["facts"] = fact_actions
            ledger.submit_bytes(json.dumps(batch).encode())
            seq += 1
            self._assert_matches_oracle(ledger, rules, facts, target_hi, trial, seq)
        ledger.close()

    def _assert_matches_oracle(self, ledger, rules, facts, target_hi, trial, seq):
        effective_spec = spec_from_dict({
            "source_spaces": {
                "s": [[p.lo, p.hi] for p in ledger.spec.source_spaces["s"]]
            },
            "target_spaces": {
                "t": [[p.lo, p.hi] for p in ledger.spec.target_spaces["t"]]
            },
            "rules": rules,
        })
        catalog = Catalog(effective_spec)
        expected: dict[int, tuple[int, int]] = {}
        # Aggregate current facts by their mapped target.
        contributions: dict[int, list[int]] = {}
        for fact_id, (code, amount) in facts.items():
            hit = catalog.source_lookup("s", code)
            if hit.status != "mapped":
                continue
            contributions.setdefault(hit.target, []).append(amount)
        for new_code, amounts in contributions.items():
            reverse = catalog.target_lookup("t", new_code)
            if reverse.status == "unique":
                expected[new_code] = (len(amounts), sum(amounts))
        actual = {
            row["new_code"]: (row["fact_count"], row["amount_sum"])
            for row in ledger.list_balances()
        }
        self.assertEqual(actual, expected, f"trial {trial} seq {seq}")

    def _random_rules(self, rng, domain_hi, target_hi):
        base_spec = {
            "source_spaces": {"s": [[0, domain_hi]]},
            "target_spaces": {"t": [[0, target_hi + 50]]},
        }
        for _ in range(20):
            lo = rng.randint(0, domain_hi)
            hi = rng.randint(lo, domain_hi)
            rules = [{
                "id": "m", "source_space": "s", "source_range": [lo, hi],
                "priority": 10, "target_space": "t",
                "direction": rng.choice([1, -1]), "offset": rng.randint(0, target_hi),
            }]
            if rng.random() < 0.4:
                r_lo = rng.randint(0, domain_hi)
                r_hi = rng.randint(r_lo, domain_hi)
                rules.append({
                    "id": "rival", "source_space": "s",
                    "source_range": [r_lo, r_hi],
                    "priority": rng.choice([5, 20]),
                    "target_space": "t", "direction": 1,
                    "offset": rng.randint(0, target_hi),
                })
            if rng.random() < 0.2:
                point = rng.randint(0, domain_hi)
                rules.append({
                    "id": "off", "source_space": "s",
                    "source_range": [point, point],
                    "priority": 40, "action": "disable",
                })
            try:
                parsed = spec_from_dict({**base_spec, "rules": rules})
                return [
                    {
                        "id": r.id,
                        "source_space": r.source_space,
                        "source_range": [r.source_range.lo, r.source_range.hi],
                        "priority": r.priority,
                        **(
                            {"action": "disable"}
                            if r.action == "disable"
                            else {
                                "target_space": r.target_space,
                                "direction": r.shift.direction,
                                "offset": r.shift.offset,
                            }
                        ),
                    }
                    for r in parsed.rules
                ]
            except (ValueError, TypeError):
                continue
        return []


class CliTests(unittest.TestCase):
    def test_cli_init_submit_balance_head(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from migration_ledger.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cli.db"
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps({
                "source_spaces": {"s": [[0, 10]]},
                "target_spaces": {"t": [[100, 110]]},
                "rules": [
                    {"id": "m", "source_space": "s", "source_range": [0, 10],
                     "priority": 10, "target_space": "t",
                     "direction": 1, "offset": 100},
                ],
            }), encoding="utf-8")
            batch_path = Path(tmp) / "b1.json"
            batch_path.write_text(json.dumps({"seq": 1, "facts": [
                {"id": "a", "old_space": "s", "old_code": 5, "amount": 250},
            ]}), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["init", str(db), str(spec_path)])
            self.assertEqual(code, 0)

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["submit", str(db), str(batch_path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["status"], "committed")

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["balance", str(db), "--space", "t", "--code", "105"])
            row = json.loads(out.getvalue())
            self.assertEqual((row["fact_count"], row["amount_sum"]), (1, 250))

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(["head", str(db)])
            self.assertEqual(json.loads(out.getvalue())["head_seq"], 1)

            # Invalid batch exits 2 and leaves state intact.
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"seq": 9, "facts": []}), encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli_main(["submit", str(db), str(bad)])
            self.assertEqual(code, 2)
            self.assertIn("expected next batch seq", err.getvalue())


if __name__ == "__main__":
    unittest.main()

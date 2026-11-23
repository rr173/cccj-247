"""Tests for the rekey rollout planner (migration_plan).

Run with ``python3 -m unittest discover -s tests -v`` (pytest also works).
"""

from __future__ import annotations

import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_catalog import Catalog, spec_from_dict  # noqa: E402
from migration_plan import PlanError, Planner  # noqa: E402
from migration_plan.cli import main as cli_main  # noqa: E402


def catalog(payload) -> Catalog:
    return Catalog(spec_from_dict(payload))


def simple_catalog() -> Catalog:
    """Every old code s:c maps to t:(c+1000), always reverse-unique."""

    return catalog(
        {
            "source_spaces": {"s": [[0, 999]]},
            "target_spaces": {"t": [[1000, 1999]]},
            "rules": [
                {
                    "id": "shift",
                    "source_space": "s",
                    "source_range": [0, 999],
                    "priority": 1,
                    "target_space": "t",
                    "direction": 1,
                    "offset": 1000,
                }
            ],
        }
    )


def row(record_id, cur, unit=None, old=None):
    """Build one manifest line; cur/old are (space, code) tuples."""

    data = {"id": record_id, "current": {"space": cur[0], "code": str(cur[1])}}
    if unit is not None:
        data["unit"] = unit
        data["old"] = {"space": old[0], "code": str(old[1])}
    return data


def run_plan(cat, lines, k=1):
    """Plan over the given manifest lines (dicts or raw strings)."""

    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.jsonl"
        with manifest.open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line if isinstance(line, str) else json.dumps(line))
                handle.write("\n")
        with Planner(cat, k, work_dir=tmp) as planner:
            planner.load(str(manifest))
            planner.plan()
            out = io.StringIO()
            planner.emit(out)
        return json.loads(out.getvalue())


def plan_error(cat, lines, k=1):
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.jsonl"
        with manifest.open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line if isinstance(line, str) else json.dumps(line))
                handle.write("\n")
        with Planner(cat, k, work_dir=tmp) as planner:
            try:
                planner.load(str(manifest))
            except PlanError as exc:
                return str(exc)
    raise AssertionError("expected PlanError")


def planned_units(plan):
    return [entry["unit"] for batch in plan["batches"] for entry in batch["units"]]


def blocked_units(plan):
    return {entry["unit"]: entry["reasons"] for entry in plan["blocked_units"]}


def reason_kinds(reasons):
    return {reason["kind"] for reason in reasons}


class ManifestErrorTests(unittest.TestCase):
    def test_invalid_json(self):
        message = plan_error(simple_catalog(), ["{not json"])
        self.assertIn("line 1", message)

    def test_blank_line(self):
        message = plan_error(simple_catalog(), [json.dumps(row("A", ("t", "1000"))), "  "])
        self.assertIn("line 2", message)
        self.assertIn("blank", message)

    def test_non_object_line(self):
        message = plan_error(simple_catalog(), ["[1, 2]"])
        self.assertIn("object", message)

    def test_unknown_field(self):
        data = row("A", ("t", "1000"))
        data["oops"] = 1
        message = plan_error(simple_catalog(), [data])
        self.assertIn("oops", message)

    def test_missing_current(self):
        message = plan_error(simple_catalog(), [{"id": "A"}])
        self.assertIn("current", message)

    def test_unit_without_old(self):
        message = plan_error(
            simple_catalog(), [{"id": "A", "current": {"space": "t", "code": "1"}, "unit": "u"}]
        )
        self.assertIn("together", message)

    def test_old_without_unit(self):
        message = plan_error(
            simple_catalog(),
            [{"id": "A", "current": {"space": "t", "code": "1"}, "old": {"space": "s", "code": "1"}}],
        )
        self.assertIn("together", message)

    def test_code_must_be_decimal_string(self):
        for bad in (1000, "12.5", "abc", "1_000", "", "-", " 12"):
            data = {"id": "A", "current": {"space": "t", "code": bad}}
            message = plan_error(simple_catalog(), [data])
            self.assertIn("decimal string", message, bad)

    def test_duplicate_id(self):
        lines = [row("A", ("t", "1000")), row("A", ("t", "1001"))]
        message = plan_error(simple_catalog(), lines)
        self.assertIn("duplicate record id", message)
        self.assertIn("line 2", message)
        self.assertIn("line 1", message)

    def test_duplicate_current_key(self):
        lines = [row("A", ("t", "1000")), row("B", ("t", "1000"))]
        message = plan_error(simple_catalog(), lines)
        self.assertIn("duplicate current key", message)
        self.assertIn("'A'", message)

    def test_duplicate_current_key_after_canonicalisation(self):
        lines = [row("A", ("t", "1000")), row("B", ("t", "01000"))]
        self.assertIn("duplicate current key", plan_error(simple_catalog(), lines))
        lines = [row("A", ("t", "-0")), row("B", ("t", "0"))]
        self.assertIn("duplicate current key", plan_error(simple_catalog(), lines))

    def test_k_must_be_positive(self):
        cat = simple_catalog()
        for bad in (0, -2, True):
            with self.assertRaises(PlanError):
                Planner(cat, bad)

    def test_empty_manifest_plans_empty(self):
        plan = run_plan(simple_catalog(), [], k=1)
        self.assertEqual(plan["batches"], [])
        self.assertEqual(plan["blocked_units"], [])
        self.assertEqual(plan["summary"]["records"], 0)


class ResolutionTests(unittest.TestCase):
    """Per-row target resolution against the candidate catalogue."""

    def setUp(self):
        self.cat = catalog(
            {
                "source_spaces": {"s": [[0, 99]], "s2": [[0, 9]], "neg": [[-5, -1]]},
                "target_spaces": {"t": [[1000, 1099]], "tn": [[-1005, -1001]]},
                "rules": [
                    {  # covers 0..89; 90..98 stay missing; 99 disabled below
                        "id": "main",
                        "source_space": "s",
                        "source_range": [0, 89],
                        "priority": 10,
                        "target_space": "t",
                        "direction": 1,
                        "offset": 1000,
                    },
                    {  # alias makes t:1005 ambiguous (s:5 and s2:5)
                        "id": "alias",
                        "source_space": "s2",
                        "source_range": [5, 5],
                        "priority": 10,
                        "target_space": "t",
                        "direction": 1,
                        "offset": 1000,
                    },
                    {
                        "id": "retire",
                        "source_space": "s",
                        "source_range": [99, 99],
                        "priority": 20,
                        "action": "disable",
                    },
                    {
                        "id": "neg",
                        "source_space": "neg",
                        "source_range": [-5, -1],
                        "priority": 10,
                        "target_space": "tn",
                        "direction": 1,
                        "offset": -1000,
                    },
                ],
            }
        )

    def test_missing_old_code_blocks_unit(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1"), "u1", ("s", "95"))])
        reasons = blocked_units(plan)["u1"]
        self.assertEqual(reasons[0]["kind"], "old_code_unmapped")
        self.assertEqual(reasons[0]["catalog_status"], "missing")
        self.assertEqual(reasons[0]["old"], {"space": "s", "code": "95"})

    def test_disabled_old_code_blocks_unit(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1"), "u1", ("s", "99"))])
        reasons = blocked_units(plan)["u1"]
        self.assertEqual(reasons[0]["catalog_status"], "disabled")

    def test_out_of_domain_old_code_blocks_unit(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1"), "u1", ("s", "500"))])
        reasons = blocked_units(plan)["u1"]
        self.assertEqual(reasons[0]["catalog_status"], "out_of_domain")

    def test_unknown_source_space_is_business_block_not_manifest_error(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1"), "u1", ("nowhere", "1"))])
        reasons = blocked_units(plan)["u1"]
        self.assertEqual(reasons[0]["catalog_status"], "unknown_source_space")

    def test_ambiguous_target_blocks_unit_with_witness(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1"), "u1", ("s2", "5"))])
        reasons = blocked_units(plan)["u1"]
        self.assertEqual(reasons[0]["kind"], "target_not_unique")
        self.assertEqual(reasons[0]["target"], {"space": "t", "code": "1005"})
        self.assertEqual(reasons[0]["reverse_status"], "ambiguous")
        self.assertEqual(len(reasons[0]["witness"]), 2)

    def test_one_bad_row_blocks_whole_unit(self):
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "1")),  # fine
            row("R2", ("t", "2"), "u1", ("s", "95")),  # missing
        ]
        plan = run_plan(self.cat, lines)
        self.assertIn("u1", blocked_units(plan))
        self.assertEqual(planned_units(plan), [])

    def test_kept_row_stays_and_unit_publishes(self):
        plan = run_plan(self.cat, [row("R1", ("t", "1001"), "u1", ("s", "1"))])
        self.assertEqual(planned_units(plan), ["u1"])
        entry = plan["batches"][0]["units"][0]
        self.assertEqual(entry["kept"], 1)
        self.assertEqual(entry["moves"], [])

    def test_negative_and_wide_codes_round_trip_exactly(self):
        wide = 10**30 + 7
        cat = catalog(
            {
                "source_spaces": {
                    "w": [[10**30, 10**30 + 9]],
                    "neg": [[-5, -1]],
                },
                "target_spaces": {
                    "x": [[10**40, 10**40 + 9]],
                    "tn": [[-1005, -1001]],
                },
                "rules": [
                    {
                        "id": "wide",
                        "source_space": "w",
                        "source_range": [10**30, 10**30 + 9],
                        "priority": 1,
                        "target_space": "x",
                        "direction": 1,
                        "offset": 10**40 - 10**30,
                    },
                    {
                        "id": "neg",
                        "source_space": "neg",
                        "source_range": [-5, -1],
                        "priority": 1,
                        "target_space": "tn",
                        "direction": 1,
                        "offset": -1000,
                    },
                ],
            }
        )
        lines = [
            row("NEG", ("t", "5"), "u1", ("neg", "-3")),
            row("WIDE", ("t", "6"), "u2", ("w", str(wide))),
        ]
        plan = run_plan(cat, lines)
        moves = {
            move["id"]: move
            for batch in plan["batches"]
            for entry in batch["units"]
            for move in entry["moves"]
        }
        self.assertEqual(moves["NEG"]["target"], {"space": "tn", "code": "-1003"})
        self.assertEqual(
            moves["WIDE"]["target"], {"space": "x", "code": str(10**40 + 7)}
        )


class ContentionTests(unittest.TestCase):
    def test_two_units_same_target_both_exit(self):
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "10")),
            row("R2", ("t", "2"), "u2", ("s", "10")),
        ]
        plan = run_plan(simple_catalog(), lines)
        blocked = blocked_units(plan)
        self.assertEqual(set(blocked), {"u1", "u2"})
        for reasons in blocked.values():
            (reason,) = reasons
            self.assertEqual(reason["kind"], "target_contention")
            self.assertEqual(reason["target"], {"space": "t", "code": "1010"})
            self.assertEqual(
                reason["applicants"],
                [{"id": "R1", "unit": "u1"}, {"id": "R2", "unit": "u2"}],
            )

    def test_same_unit_two_rows_same_target_exits(self):
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "10")),
            row("R2", ("t", "2"), "u1", ("s", "10")),
        ]
        plan = run_plan(simple_catalog(), lines)
        self.assertIn("u1", blocked_units(plan))

    def test_contention_not_reallocated_after_other_block(self):
        # u1's row is also a business block (old code out of domain would
        # block before a target exists, so use a second row); the contested
        # key must still not go to u2.
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "10")),  # claims t:1010
            row("R1B", ("t", "3"), "u1", ("s", "500")),  # out of domain -> u1 blocked
            row("R2", ("t", "2"), "u2", ("s", "10")),  # also claims t:1010
        ]
        plan = run_plan(simple_catalog(), lines)
        blocked = blocked_units(plan)
        self.assertEqual(set(blocked), {"u1", "u2"})
        self.assertIn("target_contention", reason_kinds(blocked["u2"]))


class OccupancyTests(unittest.TestCase):
    def setUp(self):
        self.cat = simple_catalog()

    def test_placeholder_pins_key(self):
        lines = [
            row("KEEP", ("t", "1010")),
            row("R1", ("t", "1"), "u1", ("s", "10")),
        ]
        plan = run_plan(self.cat, lines)
        (reason,) = blocked_units(plan)["u1"]
        self.assertEqual(reason["kind"], "target_occupied")
        self.assertEqual(reason["occupied_by"], {"id": "KEEP", "kind": "placeholder"})

    def test_kept_row_pins_key_for_other_unit(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "10")),  # u1 keeps t:1010
            row("R2", ("t", "1"), "u2", ("s", "10")),  # u2 wants t:1010
        ]
        plan = run_plan(self.cat, lines)
        self.assertEqual(planned_units(plan), ["u1"])
        (reason,) = blocked_units(plan)["u2"]
        self.assertEqual(reason["kind"], "target_occupied")
        self.assertEqual(reason["occupied_by"]["kind"], "retained")
        self.assertEqual(reason["occupied_by"]["unit"], "u1")

    def test_kept_row_pins_key_inside_same_unit(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "10")),  # kept
            row("R2", ("t", "1"), "u1", ("s", "10")),  # wants the kept key
        ]
        plan = run_plan(self.cat, lines)
        self.assertIn("u1", blocked_units(plan))

    def test_blocked_unit_does_not_free_key(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "1500")),  # u1 blocked (out of domain)
            row("R2", ("t", "1"), "u2", ("s", "10")),  # u2 wants t:1010
        ]
        plan = run_plan(self.cat, lines)
        blocked = blocked_units(plan)
        self.assertEqual(set(blocked), {"u1", "u2"})
        (reason,) = blocked["u2"]
        self.assertEqual(reason["kind"], "depends_on_blocked_unit")
        self.assertEqual(reason["unit"], "u1")
        self.assertEqual(reason["via"]["occupied_by"], {"id": "R1", "unit": "u1"})
        self.assertEqual(reason["via"]["id"], "R2")

    def test_dependency_chain_orders_batches(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "20")),  # frees 1010, takes 1020
            row("R2", ("t", "1"), "u2", ("s", "10")),  # wants 1010
        ]
        plan = run_plan(self.cat, lines)
        self.assertEqual(planned_units(plan), ["u1", "u2"])
        self.assertEqual(plan["batches"][0]["units"][0]["unit"], "u1")
        self.assertEqual(plan["batches"][1]["units"][0]["depends_on"], ["u1"])

    def test_key_swap_inside_one_unit_is_fine(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "20")),  # 1010 -> 1020
            row("R2", ("t", "1020"), "u1", ("s", "10")),  # 1020 -> 1010
        ]
        plan = run_plan(self.cat, lines)
        self.assertEqual(planned_units(plan), ["u1"])
        self.assertEqual(len(plan["batches"]), 1)

    def test_cross_unit_cycle_shares_one_batch(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "20")),
            row("R2", ("t", "1020"), "u2", ("s", "10")),
        ]
        plan = run_plan(self.cat, lines, k=2)
        self.assertEqual(len(plan["batches"]), 1)
        self.assertEqual(planned_units(plan), ["u1", "u2"])

    def test_oversize_group_blocks_dependers_only(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "20")),
            row("R2", ("t", "1020"), "u2", ("s", "10")),
            row("R3", ("t", "1"), "u3", ("s", "30")),  # wants 1030, free... make it depend:
            row("R4", ("t", "1030"), "u4", ("s", "40")),  # u4 holds 1030, moves to 1040
            row("R5", ("t", "2"), "u5", ("s", "50")),  # independent
        ]
        # u3 wants t:1030 held by u4 -> depends on u4 (publishable). Repoint:
        # make u3 depend on the cycle instead: u3 wants t:1010? contention.
        # Instead: u4's target 1040 is held by... keep simple: u4 publishable.
        plan = run_plan(self.cat, lines, k=1)
        blocked = blocked_units(plan)
        # cycle u1<->u2 is 2 units > k=1
        self.assertEqual(reason_kinds(blocked["u1"]), {"depends_on_blocked_unit", "batch_too_large"})
        self.assertEqual(reason_kinds(blocked["u2"]), {"depends_on_blocked_unit", "batch_too_large"})
        # u3, u4, u5 unaffected; u4 frees t:1030 before u3 takes it
        self.assertEqual(planned_units(plan), ["u4", "u3", "u5"])

    def test_depender_of_oversize_group_is_blocked(self):
        lines = [
            row("R1", ("t", "1010"), "u1", ("s", "20")),
            row("R2", ("t", "1020"), "u2", ("s", "10")),
            row("R3", ("t", "1"), "u3", ("s", "10")),  # u3 wants t:1010 (u1's key)
        ]
        plan = run_plan(self.cat, lines, k=1)
        blocked = blocked_units(plan)
        self.assertEqual(set(blocked), {"u1", "u2", "u3"})
        self.assertIn("depends_on_blocked_unit", reason_kinds(blocked["u3"]))

    def test_only_units_needing_blocked_keys_are_stopped(self):
        lines = [
            row("KEEP", ("t", "1010")),
            row("R1", ("t", "1"), "u1", ("s", "10")),  # pinned by KEEP
            row("R2", ("t", "2"), "u2", ("s", "20")),  # independent, t:1020 free
        ]
        plan = run_plan(self.cat, lines)
        self.assertEqual(planned_units(plan), ["u2"])
        self.assertEqual(set(blocked_units(plan)), {"u1"})


class BatchStructureTests(unittest.TestCase):
    def test_every_publishable_unit_appears_exactly_once(self):
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "1")),
            row("R2", ("t", "2"), "u2", ("s", "2")),
            row("R3", ("t", "1003"), "u3", ("s", "3")),  # kept
        ]
        plan = run_plan(simple_catalog(), lines, k=3)
        units = planned_units(plan)
        self.assertEqual(sorted(units), ["u1", "u2", "u3"])
        self.assertEqual(len(units), len(set(units)))
        # independent units never share a batch
        self.assertEqual(len(plan["batches"]), 3)

    def test_moves_list_all_actual_moves(self):
        lines = [
            row("R1", ("t", "1"), "u1", ("s", "1")),
            row("R2", ("t", "2"), "u1", ("s", "2")),
            row("R3", ("t", "1003"), "u1", ("s", "3")),
        ]
        plan = run_plan(simple_catalog(), lines)
        entry = plan["batches"][0]["units"][0]
        self.assertEqual(entry["kept"], 1)
        self.assertEqual([m["id"] for m in entry["moves"]], ["R1", "R2"])
        self.assertEqual(
            entry["moves"][0],
            {
                "id": "R1",
                "current": {"space": "t", "code": "1"},
                "target": {"space": "t", "code": "1001"},
            },
        )

    def test_plan_is_deterministic(self):
        lines = [
            row("R1", ("t", "1"), "u2", ("s", "1")),
            row("R2", ("t", "2"), "u1", ("s", "2")),
            row("R3", ("t", "3"), "u3", ("s", "3")),
        ]
        first = run_plan(simple_catalog(), lines, k=2)
        second = run_plan(simple_catalog(), lines, k=2)
        self.assertEqual(first, second)


class CliTests(unittest.TestCase):
    def _write(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_cli_success_and_manifest_error(self):
        cat = simple_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._write(tmp, "spec.json", json.dumps(_simple_spec_payload()))
            good = self._write(
                tmp, "good.jsonl", json.dumps(row("R1", ("t", "1"), "u1", ("s", "1"))) + "\n"
            )
            bad = self._write(tmp, "bad.jsonl", "{broken\n")

            out, err = io.StringIO(), io.StringIO()
            stdout, stderr = sys.stdout, sys.stderr
            try:
                sys.stdout, sys.stderr = out, err
                code = cli_main([spec, good, "--k", "1"])
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            self.assertEqual(code, 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["k"], 1)
            self.assertEqual(planned_units(plan), ["u1"])

            out, err = io.StringIO(), io.StringIO()
            try:
                sys.stdout, sys.stderr = out, err
                code = cli_main([spec, bad, "--k", "1"])
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")  # no plan on stdout
            self.assertIn("error:", err.getvalue())

    def test_cli_rejects_bad_k_and_missing_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._write(tmp, "spec.json", json.dumps(_simple_spec_payload()))
            manifest = self._write(tmp, "m.jsonl", "")
            out, err = io.StringIO(), io.StringIO()
            stdout, stderr = sys.stdout, sys.stderr
            try:
                sys.stdout, sys.stderr = out, err
                code = cli_main([spec, manifest, "--k", "0"])
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            self.assertEqual(code, 2)
            self.assertIn("positive integer", err.getvalue())
            self.assertEqual(out.getvalue(), "")

            out, err = io.StringIO(), io.StringIO()
            try:
                sys.stdout, sys.stderr = out, err
                code = cli_main([str(Path(tmp) / "nope.json"), manifest, "--k", "1"])
            finally:
                sys.stdout, sys.stderr = stdout, stderr
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")


def _simple_spec_payload():
    return {
        "source_spaces": {"s": [[0, 999]]},
        "target_spaces": {"t": [[1000, 1999]]},
        "rules": [
            {
                "id": "shift",
                "source_space": "s",
                "source_range": [0, 999],
                "priority": 1,
                "target_space": "t",
                "direction": 1,
                "offset": 1000,
            }
        ],
    }


class ScaleTests(unittest.TestCase):
    def test_several_thousand_records_plan_streamed(self):
        cat = simple_catalog()
        lines = []
        for index in range(5000):
            unit = f"u{index % 50}"
            lines.append(row(f"R{index}", ("t", str(index)), unit, ("s", str(index % 1000))))
        plan = run_plan(cat, lines, k=4)
        self.assertEqual(plan["summary"]["records"], 5000)
        self.assertEqual(plan["summary"]["units"], 50)
        # each unit claims t:1000..1999 per member old code; members of one
        # unit collide with members of other units -> everything contended
        # or moved; the point here is that the run completes and stays valid.
        self.assertEqual(
            plan["summary"]["publishable_units"] + plan["summary"]["blocked_units"], 50
        )


class RandomisedInvariantTests(unittest.TestCase):
    """Cross-check the planner against an independent ground-truth model."""

    def setUp(self):
        # s:0..449 mapped (+1000); s:450..499 missing; s2:0..19 alias t:1000..1019
        self.cat = catalog(
            {
                "source_spaces": {"s": [[0, 499]], "s2": [[0, 19]]},
                "target_spaces": {"t": [[1000, 1499]]},
                "rules": [
                    {
                        "id": "main",
                        "source_space": "s",
                        "source_range": [0, 449],
                        "priority": 10,
                        "target_space": "t",
                        "direction": 1,
                        "offset": 1000,
                    },
                    {
                        "id": "alias",
                        "source_space": "s2",
                        "source_range": [0, 19],
                        "priority": 10,
                        "target_space": "t",
                        "direction": 1,
                        "offset": 1000,
                    },
                ],
            }
        )

    def _resolve(self, old):
        space, code = old
        hit = self.cat.source_lookup(space, code)
        if hit.status != "mapped":
            return None
        reverse = self.cat.target_lookup(hit.target_space, hit.target)
        if reverse.status != "unique":
            return None
        return (hit.target_space, str(hit.target))

    def _ground_truth(self, rows, k):
        """Independent recomputation of blocked set and atomic groups."""

        states = {}  # id -> (state, target)
        for r in rows:
            if r["unit"] is None:
                states[r["id"]] = ("placeholder", None)
                continue
            target = self._resolve(r["old"])
            if target is None:
                states[r["id"]] = ("blocked", None)
            elif target == r["cur"]:
                states[r["id"]] = ("kept", target)
            else:
                states[r["id"]] = ("move", target)

        units = sorted({r["unit"] for r in rows if r["unit"] is not None})
        blocked = set()
        causes = {u: set() for u in units}
        for r in rows:
            if r["unit"] is not None and states[r["id"]][0] == "blocked":
                blocked.add(r["unit"])
                causes[r["unit"]].add("business")

        claims = {}
        for r in rows:
            state, target = states[r["id"]]
            if state == "move":
                claims.setdefault(target, []).append(r)
        for target, claimants in claims.items():
            if len(claimants) > 1:
                for r in claimants:
                    blocked.add(r["unit"])
                    causes[r["unit"]].add("contention")

        current = {r["cur"]: r for r in rows}
        deps = {u: set() for u in units}
        for r in rows:
            state, target = states[r["id"]]
            if state != "move":
                continue
            occupier = current.get(target)
            if occupier is None:
                continue
            occ_state = states[occupier["id"]][0]
            if occ_state in ("placeholder", "kept"):
                blocked.add(r["unit"])
                causes[r["unit"]].add("occupied")
            elif occupier["unit"] != r["unit"]:
                deps[r["unit"]].add(occupier["unit"])

        dependers = {u: set() for u in units}
        for u, others in deps.items():
            for v in others:
                dependers[v].add(u)

        def closure():
            queue = list(blocked)
            while queue:
                v = queue.pop()
                for u in dependers[v]:
                    if u not in blocked:
                        blocked.add(u)
                        causes[u].add("dependency")
                        queue.append(u)

        closure()
        sccs = _sccs(units, deps)
        for scc in sccs:
            if len(scc) > k:
                for u in scc:
                    if u not in blocked:
                        causes[u].add("oversize")
                    blocked.add(u)
                    causes[u].add("oversize")
                closure()
        groups = {frozenset(scc) for scc in sccs if not any(u in blocked for u in scc)}
        return blocked, groups, causes, states

    def test_random_scenarios(self):
        rng = random.Random(20261123)
        for trial in range(120):
            n_rows = rng.randint(1, 40)
            n_units = rng.randint(1, 6)
            k = rng.randint(1, 3)
            pool = [("t", str(c)) for c in range(1000, 1499)] + [
                ("other", str(c)) for c in range(200)
            ]
            rng.shuffle(pool)
            rows = []
            for index in range(n_rows):
                cur = pool[index]
                if rng.random() < 0.2:
                    rows.append({"id": f"R{index}", "cur": cur, "unit": None, "old": None})
                    continue
                if rng.random() < 0.15:
                    old = ("s2", rng.randint(0, 19))
                else:
                    old = ("s", rng.randint(0, 499))
                rows.append(
                    {
                        "id": f"R{index}",
                        "cur": cur,
                        "unit": f"u{rng.randint(0, n_units - 1)}",
                        "old": old,
                    }
                )
            self._check_scenario(rows, k, trial)

    def _check_scenario(self, rows, k, trial):
        lines = [
            row(r["id"], r["cur"], r["unit"], r["old"]) if r["unit"] else row(r["id"], r["cur"])
            for r in rows
        ]
        plan = run_plan(self.cat, lines, k=k)
        blocked, groups, causes, states = self._ground_truth(rows, k)

        with self.subTest(trial=trial, k=k):
            # exactly the right units are blocked; none are arbitrarily held back
            self.assertEqual(set(blocked_units(plan)), blocked)
            # every publishable unit appears exactly once, in a valid group
            planned = planned_units(plan)
            self.assertEqual(len(planned), len(set(planned)))
            planned_groups = {
                frozenset(entry["unit"] for entry in batch["units"])
                for batch in plan["batches"]
            }
            self.assertEqual(planned_groups, groups)
            for batch in plan["batches"]:
                self.assertLessEqual(len(batch["units"]), k)
            # batch order respects dependencies; execution stays collision-free
            self._simulate(rows, states, plan)
            # every blocked unit carries at least one verifiable reason
            for unit, reasons in blocked_units(plan).items():
                self.assertTrue(reasons, unit)
                self._check_reasons(unit, reasons, causes, rows, states, blocked)

    def _check_reasons(self, unit, reasons, causes, rows, states, blocked):
        kinds = reason_kinds(reasons)
        by_id = {r["id"]: r for r in rows}
        if "business" in causes[unit]:
            self.assertTrue(kinds & {"old_code_unmapped", "target_not_unique"})
        if "contention" in causes[unit]:
            self.assertIn("target_contention", kinds)
        if "occupied" in causes[unit]:
            self.assertIn("target_occupied", kinds)
        if "dependency" in causes[unit]:
            self.assertIn("depends_on_blocked_unit", kinds)
        if "oversize" in causes[unit]:
            self.assertIn("batch_too_large", kinds)
        for reason in reasons:
            kind = reason["kind"]
            if kind in ("old_code_unmapped", "target_not_unique"):
                old = (reason["old"]["space"], int(reason["old"]["code"]))
                self.assertIsNone(self._resolve(old))
                self.assertEqual(by_id[reason["id"]]["unit"], unit)
            elif kind == "target_contention":
                target = (reason["target"]["space"], reason["target"]["code"])
                claimants = [
                    r["id"]
                    for r in rows
                    if states[r["id"]][0] == "move" and states[r["id"]][1] == target
                ]
                self.assertGreaterEqual(len(claimants), 2)
                self.assertEqual(
                    sorted(a["id"] for a in reason["applicants"]), sorted(claimants)
                )
            elif kind == "target_occupied":
                target = (reason["target"]["space"], reason["target"]["code"])
                occupier = by_id[reason["occupied_by"]["id"]]
                self.assertEqual(occupier["cur"], target)
                self.assertIn(states[occupier["id"]][0], ("placeholder", "kept"))
            elif kind == "depends_on_blocked_unit":
                self.assertIn(reason["unit"], blocked)
                via = reason["via"]
                applicant = by_id[via["id"]]
                self.assertEqual(applicant["unit"], unit)
                target = (via["target"]["space"], via["target"]["code"])
                self.assertEqual(states[applicant["id"]][1], target)
                occupier = by_id[via["occupied_by"]["id"]]
                self.assertEqual(occupier["cur"], target)
                self.assertEqual(occupier["unit"], reason["unit"])
            elif kind == "batch_too_large":
                self.assertGreater(reason["size"], reason["k"])
                self.assertIn(unit, reason["members"])
            else:  # pragma: no cover
                self.fail(f"unknown reason kind {kind}")

    def _simulate(self, rows, states, plan):
        """Execute the batches in order: keys must stay unique throughout."""

        occupied = {r["cur"]: r["id"] for r in rows}
        order = {}
        for position, batch in enumerate(plan["batches"]):
            moves = [
                move for entry in batch["units"] for move in entry["moves"]
            ]
            for entry in batch["units"]:
                self.assertNotIn(entry["unit"], order)
                order[entry["unit"]] = position
            freed = {(m["current"]["space"], m["current"]["code"]) for m in moves}
            for move in moves:
                record = next(r for r in rows if r["id"] == move["id"])
                self.assertEqual(
                    (move["current"]["space"], move["current"]["code"]), record["cur"]
                )
                self.assertEqual(
                    (move["target"]["space"], move["target"]["code"]),
                    states[record["id"]][1],
                )
                target = (move["target"]["space"], move["target"]["code"])
                if target in occupied and target not in freed:
                    self.fail(f"target {target} still occupied at batch {position}")
            for move in moves:
                del occupied[(move["current"]["space"], move["current"]["code"])]
            for move in moves:
                target = (move["target"]["space"], move["target"]["code"])
                self.assertNotIn(target, occupied)
                occupied[target] = move["id"]
        # a unit's dependencies are never scheduled after it
        position_of = order
        for batch in plan["batches"]:
            for entry in batch["units"]:
                for other in entry["depends_on"]:
                    self.assertLessEqual(position_of[other], position_of[entry["unit"]])


def _sccs(units, deps):
    """Small independent Tarjan for the ground-truth model."""

    index_of, low, on_stack, stack, result = {}, {}, set(), [], []
    counter = [0]

    def visit(node):
        index_of[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in deps.get(node, ()):
            if nxt not in index_of:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index_of[nxt])
        if low[node] == index_of[node]:
            scc = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                scc.append(member)
                if member == node:
                    break
            result.append(sorted(scc))

    sys.setrecursionlimit(10000)
    for unit in sorted(units):
        if unit not in index_of:
            visit(unit)
    return result


if __name__ == "__main__":
    unittest.main()

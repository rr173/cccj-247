"""Tests for the migration catalogue.

Run with ``python3 -m unittest discover -s tests -v`` (pytest also works).
"""

from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import reference  # noqa: E402
from migration_catalog.cli import main as cli_main  # noqa: E402
from migration_catalog.core import (  # noqa: E402
    SRC_CONFLICT,
    SRC_DISABLED,
    SRC_MAPPED,
    SRC_MISSING,
    SRC_OUT_OF_DOMAIN,
    SRC_OUT_OF_RANGE,
    TGT_AMBIGUOUS,
    TGT_OUT_OF_DOMAIN,
    TGT_UNIQUE,
    TGT_UNUSED,
    Catalog,
)
from migration_catalog.intervals import Interval, normalize, subtract, total_width  # noqa: E402
from migration_catalog.model import DISABLE, MAPPING, Rule, Shift, spec_from_dict  # noqa: E402

EXAMPLES = ROOT / "examples"


def load_example(name: str) -> Catalog:
    with open(EXAMPLES / name, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return Catalog(spec_from_dict(data))


def rule(rule_id, space, lo, hi, priority, target=None, direction=None, offset=None):
    if target is None:
        return Rule(rule_id, space, Interval(lo, hi), priority, DISABLE)
    return Rule(
        rule_id,
        space,
        Interval(lo, hi),
        priority,
        MAPPING,
        target,
        Shift(direction, offset),
    )


def spec(source_spaces, target_spaces, rules):
    return spec_from_dict(
        {
            "source_spaces": {
                name: [[part.lo, part.hi] for part in domain]
                for name, domain in source_spaces.items()
            },
            "target_spaces": {
                name: [[part.lo, part.hi] for part in domain]
                for name, domain in target_spaces.items()
            },
            "rules": [
                _rule_to_json(item) for item in rules
            ],
        }
    )


def _rule_to_json(item: Rule) -> dict:
    data = {
        "id": item.id,
        "source_space": item.source_space,
        "source_range": [item.source_range.lo, item.source_range.hi],
        "priority": item.priority,
    }
    if item.action == DISABLE:
        data["action"] = "disable"
    else:
        data["target_space"] = item.target_space
        data["direction"] = item.shift.direction
        data["offset"] = item.shift.offset
    return data


def domain(*pairs) -> tuple:
    return normalize(Interval(lo, hi) for lo, hi in pairs)


# ----------------------------------------------------------------------
# Interval algebra
# ----------------------------------------------------------------------
class IntervalTests(unittest.TestCase):
    def test_normalize_merges_touching_and_overlapping(self):
        self.assertEqual(
            normalize([Interval(5, 9), Interval(0, 3), Interval(4, 4)]),
            (Interval(0, 9),),
        )

    def test_normalize_keeps_gaps(self):
        self.assertEqual(
            normalize([Interval(0, 3), Interval(5, 8)]),
            (Interval(0, 3), Interval(5, 8)),
        )

    def test_subtract_multiple_holes(self):
        self.assertEqual(
            subtract(Interval(0, 10), [Interval(2, 3), Interval(7, 7)]),
            (Interval(0, 1), Interval(4, 6), Interval(8, 10)),
        )

    def test_subtract_hole_covering_whole(self):
        self.assertEqual(subtract(Interval(0, 10), [Interval(-5, 20)]), ())

    def test_interval_is_unbounded_in_width(self):
        wide = Interval(0, 10**40)
        self.assertEqual(wide.width, 10**40 + 1)
        self.assertEqual(wide.shift(10**40).hi, 2 * 10**40)
        self.assertEqual(Interval(2**200, 2**200 + 3).width, 4)


# ----------------------------------------------------------------------
# Shift / direction semantics
# ----------------------------------------------------------------------
class ShiftTests(unittest.TestCase):
    def test_both_directions_round_trip(self):
        for direction in (1, -1):
            for offset in (0, 7, -13, 10**30):
                shift = Shift(direction, offset)
                for old in (-5, 0, 3, 2**100):
                    self.assertEqual(shift.inverse_point(shift.apply(old)), old)
                    self.assertEqual(shift.inverse_shift().apply(shift.apply(old)), old)

    def test_image_and_inverse_interval_flip_when_descending(self):
        shift = Shift(-1, 100)
        self.assertEqual(shift.image(Interval(1, 4)), Interval(96, 99))
        self.assertEqual(shift.inverse_interval(Interval(96, 99)), Interval(1, 4))


# ----------------------------------------------------------------------
# Point-level semantics (tiny hand-built cases)
# ----------------------------------------------------------------------
class PrioritySemanticsTests(unittest.TestCase):
    def test_overlap_higher_priority_wins(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 20))},
                {"t": domain((100, 200))},
                [
                    rule("low", "s", 0, 20, 1, "t", 1, 100),
                    rule("high", "s", 5, 9, 10, "t", 1, 150),
                ],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 4).rule_ids, ("low",))
        self.assertEqual(catalog.source_lookup("s", 4).target, 104)
        self.assertEqual(catalog.source_lookup("s", 7).rule_ids, ("high",))
        self.assertEqual(catalog.source_lookup("s", 7).target, 157)

    def test_tied_same_shift_merges_evidence(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((0, 99))},
                [
                    rule("a", "s", 0, 9, 5, "t", 1, 10),
                    rule("b", "s", 4, 9, 5, "t", 1, 10),
                ],
            )
        )
        hit = catalog.source_lookup("s", 5)
        self.assertEqual(hit.status, SRC_MAPPED)
        self.assertEqual(hit.rule_ids, ("a", "b"))
        self.assertEqual(hit.target, 15)

    def test_tied_same_disable_merges_evidence(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((0, 99))},
                [
                    rule("a", "s", 0, 9, 5),
                    rule("b", "s", 4, 9, 5),
                ],
            )
        )
        hit = catalog.source_lookup("s", 5)
        self.assertEqual(hit.status, SRC_DISABLED)
        self.assertEqual(hit.rule_ids, ("a", "b"))

    def test_crossing_rules_conflict_except_at_single_meeting_point(self):
        # old+100 and -old+110 are equal only at old=5 (both give 105).
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((0, 299))},
                [
                    rule("up", "s", 0, 10, 5, "t", 1, 100),
                    rule("down", "s", 0, 10, 5, "t", -1, 110),
                ],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 4).status, SRC_CONFLICT)
        self.assertEqual(catalog.source_lookup("s", 6).status, SRC_CONFLICT)
        meeting = catalog.source_lookup("s", 5)
        self.assertEqual(meeting.status, SRC_MAPPED)
        self.assertEqual(meeting.rule_ids, ("down", "up"))
        self.assertEqual(meeting.target, 105)
        # The meeting point is a single-code segment, not part of the conflict.
        lengths = [(s.lo, s.hi, s.status) for s in catalog.source_segments("s")]
        self.assertIn((5, 5, SRC_MAPPED), lengths)
        self.assertIn((0, 4, SRC_CONFLICT), lengths)
        self.assertIn((6, 10, SRC_CONFLICT), lengths)

    def test_crossing_at_single_point_inside_wide_range(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 10**18))},
                {"t": domain((0, 10**19))},
                [
                    rule("up", "s", 0, 10**18, 5, "t", 1, 0),
                    rule("down", "s", 0, 10**18, 5, "t", -1, 10**18),
                ],
            )
        )
        meeting = 10**18 // 2
        self.assertEqual(catalog.source_lookup("s", meeting).status, SRC_MAPPED)
        self.assertEqual(catalog.source_lookup("s", meeting - 1).status, SRC_CONFLICT)
        self.assertEqual(catalog.source_lookup("s", meeting + 1).status, SRC_CONFLICT)

    def test_disable_shadows_lower_priority_and_is_not_a_blocker(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((0, 100))},
                [
                    rule("map", "s", 0, 10, 1, "t", 1, 50),
                    rule("kill", "s", 3, 6, 9),
                ],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 4).status, SRC_DISABLED)
        self.assertEqual(catalog.source_lookup("s", 2).status, SRC_MAPPED)
        report = catalog.report()
        # Disable is a deliberate outcome, never an approval blocker by itself.
        self.assertTrue(report.approval_ready)
        self.assertEqual(
            report.source_counts["s"][SRC_DISABLED],
            4,
        )

    def test_disable_beats_mapping_at_same_priority_is_conflict(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((0, 100))},
                [
                    rule("map", "s", 0, 10, 5, "t", 1, 50),
                    rule("kill", "s", 0, 10, 5),
                ],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 0).status, SRC_CONFLICT)

    def test_no_rule_is_missing_not_disable(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 20))},
                {"t": domain((0, 100))},
                [rule("map", "s", 0, 9, 1, "t", 1, 50)],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 15).status, SRC_MISSING)
        kinds = {blocker.kind for blocker in catalog.report().blockers}
        self.assertIn("missing", kinds)

    def test_out_of_range_is_invalid_and_does_not_fall_back(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 20))},
                {"t": domain((0, 9))},
                [
                    rule("high", "s", 5, 6, 9, "t", 1, 100),
                    rule("low", "s", 0, 20, 1, "t", 1, 0),
                ],
            )
        )
        hit = catalog.source_lookup("s", 5)
        self.assertEqual(hit.status, SRC_OUT_OF_RANGE)
        self.assertEqual(hit.rule_ids, ("high",))
        self.assertEqual(hit.target, 105)
        self.assertFalse(hit.target_in_domain)
        self.assertEqual(catalog.source_lookup("s", 7).rule_ids, ("low",))
        self.assertIn("out_of_range", {b.kind for b in catalog.report().blockers})

    def test_out_of_range_uses_exact_target_domain_gaps(self):
        # Target domain is a union; the winner must be inside one of its parts.
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((100, 104), (200, 204))},
                [rule("m", "s", 0, 10, 1, "t", 1, 100)],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 3).status, SRC_MAPPED)
        self.assertEqual(catalog.source_lookup("s", 6).status, SRC_OUT_OF_RANGE)

    def test_rule_range_outside_source_domain_is_ignored(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((0, 100))},
                [
                    rule("inside", "s", 0, 10, 1, "t", 1, 50),
                    rule("outside", "s", 11, 20, 99, "t", 1, 60),
                ],
            )
        )
        # The high-priority rule has no effect outside the legal domain.
        self.assertEqual(catalog.source_lookup("s", 5).rule_ids, ("inside",))
        self.assertEqual(catalog.inert_rule_ids(), ("outside",))

    def test_source_query_out_of_domain(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 10))},
                {"t": domain((0, 100))},
                [rule("m", "s", 0, 10, 1, "t", 1, 50)],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 11).status, SRC_OUT_OF_DOMAIN)
        self.assertEqual(catalog.source_lookup("s", -1).status, SRC_OUT_OF_DOMAIN)

    def test_negative_codes_and_descending_shift(self):
        catalog = Catalog(
            spec(
                {"s": domain((-10, -1))},
                {"t": domain((-100, 0))},
                [rule("m", "s", -10, -1, 1, "t", -1, -50)],
            )
        )
        # new = -old - 50 maps -3 -> -47; the inverse must land back on -3.
        hit = catalog.source_lookup("s", -3)
        self.assertEqual(hit.status, SRC_MAPPED)
        self.assertEqual(hit.target, -47)
        reverse = catalog.target_lookup("t", -47)
        self.assertEqual(reverse.status, TGT_UNIQUE)
        self.assertEqual(reverse.preimages[0].code, -3)

    def test_empty_legal_intersection_is_inert_not_active(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((0, 100))},
                [
                    rule("real", "s", 0, 9, 1, "t", 1, 0),
                    rule("ghost", "s", 100, 200, 50, "t", 1, 0),
                ],
            )
        )
        self.assertEqual(catalog.source_lookup("s", 5).rule_ids, ("real",))
        self.assertEqual(catalog.inert_rule_ids(), ("ghost",))


# ----------------------------------------------------------------------
# Target-side semantics
# ----------------------------------------------------------------------
class TargetSemanticsTests(unittest.TestCase):
    def test_target_unique_returns_inverse_and_rule(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((100, 200))},
                [rule("m", "s", 0, 9, 1, "t", 1, 100)],
            )
        )
        hit = catalog.target_lookup("t", 105)
        self.assertEqual(hit.status, TGT_UNIQUE)
        self.assertEqual(hit.preimages[0].code, 5)
        self.assertEqual(hit.preimages[0].rule_ids, ("m",))

    def test_target_ambiguous_lists_two_distinct_witnesses(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9)), "u": domain((0, 9))},
                {"t": domain((100, 200))},
                [
                    rule("m", "s", 0, 9, 1, "t", 1, 100),
                    rule("n", "u", 0, 9, 1, "t", 1, 100),
                ],
            )
        )
        hit = catalog.target_lookup("t", 105)
        self.assertEqual(hit.status, TGT_AMBIGUOUS)
        self.assertEqual(len(hit.witness), 2)
        self.assertEqual(
            {(item.source_space, item.code) for item in hit.witness},
            {("s", 5), ("u", 5)},
        )
        self.assertEqual(len(hit.preimages), 2)
        self.assertIn("ambiguous", {b.kind for b in catalog.report().blockers})

    def test_witness_never_repeats_the_same_source_position(self):
        # One old code matches two rules with the *same* shift at the same
        # priority - that is merged evidence, not two preimages.
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((0, 100))},
                [
                    rule("a", "s", 0, 9, 5, "t", 1, 0),
                    rule("b", "s", 0, 9, 5, "t", 1, 0),
                ],
            )
        )
        hit = catalog.target_lookup("t", 5)
        self.assertEqual(hit.status, TGT_UNIQUE)
        self.assertEqual(len(hit.preimages), 1)

    def test_target_unused_and_out_of_domain(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((100, 150))},
                [rule("m", "s", 0, 9, 1, "t", 1, 100)],
            )
        )
        self.assertEqual(catalog.target_lookup("t", 140).status, TGT_UNUSED)
        self.assertEqual(catalog.target_lookup("t", 99).status, TGT_OUT_OF_DOMAIN)
        self.assertEqual(catalog.target_lookup("t", 151).status, TGT_OUT_OF_DOMAIN)
        report = catalog.report()
        self.assertTrue(report.approval_ready)

    def test_preimage_query_returns_all_legal_old_codes(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9)), "u": domain((100, 109)), "w": domain((0, 9))},
                {"t": domain((0, 1000))},
                [
                    rule("m", "s", 0, 9, 1, "t", 1, 500),
                    rule("n", "u", 100, 109, 1, "t", -1, 609),
                    rule("o", "w", 0, 9, 1, "t", 1, 500),
                ],
            )
        )
        # old=5 (s), old=104 (u), old=5 (w) all land on 505.
        hit = catalog.target_lookup("t", 505)
        self.assertEqual(hit.status, TGT_AMBIGUOUS)
        self.assertEqual(
            {(item.source_space, item.code) for item in hit.preimages},
            {("s", 5), ("u", 104), ("w", 5)},
        )

    def test_target_segments_are_contiguous_and_cover_domain(self):
        catalog = load_example("example.json")
        for space in ("modern", "archive"):
            segments = catalog.target_segments(space)
            domain = catalog.spec.target_spaces[space]
            self.assertEqual(total_width(segments), total_width(domain))
            # Every segment sits inside one declared domain piece, pieces are
            # tiled exactly, and adjacent segments remain in ascending order.
            for piece in domain:
                inside = [
                    segment
                    for segment in segments
                    if segment.lo <= piece.hi and piece.lo <= segment.hi
                ]
                self.assertTrue(inside)
                self.assertEqual(inside[0].lo, piece.lo)
                self.assertEqual(inside[-1].hi, piece.hi)
                cursor = piece.lo
                for segment in inside:
                    self.assertEqual(segment.lo, cursor)
                    self.assertLessEqual(segment.hi, piece.hi)
                    cursor = segment.hi + 1
            for left, right in zip(segments, segments[1:]):
                self.assertLess(left.hi, right.lo)


# ----------------------------------------------------------------------
# The worked example
# ----------------------------------------------------------------------
class WorkedExampleTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_example("example.json")

    def test_report_blocks_on_all_four_kinds(self):
        report = self.catalog.report()
        kinds = {blocker.kind for blocker in report.blockers}
        self.assertEqual(kinds, {"missing", "conflict", "out_of_range", "ambiguous"})
        self.assertFalse(report.approval_ready)

    def test_disable_alone_is_not_a_blocker(self):
        catalog = Catalog(
            spec(
                {"s": domain((0, 9))},
                {"t": domain((0, 100))},
                [rule("kill", "s", 0, 9, 1)],
            )
        )
        report = catalog.report()
        self.assertTrue(report.approval_ready)
        self.assertEqual(report.source_counts["s"][SRC_DISABLED], 10)

    def test_wide_range_is_one_segment_not_expanded(self):
        catalog = self.catalog
        segments = catalog.source_segments("legacy", 10**30, 10**30 + 10**21)
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment.status, SRC_MAPPED)
        self.assertEqual(segment.width, 10**21 + 1)
        # Forward and backward are exact at 10**30 + 123456789.
        old = 10**30 + 123456789
        target = catalog.source_lookup("legacy", old).target
        self.assertEqual(target, 10**40 + 123456789)
        reverse = catalog.target_lookup("modern", target)
        self.assertEqual(reverse.status, TGT_UNIQUE)
        self.assertEqual(reverse.preimages[0].code, old)

    def test_wide_range_queries_do_not_require_enumeration(self):
        catalog = self.catalog
        report = catalog.report()
        self.assertEqual(report.source_widths["legacy"], 10**21 + 1012)

    def test_out_of_range_winner_is_kept_not_downgraded(self):
        hit = self.catalog.source_lookup("legacy", 420)
        self.assertEqual(hit.status, SRC_OUT_OF_RANGE)
        self.assertEqual(hit.rule_ids, ("out-of-range",))

    def test_crossing_pair_conflicts_except_meeting_point(self):
        catalog = self.catalog
        self.assertEqual(catalog.source_lookup("legacy", 830).status, SRC_CONFLICT)
        meeting = catalog.source_lookup("legacy", 850)
        self.assertEqual(meeting.status, SRC_MAPPED)
        self.assertEqual(meeting.rule_ids, ("late-shift", "reverse-alias"))
        self.assertEqual(meeting.target, 1000850)

    def test_meeting_point_does_not_create_an_ambiguous_target(self):
        # Both rules agree at old=850; that is one source position, so the
        # target code 1000850 has a single preimage from the legacy space.
        hit = self.catalog.target_lookup("modern", 1000850)
        self.assertEqual(
            {(item.source_space, item.code) for item in hit.preimages},
            {("legacy", 850), ("aux", 50)},
        )
        self.assertEqual(hit.status, TGT_AMBIGUOUS)


# ----------------------------------------------------------------------
# Removing a high-priority rule turns a unique target range ambiguous
# ----------------------------------------------------------------------
class RuleRemovalTests(unittest.TestCase):
    def test_removing_shadow_rule_makes_target_ambiguous(self):
        # Rule "shadow" re-routes old 10..19 away from the target range that
        # "other" would otherwise send them to, so while shadow is present the
        # target 200..209 has exactly one preimage.  Remove shadow and old
        # 10..19 collide with old 0..9 on the very same targets.
        base_rules = [
            rule("main", "s", 0, 9, 10, "t", 1, 200),
            rule("shadow", "s", 10, 19, 50, "t", 1, 400),
            rule("other", "s", 10, 19, 20, "t", 1, 190),
        ]
        with_shadow = Catalog(
            spec({"s": domain((0, 19))}, {"t": domain((200, 420))}, base_rules)
        )
        for code in range(200, 210):
            self.assertEqual(with_shadow.target_lookup("t", code).status, TGT_UNIQUE)
        self.assertTrue(with_shadow.report().approval_ready)
        self.assertEqual(
            len(with_shadow.source_segments("s")), 2, "two same-status segments"
        )

        without_shadow = Catalog(
            spec(
                {"s": domain((0, 19))},
                {"t": domain((200, 420))},
                [item for item in base_rules if item.id != "shadow"],
            )
        )
        for code in range(200, 210):
            hit = without_shadow.target_lookup("t", code)
            self.assertEqual(hit.status, TGT_AMBIGUOUS)
            self.assertEqual(
                {(item.source_space, item.code) for item in hit.preimages},
                {("s", code - 200), ("s", code - 190)},
            )
        kinds = {b.kind for b in without_shadow.report().blockers}
        self.assertIn("ambiguous", kinds)


# ----------------------------------------------------------------------
# Exhaustive cross-check against the brute-force oracle
# ----------------------------------------------------------------------
def random_spec(rng: random.Random) -> dict:
    spaces = ["a", "b"]
    targets = ["t", "u"]
    source_spaces = {}
    for space in spaces:
        pieces = []
        cursor = rng.randint(-20, 0)
        for _ in range(rng.randint(1, 3)):
            length = rng.randint(0, 12)
            pieces.append([cursor, cursor + length])
            cursor += length + rng.randint(1, 5)
        source_spaces[space] = pieces
    target_spaces = {}
    for space in targets:
        pieces = []
        cursor = rng.randint(-50, 0)
        for _ in range(rng.randint(1, 3)):
            length = rng.randint(0, 30)
            pieces.append([cursor, cursor + length])
            cursor += length + rng.randint(1, 5)
        target_spaces[space] = pieces

    rules = []
    for index in range(rng.randint(1, 9)):
        space = rng.choice(spaces)
        domain = source_spaces[space]
        lo = rng.randint(-25, 25)
        hi = lo + rng.randint(0, 20)
        priority = rng.choice([0, 1, 2, 2, 3])
        if rng.random() < 0.2:
            rules.append(
                {
                    "id": f"r{index}",
                    "source_space": space,
                    "source_range": [lo, hi],
                    "priority": priority,
                    "action": "disable",
                }
            )
        else:
            rules.append(
                {
                    "id": f"r{index}",
                    "source_space": space,
                    "source_range": [lo, hi],
                    "priority": priority,
                    "target_space": rng.choice(targets),
                    "direction": rng.choice([1, -1]),
                    "offset": rng.randint(-80, 80),
                }
            )
    return {
        "source_spaces": source_spaces,
        "target_spaces": target_spaces,
        "rules": rules,
    }


class ExhaustiveOracleTests(unittest.TestCase):
    def assert_matches_oracle(self, spec_dict, label):
        spec = spec_from_dict(spec_dict)
        catalog = Catalog(spec)
        source_expected, target_expected = reference.brute_force_catalog(spec)

        for (space, code), expected in source_expected.items():
            hit = catalog.source_lookup(space, code)
            self.assertEqual(
                hit.status, expected[0], f"{label} source {space}:{code}"
            )
            self.assertEqual(
                hit.rule_ids, expected[1], f"{label} source {space}:{code} ids"
            )
            if expected[0] == "mapped":
                self.assertEqual(hit.target, expected[3], f"{label} {space}:{code}")
                self.assertEqual(hit.target_space, expected[2])

        # Segment expansion must agree with per-point lookup.
        for space in spec.source_spaces:
            for segment in catalog.source_segments(space):
                self.assertLessEqual(segment.width, 64, f"{label} tiny segments")
                for code in range(segment.lo, segment.hi + 1):
                    hit = catalog.source_lookup(space, code)
                    self.assertEqual(hit.status, segment.status)
                    self.assertEqual(hit.rule_ids, segment.rule_ids)
                    if segment.status == SRC_MAPPED:
                        self.assertEqual(hit.target, segment.target_for(code))

        for (space, code), expected in target_expected.items():
            hit = catalog.target_lookup(space, code)
            self.assertEqual(hit.status, expected[0], f"{label} target {space}:{code}")
            self.assertEqual(
                {(item.source_space, item.code) for item in hit.preimages},
                {(item[0], item[1]) for item in expected[1]},
                f"{label} target preimages {space}:{code}",
            )
            if expected[0] == "ambiguous":
                self.assertEqual(len(hit.witness), 2)
                self.assertNotEqual(hit.witness[0], hit.witness[1])

        # Report blocker kinds must be exactly the oracle's nonzero classes.
        report = catalog.report()
        expected_kinds = set()
        for (space, code), expected in source_expected.items():
            if expected[0] in ("missing", "conflict", "out_of_range"):
                expected_kinds.add(expected[0])
        for (space, code), expected in target_expected.items():
            if expected[0] == "ambiguous":
                expected_kinds.add("ambiguous")
        self.assertEqual(
            {blocker.kind for blocker in report.blockers},
            expected_kinds,
            f"{label} blocker kinds",
        )

    def test_random_specs_match_brute_force(self):
        rng = random.Random(20241123)
        for index in range(250):
            self.assert_matches_oracle(random_spec(rng), f"random#{index}")

    def test_example_tiny_matches_brute_force(self):
        with open(EXAMPLES / "tiny.json", encoding="utf-8") as handle:
            self.assert_matches_oracle(json.load(handle), "tiny")


# ----------------------------------------------------------------------
# Wide integers
# ----------------------------------------------------------------------
class WideIntegerTests(unittest.TestCase):
    def test_beyond_64_bits_forward_and_reverse(self):
        base = 2**80
        catalog = Catalog(
            spec(
                {"s": domain((base, base + 10**25))},
                {"t": domain((base * 7 - 10**25, base * 7 + 10**25))},
                [rule("m", "s", base, base + 10**25, 1, "t", -1, base * 8)],
            )
        )
        for old in (base, base + 1, base + 10**25, base + 12345678901234567890):
            hit = catalog.source_lookup("s", old)
            self.assertEqual(hit.status, SRC_MAPPED)
            self.assertEqual(hit.target, base * 8 - old)
            reverse = catalog.target_lookup("t", hit.target)
            self.assertEqual(reverse.status, TGT_UNIQUE)
            self.assertEqual(reverse.preimages[0].code, old)
        self.assertEqual(len(catalog.source_segments("s")), 1)

    def test_wide_intervals_do_not_loop(self):
        base = 2**200
        size = 10**50
        catalog = Catalog(
            spec(
                {"s": domain((base, base + size))},
                {"t": domain((0, size * 2))},
                [rule("m", "s", base, base + size, 1, "t", 1, -base + size)],
            )
        )
        report = catalog.report()
        self.assertEqual(report.source_counts["s"][SRC_MAPPED], size + 1)
        self.assertEqual(report.target_counts["t"][TGT_UNUSED], size)

    def test_out_of_range_inside_wide_range(self):
        base = 2**100
        catalog = Catalog(
            spec(
                {"s": domain((base, base + 1000))},
                {"t": domain((base + 100, base + 199))},
                [rule("m", "s", base, base + 1000, 1, "t", 1, 0)],
            )
        )
        self.assertEqual(catalog.source_lookup("s", base).status, SRC_OUT_OF_RANGE)
        self.assertEqual(catalog.source_lookup("s", base + 150).status, SRC_MAPPED)
        self.assertEqual(catalog.source_lookup("s", base + 500).status, SRC_OUT_OF_RANGE)
        out = catalog.source_segments("s")
        self.assertEqual([s.status for s in out], [SRC_OUT_OF_RANGE, SRC_MAPPED, SRC_OUT_OF_RANGE])

    def test_very_long_decimal_codes_render_without_digit_limit_error(self):
        # 400 digits exceeds CPython's default int->str digit cap of 4300 in
        # aggregate; formatting one value at a time must stay below it, and the
        # chunked formatter must not raise even for genuinely huge values.
        base = 10**400
        catalog = Catalog(
            spec(
                {"s": domain((base, base + 5))},
                {"t": domain((2 * base, 2 * base + 5))},
                [rule("m", "s", base, base + 5, 1, "t", 1, base)],
            )
        )
        self.assertEqual(catalog.source_lookup("s", base + 2).target, 2 * base + 2)
        text = catalog.render_source("s")
        self.assertIn("declared legal domain", text)
        from migration_catalog.intervals import grouped_int, plain_int

        # 10**5000 has 5001 digits, beyond the interpreter's default cap; the
        # chunked formatter stays well under the cap per str() call.
        rendered = plain_int(10**5000)
        self.assertEqual(len(rendered), 5001)
        self.assertEqual(rendered[0], "1")
        self.assertEqual(rendered[1:], "0" * 5000)
        self.assertEqual(grouped_int(-(10**60 + 7)), f"{-(10**60 + 7):,}")


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def test_rejects_undeclared_space(self):
        with self.assertRaises(ValueError):
            spec_from_dict(
                {
                    "source_spaces": {"s": [[0, 1]]},
                    "target_spaces": {"t": [[0, 1]]},
                    "rules": [
                        {
                            "id": "r",
                            "source_space": "nope",
                            "source_range": [0, 1],
                            "priority": 1,
                            "target_space": "t",
                            "direction": 1,
                            "offset": 0,
                        }
                    ],
                }
            )

    def test_rejects_bad_direction(self):
        with self.assertRaises(ValueError):
            Shift(2, 0)

    def test_rejects_non_integer_priority(self):
        with self.assertRaises(TypeError):
            rule  # keep linters quiet
            Rule("r", "s", Interval(0, 1), 1.5, DISABLE)

    def test_rejects_bool_codes(self):
        with self.assertRaises(TypeError):
            Interval(False, True)

    def test_rejects_inverted_interval(self):
        with self.assertRaises(ValueError):
            Interval(5, 4)

    def test_rejects_disable_with_target_fields(self):
        with self.assertRaises(ValueError):
            Rule("r", "s", Interval(0, 1), 1, DISABLE, "t", Shift(1, 0))

    def test_rejects_duplicate_rule_ids(self):
        with self.assertRaises(ValueError):
            spec(
                {"s": domain((0, 1))},
                {"t": domain((0, 1))},
                [
                    rule("same", "s", 0, 1, 1, "t", 1, 0),
                    rule("same", "s", 0, 1, 2, "t", 1, 0),
                ],
            )

    def test_unknown_spec_field_rejected(self):
        with self.assertRaises(ValueError):
            spec_from_dict(
                {
                    "source_spaces": {"s": [[0, 1]]},
                    "target_spaces": {"t": [[0, 1]]},
                    "rules": [],
                    "typo_field": 1,
                }
            )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_main(list(args))
        return code, buffer.getvalue()

    def test_report_and_queries(self):
        path = str(EXAMPLES / "example.json")
        code, output = self.run_cli(path, "report")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["approval_ready"])
        self.assertIn("ambiguous", {item["kind"] for item in payload["blockers"]})

        code, output = self.run_cli(
            path, "source-point", "--space", "legacy", "--code", "850"
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["target"], 1000850)
        self.assertEqual(payload["rule_ids"], ["late-shift", "reverse-alias"])

        code, output = self.run_cli(
            path, "target-point", "--space", "modern", "--code", "1000805"
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertEqual(len(payload["witness"]), 2)

    def test_missing_spec_returns_error(self):
        import io
        from contextlib import redirect_stderr

        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = cli_main(["does-not-exist.json", "report"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

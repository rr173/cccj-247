"""Symbolic source/target migration catalog.

The catalog never materialises individual code pairs.  It partitions declared
legal domains into maximal intervals that share one classification, so a range
such as ``[2**100, 2**100 + 10**25]`` is still a single catalogue row.

Source-side classifications
---------------------------
``mapped``        exactly one winning target value, inside the target domain
``out_of_range``  a winning target exists but lies outside the target domain
                  (invalid; lower-priority rules are *not* consulted)
``disabled``      the highest-priority decision is "retire"
``conflict``      tied highest priority rules disagree
``missing``       no rule applies to a legal source code (not a disable)
``out_of_domain`` query-only: the code is outside the declared legal domain

Target-side classifications
---------------------------
``unique``        exactly one legal source position maps to the code
``ambiguous``     two or more legal source positions map to the code
``unused``        no legal source position maps to the code
``out_of_domain`` query-only
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from .intervals import Interval, grouped_int, normalize, subtract, total_width
from .model import DISABLE, MAPPING, MigrationSpec, Rule, Shift

SRC_MAPPED = "mapped"
SRC_OUT_OF_RANGE = "out_of_range"
SRC_DISABLED = "disabled"
SRC_CONFLICT = "conflict"
SRC_MISSING = "missing"
SRC_OUT_OF_DOMAIN = "out_of_domain"

TGT_UNIQUE = "unique"
TGT_AMBIGUOUS = "ambiguous"
TGT_UNUSED = "unused"
TGT_OUT_OF_DOMAIN = "out_of_domain"

SOURCE_STATUS_ORDER = (
    SRC_MAPPED,
    SRC_OUT_OF_RANGE,
    SRC_DISABLED,
    SRC_CONFLICT,
    SRC_MISSING,
)
TARGET_STATUS_ORDER = (TGT_UNIQUE, TGT_AMBIGUOUS, TGT_UNUSED)

APPROVAL_BLOCKING_SOURCE = (SRC_MISSING, SRC_CONFLICT, SRC_OUT_OF_RANGE)


def _in_any(parts: Iterable[Interval], value: int) -> bool:
    return any(value in part for part in parts)


def _source_intersection(a: Rule, b: Rule) -> int | None:
    """The one old code where two mapping rules produce the same target.

    ``d1*x+o1 == d2*x+o2`` has at most one solution because each direction is
    ±1.  It is an integer when ``(o2-o1)`` is divisible by ``(d1-d2)``.
    """

    assert a.shift is not None and b.shift is not None
    if a.shift.direction == b.shift.direction:
        # Parallel lines are either identical (no single meeting point) or
        # never equal.
        return None
    numerator = b.shift.offset - a.shift.offset
    denominator = a.shift.direction - b.shift.direction
    if numerator % denominator:
        return None
    return numerator // denominator


@dataclass(frozen=True)
class SourceSegment:
    """A contiguous, same-classification source range."""

    space: str
    lo: int
    hi: int
    status: str
    rule_ids: tuple[str, ...]
    priority: int | None
    target_space: str | None = None
    target_lo: int | None = None
    target_hi: int | None = None
    shift: Shift | None = None
    reason: str = ""

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def is_valid_mapping(self) -> bool:
        return self.status == SRC_MAPPED

    def target_for(self, old: int) -> int | None:
        """Target value for any code in a valid mapped segment."""

        if self.status != SRC_MAPPED:
            return None
        if not (self.lo <= old <= self.hi):
            raise ValueError(f"{old} is outside segment [{self.lo}, {self.hi}]")
        if self.shift is not None:
            return self.shift.apply(old)
        # A one-code segment can merge differently-directed rules that happen
        # to meet at this exact value; the agreed value is stored explicitly.
        assert self.target_lo == self.target_hi
        return self.target_lo


@dataclass(frozen=True)
class TargetContributor:
    """A valid source segment projected into one target space."""

    source_space: str
    source_lo: int
    source_hi: int
    target_lo: int
    target_hi: int
    rule_ids: tuple[str, ...]
    shift: Shift | None

    def covers(self, target: int) -> bool:
        return self.target_lo <= target <= self.target_hi

    def preimage(self, target: int) -> int:
        if not self.covers(target):
            raise ValueError(f"target {target} not covered by contributor")
        if self.shift is None:
            if self.target_lo != self.target_hi:
                raise AssertionError("point contributor covers a range")
            return self.source_lo
        old = self.shift.inverse_point(target)
        if not (self.source_lo <= old <= self.source_hi):
            raise AssertionError(
                f"inverse {old} outside source [{self.source_lo}, {self.source_hi}]"
            )
        return old


@dataclass(frozen=True)
class Preimage:
    source_space: str
    code: int
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class TargetSegment:
    """A contiguous target range with a constant reverse classification."""

    space: str
    lo: int
    hi: int
    status: str
    contributors: tuple[TargetContributor, ...]
    witness: tuple[Preimage, ...] = ()

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1


@dataclass(frozen=True)
class SourceHit:
    space: str
    code: int
    status: str
    rule_ids: tuple[str, ...]
    priority: int | None
    target_space: str | None
    target: int | None
    target_in_domain: bool | None
    reason: str


@dataclass(frozen=True)
class TargetHit:
    space: str
    code: int
    status: str
    segment_lo: int | None
    segment_hi: int | None
    preimages: tuple[Preimage, ...]
    witness: tuple[Preimage, ...] = ()


@dataclass(frozen=True)
class Blocker:
    kind: str
    space: str
    count: int
    detail: str


@dataclass(frozen=True)
class MigrationReport:
    source_counts: dict[str, dict[str, int]]
    target_counts: dict[str, dict[str, int]]
    source_widths: dict[str, int]
    target_widths: dict[str, int]
    blockers: tuple[Blocker, ...]
    inert_rule_ids: tuple[str, ...]

    @property
    def approval_ready(self) -> bool:
        return not self.blockers


class Catalog:
    """Queryable forward and reverse catalogue for one migration spec."""

    def __init__(self, spec: MigrationSpec):
        self.spec = spec
        self._source: dict[str, tuple[SourceSegment, ...]] = {
            space: self._build_source(space) for space in spec.source_spaces
        }
        self._target: dict[str, tuple[TargetSegment, ...]] = {
            space: self._build_target(space) for space in spec.target_spaces
        }
        self._source_starts = {
            space: [segment.lo for segment in segments]
            for space, segments in self._source.items()
        }
        self._target_starts = {
            space: [segment.lo for segment in segments]
            for space, segments in self._target.items()
        }
        self._check_partitions()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_source(self, space: str) -> tuple[SourceSegment, ...]:
        domain = self.spec.source_spaces[space]
        rules = self.spec.rules_for(space)
        legal_pieces = {rule.id: self.spec.legal_pieces(rule) for rule in rules}

        cuts: set[int] = set()
        for part in domain:
            cuts.add(part.lo)
            cuts.add(part.hi + 1)
        for pieces in legal_pieces.values():
            for piece in pieces:
                cuts.add(piece.lo)
                cuts.add(piece.hi + 1)
        # Split where two mapping rules with different directions can agree on
        # a single value; that isolated agreement must not be swallowed by a
        # larger conflicting range.
        mapping_rules = [rule for rule in rules if rule.action == MAPPING]
        for index, first in enumerate(mapping_rules):
            for second in mapping_rules[index + 1 :]:
                meeting = _source_intersection(first, second)
                if meeting is not None:
                    cuts.add(meeting)
                    cuts.add(meeting + 1)

        ordered = sorted(cuts)
        segments: list[SourceSegment] = []
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            cell = Interval(left, right - 1)
            # A cut pair may straddle a gap between domain pieces; only the
            # cells whose start is legal are classified, so gaps stay absent.
            if not _in_any(domain, left):
                continue
            segments.extend(
                self._classify_source_cell(space, cell, rules, legal_pieces)
            )
        segments.sort(key=lambda segment: segment.lo)
        return tuple(self._coalesce_source(segments))

    def _classify_source_cell(
        self,
        space: str,
        cell: Interval,
        rules: Sequence[Rule],
        legal_pieces: dict[str, tuple[Interval, ...]],
    ) -> list[SourceSegment]:
        applicable = [
            rule for rule in rules if _in_any(legal_pieces[rule.id], cell.lo)
        ]
        if not applicable:
            return [
                SourceSegment(
                    space,
                    cell.lo,
                    cell.hi,
                    SRC_MISSING,
                    (),
                    None,
                    reason="no rule applies on the legal domain; this is not a disable",
                )
            ]

        top_priority = max(rule.priority for rule in applicable)
        winners = sorted(
            (rule for rule in applicable if rule.priority == top_priority),
            key=lambda rule: rule.id,
        )
        ids = tuple(rule.id for rule in winners)

        if any(rule.action == DISABLE for rule in winners):
            if all(rule.action == DISABLE for rule in winners):
                return [
                    SourceSegment(
                        space,
                        cell.lo,
                        cell.hi,
                        SRC_DISABLED,
                        ids,
                        top_priority,
                        reason="highest-priority decision is disable",
                    )
                ]
            return [
                self._conflict_segment(
                    space,
                    cell,
                    winners,
                    top_priority,
                    "tied highest priority mixes disable and mapping",
                )
            ]

        keys = {
            (rule.target_space, rule.shift.direction, rule.shift.offset)
            for rule in winners
            if rule.shift is not None
        }
        if len(keys) == 1:
            target_space, direction, offset = keys.pop()
            return self._split_mapped_and_out_of_range(
                space, cell, winners, top_priority, target_space, Shift(direction, offset)
            )

        if cell.lo == cell.hi:
            values = {
                (rule.target_space, rule.shift.apply(cell.lo))
                for rule in winners
                if rule.shift is not None
            }
            if len(values) == 1:
                target_space, value = values.pop()
                in_domain = _in_any(self.spec.target_spaces[target_space], value)
                return [
                    SourceSegment(
                        space,
                        cell.lo,
                        cell.hi,
                        SRC_MAPPED if in_domain else SRC_OUT_OF_RANGE,
                        ids,
                        top_priority,
                        target_space,
                        value,
                        value,
                        None,
                        (
                            "different-direction rules meet at this one value"
                            if in_domain
                            else "met value lies outside the target domain; invalid"
                        ),
                    )
                ]
            return [
                self._conflict_segment(
                    space,
                    cell,
                    winners,
                    top_priority,
                    "tied highest priority gives different values at this code",
                )
            ]

        first, second = winners[0], winners[1]
        assert first.shift is not None and second.shift is not None
        if first.target_space != second.target_space:
            detail = (
                "tied highest priority maps this range to different target "
                f"spaces {first.target_space!r} and {second.target_space!r}"
            )
        else:
            detail = (
                "tied highest priority uses different shifts: "
                f"old={cell.lo} gives {first.shift.apply(cell.lo)} vs "
                f"{second.shift.apply(cell.lo)}"
            )
        return [
            self._conflict_segment(space, cell, winners, top_priority, detail)
        ]

    def _split_mapped_and_out_of_range(
        self,
        space: str,
        cell: Interval,
        winners: Sequence[Rule],
        priority: int,
        target_space: str,
        shift: Shift,
    ) -> list[SourceSegment]:
        image = shift.image(cell)
        target_domain = self.spec.target_spaces[target_space]

        valid_sources: list[Interval] = []
        for part in target_domain:
            overlap = image.intersection(part)
            if overlap is not None:
                valid_sources.append(shift.inverse_interval(overlap))
        valid_sources = list(normalize(valid_sources))

        ids = tuple(rule.id for rule in winners)
        segments: list[SourceSegment] = []
        for source_piece in valid_sources:
            target_piece = shift.image(source_piece)
            segments.append(
                SourceSegment(
                    space,
                    source_piece.lo,
                    source_piece.hi,
                    SRC_MAPPED,
                    ids,
                    priority,
                    target_space,
                    target_piece.lo,
                    target_piece.hi,
                    shift,
                    (
                        "unique winning mapping"
                        if len(ids) == 1
                        else "tied rules share the same shift; evidence merged"
                    ),
                )
            )
        for source_piece in subtract(cell, valid_sources):
            target_piece = shift.image(source_piece)
            segments.append(
                SourceSegment(
                    space,
                    source_piece.lo,
                    source_piece.hi,
                    SRC_OUT_OF_RANGE,
                    ids,
                    priority,
                    target_space,
                    target_piece.lo,
                    target_piece.hi,
                    shift,
                    (
                        "winning target is outside the declared target domain; "
                        "invalid, no lower-priority fallback"
                    ),
                )
            )
        return segments

    def _conflict_segment(
        self,
        space: str,
        cell: Interval,
        winners: Sequence[Rule],
        priority: int,
        reason: str,
    ) -> SourceSegment:
        return SourceSegment(
            space,
            cell.lo,
            cell.hi,
            SRC_CONFLICT,
            tuple(rule.id for rule in winners),
            priority,
            reason=reason,
        )

    @staticmethod
    def _coalesce_source(segments: list[SourceSegment]) -> list[SourceSegment]:
        result: list[SourceSegment] = []
        for segment in segments:
            if result and Catalog._source_mergeable(result[-1], segment):
                previous = result[-1]
                merged_hi = segment.hi
                if previous.shift is not None:
                    image = previous.shift.image(Interval(previous.lo, merged_hi))
                    result[-1] = replace(
                        previous,
                        hi=merged_hi,
                        target_lo=image.lo,
                        target_hi=image.hi,
                    )
                else:
                    result[-1] = replace(previous, hi=merged_hi)
            else:
                result.append(segment)
        return result

    @staticmethod
    def _source_mergeable(first: SourceSegment, second: SourceSegment) -> bool:
        if first.hi + 1 != second.lo:
            return False
        if (
            first.status != second.status
            or first.rule_ids != second.rule_ids
            or first.priority != second.priority
            or first.target_space != second.target_space
            or first.shift != second.shift
        ):
            return False
        if first.status in (SRC_MAPPED, SRC_OUT_OF_RANGE):
            if first.shift is None:
                # One-code agreement of differently-directed rules; its value
                # cannot describe neighbouring codes, so never merge it away.
                return False
            assert first.target_hi is not None and second.target_lo is not None
            if first.shift.direction == 1:
                return first.target_hi + 1 == second.target_lo
            return first.target_lo - 1 == second.target_hi
        return True

    def _build_target(self, space: str) -> tuple[TargetSegment, ...]:
        domain = self.spec.target_spaces[space]
        contributors: list[TargetContributor] = []
        for source_space, segments in self._source.items():
            for segment in segments:
                if segment.status != SRC_MAPPED or segment.target_space != space:
                    continue
                assert segment.target_lo is not None and segment.target_hi is not None
                contributor = TargetContributor(
                    source_space,
                    segment.lo,
                    segment.hi,
                    segment.target_lo,
                    segment.target_hi,
                    segment.rule_ids,
                    segment.shift,
                )
                if not (
                    _in_any(domain, contributor.target_lo)
                    and _in_any(domain, contributor.target_hi)
                ):
                    raise AssertionError(
                        f"valid mapping leaves target domain {space!r}"
                    )
                contributors.append(contributor)

        cuts: set[int] = set()
        for part in domain:
            cuts.add(part.lo)
            cuts.add(part.hi + 1)
        for contributor in contributors:
            cuts.add(contributor.target_lo)
            cuts.add(contributor.target_hi + 1)

        ordered = sorted(cuts)
        segments: list[TargetSegment] = []
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            if not _in_any(domain, left):
                continue
            cell = Interval(left, right - 1)
            covering = [c for c in contributors if c.covers(cell.lo)]
            if not covering:
                status = TGT_UNUSED
                witness: tuple[Preimage, ...] = ()
            elif len(covering) == 1:
                status = TGT_UNIQUE
                witness = ()
            else:
                status = TGT_AMBIGUOUS
                witness = self._make_witness(covering, cell.lo)
            segments.append(
                TargetSegment(space, cell.lo, cell.hi, status, tuple(covering), witness)
            )
        segments.sort(key=lambda segment: segment.lo)
        return tuple(self._coalesce_target(segments))

    @staticmethod
    def _make_witness(
        covering: Sequence[TargetContributor], target: int
    ) -> tuple[Preimage, ...]:
        witness: list[Preimage] = []
        seen: set[tuple[str, int]] = set()
        for contributor in covering:
            old = contributor.preimage(target)
            position = (contributor.source_space, old)
            if position in seen:
                continue
            seen.add(position)
            witness.append(Preimage(contributor.source_space, old, contributor.rule_ids))
            if len(witness) == 2:
                break
        if len(witness) < 2:
            raise AssertionError(
                f"ambiguous target {target} has fewer than two distinct preimages"
            )
        return tuple(witness)

    @staticmethod
    def _coalesce_target(segments: list[TargetSegment]) -> list[TargetSegment]:
        result: list[TargetSegment] = []
        for segment in segments:
            if result:
                previous = result[-1]
                if (
                    previous.hi + 1 == segment.lo
                    and previous.status == segment.status
                    and previous.contributors == segment.contributors
                ):
                    result[-1] = replace(previous, hi=segment.hi)
                    continue
            result.append(segment)
        return result

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    def _check_partitions(self) -> None:
        for space, segments in self._source.items():
            domain = self.spec.source_spaces[space]
            self._check_partition(space, segments, domain)
            for segment in segments:
                if segment.status == SRC_MAPPED:
                    if segment.target_space is None:
                        raise AssertionError("mapped source segment lacks target space")
                    if segment.shift is None and segment.target_lo != segment.target_hi:
                        raise AssertionError("point mapping without a shift")

        for space, segments in self._target.items():
            domain = self.spec.target_spaces[space]
            self._check_partition(space, segments, domain)
            for segment in segments:
                if segment.status == TGT_UNIQUE and len(segment.contributors) != 1:
                    raise AssertionError("unique target without one contributor")
                if segment.status == TGT_AMBIGUOUS and len(segment.contributors) < 2:
                    raise AssertionError("ambiguous target with <2 contributors")
                if segment.status == TGT_UNUSED and segment.contributors:
                    raise AssertionError("unused target has contributors")

    @staticmethod
    def _check_partition(space: str, segments, domain) -> None:
        """Segments must exactly tile each declared legal domain piece."""

        for part in domain:
            inside = [
                segment
                for segment in segments
                if segment.lo <= part.hi and part.lo <= segment.hi
            ]
            if not inside:
                raise AssertionError(f"no catalog coverage for {space} piece {part}")
            if inside[0].lo != part.lo or inside[-1].hi != part.hi:
                raise AssertionError(f"catalog endpoints mismatch in {space}")
            previous_end: int | None = None
            for segment in inside:
                if segment.lo < part.lo or segment.hi > part.hi:
                    raise AssertionError(
                        f"catalog segment leaks outside {space} piece {part}"
                    )
                if previous_end is not None and segment.lo != previous_end + 1:
                    raise AssertionError(f"catalog gap/overlap in {space} piece {part}")
                previous_end = segment.hi
        if total_width(segments) != total_width(domain):
            raise AssertionError(f"catalog width mismatch in {space}")

    # ------------------------------------------------------------------
    # Source queries
    # ------------------------------------------------------------------
    def source_segments(
        self, space: str, lo: int | None = None, hi: int | None = None
    ) -> tuple[SourceSegment, ...]:
        self._require_space("source", space)
        if lo is None:
            lo = self.spec.source_spaces[space][0].lo
        if hi is None:
            hi = self.spec.source_spaces[space][-1].hi
        _require_range(lo, hi)
        result: list[SourceSegment] = []
        for segment in self._source[space]:
            clipped_lo = max(segment.lo, lo)
            clipped_hi = min(segment.hi, hi)
            if clipped_lo > clipped_hi:
                continue
            result.append(self._clip_source(segment, clipped_lo, clipped_hi))
        return tuple(result)

    def _clip_source(
        self, segment: SourceSegment, lo: int, hi: int
    ) -> SourceSegment:
        if segment.status in (SRC_MAPPED, SRC_OUT_OF_RANGE):
            if segment.shift is not None:
                image = segment.shift.image(Interval(lo, hi))
                return replace(
                    segment,
                    lo=lo,
                    hi=hi,
                    target_lo=image.lo,
                    target_hi=image.hi,
                )
            if lo != hi:
                raise AssertionError("point mapping clipped to a range")
            return segment
        return replace(segment, lo=lo, hi=hi)

    def source_lookup(self, space: str, code: int) -> SourceHit:
        self._require_space("source", space)
        _require_code(code)
        if not _in_any(self.spec.source_spaces[space], code):
            return SourceHit(
                space,
                code,
                SRC_OUT_OF_DOMAIN,
                (),
                None,
                None,
                None,
                None,
                "code is outside the declared legal source domain",
            )
        starts = self._source_starts[space]
        index = bisect_right(starts, code) - 1
        if index < 0:
            raise AssertionError("source coverage invariant broken")
        segment = self._source[space][index]
        if not (segment.lo <= code <= segment.hi):
            raise AssertionError("source coverage invariant broken")
        target = segment.target_for(code) if segment.status == SRC_MAPPED else None
        if segment.status == SRC_OUT_OF_RANGE:
            target = (
                segment.shift.apply(code)
                if segment.shift is not None
                else segment.target_lo
            )
        return SourceHit(
            space,
            code,
            segment.status,
            segment.rule_ids,
            segment.priority,
            segment.target_space,
            target,
            segment.status == SRC_MAPPED,
            segment.reason,
        )

    # ------------------------------------------------------------------
    # Target queries
    # ------------------------------------------------------------------
    def target_segments(
        self, space: str, lo: int | None = None, hi: int | None = None
    ) -> tuple[TargetSegment, ...]:
        self._require_space("target", space)
        if lo is None:
            lo = self.spec.target_spaces[space][0].lo
        if hi is None:
            hi = self.spec.target_spaces[space][-1].hi
        _require_range(lo, hi)
        result: list[TargetSegment] = []
        for segment in self._target[space]:
            clipped_lo = max(segment.lo, lo)
            clipped_hi = min(segment.hi, hi)
            if clipped_lo > clipped_hi:
                continue
            if clipped_lo == segment.lo and clipped_hi == segment.hi:
                result.append(segment)
                continue
            witness = (
                self._make_witness(segment.contributors, clipped_lo)
                if segment.status == TGT_AMBIGUOUS
                else ()
            )
            result.append(
                replace(
                    segment,
                    lo=clipped_lo,
                    hi=clipped_hi,
                    witness=witness,
                )
            )
        return tuple(result)

    def target_lookup(self, space: str, code: int) -> TargetHit:
        self._require_space("target", space)
        _require_code(code)
        if not _in_any(self.spec.target_spaces[space], code):
            return TargetHit(
                space, code, TGT_OUT_OF_DOMAIN, None, None, ()
            )
        starts = self._target_starts[space]
        index = bisect_right(starts, code) - 1
        if index < 0:
            raise AssertionError("target coverage invariant broken")
        segment = self._target[space][index]
        if not (segment.lo <= code <= segment.hi):
            raise AssertionError("target coverage invariant broken")
        preimages: list[Preimage] = []
        seen: set[tuple[str, int]] = set()
        for contributor in segment.contributors:
            old = contributor.preimage(code)
            position = (contributor.source_space, old)
            if position in seen:
                continue
            seen.add(position)
            preimages.append(Preimage(contributor.source_space, old, contributor.rule_ids))
        witness = (
            tuple(preimages[:2]) if segment.status == TGT_AMBIGUOUS else ()
        )
        return TargetHit(
            space,
            code,
            segment.status,
            segment.lo,
            segment.hi,
            tuple(preimages),
            witness,
        )

    def target_preimages(self, space: str, code: int) -> tuple[Preimage, ...]:
        """All legal old codes mapping to ``code`` (empty unless unique/ambiguous)."""

        return self.target_lookup(space, code).preimages

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def source_counts(self, space: str) -> dict[str, int]:
        self._require_space("source", space)
        counts = {status: 0 for status in SOURCE_STATUS_ORDER}
        for segment in self._source[space]:
            counts[segment.status] += segment.width
        return counts

    def target_counts(self, space: str) -> dict[str, int]:
        self._require_space("target", space)
        counts = {status: 0 for status in TARGET_STATUS_ORDER}
        for segment in self._target[space]:
            counts[segment.status] += segment.width
        return counts

    def inert_rule_ids(self) -> tuple[str, ...]:
        return tuple(
            rule.id
            for rule in self.spec.rules
            if not self.spec.legal_pieces(rule)
        )

    def report(self) -> MigrationReport:
        source_counts = {
            space: self.source_counts(space) for space in self.spec.source_spaces
        }
        target_counts = {
            space: self.target_counts(space) for space in self.spec.target_spaces
        }
        blockers: list[Blocker] = []
        for space, counts in source_counts.items():
            for status in APPROVAL_BLOCKING_SOURCE:
                if counts[status]:
                    blockers.append(
                        Blocker(
                            status,
                            space,
                            counts[status],
                            f"{counts[status]} legal source codes are {status}",
                        )
                    )
        for space, counts in target_counts.items():
            if counts[TGT_AMBIGUOUS]:
                blockers.append(
                    Blocker(
                        TGT_AMBIGUOUS,
                        space,
                        counts[TGT_AMBIGUOUS],
                        (
                            f"{counts[TGT_AMBIGUOUS]} target codes have two or more "
                            "legal preimages"
                        ),
                    )
                )
        return MigrationReport(
            source_counts,
            target_counts,
            {space: total_width(d) for space, d in self.spec.source_spaces.items()},
            {space: total_width(d) for space, d in self.spec.target_spaces.items()},
            tuple(blockers),
            self.inert_rule_ids(),
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render_source(self, space: str) -> str:
        domain = self.spec.source_spaces[space]
        counts = self.source_counts(space)
        lines = [
            f"Source catalog: {space} "
            f"(legal width {format_wide(total_width(domain))})",
            "  declared legal domain: "
            + ", ".join(format_interval(part.lo, part.hi) for part in domain),
        ]
        for status in SOURCE_STATUS_ORDER:
            subset = [s for s in self._source[space] if s.status == status]
            lines.append(
                f"  {status.upper()}  [{format_wide(counts[status])} codes]"
            )
            for segment in subset:
                lines.append("    " + _render_source_segment(segment))
        return "\n".join(lines)

    def render_target(self, space: str) -> str:
        domain = self.spec.target_spaces[space]
        counts = self.target_counts(space)
        lines = [
            f"Target catalog: {space} "
            f"(legal width {format_wide(total_width(domain))})",
            "  declared legal domain: "
            + ", ".join(format_interval(part.lo, part.hi) for part in domain),
        ]
        for status in TARGET_STATUS_ORDER:
            subset = [s for s in self._target[space] if s.status == status]
            lines.append(
                f"  {status.upper()}  [{format_wide(counts[status])} codes]"
            )
            for segment in subset:
                lines.append("    " + _render_target_segment(segment))
        return "\n".join(lines)

    def render_report(self) -> str:
        report = self.report()
        lines = [
            "Migration approval: " + ("READY" if report.approval_ready else "BLOCKED"),
        ]
        if report.blockers:
            lines.append("Blockers:")
            for blocker in report.blockers:
                lines.append(
                    f"  - {blocker.kind} in {blocker.space}: "
                    f"{format_wide(blocker.count)} codes ({blocker.detail})"
                )
        else:
            lines.append("Blockers: none")
        for space, counts in report.source_counts.items():
            rendered = ", ".join(
                f"{status}={format_wide(counts[status])}"
                for status in SOURCE_STATUS_ORDER
            )
            lines.append(f"Source {space}: {rendered}")
        for space, counts in report.target_counts.items():
            rendered = ", ".join(
                f"{status}={format_wide(counts[status])}"
                for status in TARGET_STATUS_ORDER
            )
            lines.append(f"Target {space}: {rendered}")
        if report.inert_rule_ids:
            lines.append(
                "Rules with no legal coverage (informational): "
                + ", ".join(report.inert_rule_ids)
            )
        lines.append(
            "Disabled source codes are not blockers; unused target codes are not blockers."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _require_space(self, kind: str, space: str) -> None:
        spaces = (
            self.spec.source_spaces
            if kind == "source"
            else self.spec.target_spaces
        )
        if space not in spaces:
            raise KeyError(f"unknown {kind} space {space!r}")


def _require_code(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"code must be an integer, got {value!r}")


def _require_range(lo: int, hi: int) -> None:
    _require_code(lo)
    _require_code(hi)
    if hi < lo:
        raise ValueError(f"inverted query range [{lo}, {hi}]")


def format_wide(value: int) -> str:
    """Group long integers for human display without changing their value."""

    return grouped_int(value)


def format_interval(lo: int, hi: int) -> str:
    if lo == hi:
        return f"[{format_wide(lo)}]"
    return f"[{format_wide(lo)}, {format_wide(hi)}]"


def _render_source_segment(segment: SourceSegment) -> str:
    head = format_interval(segment.lo, segment.hi)
    rules = ",".join(segment.rule_ids) if segment.rule_ids else "-"
    if segment.status == SRC_MAPPED:
        assert segment.target_space is not None
        if segment.shift is not None:
            transform = segment.shift.describe()
        else:
            transform = "point value agreed by all tied rules"
        return (
            f"{head}  rules={rules}  {transform}  -> "
            f"{segment.target_space} "
            f"{format_interval(segment.target_lo, segment.target_hi)}"
        )
    if segment.status == SRC_OUT_OF_RANGE:
        return (
            f"{head}  rules={rules}  would map to {segment.target_space} "
            f"{format_interval(segment.target_lo, segment.target_hi)}  INVALID "
            "(outside target domain; no fallback)"
        )
    if segment.status == SRC_DISABLED:
        return f"{head}  rules={rules}  retired (not a failure)"
    if segment.status == SRC_CONFLICT:
        return f"{head}  rules={rules}  conflict: {segment.reason}"
    return f"{head}  {segment.reason}"


def _render_target_segment(segment: TargetSegment) -> str:
    head = format_interval(segment.lo, segment.hi)
    if segment.status == TGT_UNUSED:
        return f"{head}  no legal preimage"
    if segment.status == TGT_UNIQUE:
        contributor = segment.contributors[0]
        rules = ",".join(contributor.rule_ids)
        if contributor.shift is not None:
            inverse = contributor.shift.inverse_describe()
        else:
            inverse = f"old = {format_wide(contributor.source_lo)}"
        return (
            f"{head}  rules={rules}  from {contributor.source_space} "
            f"{format_interval(contributor.source_lo, contributor.source_hi)}  "
            f"inverse: {inverse}"
        )
    witness = "; ".join(
        f"({item.source_space}, {format_wide(item.code)}) via {','.join(item.rule_ids)}"
        for item in segment.witness
    )
    return (
        f"{head}  {len(segment.contributors)} preimages  witness at "
        f"{format_wide(segment.lo)}: {witness}"
    )


# ----------------------------------------------------------------------
# Machine-readable forms (used by the CLI)
# ----------------------------------------------------------------------
def source_segment_dict(segment: SourceSegment) -> dict:
    return {
        "space": segment.space,
        "lo": segment.lo,
        "hi": segment.hi,
        "width": segment.width,
        "status": segment.status,
        "rule_ids": list(segment.rule_ids),
        "priority": segment.priority,
        "target_space": segment.target_space,
        "target_lo": segment.target_lo,
        "target_hi": segment.target_hi,
        "shift": (
            {
                "direction": segment.shift.direction,
                "offset": segment.shift.offset,
                "describe": segment.shift.describe(),
            }
            if segment.shift is not None
            else None
        ),
        "reason": segment.reason,
    }


def target_segment_dict(segment: TargetSegment) -> dict:
    return {
        "space": segment.space,
        "lo": segment.lo,
        "hi": segment.hi,
        "width": segment.width,
        "status": segment.status,
        "contributors": [
            {
                "source_space": contributor.source_space,
                "source_lo": contributor.source_lo,
                "source_hi": contributor.source_hi,
                "rule_ids": list(contributor.rule_ids),
                "shift": (
                    {
                        "direction": contributor.shift.direction,
                        "offset": contributor.shift.offset,
                    }
                    if contributor.shift is not None
                    else None
                ),
            }
            for contributor in segment.contributors
        ],
        "witness": [
            {
                "source_space": item.source_space,
                "code": item.code,
                "rule_ids": list(item.rule_ids),
            }
            for item in segment.witness
        ],
    }


def preimage_dict(item: Preimage) -> dict:
    return {
        "source_space": item.source_space,
        "code": item.code,
        "rule_ids": list(item.rule_ids),
    }


def source_hit_dict(hit: SourceHit) -> dict:
    return {
        "space": hit.space,
        "code": hit.code,
        "status": hit.status,
        "rule_ids": list(hit.rule_ids),
        "priority": hit.priority,
        "target_space": hit.target_space,
        "target": hit.target,
        "target_in_domain": hit.target_in_domain,
        "reason": hit.reason,
    }


def target_hit_dict(hit: TargetHit) -> dict:
    return {
        "space": hit.space,
        "code": hit.code,
        "status": hit.status,
        "segment_lo": hit.segment_lo,
        "segment_hi": hit.segment_hi,
        "preimages": [preimage_dict(item) for item in hit.preimages],
        "witness": [preimage_dict(item) for item in hit.witness],
    }


def report_dict(report: MigrationReport) -> dict:
    return {
        "approval_ready": report.approval_ready,
        "blockers": [
            {
                "kind": blocker.kind,
                "space": blocker.space,
                "count": blocker.count,
                "detail": blocker.detail,
            }
            for blocker in report.blockers
        ],
        "source_counts": report.source_counts,
        "target_counts": report.target_counts,
        "source_widths": report.source_widths,
        "target_widths": report.target_widths,
        "inert_rule_ids": list(report.inert_rule_ids),
    }

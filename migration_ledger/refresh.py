"""Localised balance refresh between two catalog versions.

When a batch changes rules, facts, or both, only a small set of balance rows can
possibly change.  This module computes that set *symbolically* by diffing the
old and new catalogues over their declared interval domains (no closed interval
is expanded into individual codes), then asks the SQLite layer for exactly the
facts in the affected ranges.  Facts outside every affected range are never read.

A target ``new code`` gets a balance row iff, in the *end-of-batch* catalogue:

1. the contributing old code's winning source status is ``mapped`` (a valid
   mapping inside the target legal domain), and
2. the target code's reverse status is ``unique`` (exactly one legal old-code
   source across every declared source space; whether another source actually
   has a fact is irrelevant to uniqueness).

Disabled, missing, conflicting, out-of-range and ambiguous positions are simply
absent from the balance table; facts stored there remain in the ledger and are
reconsidered on every later refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from migration_catalog import Catalog
from migration_catalog.core import (
    SRC_MAPPED,
    TGT_UNIQUE,
)
from migration_catalog.intervals import Interval, normalize


@dataclass
class RefreshStats:
    """Instrumentation proving the refresh stayed local.

    ``fact_rows_touched`` counts facts actually fetched from SQLite.  In a
    ledger holding many years of facts, a correction touching one code segment
    must keep this far below the total fact count.
    """

    facts_by_range: int = 0
    facts_by_point: int = 0
    named_lookups: int = 0
    balance_rows_read: int = 0
    candidates: int = 0
    affected_source_pieces: list[tuple[str, int, int]] = field(default_factory=list)
    affected_target_pieces: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def fact_rows_touched(self) -> int:
        return self.facts_by_range + self.facts_by_point

    def as_dict(self) -> dict:
        return {
            "facts_by_range": self.facts_by_range,
            "facts_by_point": self.facts_by_point,
            "fact_rows_touched": self.fact_rows_touched,
            "named_lookups": self.named_lookups,
            "balance_rows_read": self.balance_rows_read,
            "candidates": self.candidates,
            "affected_source_pieces": [
                {"space": s, "lo": lo, "hi": hi}
                for s, lo, hi in self.affected_source_pieces
            ],
            "affected_target_pieces": [
                {"space": s, "lo": lo, "hi": hi}
                for s, lo, hi in self.affected_target_pieces
            ],
        }


# ----------------------------------------------------------------------
# Symbolic catalog diffs
# ----------------------------------------------------------------------
def _source_signature(catalog: Catalog, space: str, code: int) -> tuple:
    """What effectively happens to one legal source code in a catalogue."""

    hit = catalog.source_lookup(space, code)
    if hit.status != SRC_MAPPED:
        return (hit.status,)
    return (SRC_MAPPED, hit.target_space, hit.target)


def _target_signature(catalog: Catalog, space: str, code: int) -> tuple:
    """Reverse-classification signature of one legal target code."""

    hit = catalog.target_lookup(space, code)
    contributors = tuple(
        (
            item.source_space,
            item.code,
            tuple(item.rule_ids),
        )
        for item in hit.preimages
    )
    return (hit.status, contributors)


def _diff_cells(
    old_catalog: Catalog,
    new_catalog: Catalog,
    spaces: list[str],
    domain_of,
    signature,
    segments_of,
) -> dict[str, list[Interval]]:
    """Return, per space, the maximal cells whose signature changed.

    Both catalogues share the same declared domains, so a cell cut by either
    catalogue's segment boundaries has a constant classification in both.

    Segment boundaries alone are not enough on the target side: a ``unique``
    target segment covers a whole source interval, and the *identity* of its
    preimage changes from code to code even though the segment is contiguous.
    Every contributor's source-interval endpoints are therefore projected into
    target space and added as cuts too.  This stays symbolic (endpoint count,
    never interval width) and keeps correctness for integers beyond 64 bits.
    """

    changed: dict[str, list[Interval]] = {}
    for space in spaces:
        pieces = list(domain_of(space))
        cuts: set[int] = set()
        for piece in pieces:
            cuts.add(piece.lo)
            cuts.add(piece.hi + 1)
        for catalog in (old_catalog, new_catalog):
            for segment in segments_of(catalog, space):
                cuts.add(segment.lo)
                cuts.add(segment.hi + 1)
                for contributor in getattr(segment, "contributors", ()):
                    # Project both source endpoints into this target space.
                    cuts.add(contributor.target_lo)
                    cuts.add(contributor.target_hi + 1)

        # A contiguous unique target segment can still change preimage
        # identity point by point (both versions use affine bijections but
        # with different shifts/ranges).  Add, for every contributor on both
        # sides, the target images of *all* contributor source endpoints under
        # each catalogue's own forward mapping, so two differently-mapped
        # source endpoints can never share one diff cell.  Endpoints only; no
        # closed interval is expanded.
        all_contributors = []
        for catalog in (old_catalog, new_catalog):
            for segment in segments_of(catalog, space):
                all_contributors.extend(getattr(segment, "contributors", ()))
        source_endpoints = {
            (contributor.source_space, point)
            for contributor in all_contributors
            for point in (contributor.source_lo, contributor.source_hi)
        }
        for catalog in (old_catalog, new_catalog):
            for source_space, point in source_endpoints:
                hit = catalog.source_lookup(source_space, point)
                if hit.status == SRC_MAPPED and hit.target_space == space:
                    cuts.add(hit.target)
                    cuts.add(hit.target + 1)

        cells: list[Interval] = []
        ordered = sorted(cuts)
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            code = left
            if not any(code in piece for piece in pieces):
                continue
            if signature(old_catalog, space, code) != signature(new_catalog, space, code):
                cells.append(Interval(left, right - 1))
        if cells:
            changed[space] = list(normalize(cells))
    return changed


def diff_source_spaces(old: Catalog, new: Catalog) -> dict[str, list[Interval]]:
    """Legal source cells whose effective mapping outcome changed."""

    return _diff_cells(
        old,
        new,
        list(new.spec.source_spaces),
        lambda space: new.spec.source_spaces[space],
        _source_signature,
        lambda catalog, space: catalog.source_segments(space),
    )


def diff_target_spaces(old: Catalog, new: Catalog) -> dict[str, list[Interval]]:
    """Legal target cells whose reverse classification changed."""

    return _diff_cells(
        old,
        new,
        list(new.spec.target_spaces),
        lambda space: new.spec.target_spaces[space],
        _target_signature,
        lambda catalog, space: catalog.target_segments(space),
    )


# ----------------------------------------------------------------------
# Refresh
# ----------------------------------------------------------------------
def refresh_balances(
    db,
    old_catalog: Catalog,
    new_catalog: Catalog,
    named_fact_ids: set[str],
    prior_positions: set[tuple[str, int]],
    rules_changed: bool,
    seq: int,
) -> RefreshStats:
    """Recompute exactly the balance rows affected by one committed batch.

    ``db`` is the storage adapter exposing the fact/balance range queries; the
    catalogue objects describe the state before and after the batch.  All work
    runs inside the caller's open transaction.  ``seq`` is the batch being
    committed (its batch row is inserted only after this refresh).

    ``prior_positions`` holds the source positions the named facts occupied
    before the batch's writes/deletes, so a moved or deleted fact's old balance
    row is recomputed (and removed when it was the last contributor).

    The candidate set is made of *source positions* ``(old_space, old_code)``;
    target-space keys are translated back through the catalogues' preimages.
    """

    stats = RefreshStats()
    source_candidates: set[tuple[str, int]] = set(prior_positions)
    range_fetched: set[tuple[str, int]] = set()

    def add_range(space: str, lo: int, hi: int) -> None:
        for old_code in db.facts_in_range(space, lo, hi):
            range_fetched.add((space, old_code))
            source_candidates.add((space, old_code))

    # (1) Every fact the batch explicitly names is evaluated at batch end.
    for fact_id in named_fact_ids:
        stats.named_lookups += 1
        row = db.get_fact_by_id(fact_id)
        if row is not None:
            source_candidates.add((row["old_space"], row["old_code"]))

    if rules_changed:
        source_diff = diff_source_spaces(old_catalog, new_catalog)
        target_diff = diff_target_spaces(old_catalog, new_catalog)

        # (2) Facts whose own source position changed classification.
        for space, pieces in source_diff.items():
            for piece in pieces:
                stats.affected_source_pieces.append((space, piece.lo, piece.hi))
                add_range(space, piece.lo, piece.hi)

        # (3) Target-side changes: a uniqueness flip can hide or reveal rows
        #     even when the contributing source signature itself is unchanged.
        #     Each target segment's contributors cover whole source intervals;
        #     collect those intervals (in both catalogues) and the preimages of
        #     existing balance rows, then fetch facts by indexed range/point.
        affected_source_intervals: set[tuple[str, int, int]] = set()
        for target_space, pieces in target_diff.items():
            for piece in pieces:
                stats.affected_target_pieces.append((target_space, piece.lo, piece.hi))
                for row in db.balances_in_range(target_space, piece.lo, piece.hi):
                    stats.balance_rows_read += 1
                    new_status = new_catalog.target_lookup(
                        target_space, row["new_code"]
                    ).status
                    if new_status != TGT_UNIQUE:
                        # The row's target flipped to ambiguous/unused (its only
                        # source was disabled, went missing, conflicting, out of
                        # range, or gained a competitor): drop it immediately.
                        db.delete_balance(target_space, row["new_code"])
                        continue
                    for preimage in new_catalog.target_lookup(
                        target_space, row["new_code"]
                    ).preimages:
                        source_candidates.add(
                            (preimage.source_space, preimage.code)
                        )
                for catalog in (old_catalog, new_catalog):
                    for segment in catalog.target_segments(
                        target_space, piece.lo, piece.hi
                    ):
                        for contributor in segment.contributors:
                            affected_source_intervals.add(
                                (
                                    contributor.source_space,
                                    contributor.source_lo,
                                    contributor.source_hi,
                                )
                            )
        for space, lo, hi in sorted(affected_source_intervals):
            add_range(space, lo, hi)

    # (4) Recompute every candidate from the end-of-batch catalogue and facts.
    stats.facts_by_range = len(range_fetched)
    stats.candidates = len(source_candidates)
    for old_space, old_code in sorted(source_candidates):
        _recompute_one(db, new_catalog, old_space, old_code, stats, seq, range_fetched)
    return stats


def _recompute_one(
    db,
    catalog: Catalog,
    old_space: str,
    old_code: int,
    stats: RefreshStats,
    seq: int,
    range_fetched: set[tuple[str, int]],
) -> None:
    hit = catalog.source_lookup(old_space, old_code)
    if hit.status != SRC_MAPPED or hit.target_space is None or hit.target is None:
        # No valid mapping at batch end.  If this position used to contribute to
        # some balance row, that row is deleted through the target-side diff
        # path (its preimage set changed), so nothing to do here.
        return

    target_space, new_code = hit.target_space, hit.target
    reverse = catalog.target_lookup(target_space, new_code)
    if reverse.status != TGT_UNIQUE:
        db.delete_balance(target_space, new_code)
        return

    preimage = reverse.preimages[0]
    position = (preimage.source_space, preimage.code)
    facts = db.facts_at(preimage.source_space, preimage.code)
    # Facts already pulled by an indexed range scan are not double counted;
    # everything else is fetched via the point index.
    stats.facts_by_point += 0 if position in range_fetched else len(facts)
    if not facts:
        # Uniqueness is computed on declared domains regardless of bookings,
        # but a row with zero contributing facts is not a balance row.
        db.delete_balance(target_space, new_code)
        return

    amount_sum = sum(fact["amount"] for fact in facts)
    db.upsert_balance(
        target_space=target_space,
        new_code=new_code,
        fact_count=len(facts),
        amount_sum=amount_sum,
        updated_seq=seq,
    )

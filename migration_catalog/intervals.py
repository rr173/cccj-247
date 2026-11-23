"""Closed integer intervals and interval-set algebra.

Every "code interval" in this project is a *closed* interval of Python
integers: ``[lo, hi]`` contains both endpoints.  Python integers are
arbitrary precision, so nothing here is limited to 32 or 64 bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, order=True)
class Interval:
    """A non-empty closed interval ``[lo, hi]``."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if isinstance(self.lo, bool) or isinstance(self.hi, bool):
            raise TypeError("interval endpoints must be integers, not bool")
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise TypeError("interval endpoints must be integers")
        if self.hi < self.lo:
            raise ValueError(f"empty/inverted interval [{self.lo}, {self.hi}]")

    @property
    def width(self) -> int:
        """Number of integer codes in the interval."""
        return self.hi - self.lo + 1

    def __contains__(self, value: int) -> bool:
        return self.lo <= value <= self.hi

    def overlaps(self, other: "Interval") -> bool:
        return self.lo <= other.hi and other.lo <= self.hi

    def intersection(self, other: "Interval") -> "Interval | None":
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        return Interval(lo, hi) if lo <= hi else None

    def shift(self, delta: int) -> "Interval":
        return Interval(self.lo + delta, self.hi + delta)


def normalize(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Sort, merge overlapping intervals, and coalesce adjacent intervals.

    Adjacent closed integer intervals such as ``[0, 3]`` and ``[4, 7]`` are
    merged because their union is the contiguous integer interval ``[0, 7]``.
    """

    ordered = sorted(intervals)
    if not ordered:
        return ()
    result: list[Interval] = [ordered[0]]
    for current in ordered[1:]:
        previous = result[-1]
        if current.lo <= previous.hi + 1:
            result[-1] = Interval(previous.lo, max(previous.hi, current.hi))
        else:
            result.append(current)
    return tuple(result)


def intersect_pieces(base: Interval, domain: Iterable[Interval]) -> tuple[Interval, ...]:
    """Intersect ``base`` with a normalized interval set, returning pieces."""

    pieces: list[Interval] = []
    for part in normalize(domain):
        overlap = base.intersection(part)
        if overlap is not None:
            pieces.append(overlap)
    return tuple(pieces)


def subtract(base: Interval, holes: Iterable[Interval]) -> tuple[Interval, ...]:
    """Return ``base`` minus a set of intervals, as normalized pieces."""

    result: list[Interval] = []
    cursor = base.lo
    for hole in normalize(holes):
        if hole.hi < cursor:
            continue
        if hole.lo > base.hi:
            break
        if hole.lo > cursor:
            result.append(Interval(cursor, min(hole.lo - 1, base.hi)))
        cursor = max(cursor, hole.hi + 1)
        if cursor > base.hi:
            break
    if cursor <= base.hi:
        result.append(Interval(cursor, base.hi))
    return tuple(result)


def total_width(intervals: Iterable[Interval]) -> int:
    """Total number of integer codes in a normalized interval set."""

    return sum(part.width for part in intervals)


def plain_int(value: int) -> str:
    """Decimal string for any Python integer.

    CPython limits ``str(int)`` to a few thousand digits by default.  Code
    domains are data, not an attack surface, so convert in chunks instead of
    temporarily disabling that safety limit.
    """

    if not isinstance(value, int):
        raise TypeError(f"expected int, got {value!r}")
    if -10**18 < value < 10**18:
        return str(value)
    sign = "-" if value < 0 else ""
    remaining = abs(value)
    chunks: list[int] = []
    while remaining:
        remaining, chunk = divmod(remaining, 10**18)
        chunks.append(chunk)
    parts = [str(chunks[-1])]
    parts.extend(f"{chunk:018d}" for chunk in reversed(chunks[:-1]))
    return sign + "".join(parts)


def grouped_int(value: int) -> str:
    """Like :func:`plain_int`, with thousands separators for readability."""

    text = plain_int(value)
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    groups: list[str] = []
    while len(text) > 3:
        groups.append(text[-3:])
        text = text[:-3]
    groups.append(text)
    return sign + ",".join(reversed(groups))

"""Sortable text keys for arbitrary-precision integer codes.

SQLite stores code values as TEXT but range queries ("all facts whose code lies
in closed interval [lo, hi]") need the ordering to follow numeric order, not
lexicographic decimal order, and codes may be wider than 64 bits.

The key format below is a fixed-shape decimal string that sorts bytewise in
exact numeric order, for *signed* integers of any Python-int magnitude:

* positive ``v`` -> ``"P" + nines_complement("L" + digits_of_len + digits)``
  with no complement needed (the whole tail is just digits/``L``)
* negative ``v`` -> ``"N" + 9 - each_digit(tail)`` where the tail is the same
  shape built from ``abs(v)``

The first letter separates the signs (``N`` < ``P``), and inside each sign
group equal-shape tails are compared numerically: positive tails are plain
magnitude strings, negative tails are 9's complements so that a *larger*
magnitude (a more negative number) sorts earlier.

Keys are opaque outside this module; only :func:`encode_key` / :func:`decode_key`
need to agree on the layout.  No closed interval is ever expanded into its
individual codes.
"""

from __future__ import annotations

_DIGITS = "0123456789"
_COMPLEMENT = str.maketrans(_DIGITS, "9876543210")
_LEN_MARK = "L"  # inside a tail; its complement is still a decimal-free marker


def _magnitude_tail(value: int) -> str:
    digits = str(value)
    # Length-of-length prefix, then the length itself, then the digits.  Every
    # integer with the same digit count now has the same prefix shape.
    return f"{_LEN_MARK}{len(str(len(digits)))}{len(digits)}{digits}"


def encode_key(value: int) -> str:
    """Return a TEXT key whose byte order matches numeric order of ``value``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"code must be an integer, got {value!r}")
    if value >= 0:
        return "P" + _magnitude_tail(value)
    tail = _magnitude_tail(-value)
    # Complement digits only; the length marker keeps its own shape (it is not a
    # decimal digit in either side and marker positions align in every tail).
    complemented = tail.translate(_COMPLEMENT)
    return "N" + complemented


def decode_key(key: str) -> int:
    """Inverse of :func:`encode_key` (mainly used by tests and diagnostics)."""

    if not isinstance(key, str) or not key:
        raise ValueError("not an encoded integer key")
    sign, tail = key[0], key[1:]
    if sign == "P":
        magnitude = _read_tail(tail)
        return magnitude
    if sign == "N":
        magnitude = _read_tail(tail.translate(_COMPLEMENT))
        return -magnitude
    raise ValueError("unknown key sign marker")


def _read_tail(tail: str) -> int:
    if not tail.startswith(_LEN_MARK):
        raise ValueError("malformed key tail")
    cursor = 1
    width_width = int(tail[cursor])
    cursor += 1
    width = int(tail[cursor : cursor + width_width])
    cursor += width_width
    digits = tail[cursor : cursor + width]
    if len(digits) != width:
        raise ValueError("truncated key tail")
    return int(digits)

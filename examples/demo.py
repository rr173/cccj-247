#!/usr/bin/env python3
"""Runnable walkthrough: boundaries, inverse lookups, wide integers, and the
effect of deleting one high-priority rule.

Run from the repository root:

    python3 examples/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_catalog import Catalog, spec_from_dict  # noqa: E402
from migration_catalog.core import format_interval  # noqa: E402


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def load(name: str) -> Catalog:
    with open(ROOT / "examples" / name, encoding="utf-8") as handle:
        return Catalog(spec_from_dict(json.load(handle)))


def boundary_example() -> None:
    heading("1. Boundary toy: closed intervals, negatives, a one-code meeting")
    catalog = load("tiny.json")
    print("source domain pieces:", catalog.spec.source_spaces["s"])
    print(catalog.render_source("s"))
    print()
    print(catalog.render_target("t"))
    print()
    print(catalog.render_report())


def shift_round_trip() -> None:
    heading("2. Forward and reverse are exact in both directions")
    catalog = load("example.json")
    for old in (0, 300, 850, 900):
        forward = catalog.source_lookup("legacy", old)
        print(
            f"old legacy:{old} -> {forward.target_space}:{forward.target} "
            f"via {','.join(forward.rule_ids)} ({forward.status})"
        )
        if forward.status == "mapped":
            back = catalog.target_lookup(forward.target_space, forward.target)
            positions = ", ".join(
                f"{item.source_space}:{item.code}" for item in back.preimages
            )
            print(f"    reverse target -> {positions} ({back.status})")


def wide_example() -> None:
    heading("3. Ranges far wider than 32/64 bits stay symbolic")
    catalog = load("example.json")
    segments = catalog.source_segments("legacy", 10**30, 10**30 + 10**21)
    for segment in segments:
        print(
            f"source {format_interval(segment.lo, segment.hi)} "
            f"width={segment.width:,} rules={','.join(segment.rule_ids)} "
            f"-> {segment.target_space} "
            f"{format_interval(segment.target_lo, segment.target_hi)}"
        )
    old = 10**30 + 7
    forward = catalog.source_lookup("legacy", old)
    reverse = catalog.target_lookup(forward.target_space, forward.target)
    print(f"old={old} -> new={forward.target} -> old={reverse.preimages[0].code}")
    print("no code pair in that range was ever materialised")


def rule_removal() -> None:
    heading("4. Remove one high-priority rule: a unique target range turns ambiguous")
    base = {
        "source_spaces": {"s": [[0, 19]]},
        "target_spaces": {"t": [[200, 420]]},
        "rules": [
            {
                "id": "main",
                "source_space": "s",
                "source_range": [0, 9],
                "priority": 10,
                "target_space": "t",
                "direction": 1,
                "offset": 200,
            },
            {
                "id": "shadow",
                "source_space": "s",
                "source_range": [10, 19],
                "priority": 50,
                "target_space": "t",
                "direction": 1,
                "offset": 400,
            },
            {
                "id": "other",
                "source_space": "s",
                "source_range": [10, 19],
                "priority": 20,
                "target_space": "t",
                "direction": 1,
                "offset": 190,
            },
        ],
    }
    with_shadow = Catalog(spec_from_dict(base))
    print("WITH shadow rule:")
    print(with_shadow.render_target("t"))
    print()
    print(
        "t 200..209 has one preimage each: old 0..9 via main. "
        "shadow sends old 10..19 to 410..419, so other never wins."
    )

    reduced = dict(base)
    reduced["rules"] = [r for r in base["rules"] if r["id"] != "shadow"]
    without_shadow = Catalog(spec_from_dict(reduced))
    print()
    print("WITHOUT shadow rule:")
    print(without_shadow.render_target("t"))
    print()
    print("Why it becomes ambiguous:")
    print("  old=0 -> 200 and old=10 -> 200 under 'other'")
    print("  old=1 -> 201 and old=11 -> 201 ... old=9 -> 209 and old=19 -> 209")
    print("  so every t in 200..209 now has two legal preimages.")
    print()
    for code in (200, 205, 209):
        hit = without_shadow.target_lookup("t", code)
        positions = ", ".join(
            f"{item.source_space}:{item.code}" for item in hit.preimages
        )
        print(f"  t:{code} -> {positions} ({hit.status})")


def main() -> None:
    boundary_example()
    shift_round_trip()
    wide_example()
    rule_removal()
    print()


if __name__ == "__main__":
    main()

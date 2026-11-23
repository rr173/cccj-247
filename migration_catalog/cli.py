"""Command-line front end for the migration catalogue.

All output is JSON on stdout so it can be piped into review tooling.  No code
pairs are materialised: ranges are reported as ``lo``/``hi`` pairs, including
ranges far wider than 64 bits.
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import Catalog, report_dict, source_hit_dict, source_segment_dict, target_hit_dict, target_segment_dict
from .model import load_spec


def _emit(value) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migration-catalog",
        description=(
            "Validate a declared integer code migration, render source-side "
            "segments and reverse target segments, and answer point queries."
        ),
    )
    parser.add_argument("spec", help="path to the migration spec JSON file")
    sub = parser.add_subparsers(dest="command", required=True)

    source = sub.add_parser("source", help="source-side segment catalogue")
    source.add_argument("--space", required=True)
    source.add_argument("--lo", type=int)
    source.add_argument("--hi", type=int)
    source.add_argument("--text", action="store_true", help="render human text")

    target = sub.add_parser("target", help="reverse target segment catalogue")
    target.add_argument("--space", required=True)
    target.add_argument("--lo", type=int)
    target.add_argument("--hi", type=int)
    target.add_argument("--text", action="store_true", help="render human text")

    source_point = sub.add_parser("source-point", help="classify one old code")
    source_point.add_argument("--space", required=True)
    source_point.add_argument("--code", type=int, required=True)

    target_point = sub.add_parser("target-point", help="reverse one new code")
    target_point.add_argument("--space", required=True)
    target_point.add_argument("--code", type=int, required=True)

    target_preimages = sub.add_parser(
        "target-preimages", help="all legal old codes for one new code"
    )
    target_preimages.add_argument("--space", required=True)
    target_preimages.add_argument("--code", type=int, required=True)

    sub.add_parser("report", help="approval blockers and coverage totals")

    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
        catalog = Catalog(spec)
        if args.command == "source":
            if args.text:
                print(catalog.render_source(args.space))
                return 0
            segments = catalog.source_segments(args.space, args.lo, args.hi)
            _emit([source_segment_dict(segment) for segment in segments])
        elif args.command == "target":
            if args.text:
                print(catalog.render_target(args.space))
                return 0
            segments = catalog.target_segments(args.space, args.lo, args.hi)
            _emit([target_segment_dict(segment) for segment in segments])
        elif args.command == "source-point":
            _emit(source_hit_dict(catalog.source_lookup(args.space, args.code)))
        elif args.command == "target-point":
            _emit(target_hit_dict(catalog.target_lookup(args.space, args.code)))
        elif args.command == "target-preimages":
            hit = catalog.target_lookup(args.space, args.code)
            _emit(
                {
                    "space": hit.space,
                    "code": hit.code,
                    "status": hit.status,
                    "preimages": [
                        {
                            "source_space": item.source_space,
                            "code": item.code,
                            "rule_ids": list(item.rule_ids),
                        }
                        for item in hit.preimages
                    ],
                }
            )
        elif args.command == "report":
            _emit(report_dict(catalog.report()))
        else:  # pragma: no cover - argparse guarantees a command
            parser.error("unknown command")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

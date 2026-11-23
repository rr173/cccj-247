"""Command-line front end for the rekey rollout planner.

Reads the candidate catalogue (an existing migration spec) and the
warehouse key manifest, and writes the rollout plan as JSON on stdout.
Nothing but the temporary work directory is ever created or modified;
manifest errors print on stderr and exit with code 2 without a plan.
"""

from __future__ import annotations

import argparse
import sys

from migration_catalog import Catalog, load_spec

from .errors import PlanError
from .plan import Planner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migration-plan",
        description=(
            "Build a rekey rollout plan from the candidate catalogue and the "
            "warehouse key manifest: which release units can publish, in "
            "which atomic batches, and which must be remediated first.  "
            "Reads only; never modifies the warehouse or the ledger."
        ),
    )
    parser.add_argument("spec", help="candidate catalogue: migration spec JSON")
    parser.add_argument("manifest", help="warehouse key manifest (JSON lines)")
    parser.add_argument(
        "--k",
        type=int,
        required=True,
        help="maximum number of business units in one atomic batch",
    )
    parser.add_argument(
        "--work-dir",
        help="directory for temporary working files (default: system temp)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = Catalog(load_spec(args.spec))
        with Planner(catalog, args.k, work_dir=args.work_dir) as planner:
            planner.load(args.manifest)
            planner.plan()
            planner.emit(
                sys.stdout,
                {
                    "spec": args.spec,
                    "manifest": args.manifest,
                    "manifest_sha256": planner.manifest_sha256,
                },
            )
    except (OSError, ValueError, TypeError, KeyError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

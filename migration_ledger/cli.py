"""Command-line front end for the SQLite ledger.

All output is JSON on stdout; rejected batches print an error on stderr and
exit with code 2 without changing any committed state.
"""

from __future__ import annotations

import argparse
import json
import sys

from migration_catalog import load_spec

from .ledger import Ledger, LedgerError


def _emit(value) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migration-ledger",
        description="Re-openable SQLite ledger of facts, rules and balances.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialise an empty ledger from a catalogue spec")
    init.add_argument("db", help="SQLite database path (must not exist)")
    init.add_argument("spec", help="migration catalogue spec JSON")

    submit = sub.add_parser("submit", help="commit one JSON batch file atomically")
    submit.add_argument("db")
    submit.add_argument("batch", help="batch JSON file (exact bytes are hashed)")

    balance = sub.add_parser("balance", help="read the current balance table")
    balance.add_argument("db")
    balance.add_argument("--space", help="target space; omit for all spaces")
    balance.add_argument("--code", type=int, help="one new code (requires --space)")

    head = sub.add_parser("head", help="latest committed batch number and rule version")
    head.add_argument("db")

    facts = sub.add_parser("facts", help="list current content of stored facts")
    facts.add_argument("db")
    facts.add_argument("--id", dest="fact_id", help="one business fact id")

    seqs = sub.add_parser("seqs", help="all committed batch numbers in order")
    seqs.add_argument("db")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            spec = load_spec(args.spec)
            ledger = Ledger.initialize(args.db, spec)
            ledger.close()
            _emit({"database": args.db, "schema_version": 1, "status": "initialised"})
            return 0

        if args.command == "submit":
            with Ledger.open(args.db) as ledger:
                result = ledger.submit_file(args.batch)
            payload = {
                "seq": result.seq,
                "status": result.status,
                "rule_version": result.rule_version,
                "rules_replaced": result.rules_replaced,
            }
            if result.refresh is not None:
                payload["refresh"] = result.refresh.as_dict()
            _emit(payload)
            return 0

        with Ledger.open(args.db) as ledger:
            if args.command == "balance":
                if args.code is not None:
                    if not args.space:
                        raise LedgerError("--code requires --space")
                    row = ledger.get_balance(args.space, args.code)
                    _emit(row)  # null means no contributing balance row
                else:
                    _emit(ledger.list_balances(args.space))
            elif args.command == "head":
                _emit(
                    {
                        "head_seq": ledger.head_seq(),
                        "rule_version": ledger.current_rule_version(),
                    }
                )
            elif args.command == "facts":
                if args.fact_id:
                    _emit(ledger.get_fact(args.fact_id))
                else:
                    rows = [
                        ledger.get_fact(row[0])
                        for row in ledger.conn.execute(
                            "SELECT fact_id FROM fact ORDER BY fact_id"
                        )
                    ]
                    _emit(rows)
            elif args.command == "seqs":
                _emit(ledger.list_committed_seqs())
            else:  # pragma: no cover - argparse guarantees a command
                parser.error("unknown command")
    except (OSError, ValueError, TypeError, KeyError, LedgerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

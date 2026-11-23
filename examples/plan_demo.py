#!/usr/bin/env python3
"""Runnable walkthrough of the rekey rollout planner.

Writes a small candidate catalogue and key manifest under
``examples/plan_run/``, runs the real local command
(``python3 -I bin/migration-plan``) twice with different batch limits, and
explains how to read the publishable batches and the blocked units.

Run from the repository root:

    python3 examples/plan_demo.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "plan_run"

WIDE_BASE = 10**30
WIDE_TARGET = 10**40

SPEC = {
    "description": (
        "Rollout planner demo: one main shift, an aliasing second space, "
        "negative codes, and codes far beyond 64 bits."
    ),
    "source_spaces": {
        "legacy": [[0, 99]],
        "legacy2": [[0, 5]],
        "legacy_neg": [[-10, -1]],
        "legacy_wide": [[WIDE_BASE, WIDE_BASE + 9]],
    },
    "target_spaces": {
        "modern": [[1000, 1199]],
        "modern_neg": [[-1010, -1001]],
        "modern_wide": [[WIDE_TARGET, WIDE_TARGET + 9]],
    },
    "rules": [
        {
            "id": "legacy-shift",
            "source_space": "legacy",
            "source_range": [0, 90],
            "priority": 10,
            "target_space": "modern",
            "direction": 1,
            "offset": 1000,
        },
        {
            # Only old code 3 maps anywhere, landing on modern:1003, which
            # legacy-shift also reaches: exactly one ambiguous target.
            "id": "legacy2-shift",
            "source_space": "legacy2",
            "source_range": [3, 3],
            "priority": 10,
            "target_space": "modern",
            "direction": 1,
            "offset": 1000,
        },
        {
            "id": "neg-shift",
            "source_space": "legacy_neg",
            "source_range": [-10, -1],
            "priority": 10,
            "target_space": "modern_neg",
            "direction": 1,
            "offset": -1000,
        },
        {
            "id": "wide-shift",
            "source_space": "legacy_wide",
            "source_range": [WIDE_BASE, WIDE_BASE + 9],
            "priority": 10,
            "target_space": "modern_wide",
            "direction": 1,
            "offset": WIDE_TARGET - WIDE_BASE,
        },
    ],
}

# (id, current key, unit, old key); unit/old None -> reserved placeholder.
ROWS = [
    # alpha: A1 already sits on its target (kept as-is), A2 really moves.
    ("A1", ("modern", "1001"), "alpha", ("legacy", "1")),
    ("A2", ("modern", "7"), "alpha", ("legacy", "2")),
    # beta wants modern:1005, currently held by gamma's moving record G1.
    ("B1", ("modern", "60"), "beta", ("legacy", "5")),
    ("G1", ("modern", "1005"), "gamma", ("legacy", "8")),
    # delta and epsilon swap keys: only publishable in the same batch.
    ("D1", ("modern", "1010"), "delta", ("legacy", "11")),
    ("E1", ("modern", "1011"), "epsilon", ("legacy", "10")),
    # eta wants modern:1020, pinned by reserved placeholder KEEP-1.
    ("H1", ("modern", "1021"), "eta", ("legacy", "20")),
    # theta and iota both land on modern:1030: contested, both exit.
    ("T1", ("modern", "31"), "theta", ("legacy", "30")),
    ("I1", ("modern", "32"), "iota", ("legacy", "30")),
    # kappa wants modern:1021, held by eta's record, and eta cannot publish.
    ("K1", ("modern", "81"), "kappa", ("legacy", "21")),
    # mu changes nothing but still gets its own result line.
    ("M1", ("modern", "1040"), "mu", ("legacy", "40")),
    # nu: negative codes; omicron: codes far beyond 64 bits.
    ("N1", ("modern", "90"), "nu", ("legacy_neg", "-5")),
    ("O1", ("modern", "91"), "omicron", ("legacy_wide", str(WIDE_BASE + 3))),
    # pi: old code 95 has no rule (missing); zeta: target 1003 is ambiguous.
    ("P1", ("modern", "95"), "pi", ("legacy", "95")),
    ("Z1", ("modern", "96"), "zeta", ("legacy2", "3")),
    # Reserved placeholders: occupy keys for old reports, never move.
    ("KEEP-1", ("modern", "1020"), None, None),
    ("KEEP-2", ("modern", "1099"), None, None),
]


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def write_inputs() -> tuple[Path, Path]:
    if RUN.exists():
        shutil.rmtree(RUN)
    RUN.mkdir(parents=True)
    spec_path = RUN / "spec.json"
    manifest_path = RUN / "manifest.jsonl"
    spec_path.write_text(json.dumps(SPEC, indent=2) + "\n", encoding="utf-8")
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record_id, current, unit, old in ROWS:
            row = {
                "id": record_id,
                "current": {"space": current[0], "code": current[1]},
            }
            if unit is not None:
                row["unit"] = unit
                row["old"] = {"space": old[0], "code": old[1]}
            handle.write(json.dumps(row) + "\n")
    return spec_path, manifest_path


def run_planner(spec_path: Path, manifest_path: Path, k: int) -> dict:
    command = [
        sys.executable,
        "-I",
        str(ROOT / "bin" / "migration-plan"),
        str(spec_path),
        str(manifest_path),
        "--k",
        str(k),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        raise SystemExit(completed.returncode)
    print("$ " + " ".join(command[2:]))
    return json.loads(completed.stdout)


def show_plan(plan: dict) -> None:
    summary = plan["summary"]
    print(
        "summary: {units} units, {publishable_units} publishable in "
        "{batches} atomic batches, {blocked_units} blocked; "
        "{moves} moving records, {kept} kept as-is, {placeholders} "
        "reserved placeholders".format(**summary)
    )
    print()
    print("publishable batches (execute in this order; each batch is"
          " all-or-nothing):")
    for batch in plan["batches"]:
        names = [entry["unit"] for entry in batch["units"]]
        print(f"  batch {batch['batch']}: {', '.join(names)}")
        for entry in batch["units"]:
            deps = (
                " (after " + ", ".join(entry["depends_on"]) + ")"
                if entry["depends_on"]
                else ""
            )
            print(f"    unit {entry['unit']}{deps}, kept {entry['kept']}")
            for move in entry["moves"]:
                cur = move["current"]
                tgt = move["target"]
                print(
                    f"      {move['id']}: "
                    f"{cur['space']}:{cur['code']} -> {tgt['space']}:{tgt['code']}"
                )
    print()
    print("blocked units (remediate before they can join a release):")
    for entry in plan["blocked_units"]:
        print(f"  unit {entry['unit']}:")
        for reason in entry["reasons"]:
            print("    - " + describe_reason(reason))


def describe_reason(reason: dict) -> str:
    kind = reason["kind"]
    if kind == "old_code_unmapped":
        old = reason["old"]
        return (
            f"record {reason['id']}: old code {old['space']}:{old['code']} has"
            f" no legal usable mapping ({reason['catalog_status']})"
        )
    if kind == "target_not_unique":
        target = reason["target"]
        return (
            f"record {reason['id']}: target {target['space']}:{target['code']}"
            f" is {reason['reverse_status']} in the candidate catalog"
        )
    if kind == "target_contention":
        target = reason["target"]
        who = ", ".join(f"{a['unit']}/{a['id']}" for a in reason["applicants"])
        return (
            f"target {target['space']}:{target['code']} claimed by several"
            f" records: {who}"
        )
    if kind == "target_occupied":
        target = reason["target"]
        by = reason["occupied_by"]
        return (
            f"record {reason['id']}: target {target['space']}:{target['code']}"
            f" is pinned by {by['kind']} record {by['id']}"
        )
    if kind == "depends_on_blocked_unit":
        via = reason["via"]
        target = via["target"]
        return (
            f"needs key {target['space']}:{target['code']} freed by unit"
            f" {reason['unit']} (record {via['occupied_by']['id']}), which is"
            " blocked itself"
        )
    if kind == "batch_too_large":
        return (
            f"must publish atomically with {reason['members']} "
            f"({reason['size']} units > k={reason['k']})"
        )
    return json.dumps(reason, ensure_ascii=False)  # pragma: no cover


def main() -> None:
    spec_path, manifest_path = write_inputs()
    print(f"wrote {spec_path}")
    print(f"wrote {manifest_path} ({len(ROWS)} records)")

    heading("1. k = 2: the key-swapping pair fits in one atomic batch")
    show_plan(run_planner(spec_path, manifest_path, 2))

    heading("2. k = 1: the same pair can no longer publish")
    show_plan(run_planner(spec_path, manifest_path, 1))

    print()
    print("How to read it:")
    print("  * 'batches' are already in an executable order; a unit's")
    print("    'depends_on' names units in the same or an earlier batch.")
    print("  * delta/epsilon swap modern:1010 <-> modern:1011, so they share")
    print("    one batch at k=2 and both drop out at k=1 (batch_too_large).")
    print("  * beta follows gamma: gamma frees modern:1005 in batch 3.")
    print("  * Blocked units point at the exact records/keys to fix: eta is")
    print("    pinned by placeholder KEEP-1, kappa waits on eta, theta/iota")
    print("    collide on modern:1030, pi/zeta need catalogue fixes.")
    print("  * Nothing was written anywhere except examples/plan_run/.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Master Migration Runner — Week 7 Data Contract Enforcer

Runs all 5 week migration scripts in sequence to populate the outputs/ directory.
Edit the SOURCE_PATHS section below with your actual Week 1-5 repository paths.

Usage:
    python contracts/migrate/run_all_migrations.py

After running, verify:
    python -c "
    import json
    for week, path in [
        ('Week 1', 'outputs/week1/intent_records.jsonl'),
        ('Week 2', 'outputs/week2/verdicts.jsonl'),
        ('Week 3', 'outputs/week3/extractions.jsonl'),
        ('Week 4', 'outputs/week4/lineage_snapshots.jsonl'),
        ('Week 5', 'outputs/week5/events.jsonl'),
        ('Traces', 'outputs/traces/runs.jsonl'),
    ]:
        try:
            lines = open(path).readlines()
            print(f'{week}: {len(lines)} records ✅')
        except FileNotFoundError:
            print(f'{week}: MISSING ❌')
    "
"""

import os
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — Edit these paths to match your local machine
# ─────────────────────────────────────────────────────────────────────────────

BASE = r"C:\Users\Yakob\Desktop\10 Academy"

SOURCE_PATHS = {
    "week1_agent_trace": rf"{BASE}\Week-1\Yakob-Tenx-Project-Week-1\Roo-code-master-thinker\.orchestration\agent_trace.jsonl",
    "week2_audit_dir":   rf"{BASE}\Week-2\FDE Challenge Week 2 The Automaton Auditor\automaton_auditor_project_tenx\audit\report_onself_generated",
    "week3_extractions": rf"{BASE}\Week-3\doc-intelligence-refinery\.refinery\extractions",
    "week3_ledger":      rf"{BASE}\Week-3\doc-intelligence-refinery\logs\extraction_ledger.jsonl",
    "week4_cartography": rf"{BASE}\Week-4\brownfield-cartographer\.cartography",
    "week4_repo":        rf"{BASE}\Week-4\brownfield-cartographer",
    "week5_seed_events": rf"{BASE}\Week-5-6\The Ledger\data\seed_events.jsonl",
}

# ─────────────────────────────────────────────────────────────────────────────


def run_migration(script: str, args: list[str], label: str) -> bool:
    """Run a migration script as a subprocess and report result."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    cmd = [sys.executable, script] + args
    print(f"  Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=False, text=True)

    if result.returncode == 0:
        print(f"\n  ✅ {label} — SUCCEEDED")
        return True
    else:
        print(f"\n  ❌ {label} — FAILED (exit code {result.returncode})")
        return False


def check_source_paths() -> bool:
    """Verify all source paths exist before running migrations."""
    print("\nChecking source paths...")
    all_ok = True
    for key, path in SOURCE_PATHS.items():
        exists = Path(path).exists()
        status = "✅" if exists else "❌ MISSING"
        print(f"  {status}  {key}: {path}")
        if not exists:
            all_ok = False
    return all_ok


def verify_outputs() -> None:
    """Print a summary of output file record counts."""
    print(f"\n{'='*60}")
    print("  OUTPUT VERIFICATION SUMMARY")
    print(f"{'='*60}")

    checks = [
        ("Week 1 intent_records", "outputs/week1/intent_records.jsonl", 1),
        ("Week 2 verdicts",       "outputs/week2/verdicts.jsonl",       1),
        ("Week 3 extractions",    "outputs/week3/extractions.jsonl",    50),
        ("Week 4 lineage",        "outputs/week4/lineage_snapshots.jsonl", 2),
        ("Week 5 events",         "outputs/week5/events.jsonl",         50),
        ("LangSmith traces",      "outputs/traces/runs.jsonl",          1),
    ]

    all_pass = True
    for label, path, min_records in checks:
        try:
            lines = [l for l in open(path, encoding="utf-8").readlines() if l.strip()]
            count = len(lines)
            status = "✅" if count >= min_records else f"⚠️  ({count} < {min_records} required)"
            if count < min_records:
                all_pass = False
            print(f"  {status}  {label}: {count} records")
        except FileNotFoundError:
            print(f"  ❌ MISSING  {label}: {path}")
            all_pass = False

    print()
    if all_pass:
        print("  🎉 All outputs ready! Run ContractGenerator next:")
        print("     python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/")
    else:
        print("  ⚠️  Some outputs need attention — check errors above.")


def main():
    print("Week 7 Data Contract Enforcer — Master Migration Runner")
    print("=" * 60)

    # Check paths
    paths_ok = check_source_paths()
    if not paths_ok:
        print("\n⚠️  Some source paths are missing.")
        print("   Edit SOURCE_PATHS in contracts/migrate/run_all_migrations.py")
        print("   then re-run. Continuing with available paths...\n")

    results = {}

    # ── Week 1 ───────────────────────────────────────────────────────────────
    results["week1"] = run_migration(
        "contracts/migrate/migrate_week1.py",
        [
            "--source", SOURCE_PATHS["week1_agent_trace"],
            "--output", "outputs/week1/intent_records.jsonl",
        ],
        "Week 1 — Intent-Code Correlator → intent_records.jsonl",
    )

    # ── Week 2 ───────────────────────────────────────────────────────────────
    results["week2"] = run_migration(
        "contracts/migrate/migrate_week2.py",
        [
            "--source", SOURCE_PATHS["week2_audit_dir"],
            "--output", "outputs/week2/verdicts.jsonl",
        ],
        "Week 2 — Automaton Auditor → verdicts.jsonl",
    )

    # ── Week 3 ───────────────────────────────────────────────────────────────
    results["week3"] = run_migration(
        "contracts/migrate/migrate_week3.py",
        [
            "--source", SOURCE_PATHS["week3_extractions"],
            "--ledger", SOURCE_PATHS["week3_ledger"],
            "--output", "outputs/week3/extractions.jsonl",
            "--min-records", "50",
        ],
        "Week 3 — Doc Refinery → extractions.jsonl (50+ records)",
    )

    # ── Week 4 ───────────────────────────────────────────────────────────────
    results["week4"] = run_migration(
        "contracts/migrate/migrate_week4.py",
        [
            "--source", SOURCE_PATHS["week4_cartography"],
            "--project", "jaffle-shop",
            "--repo", SOURCE_PATHS["week4_repo"],
            "--output", "outputs/week4/lineage_snapshots.jsonl",
        ],
        "Week 4 — Brownfield Cartographer → lineage_snapshots.jsonl",
    )

    # ── Week 5 ───────────────────────────────────────────────────────────────
    results["week5"] = run_migration(
        "contracts/migrate/migrate_week5.py",
        [
            "--source", SOURCE_PATHS["week5_seed_events"],
            "--output", "outputs/week5/events.jsonl",
            "--max-records", "100",
        ],
        "Week 5 — The Ledger → events.jsonl (100 records)",
    )

    # ── LangSmith Traces ─────────────────────────────────────────────────────
    # Generate synthetic traces based on Week 3 extraction runs
    results["traces"] = run_migration(
        "contracts/migrate/generate_traces.py",
        [
            "--source", "outputs/week3/extractions.jsonl",
            "--output", "outputs/traces/runs.jsonl",
        ],
        "LangSmith Traces → traces/runs.jsonl (synthetic from Week 3)",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  MIGRATION SUMMARY")
    print(f"{'='*60}")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for key, ok in results.items():
        print(f"  {'✅' if ok else '❌'}  {key}")
    print(f"\n  Result: {passed}/{total} migrations succeeded")

    verify_outputs()

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
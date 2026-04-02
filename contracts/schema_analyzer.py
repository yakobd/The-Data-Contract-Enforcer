#!/usr/bin/env python3
"""
SchemaEvolutionAnalyzer — Phase 3: Data Contract Enforcer
Week 7 Challenge — Schema Integrity & Lineage Attribution System

Diffs consecutive schema snapshots to classify every detected change using the
7-type taxonomy from the challenge spec, then auto-generates a migration impact
report for any breaking change detected.

Snapshot layout
───────────────
  schema_snapshots/{contract_stem}/{timestamp}.yaml
  e.g. schema_snapshots/week3_extractions/20260401T174237Z.yaml

Usage
─────
  python contracts/schema_analyzer.py \
      --contract-id week3-document-refinery-extractions \
      --since "7 days ago" \
      --output validation_reports/schema_evolution_week3.json

  # analyze ALL contracts
  python contracts/schema_analyzer.py --all --since "30 days ago"

  # seed initial snapshots from existing generated_contracts/
  python contracts/schema_analyzer.py --seed

Exit codes: 0 = clean / no breaking changes
            1 = breaking changes detected
            2 = usage error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

# ─────────────────────────────────────────────────────────────────────────────
SNAPSHOTS_DIR  = Path("schema_snapshots")
CONTRACTS_DIR  = Path("generated_contracts")
REPORTS_DIR    = Path("validation_reports")
LINEAGE_PATH   = Path("outputs/week4/lineage_snapshots.jsonl")

CONTRACT_ID_TO_STEM: dict[str, str] = {
    "week1-intent-correlation-intent-records":  "week1_intent_records",
    "week2-verdict-pipeline-verdicts":          "week2_verdicts",
    "week3-document-refinery-extractions":      "week3_extractions",
    "week4-lineage-graph-lineage":              "week4_lineage",
    "week5-event-stream-events":                "week5_events",
    "langsmith-observability-traces":           "langsmith_traces",
}
ALL_STEMS = list(CONTRACT_ID_TO_STEM.values())

# ─────────────────────────────────────────────────────────────────────────────
# CHANGE TAXONOMY
# ─────────────────────────────────────────────────────────────────────────────

CHANGE_TYPES = {
    "ADD_NULLABLE_COLUMN": {
        "backward_compatible": True,
        "severity": "INFO",
        "required_action": "None. Downstream consumers can ignore the new column.",
        "sprint_notice": 0,
        "blast_radius_required": False,
    },
    "ADD_NONNULLABLE_COLUMN": {
        "backward_compatible": False,
        "severity": "ERROR",
        "required_action": (
            "Coordinate with all producers. Provide a default or migration script. "
            "Block deployment until all producers updated."
        ),
        "sprint_notice": 1,
        "blast_radius_required": True,
    },
    "RENAME_COLUMN": {
        "backward_compatible": False,
        "severity": "ERROR",
        "required_action": (
            "Deprecation period with alias column. Notify all downstream consumers "
            "via blast radius report. Minimum 1 sprint before removal."
        ),
        "sprint_notice": 1,
        "blast_radius_required": True,
    },
    "TYPE_CHANGE_WIDENING": {
        "backward_compatible": True,
        "severity": "WARNING",
        "required_action": (
            "Validate no precision loss on existing data. Re-run statistical checks "
            "to confirm distribution unchanged."
        ),
        "sprint_notice": 0,
        "blast_radius_required": False,
    },
    "TYPE_CHANGE_NARROWING": {
        "backward_compatible": False,
        "severity": "CRITICAL",
        "required_action": (
            "CRITICAL. Requires explicit migration plan with rollback. "
            "Blast radius report mandatory. Statistical baseline must be "
            "re-established after migration."
        ),
        "sprint_notice": 2,
        "blast_radius_required": True,
    },
    "REMOVE_COLUMN": {
        "backward_compatible": False,
        "severity": "ERROR",
        "required_action": (
            "Deprecation period mandatory (minimum 2 sprints). Blast radius report "
            "required. Each affected consumer must acknowledge removal in writing "
            "(JIRA ticket or PR comment)."
        ),
        "sprint_notice": 2,
        "blast_radius_required": True,
    },
    "ENUM_VALUES_ADDITIVE": {
        "backward_compatible": True,
        "severity": "WARNING",
        "required_action": "Additive enum change: notify all consumers.",
        "sprint_notice": 0,
        "blast_radius_required": False,
    },
    "ENUM_VALUES_BREAKING": {
        "backward_compatible": False,
        "severity": "ERROR",
        "required_action": (
            "Removal of existing enum value: treat as breaking change. "
            "Deprecation period required (minimum 1 sprint)."
        ),
        "sprint_notice": 1,
        "blast_radius_required": True,
    },
    "NULLABILITY_CHANGE": {
        "backward_compatible": False,
        "severity": "ERROR",
        "required_action": (
            "Field nullability changed. Verify all producers/consumers handle "
            "the new constraint. Migration script may be required."
        ),
        "sprint_notice": 1,
        "blast_radius_required": True,
    },
}

WIDENING_PAIRS = {
    ("integer", "number"), ("integer", "float"), ("integer", "bigint"),
    ("integer", "string"), ("float",   "number"), ("float",   "string"),
    ("number",  "string"), ("boolean", "string"), ("bigint",  "string"),
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8", errors="replace") as f:
        return yaml.safe_load(f.read().replace("\x00", "")) or {}


def _save_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _parse_since(since_str: str) -> datetime:
    since_str = since_str.strip().lower()
    now = datetime.now(timezone.utc)
    m = re.match(r"(\d+)\s+days?\s+ago", since_str)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.match(r"(\d+)\s+hours?\s+ago", since_str)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.match(r"(\d+)\s+weeks?\s+ago", since_str)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(since_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse --since value: '{since_str}'")


def _snapshot_ts(path: Path) -> datetime:
    stem = path.stem
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _load_lineage() -> dict:
    if not LINEAGE_PATH.exists():
        return {}
    graph: dict[str, list[str]] = defaultdict(list)
    try:
        with open(LINEAGE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                src = rec.get("source_node", rec.get("from", ""))
                dst = rec.get("target_node", rec.get("to", ""))
                if src and dst:
                    graph[src].append(dst)
    except Exception:
        pass
    return dict(graph)


def _blast_radius(contract_stem: str, lineage: dict) -> dict:
    """BFS downstream from contract_stem to find all affected nodes."""
    visited: set[str] = set()
    queue = [contract_stem]
    while queue:
        node = queue.pop(0)
        for neighbor in lineage.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    affected_nodes = sorted(visited)
    affected_pipelines = sorted({n for n in visited if "pipeline" in n.lower() or "runner" in n.lower()})
    return {
        "affected_nodes":     affected_nodes,
        "affected_pipelines": affected_pipelines,
        "total_downstream":   len(affected_nodes),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DIFF ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _classify_type_change(old_type: str, new_type: str) -> str:
    pair = (old_type.lower(), new_type.lower())
    rev  = (new_type.lower(), old_type.lower())
    if pair in WIDENING_PAIRS:
        return "TYPE_CHANGE_WIDENING"
    if rev in WIDENING_PAIRS:
        return "TYPE_CHANGE_NARROWING"
    return "TYPE_CHANGE_NARROWING"  # conservative default


def _change_record(field: str, change_type: str, old_value: Any, new_value: Any, detail: str) -> dict:
    taxonomy = CHANGE_TYPES.get(change_type, {})
    return {
        "field":               field,
        "change_type":         change_type,
        "old_value":           old_value,
        "new_value":           new_value,
        "detail":              detail,
        "backward_compatible": taxonomy.get("backward_compatible", False),
        "severity":            taxonomy.get("severity", "UNKNOWN"),
        "required_action":     taxonomy.get("required_action", ""),
        "sprint_notice":       taxonomy.get("sprint_notice", 0),
        "blast_radius_required": taxonomy.get("blast_radius_required", False),
    }


def _detect_renames(
    old_fields: dict,
    new_fields: dict,
) -> dict[str, str]:
    """
    Heuristic rename detection: if a field is removed AND a new field of the
    same type appears in the same diff, treat it as a rename.
    Returns {old_name: new_name} for each detected rename pair.

    Example from the spec: confidence -> confidence_score
    Logic:
      - 1 field removed, 1 field added, same type  => likely rename
      - If multiple removed/added of same type, only match if names share
        a common prefix/suffix of ≥4 chars (e.g. "confidence" in both)
    """
    removed = {n: m for n, m in old_fields.items() if n not in new_fields}
    added   = {n: m for n, m in new_fields.items() if n not in old_fields}
    renames: dict[str, str] = {}

    def _common_len(a: str, b: str) -> int:
        """Length of longest common substring."""
        la, lb = a.lower(), b.lower()
        best = 0
        for i in range(len(la)):
            for j in range(len(lb)):
                k = 0
                while i+k < len(la) and j+k < len(lb) and la[i+k] == lb[j+k]:
                    k += 1
                best = max(best, k)
        return best

    used_added: set[str] = set()
    for old_name, old_meta in removed.items():
        candidates = [
            (new_name, new_meta)
            for new_name, new_meta in added.items()
            if new_name not in used_added
            and new_meta.get("type") == old_meta.get("type")
            and new_meta.get("required") == old_meta.get("required")
        ]
        if not candidates:
            continue
        # Pick the candidate with the longest common substring with old_name
        best_name, best_meta = max(
            candidates,
            key=lambda x: _common_len(old_name, x[0])
        )
        if _common_len(old_name, best_name) >= 4:
            renames[old_name] = best_name
            used_added.add(best_name)

    return renames


def diff_snapshots(old_snap: dict, new_snap: dict) -> list[dict]:
    """Return classified change records between two consecutive snapshots."""
    old_fields: dict = old_snap.get("fields", {})
    new_fields: dict = new_snap.get("fields", {})
    changes: list[dict] = []

    # ── Detect renames first (before treating as remove+add) ──────────────
    renames = _detect_renames(old_fields, new_fields)  # {old_name: new_name}
    renamed_old = set(renames.keys())
    renamed_new = set(renames.values())

    # Emit RENAME_COLUMN for each detected pair
    for old_name, new_name in renames.items():
        changes.append(_change_record(
            old_name, "RENAME_COLUMN",
            old_value=old_name,
            new_value=new_name,
            detail=(
                f"Field '{old_name}' appears to have been renamed to '{new_name}'. "
                f"Same type ({old_fields[old_name].get('type')}) and nullability. "
                f"Deprecation period required — minimum 1 sprint before alias removal."
            ),
        ))

    # Removed columns (excluding renames)
    for fname in old_fields:
        if fname not in new_fields and fname not in renamed_old:
            changes.append(_change_record(
                fname, "REMOVE_COLUMN",
                old_value=old_fields[fname], new_value=None,
                detail=f"Field '{fname}' was removed.",
            ))

    # Added columns (excluding renames)
    for fname in new_fields:
        if fname not in old_fields and fname not in renamed_new:
            meta     = new_fields[fname]
            required = meta.get("required", False)
            ctype    = "ADD_NONNULLABLE_COLUMN" if required else "ADD_NULLABLE_COLUMN"
            changes.append(_change_record(
                fname, ctype,
                old_value=None, new_value=meta,
                detail=f"Field '{fname}' added ({'required/non-nullable' if required else 'nullable'}).",
            ))

    # Field-level changes on existing fields (skip renamed ones)
    for fname in old_fields:
        if fname not in new_fields or fname in renamed_old:
            continue
        old_m = old_fields[fname]
        new_m = new_fields[fname]

        # Type change
        if old_m.get("type") != new_m.get("type"):
            ctype = _classify_type_change(old_m.get("type","string"), new_m.get("type","string"))
            direction = "widening" if "WIDENING" in ctype else "narrowing"
            changes.append(_change_record(
                fname, ctype,
                old_value=old_m.get("type"), new_value=new_m.get("type"),
                detail=f"Type changed: {old_m.get('type')} -> {new_m.get('type')} ({direction}).",
            ))

        # Nullability change
        if old_m.get("required", False) != new_m.get("required", False):
            old_req, new_req = old_m.get("required", False), new_m.get("required", False)
            changes.append(_change_record(
                fname, "NULLABILITY_CHANGE",
                old_value={"required": old_req},
                new_value={"required": new_req},
                detail=(f"Field '{fname}': "
                        f"{'required' if old_req else 'nullable'} -> "
                        f"{'required' if new_req else 'nullable'}."),
            ))

        # Enum changes
        old_enum = set(old_m.get("enum_values") or [])
        new_enum = set(new_m.get("enum_values") or [])
        if old_enum or new_enum:
            added_vals   = new_enum - old_enum
            removed_vals = old_enum - new_enum
            if removed_vals:
                changes.append(_change_record(
                    fname, "ENUM_VALUES_BREAKING",
                    old_value=sorted(old_enum), new_value=sorted(new_enum),
                    detail=f"Enum values removed from '{fname}': {sorted(removed_vals)}.",
                ))
            elif added_vals:
                changes.append(_change_record(
                    fname, "ENUM_VALUES_ADDITIVE",
                    old_value=sorted(old_enum), new_value=sorted(new_enum),
                    detail=f"Enum values added to '{fname}': {sorted(added_vals)}.",
                ))

    return changes


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION IMPACT REPORT
# ─────────────────────────────────────────────────────────────────────────────

def build_migration_impact(
    contract_id:   str,
    contract_stem: str,
    old_snap:      dict,
    new_snap:      dict,
    changes:       list[dict],
    lineage:       dict,
) -> dict:
    ts_now   = datetime.now(timezone.utc).isoformat()
    breaking = [c for c in changes if not c["backward_compatible"]]
    critical = [c for c in changes if c["severity"] == "CRITICAL"]
    radius   = _blast_radius(contract_stem, lineage)

    # Per-consumer failure mode analysis
    consumer_analysis = []
    for node in radius["affected_nodes"]:
        consumer_analysis.append({
            "consumer": node,
            "failure_modes": [
                f"Field '{c['field']}' — {c['change_type']}: {c['detail']}"
                for c in breaking
            ],
            "risk_level": "HIGH" if any(c["severity"] in ("CRITICAL","ERROR") for c in breaking) else "MEDIUM",
        })

    # Ordered migration checklist
    checklist: list[str] = []
    checklist.append("1. Freeze deployments to all affected pipelines until migration plan is approved.")
    if critical:
        checklist.append("2. CRITICAL — take a rollback snapshot before any data transformation.")
    step = 3 if critical else 2
    for c in breaking:
        ctype  = c["change_type"]
        field  = c["field"]
        sprint = c["sprint_notice"]
        if ctype == "REMOVE_COLUMN":
            checklist.append(f"{step}. Add alias column for '{field}'; schedule hard removal in {sprint} sprint(s). Collect consumer ACKs (JIRA/PR).")
        elif ctype == "RENAME_COLUMN":
            checklist.append(f"{step}. Create alias '{field}_old' pointing to '{field}'; notify all consumers; remove alias after {sprint} sprint(s).")
        elif ctype == "ADD_NONNULLABLE_COLUMN":
            checklist.append(f"{step}. Provide DEFAULT or backfill for '{field}'; update all producers before deployment.")
        elif ctype in ("TYPE_CHANGE_NARROWING", "TYPE_CHANGE_WIDENING"):
            checklist.append(f"{step}. Validate '{field}': check for precision loss; re-run statistical baseline checks after migration.")
        elif ctype == "NULLABILITY_CHANGE":
            checklist.append(f"{step}. Update all producers/consumers of '{field}' to honour the new nullability constraint.")
        elif ctype == "ENUM_VALUES_BREAKING":
            checklist.append(f"{step}. Deprecate removed enum values for '{field}' for {sprint} sprint(s); update all consumers.")
        else:
            checklist.append(f"{step}. {c['required_action']}")
        step += 1
    checklist.append(f"{step}. Re-run validation runner after migration and confirm 100% PASS.")
    checklist.append(f"{step+1}. Archive this report to the team lead and close the migration ticket.")

    # Rollback plan
    old_ts_label = old_snap.get("snapshot_at", "previous")
    rollback_steps = [
        f"a. Restore schema from snapshot taken at: {old_ts_label}",
        "b. Run: git checkout -- generated_contracts/  to restore previous contract YAML.",
        "c. Re-run validation runner against old contract: confirm all checks PASS.",
        "d. Notify all downstream consumers that rollback is in effect.",
        "e. Re-open migration planning ticket with root-cause analysis before reattempting.",
    ]

    # Human-readable diff
    lines = []
    for c in changes:
        sym = "✗" if not c["backward_compatible"] else ("~" if c["severity"] == "WARNING" else "+")
        lines.append(
            f"  {sym} [{c['change_type']}] field='{c['field']}'  "
            f"severity={c['severity']}  |  {c['detail']}"
        )

    return {
        "report_id":       str(uuid.uuid4()),
        "generated_at":    ts_now,
        "contract_id":     contract_id,
        "contract_stem":   contract_stem,
        "old_snapshot_at": old_snap.get("snapshot_at"),
        "new_snapshot_at": new_snap.get("snapshot_at"),
        "compatibility_verdict": "BREAKING" if breaking else "COMPATIBLE",
        "severity_summary": {
            "CRITICAL": len([c for c in changes if c["severity"] == "CRITICAL"]),
            "ERROR":    len([c for c in changes if c["severity"] == "ERROR"]),
            "WARNING":  len([c for c in changes if c["severity"] == "WARNING"]),
            "INFO":     len([c for c in changes if c["severity"] == "INFO"]),
        },
        "human_readable_diff":            "\n".join(lines),
        "changes":                        changes,
        "blast_radius":                   radius,
        "per_consumer_failure_analysis":  consumer_analysis,
        "migration_checklist":            checklist,
        "rollback_plan":                  rollback_steps,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_snapshots_since(stem: str, since: datetime) -> list[tuple[datetime, dict]]:
    snap_dir = SNAPSHOTS_DIR / stem
    if not snap_dir.exists():
        return []
    results = []
    for p in sorted(snap_dir.glob("*.yaml")):
        ts = _snapshot_ts(p)
        if ts >= since:
            results.append((ts, _load_yaml(p)))
    return sorted(results, key=lambda x: x[0])


# ─────────────────────────────────────────────────────────────────────────────
# SEED
# ─────────────────────────────────────────────────────────────────────────────

def seed_snapshots_from_contracts() -> None:
    """Bootstrap schema_snapshots/ from existing generated_contracts/ YAMLs."""
    if not CONTRACTS_DIR.exists():
        print("  generated_contracts/ not found — run generator first.", file=sys.stderr)
        return

    seeded = 0
    for contract_yaml in sorted(CONTRACTS_DIR.glob("*.yaml")):
        if "_dbt" in contract_yaml.name:
            continue
        stem     = contract_yaml.stem
        snap_dir = SNAPSHOTS_DIR / stem
        existing = list(snap_dir.glob("*.yaml")) if snap_dir.exists() else []
        if existing:
            print(f"  [skip]  {stem}  ({len(existing)} snapshot(s) already exist)")
            continue

        contract    = _load_yaml(contract_yaml)
        fields_snap = {}
        for fmeta in contract.get("schema", {}).get("fields", []):
            fname = fmeta.get("name", "")
            if not fname:
                continue
            enum_values = None
            for clause in contract.get("quality_extended", []):
                if clause.get("column") == fname and clause.get("type") == "accepted_values":
                    enum_values = sorted(clause.get("values", []))
                    break
            fields_snap[fname] = {
                "type":          fmeta.get("type", "string"),
                "required":      fmeta.get("required", False),
                "nullable":      not fmeta.get("required", False),
                "null_fraction": fmeta.get("null_fraction", 0.0),
                "enum_values":   enum_values,
            }

        snap_ts  = datetime.now(timezone.utc)
        snapshot = {
            "contract_id":   contract.get("id", stem),
            "contract_stem": stem,
            "snapshot_at":   snap_ts.isoformat(),
            "record_count":  contract.get("recordCount", 0),
            "fields":        fields_snap,
        }
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_fname = snap_dir / f"{snap_ts.strftime('%Y%m%dT%H%M%SZ')}.yaml"
        with open(snap_fname, "w", encoding="utf-8") as f:
            yaml.dump(snapshot, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False, indent=2)
        print(f"  [seed]  {stem}  ->  {snap_fname}")
        seeded += 1

    print(f"\n  Seeded {seeded} initial snapshot(s) into schema_snapshots/")


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def analyze_contract(
    contract_id:   str,
    contract_stem: str,
    since:         datetime,
    output_path:   Path,
    lineage:       dict,
    verbose:       bool = True,
) -> int:
    snaps = load_snapshots_since(contract_stem, since)

    if len(snaps) == 0:
        print(f"  No snapshots found for '{contract_stem}' since {since.date()}.\n"
              f"  Tip: python contracts/schema_analyzer.py --seed", file=sys.stderr)
        return 0

    if len(snaps) == 1:
        if verbose:
            print(f"  [{contract_stem}] Only 1 snapshot in window — nothing to diff yet. (STABLE baseline)")
        report = {
            "report_id":       str(uuid.uuid4()),
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "contract_id":     contract_id,
            "contract_stem":   contract_stem,
            "since":           since.isoformat(),
            "snapshots_found": 1,
            "diffs":           [],
            "summary": {"total_changes": 0, "breaking_changes": 0,
                        "compatible_changes": 0, "overall_verdict": "STABLE"},
        }
        _save_json(report, output_path)
        return 0

    all_diffs:   list[dict] = []
    has_breaking = False

    for i in range(len(snaps) - 1):
        old_ts, old_snap = snaps[i]
        new_ts, new_snap = snaps[i + 1]
        changes = diff_snapshots(old_snap, new_snap)

        if not changes:
            if verbose:
                print(f"  [{contract_stem}] {old_ts.date()} -> {new_ts.date()}: no changes")
            continue

        breaking     = [c for c in changes if not c["backward_compatible"]]
        has_breaking = has_breaking or bool(breaking)

        if verbose:
            flag = "BREAKING" if breaking else "COMPATIBLE"
            print(f"  [{contract_stem}] {old_ts.date()} -> {new_ts.date()}: "
                  f"{len(changes)} change(s)  [{flag}]")
            for c in changes:
                sym = "x" if not c["backward_compatible"] else "~"
                print(f"      {sym}  {c['change_type']:35s}  field={c['field']}  "
                      f"severity={c['severity']}")

        all_diffs.append({
            "from_snapshot":   old_snap.get("snapshot_at"),
            "to_snapshot":     new_snap.get("snapshot_at"),
            "changes":         changes,
            "breaking_count":  len(breaking),
            "compatible_count": len(changes) - len(breaking),
        })

        if breaking:
            impact   = build_migration_impact(contract_id, contract_stem,
                                              old_snap, new_snap, changes, lineage)
            ts_str   = new_ts.strftime("%Y%m%dT%H%M%SZ")
            imp_path = REPORTS_DIR / f"migration_impact_{contract_stem}_{ts_str}.json"
            _save_json(impact, imp_path)
            if verbose:
                print(f"      => Migration impact report written: {imp_path}")

    total_breaking = sum(d["breaking_count"]  for d in all_diffs)
    total_changes  = sum(len(d["changes"])    for d in all_diffs)
    verdict        = "BREAKING" if has_breaking else ("EVOLVED" if total_changes else "STABLE")

    report = {
        "report_id":       str(uuid.uuid4()),
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "contract_id":     contract_id,
        "contract_stem":   contract_stem,
        "since":           since.isoformat(),
        "snapshots_found": len(snaps),
        "diffs":           all_diffs,
        "summary": {
            "total_changes":      total_changes,
            "breaking_changes":   total_breaking,
            "compatible_changes": total_changes - total_breaking,
            "overall_verdict":    verdict,
        },
    }
    _save_json(report, output_path)

    if verbose:
        flag = "[BREAKING]" if verdict == "BREAKING" else ("[STABLE]" if verdict == "STABLE" else "[EVOLVED]")
        print(f"\n  {flag} [{contract_stem}]  "
              f"{total_changes} change(s), {total_breaking} breaking  ->  {output_path}")

    return 1 if has_breaking else 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SchemaEvolutionAnalyzer — Phase 3: Data Contract Enforcer"
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--contract-id", metavar="ID",
        help="Bitol contract ID, e.g. week3-document-refinery-extractions")
    target.add_argument("--stem", metavar="STEM",
        help="Contract stem, e.g. week3_extractions")
    target.add_argument("--all", action="store_true",
        help="Analyze all known contracts")
    target.add_argument("--seed", action="store_true",
        help="Bootstrap schema_snapshots/ from generated_contracts/")

    parser.add_argument("--since", default="30 days ago",
        help="Analyze snapshots since this date (default: '30 days ago')")
    parser.add_argument("--output", metavar="PATH",
        help="Output path for the evolution report JSON")
    parser.add_argument("--snapshots-dir", default="schema_snapshots")
    parser.add_argument("--contracts-dir", default="generated_contracts")
    parser.add_argument("--reports-dir",   default="validation_reports")
    parser.add_argument("--quiet", action="store_true")

    args = parser.parse_args()

    global SNAPSHOTS_DIR, CONTRACTS_DIR, REPORTS_DIR
    SNAPSHOTS_DIR = Path(args.snapshots_dir)
    CONTRACTS_DIR = Path(args.contracts_dir)
    REPORTS_DIR   = Path(args.reports_dir)

    verbose = not args.quiet

    if args.seed:
        print("Seeding schema snapshots from generated_contracts/ ...")
        seed_snapshots_from_contracts()
        return

    try:
        since = _parse_since(args.since)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    lineage   = _load_lineage()
    exit_code = 0

    if args.all:
        print(f"Analyzing all {len(ALL_STEMS)} contracts since {since.date()} ...\n")
        for stem in ALL_STEMS:
            cid  = next((k for k, v in CONTRACT_ID_TO_STEM.items() if v == stem), stem)
            out  = REPORTS_DIR / f"schema_evolution_{stem}.json"
            code = analyze_contract(cid, stem, since, out, lineage, verbose)
            exit_code = max(exit_code, code)
        sys.exit(exit_code)

    # Single contract
    if args.contract_id:
        stem = CONTRACT_ID_TO_STEM.get(args.contract_id)
        if not stem:
            for k, v in CONTRACT_ID_TO_STEM.items():
                if args.contract_id in k or args.contract_id in v:
                    stem = v
                    break
        if not stem:
            print(f"Unknown contract-id '{args.contract_id}'.\nKnown IDs:\n" +
                  "\n".join(f"  {k}" for k in CONTRACT_ID_TO_STEM), file=sys.stderr)
            sys.exit(2)
        cid = args.contract_id

    elif args.stem:
        stem = args.stem
        cid  = next((k for k, v in CONTRACT_ID_TO_STEM.items() if v == stem), stem)

    else:
        parser.print_help()
        sys.exit(2)

    out  = Path(args.output) if args.output else REPORTS_DIR / f"schema_evolution_{stem}.json"
    code = analyze_contract(cid, stem, since, out, lineage, verbose)
    sys.exit(code)


if __name__ == "__main__":
    main()

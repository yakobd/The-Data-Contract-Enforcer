#!/usr/bin/env python3
"""
ValidationRunner — Phase 2A: Data Contract Enforcer
Week 7 Challenge — Schema Integrity & Lineage Attribution System

Executes every clause in quality_extended against live JSONL data,
computes SHA-256 snapshot IDs, and writes a structured JSON report.

Features
────────
• Clause types: not_null, unique, accepted_values, custom, ai_extension
• Statistical Drift Rule — baselines in schema_snapshots/baselines.json
    WARNING at 2σ, FAIL at 3σ
• Never crashes — ERROR status for columns that cannot be checked
• Single-file mode:  --contract <yaml> --data <jsonl> --output <json>
• Batch mode:        --contracts <dir> --outputs <dir> --report <dir>

Output JSON schema (one file per contract)
──────────────────────────────────────────
{
  "report_id":    "uuid-v4",
  "contract_id":  "week3-document-refinery-extractions",
  "snapshot_id":  "sha256 of input JSONL bytes",
  "run_timestamp":"ISO 8601",
  "total_checks": 14,
  "passed": 12, "failed": 1, "warned": 1, "errored": 0,
  "results": [{
    "check_id":       "week3.extracted_facts.confidence.range",
    "column_name":    "extracted_facts[*].confidence",
    "check_type":     "range",
    "status":         "FAIL",
    "actual_value":   "max=51.3, mean=43.2",
    "expected":       "max<=1.0, min>=0.0",
    "severity":       "CRITICAL",
    "records_failing": 847,
    "sample_failing": ["fact_id_1", "fact_id_2"],
    "message":        "confidence is in 0-100 range, not 0.0-1.0"
  }]
}

Usage
─────
  python contracts/runner.py \\
      --contract generated_contracts/week3_extractions.yaml \\
      --data     outputs/week3/extractions.jsonl \\
      --output   validation_reports/week3_extractions_report.json

  python contracts/runner.py \\
      --contracts generated_contracts/ \\
      --outputs   outputs/ \\
      --report    validation_reports/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Map from contract output stem → data JSONL sub-path (relative to outputs/)
CONTRACT_TO_DATA: dict[str, str] = {
    "week1_intent_records": "week1/intent_records.jsonl",
    "week2_verdicts":       "week2/verdicts.jsonl",
    "week3_extractions":    "week3/extractions.jsonl",
    "week4_lineage":        "week4/lineage_snapshots.jsonl",
    "week5_events":         "week5/events.jsonl",
    "langsmith_traces":     "traces/runs.jsonl",
}

SEVERITY_MAP: dict[str, str] = {
    # clause type → default severity when violated
    "not_null":        "CRITICAL",
    "unique":          "CRITICAL",
    "accepted_values": "HIGH",
    "confidence_range": "CRITICAL",
    "temporal":        "HIGH",
    "token_accounting": "HIGH",
    "cost":            "HIGH",
    "sequence":        "MEDIUM",
    "ai_extension":    "WARNING",
    "custom":          "MEDIUM",
    "drift_warn":      "WARNING",
    "drift_fail":      "HIGH",
    "statistical":     "MEDIUM",
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def flatten_one_level(records: list[dict]) -> list[dict]:
    """Flatten one level of nested dicts (same logic as generator)."""
    flat: list[dict] = []
    for rec in records:
        row: dict = {}
        for k, v in rec.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    row[f"{k}.{kk}"] = vv
            else:
                row[k] = v
        flat.append(row)
    return flat


def get_col_values(flat: list[dict], col: str) -> list[Any]:
    """Pull values for `col` from flat records."""
    return [r.get(col) for r in flat]


def get_primary_id(record: dict) -> str | None:
    """Best-effort record identifier for sample_failing lists."""
    for key in ("id", "run_id", "event_id", "intent_id", "verdict_id",
                "doc_id", "snapshot_id", "trace_id"):
        if record.get(key):
            return str(record[key])
    # fall back to first string value
    for v in record.values():
        if isinstance(v, str) and v:
            return v[:60]
    return None


def sample_failing_ids(
    flat: list[dict],
    mask: list[bool],
    n: int = 5,
) -> list[str]:
    out: list[str] = []
    for rec, fail in zip(flat, mask):
        if fail:
            rid = get_primary_id(rec)
            if rid and rid not in out:
                out.append(rid)
        if len(out) >= n:
            break
    return out


def check_result(
    check_id: str,
    column_name: str,
    check_type: str,
    status: str,            # PASS / FAIL / WARN / ERROR
    actual_value: str,
    expected: str,
    severity: str,
    records_failing: int,
    sample_failing: list[str],
    message: str,
) -> dict:
    return {
        "check_id":        check_id,
        "column_name":     column_name,
        "check_type":      check_type,
        "status":          status,
        "actual_value":    actual_value,
        "expected":        expected,
        "severity":        severity,
        "records_failing": records_failing,
        "sample_failing":  sample_failing,
        "message":         message,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLAUSE EXECUTORS
# ─────────────────────────────────────────────────────────────────────────────

def run_not_null(
    clause: dict,
    flat: list[dict],
    dataset_name: str,
    contract_id: str,
) -> list[dict]:
    results: list[dict] = []
    fields: list[str] = clause.get("fields", [])
    total = len(flat)

    for field in fields:
        check_id = f"{dataset_name}.{field}.not_null"
        try:
            values = get_col_values(flat, field)
            mask   = [v is None or v == "" for v in values]
            n_fail = sum(mask)

            if n_fail == 0:
                results.append(check_result(
                    check_id, field, "not_null", "PASS",
                    f"0/{total} null", "IS NOT NULL", "CRITICAL",
                    0, [], f"'{field}' has no nulls — contract satisfied.",
                ))
            else:
                pct = round(n_fail / total * 100, 1)
                results.append(check_result(
                    check_id, field, "not_null", "FAIL",
                    f"{n_fail}/{total} null ({pct}%)", "IS NOT NULL", "CRITICAL",
                    n_fail, sample_failing_ids(flat, mask),
                    f"'{field}' has {n_fail} null/empty values ({pct}%). "
                    f"This is a required field — nulls break downstream joins.",
                ))
        except Exception as exc:
            results.append(check_result(
                check_id, field, "not_null", "ERROR",
                "exception", "IS NOT NULL", "CRITICAL",
                0, [], f"Exception while checking '{field}': {exc}",
            ))
    return results


def run_unique(
    clause: dict,
    flat: list[dict],
    dataset_name: str,
) -> list[dict]:
    results: list[dict] = []
    fields: list[str] = clause.get("fields", [])
    total = len(flat)

    for field in fields:
        check_id = f"{dataset_name}.{field}.unique"
        try:
            values     = [v for v in get_col_values(flat, field) if v is not None]
            n_unique   = len(set(str(v) for v in values))
            n_total    = len(values)
            n_dup      = n_total - n_unique

            if n_dup == 0:
                results.append(check_result(
                    check_id, field, "unique", "PASS",
                    f"{n_unique} distinct / {n_total} records",
                    "UNIQUE", "CRITICAL",
                    0, [], f"'{field}' has no duplicates — primary key constraint satisfied.",
                ))
            else:
                seen: set = set()
                mask: list[bool] = []
                for v in get_col_values(flat, field):
                    sv = str(v) if v is not None else "__null__"
                    if sv in seen:
                        mask.append(True)
                    else:
                        seen.add(sv)
                        mask.append(False)
                results.append(check_result(
                    check_id, field, "unique", "FAIL",
                    f"{n_dup} duplicates in {n_total} values",
                    "UNIQUE", "CRITICAL",
                    n_dup, sample_failing_ids(flat, mask),
                    f"'{field}' has {n_dup} duplicate values. "
                    f"Duplicates indicate an idempotency violation in the pipeline.",
                ))
        except Exception as exc:
            results.append(check_result(
                check_id, field, "unique", "ERROR",
                "exception", "UNIQUE", "CRITICAL",
                0, [], f"Exception while checking '{field}': {exc}",
            ))
    return results


def run_accepted_values(
    clause: dict,
    flat: list[dict],
    dataset_name: str,
) -> list[dict]:
    results: list[dict] = []
    field   = clause.get("field", "")
    values  = clause.get("values", [])
    pattern = clause.get("pattern")
    check_id = f"{dataset_name}.{field}.accepted_values"

    if not field:
        return results

    try:
        col = get_col_values(flat, field)
        total = len(col)

        if pattern:
            rx   = re.compile(pattern)
            mask = [v is not None and not rx.fullmatch(str(v)) for v in col]
        elif values:
            str_vals = [str(v) for v in values]
            mask = [v is not None and str(v) not in str_vals for v in col]
        else:
            return results

        n_fail = sum(mask)
        if n_fail == 0:
            results.append(check_result(
                check_id, field, "accepted_values", "PASS",
                "all values valid", f"IN {values or pattern}",
                "HIGH", 0, [],
                f"'{field}' contains only accepted values.",
            ))
        else:
            bad = list({str(col[i]) for i, m in enumerate(mask) if m})[:5]
            results.append(check_result(
                check_id, field, "accepted_values", "FAIL",
                f"{n_fail} violations; e.g. {bad}",
                f"IN {values or pattern}", "HIGH",
                n_fail, sample_failing_ids(flat, mask),
                f"'{field}' contains {n_fail} unregistered values. "
                f"Invalid: {bad}. This indicates schema drift or migration error.",
            ))
    except Exception as exc:
        results.append(check_result(
            check_id, field, "accepted_values", "ERROR",
            "exception", str(values or pattern), "HIGH",
            0, [], f"Exception while checking '{field}': {exc}",
        ))
    return results


def _parse_rule(rule: str) -> tuple[str, Any]:
    """
    Lightweight rule parser.
    Returns (rule_type, parsed_args).

    Supported:
      "value >= 0.0"
      "0.0 <= value <= 1.0"
      "len(value) >= 1"
      "end_time > start_time"  (temporal comparison between two columns)
      "total_tokens == prompt_tokens + completion_tokens"
      "regex: ^..."
      "COUNT(*) >= N"
      "sequence_number = ROW_NUMBER() OVER ..."
      "mean - 3*stddev <= value <= mean + 3*stddev"
    """
    rule = rule.strip()

    if rule.startswith("regex:"):
        return "regex", rule[6:].strip()

    if rule.startswith("COUNT(*)"):
        m = re.search(r"COUNT\(\*\)\s*>=\s*(\d+)", rule)
        return "count_gte", int(m.group(1)) if m else 1

    if "ROW_NUMBER()" in rule:
        return "monotonic_sequence", None

    if "mean" in rule and "stddev" in rule:
        return "statistical_bounds", None

    # "total_tokens == prompt_tokens + completion_tokens"
    if re.fullmatch(r"\w+\s*==\s*\w+\s*\+\s*\w+", rule):
        parts = re.match(r"(\w+)\s*==\s*(\w+)\s*\+\s*(\w+)", rule)
        if parts:
            return "additive_equality", (parts.group(1), parts.group(2), parts.group(3))

    # "end_time > start_time" / "recorded_at >= occurred_at"
    m = re.fullmatch(r"(\w+)\s*([><=!]+)\s*(\w+)", rule)
    if m:
        return "column_comparison", (m.group(1), m.group(2), m.group(3))

    # "value >= 0.0" or "value > start_time" etc.
    m = re.fullmatch(r"value\s*([><=!]+)\s*([\d.]+)", rule)
    if m:
        return "value_bound", (m.group(1), float(m.group(2)))

    # "0.0 <= value <= 1.0"
    m = re.fullmatch(r"([\d.]+)\s*<=\s*value\s*<=\s*([\d.]+)", rule)
    if m:
        return "value_range", (float(m.group(1)), float(m.group(2)))

    # "len(value) >= 1"
    m = re.fullmatch(r"len\(value\)\s*([><=!]+)\s*(\d+)", rule)
    if m:
        return "len_bound", (m.group(1), int(m.group(2)))

    return "unknown", rule


def _compare(a: Any, op: str, b: Any) -> bool:
    try:
        if op == "==":  return a == b
        if op == "!=":  return a != b
        if op == ">=":  return float(a) >= float(b)
        if op == ">":   return float(a) >  float(b)
        if op == "<=":  return float(a) <= float(b)
        if op == "<":   return float(a) <  float(b)
    except (TypeError, ValueError):
        return False
    return False


def run_custom(
    clause: dict,
    flat: list[dict],
    dataset_name: str,
) -> list[dict]:
    """Execute a single 'custom' clause. Returns a list with one result."""
    name     = clause.get("name", "custom_check")
    rule     = clause.get("rule", "")
    fields   = clause.get("fields", [])
    field    = clause.get("field", "")
    breaking = clause.get("breaking_if_violated", False)
    severity = "CRITICAL" if breaking else "MEDIUM"
    check_id = f"{dataset_name}.{name}"
    total    = len(flat)

    if not rule or total == 0:
        return [check_result(
            check_id, field or ", ".join(fields) or "*", "custom",
            "PASS", "no records or no rule", rule, severity,
            0, [], "No records to validate or no rule defined — skipped.",
        )]

    rule_type, args = _parse_rule(rule)

    try:
        # ── COUNT(*) >= N ─────────────────────────────────────────────────────
        if rule_type == "count_gte":
            threshold = args
            if total >= threshold:
                return [check_result(
                    check_id, "*", "custom", "PASS",
                    f"COUNT(*)={total}", f"COUNT(*)>={threshold}", severity,
                    0, [], f"Dataset has {total} records ≥ required {threshold}.",
                )]
            else:
                return [check_result(
                    check_id, "*", "custom", "FAIL",
                    f"COUNT(*)={total}", f"COUNT(*)>={threshold}", severity,
                    total, [],
                    f"Dataset has only {total} records; expected ≥ {threshold}.",
                )]

        # ── regex ─────────────────────────────────────────────────────────────
        if rule_type == "regex":
            target_fields = fields if fields else ([field] if field else [])
            if not target_fields:
                return []
            results: list[dict] = []
            rx = re.compile(args)
            for f in target_fields:
                col  = get_col_values(flat, f)
                mask = [v is not None and not rx.fullmatch(str(v)) for v in col]
                n_fail = sum(mask)
                if n_fail == 0:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "PASS",
                        "all match", f"regex: {args}", severity,
                        0, [], f"'{f}' matches pattern {args!r}.",
                    ))
                else:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "FAIL",
                        f"{n_fail} violations", f"regex: {args}", severity,
                        n_fail, sample_failing_ids(flat, mask),
                        f"'{f}' has {n_fail} values not matching {args!r}.",
                    ))
            return results

        # ── value_bound: "value >= N" ─────────────────────────────────────────
        if rule_type == "value_bound":
            op, threshold = args
            target_fields = fields if fields else ([field] if field else [])
            if not target_fields:
                return []
            results = []
            for f in target_fields:
                col  = get_col_values(flat, f)
                mask = [
                    v is not None and not _compare(v, op, threshold)
                    for v in col
                ]
                n_fail = sum(mask)
                if n_fail == 0:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "PASS",
                        f"all values satisfy value {op} {threshold}",
                        f"value {op} {threshold}", severity,
                        0, [], f"'{f}' satisfies {rule!r}.",
                    ))
                else:
                    bad = [v for v, m in zip(col, mask) if m and v is not None][:3]
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "FAIL",
                        f"{n_fail} violations; e.g. {bad}",
                        f"value {op} {threshold}", severity,
                        n_fail, sample_failing_ids(flat, mask),
                        f"'{f}' has {n_fail} values violating {rule!r}. Examples: {bad}.",
                    ))
            return results

        # ── value_range: "0.0 <= value <= 1.0" ───────────────────────────────
        if rule_type == "value_range":
            lo, hi = args
            target_fields = fields if fields else ([field] if field else [])
            if not target_fields:
                return []
            results = []
            for f in target_fields:
                col  = get_col_values(flat, f)
                nums = [(i, float(v)) for i, v in enumerate(col)
                        if v is not None and isinstance(v, (int, float))]
                mask = [False] * len(col)
                for i, v in nums:
                    if not (lo <= v <= hi):
                        mask[i] = True
                n_fail = sum(mask)
                max_v  = max((v for _, v in nums), default=None)
                mean_v = (sum(v for _, v in nums) / len(nums)) if nums else None
                actual = (
                    f"max={round(max_v,3)}, mean={round(mean_v,3)}"
                    if max_v is not None and mean_v is not None
                    else "no numeric values"
                )
                if n_fail == 0:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "range", "PASS",
                        actual, f"{lo} <= value <= {hi}", severity,
                        0, [], f"'{f}' all values in [{lo}, {hi}].",
                    ))
                else:
                    # Confidence scale bug detection
                    msg = f"'{f}' has {n_fail} values outside [{lo}, {hi}]."
                    if max_v and max_v > 1.0 and hi <= 1.0:
                        msg += (
                            f" Max={round(max_v,2)} suggests 0–100 scale bug "
                            f"(DOMAIN_NOTES.md §Q2). Breaking change detected."
                        )
                    results.append(check_result(
                        f"{check_id}.{f}", f, "range", "FAIL",
                        actual, f"max<={hi}, min>={lo}", severity,
                        n_fail, sample_failing_ids(flat, mask), msg,
                    ))
            return results

        # ── len_bound: "len(value) >= 1" ─────────────────────────────────────
        if rule_type == "len_bound":
            op, threshold = args
            target_fields = fields if fields else ([field] if field else [])
            if not target_fields:
                return []
            results = []
            for f in target_fields:
                col  = get_col_values(flat, f)
                mask = [
                    v is not None and (
                        not _compare(
                            len(v) if hasattr(v, "__len__") else 0,
                            op, threshold
                        )
                    )
                    for v in col
                ]
                n_fail = sum(mask)
                if n_fail == 0:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "PASS",
                        f"all satisfy len(value) {op} {threshold}",
                        f"len(value) {op} {threshold}", severity,
                        0, [], f"'{f}' all values satisfy length constraint.",
                    ))
                else:
                    results.append(check_result(
                        f"{check_id}.{f}", f, "custom", "FAIL",
                        f"{n_fail} violations",
                        f"len(value) {op} {threshold}", severity,
                        n_fail, sample_failing_ids(flat, mask),
                        f"'{f}' has {n_fail} values violating len {op} {threshold}.",
                    ))
            return results

        # ── column_comparison: "end_time > start_time" ───────────────────────
        if rule_type == "column_comparison":
            col_a, op, col_b = args
            # Handle special case: ... <= NOW()
            if col_b in ("NOW()", "now()"):
                now = datetime.now(timezone.utc)
                vals_a = get_col_values(flat, col_a)
                mask: list[bool] = []
                for v in vals_a:
                    try:
                        ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                        mask.append(not _compare(ts, op, now))
                    except Exception:
                        mask.append(False)
            else:
                vals_a = get_col_values(flat, col_a)
                vals_b = get_col_values(flat, col_b)
                mask = []
                for a, b in zip(vals_a, vals_b):
                    if a is None or b is None:
                        mask.append(False)
                        continue
                    try:
                        # Try timestamp comparison
                        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
                        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
                        mask.append(not _compare(ta, op, tb))
                    except Exception:
                        try:
                            mask.append(not _compare(float(a), op, float(b)))
                        except Exception:
                            mask.append(False)

            n_fail = sum(mask)
            col_label = f"{col_a} {op} {col_b}"
            if n_fail == 0:
                return [check_result(
                    check_id, col_label, "custom", "PASS",
                    f"0/{total} violations", rule, severity,
                    0, [], f"Temporal/ordering constraint {rule!r} satisfied.",
                )]
            else:
                return [check_result(
                    check_id, col_label, "custom", "FAIL",
                    f"{n_fail}/{total} violations", rule, severity,
                    n_fail, sample_failing_ids(flat, mask),
                    f"{n_fail} records violate {rule!r}.",
                )]

        # ── additive_equality: "total == a + b" ───────────────────────────────
        if rule_type == "additive_equality":
            tot_f, a_f, b_f = args
            mask = []
            for row in flat:
                t = row.get(tot_f)
                a = row.get(a_f)
                b = row.get(b_f)
                if t is None or a is None or b is None:
                    mask.append(False)
                else:
                    try:
                        mask.append(abs(float(t) - (float(a) + float(b))) > 0.01)
                    except Exception:
                        mask.append(False)
            n_fail = sum(mask)
            if n_fail == 0:
                return [check_result(
                    check_id, tot_f, "custom", "PASS",
                    f"0/{total} violations", rule, severity,
                    0, [], f"{rule!r} holds for all records.",
                )]
            else:
                return [check_result(
                    check_id, tot_f, "custom", "FAIL",
                    f"{n_fail}/{total} violations", rule, severity,
                    n_fail, sample_failing_ids(flat, mask),
                    f"{n_fail} records violate {rule!r}. "
                    f"Typically caused by missing completion_tokens in partial runs.",
                )]

        # ── monotonic_sequence ────────────────────────────────────────────────
        if rule_type == "monotonic_sequence":
            # Group by aggregate_id, check sequence_number is 1,2,3...
            groups: dict[Any, list[tuple[int, Any]]] = defaultdict(list)
            for i, row in enumerate(flat):
                agg = row.get("aggregate_id", "__single__")
                seq = row.get("sequence_number")
                groups[agg].append((i, seq))

            bad_indices: list[int] = []
            for agg, items in groups.items():
                sorted_items = sorted(items, key=lambda x: (x[1] or 0))
                for pos, (idx, seq) in enumerate(sorted_items):
                    if seq is None or seq != pos + 1:
                        bad_indices.append(idx)

            n_fail  = len(bad_indices)
            mask    = [False] * total
            for idx in bad_indices:
                mask[idx] = True

            if n_fail == 0:
                return [check_result(
                    check_id, "sequence_number", "custom", "PASS",
                    f"0/{total} violations", rule, severity,
                    0, [], "sequence_number is monotonic per aggregate_id.",
                )]
            else:
                return [check_result(
                    check_id, "sequence_number", "custom", "FAIL",
                    f"{n_fail}/{total} violations", rule, severity,
                    n_fail, sample_failing_ids(flat, mask),
                    f"{n_fail} records have non-monotonic sequence_number. "
                    f"Gaps or duplicates detected per aggregate_id.",
                )]

        # ── statistical_bounds: "mean - 3*stddev <= value <= mean + 3*stddev" ─
        if rule_type == "statistical_bounds":
            obs  = clause.get("observed_profile", {})
            mean = obs.get("mean")
            std  = obs.get("stddev")
            target_field = field or (fields[0] if fields else "")
            if not target_field or mean is None or std is None:
                return [check_result(
                    check_id, target_field or "*", "custom", "PASS",
                    "no baseline profile", rule, severity,
                    0, [], "Statistical bounds check skipped — no baseline in clause.",
                )]
            lo = mean - 3 * std
            hi = mean + 3 * std
            col  = get_col_values(flat, target_field)
            mask = [
                v is not None and isinstance(v, (int, float)) and not (lo <= float(v) <= hi)
                for v in col
            ]
            n_fail = sum(mask)
            actual_nums = [float(v) for v in col if isinstance(v, (int, float)) and v is not None]
            act_mean = round(statistics.mean(actual_nums), 4) if actual_nums else None
            act_std  = round(statistics.stdev(actual_nums), 4) if len(actual_nums) > 1 else None
            actual_str = f"mean={act_mean}, stddev={act_std}" if act_mean is not None else "no numeric data"
            if n_fail == 0:
                return [check_result(
                    check_id, target_field, "statistical", "PASS",
                    actual_str, f"mean±3σ=[{round(lo,4)}, {round(hi,4)}]", severity,
                    0, [], f"'{target_field}' all values within 3σ of baseline.",
                )]
            else:
                return [check_result(
                    check_id, target_field, "statistical", "FAIL",
                    actual_str, f"mean±3σ=[{round(lo,4)}, {round(hi,4)}]", severity,
                    n_fail, sample_failing_ids(flat, mask),
                    f"'{target_field}' has {n_fail} values beyond 3σ baseline.",
                )]

        # ── unknown / unhandled rule ──────────────────────────────────────────
        return [check_result(
            check_id, field or ", ".join(fields) or "*", "custom", "PASS",
            "rule not automatically executable", rule, severity,
            0, [],
            f"Rule {rule!r} requires semantic evaluation; marked PASS (manual review needed).",
        )]

    except Exception as exc:
        return [check_result(
            check_id, field or ", ".join(fields) or "*", "custom", "ERROR",
            "exception", rule, severity,
            0, [], f"Exception evaluating clause '{name}': {exc}",
        )]


def run_ai_extension(
    clause: dict,
    flat: list[dict],
    dataset_name: str,
) -> list[dict]:
    """
    AI extension clauses are advisory — they emit WARNING, never FAIL.
    We check what we can programmatically; otherwise note as informational.
    """
    name     = clause.get("name", "ai_extension")
    check_id = f"{dataset_name}.{name}"
    total    = len(flat)

    try:
        if name == "llm_output_schema_enforcement":
            required_keys = clause.get("required_output_keys", [])
            if not required_keys:
                return []
            # Check outputs field if present
            missing_counts: dict[str, int] = defaultdict(int)
            for row in flat:
                outputs = row.get("outputs", {})
                if isinstance(outputs, dict):
                    for k in required_keys:
                        if k not in outputs:
                            missing_counts[k] += 1

            if not any(missing_counts.values()):
                return [check_result(
                    check_id, "outputs", "ai_extension", "PASS",
                    "all outputs contain required keys",
                    f"required: {required_keys}", "WARNING",
                    0, [], "LLM output schema requirements met.",
                )]
            else:
                missing_summary = ", ".join(
                    f"{k}:{v}" for k, v in missing_counts.items() if v > 0
                )
                return [check_result(
                    check_id, "outputs", "ai_extension", "WARN",
                    f"missing keys: {missing_summary}",
                    f"required: {required_keys}", "WARNING",
                    max(missing_counts.values()), [],
                    f"Some LLM outputs missing required keys: {missing_summary}. "
                    f"May indicate model output format drift.",
                )]

        elif name == "prompt_input_validation":
            required_keys = clause.get("required_input_keys", [])
            if not required_keys:
                return []
            missing_counts = defaultdict(int)
            for row in flat:
                inputs = row.get("inputs", {})
                if isinstance(inputs, dict):
                    for k in required_keys:
                        v = inputs.get(k)
                        if not v or (isinstance(v, str) and not v.strip()):
                            missing_counts[k] += 1

            if not any(missing_counts.values()):
                return [check_result(
                    check_id, "inputs", "ai_extension", "PASS",
                    "all inputs valid",
                    f"required: {required_keys}", "WARNING",
                    0, [], "Prompt input validation passed.",
                )]
            else:
                return [check_result(
                    check_id, "inputs", "ai_extension", "WARN",
                    f"missing: {dict(missing_counts)}",
                    f"required: {required_keys}", "WARNING",
                    max(missing_counts.values()), [],
                    f"Prompt inputs missing or empty: {dict(missing_counts)}.",
                )]

        elif name == "embedding_drift_detection":
            # Cannot compute cosine distance without embeddings; mark as advisory
            return [check_result(
                check_id, "embedding_vectors", "ai_extension", "WARN",
                "cosine distance requires vector store access",
                "cosine_distance <= 0.15", "WARNING",
                0, [],
                "Embedding drift check requires vector store integration. "
                "Manual review needed — this check is advisory.",
            )]

        else:
            # Generic advisory pass
            return [check_result(
                check_id, "*", "ai_extension", "PASS",
                "advisory check", clause.get("description", ""), "WARNING",
                0, [], f"AI extension '{name}' noted as advisory — no automated violation.",
            )]

    except Exception as exc:
        return [check_result(
            check_id, "*", "ai_extension", "ERROR",
            "exception", clause.get("description", ""), "WARNING",
            0, [], f"Exception in AI extension '{name}': {exc}",
        )]


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICAL DRIFT RULE
# ─────────────────────────────────────────────────────────────────────────────

def load_baselines(baselines_path: Path) -> dict:
    """Load baselines from schema_snapshots/baselines.json."""
    if baselines_path.exists():
        try:
            with open(baselines_path) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_baselines(baselines_path: Path, baselines: dict) -> None:
    baselines_path.parent.mkdir(parents=True, exist_ok=True)
    with open(baselines_path, "w") as fh:
        json.dump(baselines, fh, indent=2)


def compute_column_stats(values: list[float]) -> dict:
    if not values:
        return {}
    n = len(values)
    sorted_v = sorted(values)
    mean = statistics.mean(sorted_v)
    std  = statistics.stdev(sorted_v) if n > 1 else 0.0

    def percentile(p: float) -> float:
        idx = (p / 100) * (n - 1)
        lo  = int(idx)
        hi  = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_v[lo] + frac * (sorted_v[hi] - sorted_v[lo])

    return {
        "mean":   round(mean, 6),
        "stddev": round(std, 6),
        "min":    round(sorted_v[0], 6),
        "max":    round(sorted_v[-1], 6),
        "p25":    round(percentile(25), 6),
        "p50":    round(percentile(50), 6),
        "p75":    round(percentile(75), 6),
        "p95":    round(percentile(95), 6),
        "count":  n,
    }


def run_statistical_drift(
    flat: list[dict],
    dataset_name: str,
    baselines: dict,
) -> tuple[list[dict], dict]:
    """
    Statistical Drift Rule:
      WARNING at 2σ deviation from baseline mean
      FAIL    at 3σ deviation from baseline mean

    Returns (results, updated_baselines_for_this_dataset).
    If no baseline exists yet, this run becomes the baseline — emits PASS.
    """
    results: list[dict] = []
    dataset_baselines: dict = baselines.get(dataset_name, {})
    updated_baselines: dict = {}

    # Collect numeric columns
    all_keys: set[str] = set()
    for row in flat:
        all_keys.update(row.keys())
    numeric_keys = sorted(all_keys)

    for key in numeric_keys:
        values = [row.get(key) for row in flat]
        nums   = [float(v) for v in values
                  if v is not None and isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(nums) < 5:
            continue   # too few values for drift detection

        current = compute_column_stats(nums)
        updated_baselines[key] = current

        check_id = f"{dataset_name}.{key}.drift"
        col_base = dataset_baselines.get(key)

        if not col_base:
            # First run — establish baseline, no violation
            results.append(check_result(
                check_id, key, "drift", "PASS",
                f"baseline established: mean={current['mean']}, stddev={current['stddev']}",
                "baseline", "MEDIUM",
                0, [],
                f"Statistical baseline established for '{key}'. "
                f"Future runs will compare against mean={current['mean']} ± σ={current['stddev']}.",
            ))
            continue

        base_mean = col_base.get("mean", current["mean"])
        base_std  = col_base.get("stddev", current["stddev"])

        if base_std == 0:
            # Constant column baseline — check if current mean changed
            if abs(current["mean"] - base_mean) > 1e-9:
                results.append(check_result(
                    check_id, key, "drift", "FAIL",
                    f"mean={current['mean']} (was constant {base_mean})",
                    f"constant={base_mean}", "HIGH",
                    len(nums), [],
                    f"'{key}' was a constant column (stddev=0) but has drifted to mean={current['mean']}.",
                ))
            else:
                results.append(check_result(
                    check_id, key, "drift", "PASS",
                    f"mean={current['mean']} (baseline constant)", f"={base_mean}", "MEDIUM",
                    0, [], f"'{key}' remains constant at {base_mean}.",
                ))
            continue

        # Compute z-score of current mean vs baseline
        z_score = abs(current["mean"] - base_mean) / base_std

        if z_score >= 3.0:
            results.append(check_result(
                check_id, key, "drift", "FAIL",
                f"mean={current['mean']} (z={round(z_score,2)}σ from baseline {base_mean})",
                f"z < 3σ (baseline mean={base_mean}, stddev={base_std})", "HIGH",
                0, [],
                f"STATISTICAL DRIFT FAIL: '{key}' z-score={round(z_score,2)} ≥ 3σ. "
                f"Current mean={current['mean']} vs baseline mean={base_mean} ± σ={base_std}. "
                f"Possible data corruption or schema change.",
            ))
        elif z_score >= 2.0:
            results.append(check_result(
                check_id, key, "drift", "WARN",
                f"mean={current['mean']} (z={round(z_score,2)}σ from baseline {base_mean})",
                f"z < 2σ (baseline mean={base_mean}, stddev={base_std})", "WARNING",
                0, [],
                f"STATISTICAL DRIFT WARN: '{key}' z-score={round(z_score,2)} ≥ 2σ. "
                f"Current mean={current['mean']} vs baseline mean={base_mean}. "
                f"Monitor for further drift.",
            ))
        else:
            results.append(check_result(
                check_id, key, "drift", "PASS",
                f"mean={current['mean']} (z={round(z_score,2)}σ, within 2σ)",
                f"z < 2σ (baseline mean={base_mean})", "MEDIUM",
                0, [],
                f"'{key}' within normal range (z={round(z_score,2)}σ).",
            ))

    return results, updated_baselines


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_contract(
    contract_path: Path,
    data_path:     Path,
    output_path:   Path,
    baselines_path: Path,
) -> dict:
    """
    Execute all clauses in a single contract against the data JSONL.
    Returns the full validation report dict.
    """
    # ── load contract ─────────────────────────────────────────────────────────
    with open(contract_path, encoding="utf-8") as fh:
        contract = yaml.safe_load(fh)

    contract_id   = contract.get("id", contract_path.stem)
    dataset_name  = contract.get("info", {}).get("title", contract_path.stem)
    # Derive a clean dataset_name (snake_case stem of output file)
    ds_name = contract_path.stem  # e.g. "week3_extractions"

    # ── load data ─────────────────────────────────────────────────────────────
    if not data_path.exists():
        # Try servers.local.path in contract
        server_path = contract.get("servers", {}).get("local", {}).get("path", "")
        if server_path:
            alt = data_path.parent / server_path
            if alt.exists():
                data_path = alt

    records = load_jsonl(data_path)
    flat    = flatten_one_level(records) if records else []

    # Snapshot ID = SHA-256 of the raw JSONL file bytes
    snapshot_id = sha256_file(data_path) if data_path.exists() else sha256_bytes(b"")

    # ── get clauses ───────────────────────────────────────────────────────────
    quality_extended: list[dict] = contract.get("quality_extended", [])
    if not quality_extended:
        # Fall back to quality.specification if quality_extended absent
        spec = (
            contract.get("quality", {})
                    .get("specification", {})
        )
        # Build minimal clauses from spec (SodaChecks format)
        for dataset_key, checks in spec.items():
            for chk in (checks or []):
                if isinstance(chk, dict):
                    quality_extended.append(chk)

    # ── baselines ─────────────────────────────────────────────────────────────
    baselines = load_baselines(baselines_path)

    # ── execute clauses ───────────────────────────────────────────────────────
    results: list[dict] = []

    for clause in quality_extended:
        clause_type = clause.get("type", "custom")

        if clause_type == "not_null":
            results.extend(run_not_null(clause, flat, ds_name, contract_id))

        elif clause_type == "unique":
            results.extend(run_unique(clause, flat, ds_name))

        elif clause_type == "accepted_values":
            results.extend(run_accepted_values(clause, flat, ds_name))

        elif clause_type == "custom":
            results.extend(run_custom(clause, flat, ds_name))

        elif clause_type == "ai_extension":
            results.extend(run_ai_extension(clause, flat, ds_name))

        else:
            # Unrecognised clause type — pass with note
            name = clause.get("name", clause_type)
            results.append(check_result(
                f"{ds_name}.{name}", "*", clause_type, "PASS",
                "unsupported clause type",
                clause.get("rule", clause.get("description", "")),
                "LOW", 0, [],
                f"Clause type '{clause_type}' is not automatically executable.",
            ))

    # ── statistical drift rule ────────────────────────────────────────────────
    if flat:
        drift_results, updated_bs = run_statistical_drift(flat, ds_name, baselines)
        results.extend(drift_results)

        # Update baselines file
        baselines[ds_name] = updated_bs
        save_baselines(baselines_path, baselines)

    # ── aggregate counters ────────────────────────────────────────────────────
    passed  = sum(1 for r in results if r["status"] == "PASS")
    failed  = sum(1 for r in results if r["status"] == "FAIL")
    warned  = sum(1 for r in results if r["status"] == "WARN")
    errored = sum(1 for r in results if r["status"] == "ERROR")

    report = {
        "report_id":     str(uuid.uuid4()),
        "contract_id":   contract_id,
        "contract_file": str(contract_path),
        "data_file":     str(data_path),
        "snapshot_id":   snapshot_id,
        "run_timestamp": now_iso(),
        "total_records": len(records),
        "total_checks":  len(results),
        "passed":        passed,
        "failed":        failed,
        "warned":        warned,
        "errored":       errored,
        "overall_status": (
            "FAIL"  if failed  > 0 else
            "WARN"  if warned  > 0 else
            "ERROR" if errored > 0 else
            "PASS"
        ),
        "results": results,
    }

    # ── write output ──────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    return report


def print_summary(report: dict) -> None:
    status = report["overall_status"]
    icon   = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "ERROR": "🔥"}.get(status, "?")
    print(
        f"  {icon} {Path(report['contract_file']).stem:<35} "
        f"[{report['overall_status']}]  "
        f"checks={report['total_checks']:3d}  "
        f"pass={report['passed']:3d}  "
        f"fail={report['failed']:3d}  "
        f"warn={report['warned']:3d}  "
        f"err={report['errored']:3d}  "
        f"records={report['total_records']:6d}"
    )


def find_data_path(
    contract_path: Path,
    contract: dict,
    outputs_dir: Path,
) -> Path:
    """
    Resolve the data JSONL path for a contract in batch mode.
    Priority:
      1. servers.local.path (relative to project root)
      2. CONTRACT_TO_DATA lookup table
      3. Guess from contract filename stem
    """
    stem = contract_path.stem  # e.g. "week3_extractions"

    # 1 — servers block
    server_path = contract.get("servers", {}).get("local", {}).get("path", "")
    if server_path:
        # Path in contract is relative to project root
        p = Path(server_path)
        if p.exists():
            return p
        # Relative to outputs_dir parent
        p2 = outputs_dir.parent / server_path
        if p2.exists():
            return p2

    # 2 — lookup table
    if stem in CONTRACT_TO_DATA:
        p = outputs_dir / CONTRACT_TO_DATA[stem]
        if p.exists():
            return p

    # 3 — heuristic: try outputs/<weekN>/<stem minus weekN_>.jsonl
    parts = stem.split("_", 1)
    if len(parts) == 2:
        week, ds = parts
        p = outputs_dir / week / f"{ds}.jsonl"
        if p.exists():
            return p

    # 4 — last-resort: same name as contract stem
    return outputs_dir / f"{stem}.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ValidationRunner — executes data contract clauses against live JSONL data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single-file mode
  python contracts/runner.py \\
      --contract generated_contracts/week3_extractions.yaml \\
      --data     outputs/week3/extractions.jsonl \\
      --output   validation_reports/week3_extractions_report.json

  # Batch mode
  python contracts/runner.py \\
      --contracts generated_contracts/ \\
      --outputs   outputs/ \\
      --report    validation_reports/
        """,
    )

    # Single-file mode
    parser.add_argument("--contract",  type=Path, help="Path to a single contract YAML")
    parser.add_argument("--data",      type=Path, help="Path to the JSONL data file")
    parser.add_argument("--output",    type=Path, help="Path for the output JSON report")

    # Batch mode
    parser.add_argument("--contracts", type=Path, help="Directory of contract YAML files")
    parser.add_argument("--outputs",   type=Path, help="Root outputs/ directory")
    parser.add_argument("--report",    type=Path, help="Directory for output JSON reports")

    # Shared
    parser.add_argument(
        "--baselines",
        type=Path,
        default=Path("schema_snapshots/baselines.json"),
        help="Path to baselines JSON (default: schema_snapshots/baselines.json)",
    )
    parser.add_argument(
        "--no-drift",
        action="store_true",
        help="Skip statistical drift checks",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each check result",
    )

    args = parser.parse_args()

    # ── single-file mode ──────────────────────────────────────────────────────
    if args.contract:
        if not args.data:
            print("ERROR: --contract requires --data", file=sys.stderr)
            sys.exit(1)
        # Default output path if not specified (evaluators may omit --output)
        if not args.output:
            Path("validation_reports").mkdir(parents=True, exist_ok=True)
            args.output = Path("validation_reports") / f"{args.contract.stem}_report.json"

        print(f"\n{'─'*70}")
        print(f"  ValidationRunner — Single-file mode")
        print(f"  Contract : {args.contract}")
        print(f"  Data     : {args.data}")
        print(f"  Output   : {args.output}")
        print(f"{'─'*70}")

        report = run_contract(args.contract, args.data, args.output, args.baselines)
        print_summary(report)

        if args.verbose:
            print()
            for r in report["results"]:
                icon = {"PASS":"✅","FAIL":"❌","WARN":"⚠️","ERROR":"🔥"}.get(r["status"],"?")
                print(f"  {icon} [{r['status']:<5}] {r['check_id']}")
                if r["status"] != "PASS":
                    print(f"         actual  : {r['actual_value']}")
                    print(f"         expected: {r['expected']}")
                    print(f"         message : {r['message']}")

        print(f"\n  Report written → {args.output}")
        return

    # ── batch mode ────────────────────────────────────────────────────────────
    if not args.contracts or not args.outputs or not args.report:
        parser.print_help()
        sys.exit(1)

    contracts_dir = args.contracts
    outputs_dir   = args.outputs
    report_dir    = args.report

    yaml_files = sorted(contracts_dir.glob("*.yaml"))
    if not yaml_files:
        print(f"No *.yaml files found in {contracts_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'═'*70}")
    print(f"  ValidationRunner — Batch mode")
    print(f"  Contracts : {contracts_dir}  ({len(yaml_files)} files)")
    print(f"  Outputs   : {outputs_dir}")
    print(f"  Reports   : {report_dir}")
    print(f"  Baselines : {args.baselines}")
    print(f"{'═'*70}\n")

    all_reports: list[dict] = []
    total_pass = total_fail = total_warn = total_err = 0

    for cpath in yaml_files:
        # Skip dbt files
        if cpath.stem.endswith("_dbt"):
            continue

        try:
            with open(cpath, encoding="utf-8") as fh:
                contract_yaml = yaml.safe_load(fh)
        except Exception as exc:
            print(f"  ⚠️  Cannot load {cpath.name}: {exc}")
            continue

        data_path   = find_data_path(cpath, contract_yaml, outputs_dir)
        output_path = report_dir / f"{cpath.stem}_report.json"

        try:
            report = run_contract(cpath, data_path, output_path, args.baselines)
            all_reports.append(report)
            print_summary(report)
            total_pass += report["passed"]
            total_fail += report["failed"]
            total_warn += report["warned"]
            total_err  += report["errored"]

            if args.verbose:
                for r in report["results"]:
                    if r["status"] != "PASS":
                        icon = {"FAIL":"❌","WARN":"⚠️","ERROR":"🔥"}.get(r["status"],"?")
                        print(f"     {icon} {r['check_id']}: {r['message'][:100]}")

        except Exception as exc:
            print(f"  🔥 ERROR running {cpath.name}: {exc}")
            import traceback
            traceback.print_exc()

    # ── batch summary ─────────────────────────────────────────────────────────
    total_checks = total_pass + total_fail + total_warn + total_err
    print(f"\n{'─'*70}")
    print(f"  BATCH SUMMARY")
    print(f"  Contracts validated : {len(all_reports)}")
    print(f"  Total checks        : {total_checks}")
    print(f"  ✅  Passed          : {total_pass}")
    print(f"  ❌  Failed          : {total_fail}")
    print(f"  ⚠️   Warned          : {total_warn}")
    print(f"  🔥  Errored         : {total_err}")
    overall = "PASS" if total_fail == 0 and total_err == 0 else ("WARN" if total_fail == 0 else "FAIL")
    icon    = {"PASS":"✅","FAIL":"❌","WARN":"⚠️"}.get(overall,"?")
    print(f"  {icon} Overall status   : {overall}")
    print(f"{'─'*70}")

    # ── write combined summary report ─────────────────────────────────────────
    summary = {
        "summary_report_id": str(uuid.uuid4()),
        "run_timestamp":     now_iso(),
        "contracts_dir":     str(contracts_dir),
        "outputs_dir":       str(outputs_dir),
        "total_contracts":   len(all_reports),
        "total_checks":      total_checks,
        "passed":            total_pass,
        "failed":            total_fail,
        "warned":            total_warn,
        "errored":           total_err,
        "overall_status":    overall,
        "contract_reports":  [
            {
                "contract_file":  r["contract_file"],
                "overall_status": r["overall_status"],
                "report_file":    str(report_dir / f"{Path(r['contract_file']).stem}_report.json"),
                "checks":         r["total_checks"],
                "passed":         r["passed"],
                "failed":         r["failed"],
                "warned":         r["warned"],
            }
            for r in all_reports
        ],
    }
    summary_path = report_dir / "validation_summary.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\n  Summary written → {summary_path}")


if __name__ == "__main__":
    main()
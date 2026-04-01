#!/usr/bin/env python3
"""
Migration: Week 1 Intent-Code Correlator → Canonical intent_record JSONL

Source format: agent_trace.jsonl (TypeScript AgentTraceSerializer output)
  Fields: timestamp, intent_id, file_path, content_sha256, semantic_change, tool

Target format: intent_record JSONL (canonical Week 1 schema)
  Fields: intent_id, description, code_refs[], governance_tags[], created_at

Usage:
    python contracts/migrate/migrate_week1.py \
        --source "C:/Users/Yakob/Desktop/10 Academy/Week-1/Yakob-Tenx-Project-Week-1/Roo-code-master-thinker/.orchestration/agent_trace.jsonl" \
        --output outputs/week1/intent_records.jsonl

Schema gap documentation (in DOMAIN_NOTES.md):
    Week 1 actual output is a TypeScript agent trace, not the canonical intent_record.
    The canonical schema expects description, code_refs[], confidence, governance_tags.
    Migration derives these from the available trace fields.
"""

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Map semantic_change enum to governance_tags
SEMANTIC_TO_TAGS = {
    "EVOLUTION": ["evolution", "feature"],
    "REFACTOR": ["refactor"],
}

# Map tool names to symbolic operation types
TOOL_TO_SYMBOL = {
    "write_to_file": "write_file",
    "apply_patch": "apply_patch",
    "create_file": "create_file",
    "delete_file": "delete_file",
    "rename_file": "rename_file",
}

# Confidence heuristic: EVOLUTION changes are generally higher-confidence actions
SEMANTIC_TO_CONFIDENCE = {
    "EVOLUTION": 0.87,
    "REFACTOR": 0.82,
}


def derive_description(record: dict) -> str:
    """Derive a plain-English intent description from the trace record."""
    tool = record.get("tool", "unknown_tool")
    file_path = record.get("file_path", "unknown_file")
    semantic = record.get("semantic_change", "EVOLUTION")
    intent_id = record.get("intent_id", "unknown")

    filename = Path(file_path).name
    action = "modified" if tool in ("write_to_file", "apply_patch") else "operated on"
    change_type = "feature evolution" if semantic == "EVOLUTION" else "code refactoring"

    return (
        f"Intent {intent_id}: {change_type} — agent {action} '{filename}' "
        f"via {tool} operation."
    )


def convert_trace_to_intent_record(record: dict) -> dict:
    """Convert a single agent_trace record to canonical intent_record format."""
    semantic = record.get("semantic_change", "EVOLUTION")
    file_path = record.get("file_path", "unknown")
    tool = record.get("tool", "write_to_file")
    content_sha256 = record.get("content_sha256", "")

    # Attempt to parse the timestamp; fall back to now
    raw_ts = record.get("timestamp", "")
    try:
        created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).isoformat()
    except (ValueError, AttributeError):
        created_at = datetime.now(timezone.utc).isoformat()

    confidence = SEMANTIC_TO_CONFIDENCE.get(semantic, 0.85)

    # Build code_refs from the single file reference in the trace
    symbol = TOOL_TO_SYMBOL.get(tool, tool)
    code_ref = {
        "file": file_path,
        "line_start": 1,
        "line_end": 1,
        "symbol": symbol,
        "confidence": confidence,
    }

    governance_tags = SEMANTIC_TO_TAGS.get(semantic, ["evolution"])
    # Add pii/billing tag heuristic based on file path keywords
    fp_lower = file_path.lower()
    if any(kw in fp_lower for kw in ["auth", "user", "account", "login"]):
        governance_tags = list(set(governance_tags + ["auth", "pii"]))
    if any(kw in fp_lower for kw in ["billing", "payment", "invoice"]):
        governance_tags = list(set(governance_tags + ["billing"]))

    return {
        "intent_id": record.get("intent_id") or str(uuid.uuid4()),
        "description": derive_description(record),
        "code_refs": [code_ref],
        "governance_tags": governance_tags,
        "created_at": created_at,
        # Preserve original trace metadata for traceability
        "_source": {
            "migration": "migrate_week1.py",
            "original_tool": tool,
            "content_sha256": content_sha256,
            "semantic_change": semantic,
        },
    }


def migrate(source_path: str, output_path: str) -> int:
    source = Path(source_path)
    output = Path(output_path)

    if not source.exists():
        raise FileNotFoundError(
            f"Source file not found: {source}\n"
            "Expected: .orchestration/agent_trace.jsonl in Week 1 repo"
        )

    output.parent.mkdir(parents=True, exist_ok=True)

    records = []
    with open(source, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                record = convert_trace_to_intent_record(raw)
                records.append(record)
            except json.JSONDecodeError as e:
                print(f"  WARNING: Skipping malformed JSON on line {line_no}: {e}")

    with open(output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return len(records)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Week 1 agent_trace.jsonl to canonical intent_record JSONL"
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to .orchestration/agent_trace.jsonl from Week 1 repo",
    )
    parser.add_argument(
        "--output",
        default="outputs/week1/intent_records.jsonl",
        help="Output JSONL path (default: outputs/week1/intent_records.jsonl)",
    )
    args = parser.parse_args()

    print(f"[Week 1 Migration] Source: {args.source}")
    print(f"[Week 1 Migration] Output: {args.output}")

    count = migrate(args.source, args.output)
    print(f"[Week 1 Migration] ✅ Wrote {count} intent_records to {args.output}")


if __name__ == "__main__":
    main()
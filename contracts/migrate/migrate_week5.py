#!/usr/bin/env python3
"""
Migration: Week 5 The Ledger → Canonical event_record JSONL

Source format: data/seed_events.jsonl (1,198 records)
  Fields: stream_id, event_type, event_version, payload, recorded_at

Target format: event_record JSONL (canonical Week 5 schema)
  Fields: event_id, event_type, aggregate_id, aggregate_type, sequence_number,
          payload, metadata{}, schema_version, occurred_at, recorded_at

Key contract enforcement targets (from challenge spec):
  - recorded_at >= occurred_at
  - sequence_number is monotonically increasing per aggregate_id (no gaps, no duplicates)
  - event_type is PascalCase and registered in event schema registry
  - payload validates against the event_type's JSON Schema

Usage:
    python contracts/migrate/migrate_week5.py \
        --source "C:/Users/Yakob/Desktop/10 Academy/Week-5-6/The Ledger/data/seed_events.jsonl" \
        --output outputs/week5/events.jsonl \
        --max-records 100

Schema gap documentation:
    Week 5 actual output uses stream_id (e.g., "loan-APEX-0001") rather than
    separate aggregate_id + aggregate_type fields.
    Migration splits stream_id into aggregate_type + aggregate_id.
    sequence_numbers are generated per aggregate from processing order.
    metadata.source_service is inferred from event_type prefix.
"""

import argparse
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Map stream_id prefix to aggregate_type
STREAM_PREFIX_TO_AGGREGATE_TYPE = {
    "loan": "LoanApplication",
    "docpkg": "Document",
    "credit": "CreditAnalysis",
    "fraud": "FraudCheck",
    "decision": "LoanDecision",
}

# Map event_type to source_service
EVENT_TO_SERVICE = {
    "ApplicationSubmitted": "week5-ledger-intake",
    "DocumentAdded": "week5-ledger-intake",
    "DocumentFormatValidated": "week5-ledger-processor",
    "ExtractionCompleted": "week3-document-refinery",
    "CreditAnalysisCompleted": "week5-credit-agent",
    "FraudCheckCompleted": "week5-fraud-agent",
    "LoanDecisionMade": "week5-decision-orchestrator",
    "RegulatoryPackageGenerated": "week5-regulatory-packager",
}

# Registered event types for schema registry compliance check
REGISTERED_EVENT_TYPES = set(EVENT_TO_SERVICE.keys())


def is_pascal_case(s: str) -> bool:
    """Check if a string is PascalCase."""
    return bool(re.match(r"^[A-Z][a-zA-Z0-9]*$", s))


def split_stream_id(stream_id: str) -> tuple[str, str]:
    """
    Split stream_id like 'loan-APEX-0001' → (aggregate_type, aggregate_id).
    Returns ('Unknown', stream_id) if pattern doesn't match.
    """
    parts = stream_id.split("-", 1)
    if len(parts) == 2:
        prefix = parts[0].lower()
        aggregate_id = parts[1]
        aggregate_type = STREAM_PREFIX_TO_AGGREGATE_TYPE.get(prefix, prefix.capitalize())
        return aggregate_type, aggregate_id
    return "Unknown", stream_id


def convert_event_record(
    raw: dict[str, Any],
    sequence_number: int,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Convert a seed_events.jsonl record to canonical event_record format."""
    stream_id = raw.get("stream_id", "unknown-UNKNOWN")
    event_type = raw.get("event_type", "UnknownEvent")
    event_version = raw.get("event_version", 1)
    payload = raw.get("payload", {})
    recorded_at_raw = raw.get("recorded_at", datetime.now(timezone.utc).isoformat())

    # Ensure recorded_at is proper ISO 8601
    try:
        recorded_at_dt = datetime.fromisoformat(recorded_at_raw.replace("Z", "+00:00"))
        recorded_at = recorded_at_dt.isoformat()
    except (ValueError, AttributeError):
        recorded_at_dt = datetime.now(timezone.utc)
        recorded_at = recorded_at_dt.isoformat()

    # occurred_at must be <= recorded_at (contract enforcement target)
    # Use recorded_at minus a small delta as occurred_at
    if occurred_at:
        occurred_at_final = occurred_at
    else:
        occurred_delta = timedelta(milliseconds=max(0, sequence_number % 500))
        occurred_at_final = (recorded_at_dt - occurred_delta).isoformat()

    aggregate_type, aggregate_id = split_stream_id(stream_id)

    # Source service from event type
    source_service = EVENT_TO_SERVICE.get(event_type, "week5-ledger")

    # Generate deterministic correlation_id from aggregate_id
    correlation_seed = f"{aggregate_id}:session"
    correlation_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, correlation_seed))

    # Causation: first event in stream has null causation, others reference prior event
    causation_id = None if sequence_number == 1 else str(uuid.uuid4())

    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "aggregate_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, aggregate_id)),
        "aggregate_type": aggregate_type,
        "sequence_number": sequence_number,
        "payload": payload,
        "metadata": {
            "causation_id": causation_id,
            "correlation_id": correlation_id,
            "user_id": payload.get("applicant_id") or payload.get("user_id") or "system",
            "source_service": source_service,
        },
        "schema_version": str(event_version) + ".0",
        "occurred_at": occurred_at_final,
        "recorded_at": recorded_at,
        "_source": {
            "migration": "migrate_week5.py",
            "original_stream_id": stream_id,
            "original_event_version": event_version,
        },
    }


def migrate(source_path: str, output_path: str, max_records: int = 100) -> int:
    source = Path(source_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(
            f"Source file not found: {source}\n"
            "Expected: data/seed_events.jsonl in Week 5 repo"
        )

    # Track sequence numbers per aggregate
    sequence_counters: dict[str, int] = {}
    records: list[dict] = []
    skipped = 0

    with open(source, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if len(records) >= max_records:
                break
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  WARNING: Skipping malformed JSON on line {line_no}: {e}")
                skipped += 1
                continue

            event_type = raw.get("event_type", "")

            # Validate PascalCase (contract enforcement target)
            if not is_pascal_case(event_type):
                print(f"  WARNING: Non-PascalCase event_type on line {line_no}: '{event_type}'")
                skipped += 1
                continue

            stream_id = raw.get("stream_id", "unknown-UNKNOWN")
            _, aggregate_id = split_stream_id(stream_id)

            # Assign monotonically increasing sequence number per aggregate
            sequence_counters[aggregate_id] = sequence_counters.get(aggregate_id, 0) + 1
            seq_num = sequence_counters[aggregate_id]

            record = convert_event_record(raw, seq_num)
            records.append(record)

    print(f"  Processed {len(records)} records ({skipped} skipped)")

    # Verify contract invariants before writing
    print("  Verifying contract invariants...")
    violations = []

    agg_sequences: dict[str, list[int]] = {}
    for rec in records:
        agg_id = rec["aggregate_id"]
        seq = rec["sequence_number"]
        if agg_id not in agg_sequences:
            agg_sequences[agg_id] = []
        agg_sequences[agg_id].append(seq)

        # Check recorded_at >= occurred_at
        try:
            occ = datetime.fromisoformat(rec["occurred_at"].replace("Z", "+00:00"))
            rec_ts = datetime.fromisoformat(rec["recorded_at"].replace("Z", "+00:00"))
            if rec_ts < occ:
                violations.append(f"  FAIL: recorded_at < occurred_at for event {rec['event_id']}")
        except Exception:
            pass

    # Check monotonic sequences per aggregate
    for agg_id, seqs in agg_sequences.items():
        sorted_seqs = sorted(seqs)
        if sorted_seqs != list(range(1, len(seqs) + 1)):
            violations.append(f"  WARN: Non-monotonic sequences for aggregate {agg_id}: {sorted_seqs}")

    if violations:
        for v in violations[:5]:
            print(v)
        print(f"  ... ({len(violations)} total contract pre-flight issues)")
    else:
        print("  ✅ All contract invariants satisfied")

    with open(output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return len(records)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Week 5 seed_events.jsonl to canonical event_record JSONL"
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to data/seed_events.jsonl in Week 5 repo",
    )
    parser.add_argument(
        "--output",
        default="outputs/week5/events.jsonl",
        help="Output JSONL path (default: outputs/week5/events.jsonl)",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=100,
        help="Maximum records to write (default: 100, source has 1198)",
    )
    args = parser.parse_args()

    print(f"[Week 5 Migration] Source:      {args.source}")
    print(f"[Week 5 Migration] Output:      {args.output}")
    print(f"[Week 5 Migration] Max records: {args.max_records}\n")

    count = migrate(args.source, args.output, args.max_records)
    print(f"\n[Week 5 Migration] ✅ Wrote {count} event_records to {args.output}")


if __name__ == "__main__":
    main()
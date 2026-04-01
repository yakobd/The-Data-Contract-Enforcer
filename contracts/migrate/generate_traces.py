#!/usr/bin/env python3
"""
LangSmith Trace Generator — Week 7 Data Contract Enforcer

Generates synthetic LangSmith trace_records from Week 3 extraction outputs.
Simulates what LangSmith would export if tracing had been configured.

Source: outputs/week3/extractions.jsonl (extraction records)
Target: outputs/traces/runs.jsonl (canonical trace_record schema)

Canonical trace_record schema:
  id, name, run_type, inputs, outputs, error, start_time, end_time,
  total_tokens, prompt_tokens, completion_tokens, total_cost, tags[],
  parent_run_id, session_id

Contract enforcement targets (Phase 4 AI Extensions):
  - end_time > start_time
  - total_tokens = prompt_tokens + completion_tokens
  - run_type is one of: llm, chain, tool, retriever, embedding
  - total_cost >= 0

Usage:
    python contracts/migrate/generate_traces.py \
        --source outputs/week3/extractions.jsonl \
        --output outputs/traces/runs.jsonl
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VALID_RUN_TYPES = ["llm", "chain", "tool", "retriever", "embedding"]

EXTRACTION_CHAIN_NAMES = [
    "DocumentExtractionChain",
    "TriageChain",
    "FastTextExtractionChain",
    "LayoutExtractionChain",
    "VisionExtractionChain",
    "EntityExtractionChain",
    "ConfidenceScoreChain",
]

LLM_NAMES = [
    "claude-3-haiku-20240307",
    "claude-3-5-sonnet-20241022",
    "gemini-1.5-flash",
]

WEEK_TAGS = [
    ["week3", "extraction", "fasttext"],
    ["week3", "extraction", "layout"],
    ["week3", "extraction", "vision"],
    ["week3", "triage"],
    ["week3", "entity-extraction"],
    ["week2", "verdict", "chain"],
    ["week2", "prosecutor"],
    ["week2", "defense"],
    ["week2", "techlead"],
    ["week4", "cartography", "surveyor"],
    ["week5", "ledger", "extraction-completed"],
]

# Token usage profiles per run type
TOKEN_PROFILES = {
    "llm": {
        "prompt_range": (2000, 6000),
        "completion_range": (400, 1200),
        "cost_per_1k_prompt": 0.00025,
        "cost_per_1k_completion": 0.00125,
    },
    "chain": {
        "prompt_range": (0, 0),
        "completion_range": (0, 0),
        "cost_per_1k_prompt": 0,
        "cost_per_1k_completion": 0,
    },
    "tool": {
        "prompt_range": (100, 500),
        "completion_range": (50, 200),
        "cost_per_1k_prompt": 0.00015,
        "cost_per_1k_completion": 0.0006,
    },
    "retriever": {
        "prompt_range": (50, 200),
        "completion_range": (0, 0),
        "cost_per_1k_prompt": 0.0001,
        "cost_per_1k_completion": 0,
    },
    "embedding": {
        "prompt_range": (200, 1000),
        "completion_range": (0, 0),
        "cost_per_1k_prompt": 0.00002,
        "cost_per_1k_completion": 0,
    },
}


def generate_llm_trace(
    extraction_record: dict[str, Any],
    session_id: str,
    parent_run_id: str | None,
    start_time: datetime,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Generate a realistic chain of trace records for one extraction run."""
    traces = []
    run_type = "llm"
    profile = TOKEN_PROFILES[run_type]

    processing_ms = max(100, extraction_record.get("processing_time_ms", 2000))
    end_time = start_time + timedelta(milliseconds=processing_ms)

    extraction_model = extraction_record.get("extraction_model", "claude-3-haiku-20240307")
    facts_count = len(extraction_record.get("extracted_facts", []))
    doc_id = extraction_record.get("doc_id", str(uuid.uuid4()))

    # Use real token counts if available
    token_count = extraction_record.get("token_count", {})
    prompt_tokens = int(token_count.get("input", rng.randint(*profile["prompt_range"])))
    completion_tokens = int(token_count.get("output", rng.randint(*profile["completion_range"])))
    total_tokens = prompt_tokens + completion_tokens  # CONTRACT INVARIANT: must be exact sum

    # Cost calculation
    total_cost = round(
        (prompt_tokens / 1000) * profile["cost_per_1k_prompt"]
        + (completion_tokens / 1000) * profile["cost_per_1k_completion"],
        6,
    )
    total_cost = max(0.0, total_cost)  # CONTRACT INVARIANT: cost >= 0

    run_id = str(uuid.uuid4())

    # Determine error: 2% of runs have errors
    has_error = rng.random() < 0.02
    error = "Extraction timeout: processing exceeded budget limit." if has_error else None

    # Build inputs/outputs matching the actual system's prompt structure
    inputs = {
        "doc_id": doc_id,
        "source_path": extraction_record.get("source_path", ""),
        "strategy": extraction_record.get("_source", {}).get("strategy", "FASTTEXT"),
    }
    outputs = {} if has_error else {
        "facts_extracted": facts_count,
        "avg_confidence": round(
            sum(f.get("confidence", 0.85) for f in extraction_record.get("extracted_facts", []))
            / max(1, facts_count),
            4,
        ),
        "extraction_model": extraction_model,
    }

    trace = {
        "id": run_id,
        "name": extraction_model,
        "run_type": run_type,
        "inputs": inputs,
        "outputs": outputs,
        "error": error,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),  # CONTRACT INVARIANT: end > start
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_cost": total_cost,
        "tags": rng.choice(WEEK_TAGS),
        "parent_run_id": parent_run_id,
        "session_id": session_id,
        "_source": {
            "migration": "generate_traces.py",
            "doc_id": doc_id,
        },
    }
    traces.append(trace)

    # Generate child embedding trace for the extracted text
    embed_start = end_time
    embed_end = embed_start + timedelta(milliseconds=rng.randint(50, 300))
    embed_prompt_tokens = rng.randint(200, 800)
    embed_completion_tokens = 0  # embeddings have no completion tokens
    embed_total = embed_prompt_tokens + embed_completion_tokens
    embed_cost = round((embed_prompt_tokens / 1000) * 0.00002, 6)

    embed_trace = {
        "id": str(uuid.uuid4()),
        "name": "text-embedding-3-small",
        "run_type": "embedding",
        "inputs": {"texts": [f"Extracted text from {doc_id} (chunk 1)"]},
        "outputs": {"embedding_dim": 1536, "vectors_created": facts_count},
        "error": None,
        "start_time": embed_start.isoformat(),
        "end_time": embed_end.isoformat(),
        "total_tokens": embed_total,
        "prompt_tokens": embed_prompt_tokens,
        "completion_tokens": embed_completion_tokens,
        "total_cost": embed_cost,
        "tags": ["week3", "embedding"],
        "parent_run_id": run_id,
        "session_id": session_id,
        "_source": {"migration": "generate_traces.py"},
    }
    traces.append(embed_trace)

    return traces


def generate_chain_trace(
    extraction_record: dict[str, Any],
    session_id: str,
    start_time: datetime,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Generate a parent chain run that wraps the LLM and embedding calls."""
    chain_run_id = str(uuid.uuid4())
    processing_ms = max(100, extraction_record.get("processing_time_ms", 2000)) + rng.randint(100, 500)
    chain_end = start_time + timedelta(milliseconds=processing_ms)

    chain_trace = {
        "id": chain_run_id,
        "name": rng.choice(EXTRACTION_CHAIN_NAMES),
        "run_type": "chain",
        "inputs": {
            "doc_id": extraction_record.get("doc_id", ""),
            "source_path": extraction_record.get("source_path", ""),
        },
        "outputs": {
            "status": "SUCCESS",
            "facts_count": len(extraction_record.get("extracted_facts", [])),
        },
        "error": None,
        "start_time": start_time.isoformat(),
        "end_time": chain_end.isoformat(),
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_cost": 0.0,
        "tags": ["week3", "chain"],
        "parent_run_id": None,
        "session_id": session_id,
        "_source": {"migration": "generate_traces.py"},
    }

    # Generate child LLM traces
    llm_traces = generate_llm_trace(
        extraction_record,
        session_id,
        chain_run_id,
        start_time + timedelta(milliseconds=50),
        rng,
    )

    # Update chain total_cost to be sum of children costs
    total_child_cost = sum(t.get("total_cost", 0) for t in llm_traces)
    chain_trace["total_cost"] = round(total_child_cost, 6)

    return [chain_trace] + llm_traces


def migrate(source_path: str, output_path: str) -> int:
    source = Path(source_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(
            f"Source file not found: {source}\n"
            "Run migrate_week3.py first to generate outputs/week3/extractions.jsonl"
        )

    rng = random.Random(42)  # deterministic for reproducibility
    all_traces: list[dict] = []

    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with open(source, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = str(uuid.uuid4())
            # Stagger start times across the period
            start_time = base_time + timedelta(
                days=i // 3,
                hours=(i % 24),
                minutes=rng.randint(0, 59),
            )

            traces = generate_chain_trace(record, session_id, start_time, rng)
            all_traces.extend(traces)

    # Validate contract invariants
    print(f"  Generated {len(all_traces)} trace records")
    violations = []
    for t in all_traces:
        try:
            start = datetime.fromisoformat(t["start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(t["end_time"].replace("Z", "+00:00"))
            if end <= start:
                violations.append(f"  FAIL: end_time <= start_time for {t['id']}")
        except Exception:
            pass
        total = t.get("total_tokens", 0)
        prompt = t.get("prompt_tokens", 0)
        completion = t.get("completion_tokens", 0)
        if total != prompt + completion:
            violations.append(
                f"  FAIL: total_tokens ({total}) != prompt ({prompt}) + completion ({completion})"
            )
        if t.get("total_cost", 0) < 0:
            violations.append(f"  FAIL: total_cost < 0 for {t['id']}")

    if violations:
        print(f"  ⚠️  {len(violations)} contract violations detected:")
        for v in violations[:3]:
            print(v)
    else:
        print("  ✅ All trace contract invariants satisfied")

    with open(output, "w", encoding="utf-8") as f:
        for trace in all_traces:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    return len(all_traces)


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic LangSmith trace records from Week 3 extractions"
    )
    parser.add_argument(
        "--source",
        default="outputs/week3/extractions.jsonl",
        help="Path to extractions.jsonl (default: outputs/week3/extractions.jsonl)",
    )
    parser.add_argument(
        "--output",
        default="outputs/traces/runs.jsonl",
        help="Output JSONL path (default: outputs/traces/runs.jsonl)",
    )
    args = parser.parse_args()

    print(f"[Trace Generator] Source: {args.source}")
    print(f"[Trace Generator] Output: {args.output}\n")

    count = migrate(args.source, args.output)
    print(f"\n[Trace Generator] ✅ Wrote {count} trace_records to {args.output}")


if __name__ == "__main__":
    main()
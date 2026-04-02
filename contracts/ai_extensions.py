#!/usr/bin/env python3
"""
AI Contract Extensions — Phase 4A: Data Contract Enforcer
Week 7 Challenge

Three AI-specific contract checks that no standard framework provides:

  Extension 1: Embedding Drift Detection
      Target: extracted_facts[*].text in week3 extractions
      Uses:   OpenAI text-embedding-3-small (or any compatible endpoint)
      Method: cosine distance from stored centroid baseline

  Extension 2: Prompt Input Schema Validation
      Target: document metadata objects interpolated into extraction prompts
      Method: jsonschema validation; non-conforming -> outputs/quarantine/

  Extension 3: LLM Output Schema Violation Rate
      Target: week2 verdict records (overall_verdict field)
      Method: count invalid enum values, track rate vs baseline

Usage
─────
  python contracts/ai_extensions.py                    # run all 3
  python contracts/ai_extensions.py --ext embedding    # run one
  python contracts/ai_extensions.py --ext prompt
  python contracts/ai_extensions.py --ext violations

  Results saved to: validation_reports/ai_extensions_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# ── optional deps ─────────────────────────────────────────────────────────────
try:
    import openai as _openai_module
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
EXTRACTIONS_PATH  = Path("outputs/week3/extractions.jsonl")
VERDICTS_PATH     = Path("outputs/week2/verdicts.jsonl")
QUARANTINE_DIR    = Path("outputs/quarantine")
REPORTS_DIR       = Path("validation_reports")
BASELINE_DIR      = Path("schema_snapshots/ai_baselines")
EMBEDDING_BASELINE = BASELINE_DIR / "embedding_centroid.npz"
VIOLATION_BASELINE = BASELINE_DIR / "violation_rate_baseline.json"

# ─────────────────────────────────────────────────────────────────────────────
# EXTENSION 1 — EMBEDDING DRIFT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["doc_id", "source_path", "content_preview"],
    "properties": {
        "doc_id":          {"type": "string", "minLength": 1},
        "source_path":     {"type": "string", "minLength": 1},
        "content_preview": {"type": "string", "maxLength": 8000},
    },
    "additionalProperties": False,
}


def _get_openai_client():
    """Return an OpenAI-compatible client using OPENAI_API_KEY or OPENROUTER_API_KEY."""
    if not HAS_OPENAI:
        return None
    api_key  = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    base_url = None
    if not os.getenv("OPENAI_API_KEY") and os.getenv("OPENROUTER_API_KEY"):
        base_url = "https://openrouter.ai/api/v1"
    if not api_key:
        return None
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return _openai_module.OpenAI(**kwargs)


def embed_sample(texts: list[str], n: int = 200, client=None) -> np.ndarray | None:
    """
    Embed up to n texts using text-embedding-3-small.
    Returns (n, dim) float32 array, or None if API unavailable.
    """
    if client is None:
        client = _get_openai_client()
    if client is None:
        return None

    sample = texts[:n] if len(texts) >= n else texts
    # filter out empty strings
    sample = [t for t in sample if t and t.strip()]
    if not sample:
        return None

    try:
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=sample,
        )
        vectors = np.array([e.embedding for e in resp.data], dtype=np.float32)
        return vectors
    except Exception as exc:
        print(f"  [embedding] API error: {exc}", file=sys.stderr)
        return None


def check_embedding_drift(
    texts: list[str],
    baseline_path: Path = EMBEDDING_BASELINE,
    threshold: float = 0.15,
    client=None,
) -> dict:
    """
    Extension 1 — Embedding Drift Detection.
    Embeds a sample of texts, computes centroid, compares to stored baseline
    via cosine distance. Saves/updates baseline on first run.
    """
    baseline_path = Path(baseline_path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)

    vectors = embed_sample(texts, n=200, client=client)

    if vectors is None:
        return {
            "extension":    "embedding_drift",
            "status":       "SKIPPED",
            "reason":       "OpenAI API not available (set OPENAI_API_KEY or OPENROUTER_API_KEY)",
            "drift_score":  None,
            "threshold":    threshold,
        }

    current_centroid = np.mean(vectors, axis=0)

    if not baseline_path.exists():
        np.savez(str(baseline_path), centroid=current_centroid)
        return {
            "extension":        "embedding_drift",
            "status":           "BASELINE_SET",
            "drift_score":      0.0,
            "threshold":        threshold,
            "texts_sampled":    len(vectors),
            "embedding_dim":    int(current_centroid.shape[0]),
            "message":          f"Baseline centroid saved to {baseline_path}. "
                                f"Drift will be measured on next run.",
        }

    baseline_data    = np.load(str(baseline_path))
    baseline_centroid = baseline_data["centroid"].astype(np.float32)

    # Cosine similarity → distance
    dot   = float(np.dot(current_centroid, baseline_centroid))
    norm  = float(np.linalg.norm(current_centroid) * np.linalg.norm(baseline_centroid) + 1e-9)
    cosine_sim = dot / norm
    drift      = round(1.0 - cosine_sim, 4)

    status = "FAIL" if drift > threshold else "PASS"
    return {
        "extension":     "embedding_drift",
        "status":        status,
        "drift_score":   drift,
        "cosine_sim":    round(cosine_sim, 4),
        "threshold":     threshold,
        "texts_sampled": len(vectors),
        "embedding_dim": int(current_centroid.shape[0]),
        "message": (
            f"Embedding drift={drift:.4f} vs threshold={threshold}. "
            f"{'DRIFT DETECTED — investigate text distribution change.' if status == 'FAIL' else 'Within acceptable bounds.'}"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXTENSION 2 — PROMPT INPUT SCHEMA VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt_input(record: dict) -> dict:
    """
    Convert an extractions record into the prompt input object that would
    have been passed into the extraction prompt template.
    Fields: doc_id, source_path, content_preview (first 8000 chars of extracted_facts text)
    """
    facts = record.get("extracted_facts", [])
    if isinstance(facts, list):
        texts = " ".join(
            f.get("text", "") if isinstance(f, dict) else str(f)
            for f in facts[:10]
        )
    else:
        texts = str(facts)

    return {
        "doc_id":          record.get("doc_id", ""),
        "source_path":     record.get("source_path", ""),
        "content_preview": texts[:8000],
    }


def check_prompt_input_schema(
    records: list[dict],
    quarantine_dir: Path = QUARANTINE_DIR,
    schema: dict = None,
) -> dict:
    """
    Extension 2 — Prompt Input Schema Validation.
    Validates each extraction record's prompt input object against PROMPT_INPUT_SCHEMA.
    Non-conforming records are written to outputs/quarantine/ — never silently dropped.
    """
    if schema is None:
        schema = PROMPT_INPUT_SCHEMA

    quarantine_dir = Path(quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    if not HAS_JSONSCHEMA:
        return {
            "extension": "prompt_input_schema",
            "status":    "SKIPPED",
            "reason":    "jsonschema not installed (pip install jsonschema)",
        }

    validator  = jsonschema.Draft7Validator(schema)
    passed     = []
    quarantined = []

    for rec in records:
        prompt_obj = _build_prompt_input(rec)
        errors     = list(validator.iter_errors(prompt_obj))
        if errors:
            quarantine_record = {
                "original_record": rec,
                "prompt_input":    prompt_obj,
                "validation_errors": [
                    {"path": ".".join(str(p) for p in e.absolute_path), "message": e.message}
                    for e in errors
                ],
                "quarantined_at": datetime.now(timezone.utc).isoformat(),
            }
            quarantined.append(quarantine_record)
        else:
            passed.append(rec)

    # Write quarantine file
    if quarantined:
        ts    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        qpath = quarantine_dir / f"prompt_input_quarantine_{ts}.jsonl"
        with open(qpath, "w", encoding="utf-8") as f:
            for q in quarantined:
                f.write(json.dumps(q) + "\n")
        quarantine_path = str(qpath)
    else:
        quarantine_path = None

    total      = len(records)
    n_failed   = len(quarantined)
    fail_rate  = round(n_failed / max(total, 1), 4)
    status     = "FAIL" if n_failed > 0 else "PASS"

    return {
        "extension":            "prompt_input_schema",
        "status":               status,
        "total_records":        total,
        "passed":               len(passed),
        "quarantined":          n_failed,
        "quarantine_rate":      fail_rate,
        "quarantine_file":      quarantine_path,
        "schema_used":          "PROMPT_INPUT_SCHEMA (doc_id, source_path, content_preview)",
        "message": (
            f"{n_failed}/{total} records failed prompt input schema validation "
            f"and were written to quarantine. "
            if n_failed else
            f"All {total} records passed prompt input schema validation."
        ),
        "sample_errors": [
            q["validation_errors"]
            for q in quarantined[:3]
        ] if quarantined else [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXTENSION 3 — LLM OUTPUT SCHEMA VIOLATION RATE
# ─────────────────────────────────────────────────────────────────────────────

VALID_VERDICTS = {"PASS", "FAIL", "WARN"}


def check_output_schema_violation_rate(
    verdict_records: list[dict],
    baseline_rate: float | None = None,
    warn_threshold: float = 0.02,
) -> dict:
    """
    Extension 3 — LLM Output Schema Violation Rate.
    Tracks the fraction of verdict records where overall_verdict is not in
    {PASS, FAIL, WARN}. A rising rate signals prompt degradation or model change.
    """
    total      = len(verdict_records)
    violations = sum(
        1 for v in verdict_records
        if v.get("overall_verdict") not in VALID_VERDICTS
    )
    rate  = round(violations / max(total, 1), 4)
    trend = "unknown"

    if baseline_rate is not None:
        if rate > baseline_rate * 1.5:
            trend = "rising"
        elif rate < baseline_rate * 0.5:
            trend = "falling"
        else:
            trend = "stable"

    status = "WARN" if rate > warn_threshold else "PASS"

    # Sample violating records
    sample_violations = [
        {"verdict_id": v.get("verdict_id"), "overall_verdict": v.get("overall_verdict")}
        for v in verdict_records
        if v.get("overall_verdict") not in VALID_VERDICTS
    ][:5]

    return {
        "extension":          "output_schema_violation_rate",
        "status":             status,
        "total_outputs":      total,
        "schema_violations":  violations,
        "violation_rate":     rate,
        "warn_threshold":     warn_threshold,
        "baseline_rate":      baseline_rate,
        "trend":              trend,
        "sample_violations":  sample_violations,
        "message": (
            f"Violation rate={rate:.1%} (threshold={warn_threshold:.1%}). "
            f"Trend vs baseline: {trend}. "
            f"{'Rate within acceptable bounds.' if status == 'PASS' else 'Rate exceeds warn threshold — investigate prompt or model change.'}"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def load_jsonl(path: Path) -> list[dict]:
    recs = []
    if not path.exists():
        return recs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return recs


def _load_violation_baseline() -> float | None:
    if VIOLATION_BASELINE.exists():
        try:
            data = json.loads(VIOLATION_BASELINE.read_text())
            return float(data.get("violation_rate", 0.0))
        except Exception:
            pass
    return None


def _save_violation_baseline(rate: float) -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    VIOLATION_BASELINE.write_text(json.dumps({
        "violation_rate": rate,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }))


def run_all_extensions(
    ext_filter: str | None = None,
    verbose: bool = True,
) -> dict:
    """Run all 3 AI extensions and return combined results dict."""
    results = {}
    client  = _get_openai_client()

    # ── Extension 1: Embedding Drift ─────────────────────────────────────────
    if ext_filter in (None, "embedding"):
        if verbose:
            print("\n[Extension 1] Embedding Drift Detection")
            print("  Loading extractions.jsonl ...")
        extraction_records = load_jsonl(EXTRACTIONS_PATH)
        # Collect text values from extracted_facts[*].text
        texts = []
        for rec in extraction_records:
            facts = rec.get("extracted_facts", [])
            if isinstance(facts, list):
                for f in facts:
                    t = f.get("text", "") if isinstance(f, dict) else str(f)
                    if t and t.strip():
                        texts.append(t.strip())
        if verbose:
            print(f"  Collected {len(texts)} text values from extracted_facts[*].text")
        result1 = check_embedding_drift(texts, EMBEDDING_BASELINE, client=client)
        results["embedding_drift"] = result1
        if verbose:
            print(f"  Status: {result1['status']}")
            print(f"  {result1.get('message', result1.get('detail', ''))}")

    # ── Extension 2: Prompt Input Schema Validation ───────────────────────────
    if ext_filter in (None, "prompt"):
        if verbose:
            print("\n[Extension 2] Prompt Input Schema Validation")
        extraction_records = load_jsonl(EXTRACTIONS_PATH)
        if verbose:
            print(f"  Validating {len(extraction_records)} records against PROMPT_INPUT_SCHEMA ...")
        result2 = check_prompt_input_schema(extraction_records)
        results["prompt_input_schema"] = result2
        if verbose:
            print(f"  Status: {result2['status']}")
            print(f"  {result2.get('message', result2.get('detail', ''))}")
            if result2.get("quarantine_file"):
                print(f"  Quarantine file: {result2['quarantine_file']}")

    # ── Extension 3: LLM Output Schema Violation Rate ─────────────────────────
    if ext_filter in (None, "violations"):
        if verbose:
            print("\n[Extension 3] LLM Output Schema Violation Rate")
        verdict_records = load_jsonl(VERDICTS_PATH)
        if verbose:
            print(f"  Checking {len(verdict_records)} verdict records ...")
        baseline_rate = _load_violation_baseline()
        result3 = check_output_schema_violation_rate(verdict_records, baseline_rate)
        results["output_schema_violations"] = result3
        # Save current rate as new baseline if none exists
        if baseline_rate is None:
            _save_violation_baseline(result3["violation_rate"])
            if verbose:
                print(f"  Baseline violation rate saved: {result3['violation_rate']}")
        if verbose:
            print(f"  Status: {result3['status']}")
            print(f"  {result3.get('message', result3.get('detail', ''))}")

    # ── Save combined report ──────────────────────────────────────────────────
    report = {
        "report_id":    str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "extensions":   results,
        "overall_status": (
            "FAIL"   if any(r.get("status") == "FAIL"  for r in results.values()) else
            "WARN"   if any(r.get("status") == "WARN"  for r in results.values()) else
            "PASS"   if any(r.get("status") == "PASS"  for r in results.values()) else
            "SKIPPED"
        ),
    }
    out_path = REPORTS_DIR / "ai_extensions_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))

    if verbose:
        print(f"\n  Overall AI extensions status: {report['overall_status']}")
        print(f"  Report saved: {out_path}")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Contract Extensions — Phase 4A"
    )
    parser.add_argument("--ext", choices=["embedding", "prompt", "violations"],
        help="Run only one extension (default: all)")
    parser.add_argument("--extractions", default=str(EXTRACTIONS_PATH),
        help="Path to week3 extractions JSONL")
    parser.add_argument("--verdicts", default=str(VERDICTS_PATH),
        help="Path to week2 verdicts JSONL")
    parser.add_argument("--threshold", type=float, default=0.15,
        help="Embedding drift threshold (default: 0.15)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import sys as _sys
    _mod = _sys.modules[__name__]
    _mod.EXTRACTIONS_PATH = Path(args.extractions)
    _mod.VERDICTS_PATH    = Path(args.verdicts)

    print("AI Contract Extensions — Phase 4A")
    print("=" * 50)
    run_all_extensions(ext_filter=args.ext, verbose=not args.quiet)


if __name__ == "__main__":
    main()

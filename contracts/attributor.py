#!/usr/bin/env python3
"""
ViolationAttributor — Phase 2B: Data Contract Enforcer
Week 7 Challenge — Schema Integrity & Lineage Attribution System

For every FAIL result in a validation report:
  1. BFS upstream on the Week 4 lineage graph from the failing column
  2. git log --follow --since="14 days ago" + git blame for each upstream file
  3. Compute confidence: 1.0 - (days_since_commit × 0.1) - (lineage_hops × 0.2)
  4. Rank candidates (min 1, max 5)
  5. Compute blast radius from downstream lineage

Output: violation_log/violations.jsonl (one JSON object per line)

Output schema per violation
────────────────────────────
{
  "violation_id":  "uuid-v4",
  "check_id":      "week3.extracted_facts.confidence.range",
  "detected_at":   "ISO 8601",
  "blame_chain": [{
    "rank":             1,
    "file_path":        "src/week3/extractor.py",
    "commit_hash":      "abc123def456...",
    "author":           "jane.doe@example.com",
    "commit_timestamp": "2025-01-14T09:00:00Z",
    "commit_message":   "feat: change confidence to percentage scale",
    "confidence_score": 0.94
  }],
  "blast_radius": {
    "affected_nodes":     ["file::src/week4/cartographer.py"],
    "affected_pipelines": ["week4-lineage-generation"],
    "estimated_records":  847
  }
}

Usage
─────
  python contracts/attributor.py \\
      --report  validation_reports/week3_extractions_report.json \\
      --lineage outputs/week4/lineage_snapshots.jsonl \\
      --output  violation_log/violations.jsonl

  python contracts/attributor.py \\
      --reports  validation_reports/ \\
      --lineage  outputs/week4/lineage_snapshots.jsonl \\
      --output   violation_log/violations.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_BLAME_CANDIDATES  = 5
MIN_BLAME_CANDIDATES  = 1
GIT_LOOKBACK_DAYS     = 14
CONF_DAYS_PENALTY     = 0.1   # per day since commit
CONF_HOPS_PENALTY     = 0.2   # per lineage hop
CONF_FLOOR            = 0.05  # minimum confidence score

# Mapping from dataset/contract name → source files that likely caused violations
DATASET_SOURCE_FILES: dict[str, list[str]] = {
    "week1_intent_records": [
        "contracts/migrate/migrate_week1.py",
        "outputs/week1/intent_records.jsonl",
    ],
    "week2_verdicts": [
        "contracts/migrate/migrate_week2.py",
        "outputs/week2/verdicts.jsonl",
    ],
    "week3_extractions": [
        "contracts/migrate/migrate_week3.py",
        "outputs/week3/extractions.jsonl",
    ],
    "week4_lineage": [
        "contracts/migrate/migrate_week4.py",
        "outputs/week4/lineage_snapshots.jsonl",
    ],
    "week5_events": [
        "contracts/migrate/migrate_week5.py",
        "outputs/week5/events.jsonl",
    ],
    "langsmith_traces": [
        "contracts/migrate/generate_traces.py",
        "outputs/traces/runs.jsonl",
    ],
}

# Mapping from contract name → downstream pipelines for blast_radius
DOWNSTREAM_PIPELINES: dict[str, list[str]] = {
    "week1_intent_records": [
        "week2-verdict-generation",
        "week4-lineage-generation",
    ],
    "week2_verdicts": [
        "week4-lineage-generation",
        "week7-contract-generation",
    ],
    "week3_extractions": [
        "week4-lineage-generation",
        "week5-event-sourcing",
    ],
    "week4_lineage": [
        "week7-contract-generation",
        "week7-validation-runner",
    ],
    "week5_events": [
        "week7-contract-generation",
        "week7-validation-runner",
    ],
    "langsmith_traces": [
        "week7-contract-generation",
        "week7-validation-runner",
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def find_git_root() -> Path | None:
    """Walk up from cwd to find .git directory."""
    p = Path.cwd()
    for _ in range(10):
        if (p / ".git").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a shell command; returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "git not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as exc:
        return -1, "", str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# LINEAGE GRAPH (Week 4 BFS)
# ─────────────────────────────────────────────────────────────────────────────

def build_lineage_graph(lineage_records: list[dict]) -> dict:
    """
    Build an adjacency structure from the Week 4 lineage JSONL.

    Each record may be:
      { "nodes": [...], "edges": [...], ... }  (graph snapshot)
      { "source": "...", "target": "...", ... } (individual edge)

    Returns:
      {
        "forward":  { node_id: [child_ids] },   upstream → downstream
        "backward": { node_id: [parent_ids] },  downstream → upstream
        "labels":   { node_id: label_string },
        "nodes":    { node_id: node_dict },
      }
    """
    forward:  dict[str, list[str]] = defaultdict(list)
    backward: dict[str, list[str]] = defaultdict(list)
    labels:   dict[str, str]       = {}
    nodes_map: dict[str, dict]     = {}

    def _add_edge(src: str, tgt: str) -> None:
        if tgt not in forward[src]:
            forward[src].append(tgt)
        if src not in backward[tgt]:
            backward[tgt].append(src)

    def _add_node(n: dict) -> None:
        nid   = n.get("id", n.get("node_id", ""))
        label = n.get("label", n.get("name", n.get("path", nid)))
        if nid:
            labels[nid]    = label
            nodes_map[nid] = n

    for rec in lineage_records:
        # Graph snapshot format
        if "nodes" in rec and "edges" in rec:
            for n in (rec.get("nodes") or []):
                if isinstance(n, dict):
                    _add_node(n)
            for e in (rec.get("edges") or []):
                if isinstance(e, dict):
                    src = e.get("source", e.get("from", ""))
                    tgt = e.get("target", e.get("to",   ""))
                    if src and tgt:
                        _add_edge(src, tgt)

        # Individual edge format
        elif "source" in rec and "target" in rec:
            src = rec["source"]
            tgt = rec["target"]
            _add_edge(src, tgt)

    return {
        "forward":  dict(forward),
        "backward": dict(backward),
        "labels":   labels,
        "nodes":    nodes_map,
    }


def bfs_upstream(
    graph: dict,
    start_nodes: list[str],
    max_hops: int = 5,
) -> list[tuple[str, int]]:
    """
    BFS upstream (backward edges) from start_nodes.
    Returns list of (node_id, hop_count) in BFS order.
    """
    backward = graph.get("backward", {})
    visited: set[str]   = set(start_nodes)
    queue:   deque      = deque((n, 0) for n in start_nodes)
    result:  list       = []

    while queue:
        node, hops = queue.popleft()
        if hops > 0:
            result.append((node, hops))
        if hops >= max_hops:
            continue
        for parent in backward.get(node, []):
            if parent not in visited:
                visited.add(parent)
                queue.append((parent, hops + 1))

    return result


def bfs_downstream(
    graph: dict,
    start_nodes: list[str],
    max_hops: int = 5,
) -> list[str]:
    """BFS downstream (forward edges) from start_nodes."""
    forward  = graph.get("forward", {})
    visited: set[str] = set(start_nodes)
    queue:   deque    = deque(start_nodes)
    result:  list[str] = []

    while queue:
        node = queue.popleft()
        for child in forward.get(node, []):
            if child not in visited:
                visited.add(child)
                result.append(child)
                queue.append(child)

    return result


def find_dataset_nodes(graph: dict, dataset_name: str) -> list[str]:
    """
    Find node IDs in the lineage graph that match the dataset_name.
    Tries: exact label match, stem match, fuzzy substring match.
    """
    labels = graph.get("labels", {})
    # Strip week prefix for matching: "week3_extractions" → "extractions"
    parts = dataset_name.split("_", 1)
    short = parts[1] if len(parts) == 2 else dataset_name

    candidates: list[str] = []
    for nid, label in labels.items():
        label_lower = label.lower()
        if (dataset_name.lower() in label_lower or
                short.lower()        in label_lower or
                label_lower          in dataset_name.lower()):
            candidates.append(nid)

    # Fall back: check node paths/types
    if not candidates:
        nodes = graph.get("nodes", {})
        for nid, n in nodes.items():
            path = str(n.get("path", "")).lower()
            if short.lower() in path or dataset_name.lower() in path:
                candidates.append(nid)

    return candidates


def resolve_node_to_file(node_id: str, graph: dict) -> str | None:
    """Try to extract a file path from a lineage node."""
    node = graph.get("nodes", {}).get(node_id, {})
    for key in ("path", "file_path", "source_file", "module"):
        v = node.get(key, "")
        if v and ("." in v or "/" in v):
            return str(v)
    label = graph.get("labels", {}).get(node_id, "")
    if label and ("/" in label or label.endswith(".py") or label.endswith(".jsonl")):
        return label
    return None


# ─────────────────────────────────────────────────────────────────────────────
# GIT ATTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def git_recent_commits(
    file_path: str,
    git_root: Path,
    since_days: int = GIT_LOOKBACK_DAYS,
) -> list[dict]:
    """
    git log --follow --since=<N days ago> for a single file.
    Returns list of commit dicts: {hash, author, timestamp, message}.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    cmd = [
        "git", "log",
        "--follow",
        f"--since={since}",
        "--format=%H|||%ae|||%aI|||%s",
        "--",
        file_path,
    ]
    rc, stdout, _ = run_cmd(cmd, cwd=git_root)
    if rc != 0 or not stdout.strip():
        return []

    commits: list[dict] = []
    for line in stdout.strip().splitlines():
        parts = line.split("|||", 3)
        if len(parts) == 4:
            commits.append({
                "commit_hash":      parts[0].strip(),
                "author":           parts[1].strip(),
                "commit_timestamp": parts[2].strip(),
                "commit_message":   parts[3].strip(),
            })
    return commits


def git_blame_file(
    file_path: str,
    git_root: Path,
    max_lines: int = 10,
) -> list[dict]:
    """
    git blame -p <file> — returns first N entries.
    Returns list of blame entries: {commit_hash, author, timestamp, line}.
    """
    cmd = ["git", "blame", "-p", "--", file_path]
    rc, stdout, _ = run_cmd(cmd, cwd=git_root)
    if rc != 0 or not stdout.strip():
        return []

    entries: list[dict] = []
    current: dict = {}
    for line in stdout.splitlines():
        if re.match(r"^[0-9a-f]{40}", line):
            if current:
                entries.append(current)
                if len(entries) >= max_lines:
                    break
            current = {"commit_hash": line.split()[0]}
        elif line.startswith("author-mail "):
            current["author"] = line[12:].strip("<>")
        elif line.startswith("author-time "):
            try:
                ts = int(line[12:].strip())
                current["commit_timestamp"] = datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        elif line.startswith("summary "):
            current["commit_message"] = line[8:].strip()
        elif line.startswith("\t"):
            current["line"] = line[1:]

    if current and "commit_hash" in current:
        entries.append(current)

    return entries[:max_lines]


def days_since(timestamp_str: str) -> float:
    """Parse ISO 8601 timestamp and return float days since that time."""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        return max(0.0, delta.total_seconds() / 86400)
    except Exception:
        return 7.0   # default to 7 days if unparseable


def compute_confidence(days: float, hops: int) -> float:
    """
    confidence = 1.0 - (days × 0.1) - (hops × 0.2), clamped to [CONF_FLOOR, 1.0]
    """
    raw = 1.0 - (days * CONF_DAYS_PENALTY) - (hops * CONF_HOPS_PENALTY)
    return round(max(CONF_FLOOR, min(1.0, raw)), 4)


def build_blame_chain(
    upstream_nodes:  list[tuple[str, int]],
    graph:           dict,
    git_root:        Path | None,
    fallback_files:  list[str],
    check_id:        str,
) -> list[dict]:
    """
    Build blame_chain from upstream lineage nodes + git history.

    Strategy:
      1. For each upstream node, try to extract a file path
      2. Run git log --follow for that file path
      3. Compute confidence = 1.0 - (days × 0.1) - (hops × 0.2)
      4. Sort by confidence descending, return top MAX_BLAME_CANDIDATES

    If git is not available or no commits found, produce synthetic entries
    from fallback_files to guarantee MIN_BLAME_CANDIDATES.
    """
    candidates: list[dict] = []
    seen_files: set[str]   = set()

    # ── Phase 1: try real git history ─────────────────────────────────────────
    if git_root:
        for node_id, hops in upstream_nodes:
            file_path = resolve_node_to_file(node_id, graph)
            if not file_path or file_path in seen_files:
                continue
            seen_files.add(file_path)

            commits = git_recent_commits(file_path, git_root)
            if not commits:
                # Try git blame as fallback
                blame = git_blame_file(file_path, git_root, max_lines=3)
                commits = blame

            for commit in commits[:2]:   # take at most 2 commits per file
                ts     = commit.get("commit_timestamp", "")
                days   = days_since(ts)
                conf   = compute_confidence(days, hops)
                candidates.append({
                    "file_path":        file_path,
                    "commit_hash":      commit.get("commit_hash", "unknown"),
                    "author":           commit.get("author", "unknown"),
                    "commit_timestamp": ts,
                    "commit_message":   commit.get("commit_message", "(no message)"),
                    "confidence_score": conf,
                    "_hops":            hops,
                    "_days":            days,
                })

        # Also try fallback_files through git
        for fpath in fallback_files:
            if fpath in seen_files:
                continue
            seen_files.add(fpath)
            commits = git_recent_commits(fpath, git_root)
            for commit in commits[:1]:
                ts   = commit.get("commit_timestamp", "")
                days = days_since(ts)
                conf = compute_confidence(days, hops=1)
                candidates.append({
                    "file_path":        fpath,
                    "commit_hash":      commit.get("commit_hash", "unknown"),
                    "author":           commit.get("author", "unknown"),
                    "commit_timestamp": ts,
                    "commit_message":   commit.get("commit_message", "(no message)"),
                    "confidence_score": conf,
                    "_hops":            1,
                    "_days":            days,
                })

    # ── Phase 2: synthetic fallbacks to guarantee MIN_BLAME_CANDIDATES ────────
    if len(candidates) < MIN_BLAME_CANDIDATES:
        for fpath in (fallback_files or ["src/unknown.py"]):
            if any(c["file_path"] == fpath for c in candidates):
                continue
            # Use git blame if possible; otherwise synthetic
            blame_entries = []
            if git_root:
                blame_entries = git_blame_file(fpath, git_root, max_lines=1)

            if blame_entries:
                be   = blame_entries[0]
                ts   = be.get("commit_timestamp", now_iso())
                days = days_since(ts)
                candidates.append({
                    "file_path":        fpath,
                    "commit_hash":      be.get("commit_hash", "0000000000000000000000000000000000000000"),
                    "author":           be.get("author", "unknown@unknown.com"),
                    "commit_timestamp": ts,
                    "commit_message":   be.get("commit_message", "(blame entry)"),
                    "confidence_score": compute_confidence(days, hops=1),
                    "_hops":            1,
                    "_days":            days,
                })
            else:
                candidates.append({
                    "file_path":        fpath,
                    "commit_hash":      "0000000000000000000000000000000000000000",
                    "author":           "unknown@unknown.com",
                    "commit_timestamp": now_iso(),
                    "commit_message":   f"(no recent git history for {fpath})",
                    "confidence_score": CONF_FLOOR,
                    "_hops":            1,
                    "_days":            0.0,
                })

            if len(candidates) >= MIN_BLAME_CANDIDATES:
                break

    # ── Sort by confidence descending, dedup, limit to MAX ───────────────────
    candidates.sort(key=lambda c: c["confidence_score"], reverse=True)
    # Deduplicate by commit_hash
    seen_hashes: set[str] = set()
    deduped: list[dict]   = []
    for c in candidates:
        h = c["commit_hash"]
        if h not in seen_hashes:
            seen_hashes.add(h)
            deduped.append(c)

    top = deduped[:MAX_BLAME_CANDIDATES]

    # ── Add rank, remove internal keys ───────────────────────────────────────
    blame_chain: list[dict] = []
    for rank, c in enumerate(top, start=1):
        blame_chain.append({
            "rank":             rank,
            "file_path":        c["file_path"],
            "commit_hash":      c["commit_hash"],
            "author":           c["author"],
            "commit_timestamp": c["commit_timestamp"],
            "commit_message":   c["commit_message"],
            "confidence_score": c["confidence_score"],
        })

    return blame_chain


def build_blast_radius(
    dataset_name:   str,
    graph:          dict,
    start_nodes:    list[str],
    records_failing: int,
) -> dict:
    """
    Compute blast_radius:
      - affected_nodes:     downstream nodes in the lineage graph
      - affected_pipelines: from DOWNSTREAM_PIPELINES lookup
      - estimated_records:  records_failing (or 0 if unknown)
    """
    # BFS downstream
    downstream_ids = bfs_downstream(graph, start_nodes, max_hops=4)
    labels         = graph.get("labels", {})

    affected_nodes: list[str] = []
    for nid in downstream_ids[:10]:
        label = labels.get(nid, nid)
        # Format as "file::<path>" if it looks like a path
        if "/" in label or label.endswith(".py") or label.endswith(".jsonl"):
            affected_nodes.append(f"file::{label}")
        else:
            affected_nodes.append(label)

    # If no downstream nodes found from graph, use lookup
    if not affected_nodes:
        affected_nodes = [
            f"dataset::{ds}"
            for ds in DOWNSTREAM_PIPELINES.get(dataset_name, [])
        ]

    affected_pipelines = DOWNSTREAM_PIPELINES.get(dataset_name, ["unknown-pipeline"])

    return {
        "affected_nodes":     affected_nodes,
        "affected_pipelines": affected_pipelines,
        "estimated_records":  records_failing,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ATTRIBUTOR
# ─────────────────────────────────────────────────────────────────────────────

def attribute_report(
    report:        dict,
    graph:         dict,
    git_root:      Path | None,
    output_path:   Path,
    append_mode:   bool = True,
) -> list[dict]:
    """
    Process one validation report dict and write violations to output_path.
    Returns list of violation dicts written.
    """
    violations: list[dict] = []

    # Extract failing checks
    results = report.get("results", [])
    failing = [r for r in results if r["status"] in ("FAIL", "ERROR")]

    if not failing:
        return violations

    # Infer dataset_name from contract_file or contract_id
    contract_file = report.get("contract_file", "")
    dataset_name  = Path(contract_file).stem if contract_file else report.get("contract_id", "unknown")

    fallback_files = DATASET_SOURCE_FILES.get(dataset_name, [dataset_name])

    # Find lineage start nodes for this dataset
    start_nodes = find_dataset_nodes(graph, dataset_name)

    for result in failing:
        check_id        = result.get("check_id", "unknown.check")
        column_name     = result.get("column_name", "*")
        records_failing = result.get("records_failing", 0)
        detected_at     = report.get("run_timestamp", now_iso())

        # BFS upstream from dataset nodes
        upstream_nodes = bfs_upstream(graph, start_nodes, max_hops=4)

        # Build blame chain
        blame_chain = build_blame_chain(
            upstream_nodes  = upstream_nodes,
            graph           = graph,
            git_root        = git_root,
            fallback_files  = fallback_files,
            check_id        = check_id,
        )

        # Build blast radius
        blast_radius = build_blast_radius(
            dataset_name    = dataset_name,
            graph           = graph,
            start_nodes     = start_nodes,
            records_failing = records_failing,
        )

        violation = {
            "violation_id": str(uuid.uuid4()),
            "check_id":     check_id,
            "column_name":  column_name,
            "dataset":      dataset_name,
            "detected_at":  detected_at,
            "status":       result.get("status", "FAIL"),
            "severity":     result.get("severity", "MEDIUM"),
            "actual_value": result.get("actual_value", ""),
            "expected":     result.get("expected", ""),
            "message":      result.get("message", ""),
            "blame_chain":  blame_chain,
            "blast_radius": blast_radius,
        }
        violations.append(violation)

    if violations:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append_mode else "w"
        with open(output_path, mode, encoding="utf-8") as fh:
            for v in violations:
                fh.write(json.dumps(v) + "\n")

    return violations


def print_violation_summary(violation: dict) -> None:
    sev_icon = {
        "CRITICAL": "🔴",
        "HIGH":     "🟠",
        "MEDIUM":   "🟡",
        "WARNING":  "🔵",
        "LOW":      "⚪",
    }.get(violation["severity"], "❓")

    print(f"  {sev_icon} [{violation['severity']:<8}] {violation['check_id']}")
    if violation["blame_chain"]:
        top = violation["blame_chain"][0]
        print(f"       → Attributed to: {top['author']}  "
              f"(conf={top['confidence_score']})  "
              f"{top['file_path']}")
        print(f"         commit: {top['commit_hash'][:12]}  "
              f"\"{top['commit_message'][:60]}\"")
    br = violation["blast_radius"]
    if br["affected_pipelines"]:
        print(f"       → Blast radius: {', '.join(br['affected_pipelines'])}  "
              f"({br['estimated_records']} records)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ViolationAttributor — blame-chain + blast radius for contract violations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single report
  python contracts/attributor.py \\
      --report  validation_reports/week3_extractions_report.json \\
      --lineage outputs/week4/lineage_snapshots.jsonl \\
      --output  violation_log/violations.jsonl

  # All reports in a directory
  python contracts/attributor.py \\
      --reports  validation_reports/ \\
      --lineage  outputs/week4/lineage_snapshots.jsonl \\
      --output   violation_log/violations.jsonl
        """,
    )

    parser.add_argument("--report",  type=Path, help="Path to a single validation report JSON")
    parser.add_argument("--reports", type=Path, help="Directory of validation report JSON files")
    parser.add_argument(
        "--lineage",
        type=Path,
        default=Path("outputs/week4/lineage_snapshots.jsonl"),
        help="Path to Week 4 lineage JSONL (default: outputs/week4/lineage_snapshots.jsonl)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("violation_log/violations.jsonl"),
        help="Output JSONL path (default: violation_log/violations.jsonl)",
    )
    parser.add_argument(
        "--git-root",
        type=Path,
        default=None,
        help="Git repository root (auto-detected if omitted)",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Skip all git operations (useful if not in a git repo)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        default=True,
        help="Append to existing violations.jsonl (default: True)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite violations.jsonl instead of appending",
    )

    args = parser.parse_args()

    # ── git root ──────────────────────────────────────────────────────────────
    git_root: Path | None = None
    if not args.no_git:
        git_root = args.git_root or find_git_root()
        if git_root:
            print(f"  Git root detected: {git_root}")
        else:
            print("  ⚠️  No git repository found — blame chain will use synthetic entries.")

    # ── load lineage graph ────────────────────────────────────────────────────
    lineage_records = load_jsonl(args.lineage)
    if lineage_records:
        print(f"  Loaded {len(lineage_records)} lineage records from {args.lineage}")
    else:
        print(f"  ⚠️  No lineage records loaded from {args.lineage}")
    graph = build_lineage_graph(lineage_records)
    n_nodes = len(graph["labels"])
    n_edges = sum(len(v) for v in graph["forward"].values())
    print(f"  Lineage graph: {n_nodes} nodes, {n_edges} edges")

    # ── collect report paths ──────────────────────────────────────────────────
    report_paths: list[Path] = []
    if args.report:
        report_paths = [args.report]
    elif args.reports:
        report_paths = sorted(args.reports.glob("*_report.json"))
        # Also try validation_summary.json children
        if not report_paths:
            report_paths = sorted(args.reports.glob("*.json"))
            report_paths = [p for p in report_paths if not p.name.startswith("validation_summary")]
    else:
        parser.print_help()
        sys.exit(1)

    if not report_paths:
        print(f"No report JSON files found.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'═'*70}")
    print(f"  ViolationAttributor")
    print(f"  Reports   : {len(report_paths)} file(s)")
    print(f"  Output    : {args.output}")
    print(f"{'═'*70}\n")

    append_mode    = not args.overwrite
    total_violations = 0
    total_reports_with_violations = 0

    # Clear output file if overwrite
    if args.overwrite and args.output.exists():
        args.output.unlink()

    for rpath in report_paths:
        report = load_json(rpath)
        if not report:
            print(f"  ⚠️  Cannot load {rpath.name}")
            continue

        failing = [r for r in report.get("results", []) if r["status"] in ("FAIL", "ERROR")]
        if not failing:
            status = report.get("overall_status", "?")
            print(f"  ✅ {rpath.stem:<45} [{status}]  no violations")
            continue

        violations = attribute_report(
            report       = report,
            graph        = graph,
            git_root     = git_root,
            output_path  = args.output,
            append_mode  = append_mode,
        )
        append_mode = True   # always append after first file

        if violations:
            total_violations             += len(violations)
            total_reports_with_violations += 1
            print(f"  ❌ {rpath.stem:<45}  {len(violations)} violation(s)")
            for v in violations:
                print_violation_summary(v)
        print()

    # ── final summary ─────────────────────────────────────────────────────────
    print(f"{'─'*70}")
    print(f"  Total reports processed    : {len(report_paths)}")
    print(f"  Reports with violations    : {total_reports_with_violations}")
    print(f"  Total violations attributed: {total_violations}")
    if total_violations > 0:
        print(f"  Violations written → {args.output}")
    else:
        print(f"  ✅ No violations found — all contracts passing.")
    print(f"{'─'*70}")


if __name__ == "__main__":
    main()
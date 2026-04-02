#!/usr/bin/env python3
"""
test_system.py  -  Full system verification for the Data Contract Enforcer
TenX Academy Week 7

Run AFTER the full pipeline:
  python contracts/runner.py --contracts generated_contracts/ --outputs outputs/ --report validation_reports/
  python contracts/schema_analyzer.py --all
  python contracts/ai_extensions.py
  python contracts/report_generator.py

Then:
  python test_system.py

Exit 0 = all checks passed
Exit 1 = one or more checks failed
"""

import json
import sys
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
PASS  = "OK "
FAIL  = "FAIL"
results = []


def _load_json(path) -> dict:
    """Load JSON, stripping null bytes that Windows can embed in files."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().replace("\x00", "")
    return json.loads(raw)


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = "  [PASS]" if ok else "  [FAIL]"
    suffix = f"  ({detail})" if detail else ""
    print(f"{icon}  {label}{suffix}")
    results.append(ok)
    return ok


def section(title: str) -> None:
    print(f"\n{'─' * 62}")
    print(f"  {title}")
    print(f"{'─' * 62}")


# ─────────────────────────────────────────────────────────────────
# Phase 1A — Repository Layout
# ─────────────────────────────────────────────────────────────────
section("Phase 1A - Repository Layout")

for s in [
    "contracts/generator.py", "contracts/runner.py", "contracts/attributor.py",
    "contracts/schema_analyzer.py", "contracts/ai_extensions.py",
    "contracts/report_generator.py",
]:
    check(s, (ROOT / s).exists())

for c in [
    "generated_contracts/week1_intent_records.yaml",
    "generated_contracts/week2_verdicts.yaml",
    "generated_contracts/week3_extractions.yaml",
    "generated_contracts/week4_lineage.yaml",
    "generated_contracts/week5_events.yaml",
    "generated_contracts/langsmith_traces.yaml",
]:
    check(c, (ROOT / c).exists())
    dbt = c.replace(".yaml", "_dbt.yml")
    check(dbt, (ROOT / dbt).exists())

for d in ["validation_reports", "violation_log", "schema_snapshots", "enforcer_report"]:
    check(f"{d}/", (ROOT / d).is_dir())

for i in [
    "outputs/week1/intent_records.jsonl", "outputs/week2/verdicts.jsonl",
    "outputs/week3/extractions.jsonl",    "outputs/week4/lineage_snapshots.jsonl",
    "outputs/week5/events.jsonl",
]:
    check(i, (ROOT / i).exists())

check("DOMAIN_NOTES.md", (ROOT / "DOMAIN_NOTES.md").exists())


# ─────────────────────────────────────────────────────────────────
# Phase 0 — DOMAIN_NOTES.md
# ─────────────────────────────────────────────────────────────────
section("Phase 0 - DOMAIN_NOTES.md (5 questions)")
try:
    notes = (ROOT / "DOMAIN_NOTES.md").read_text(encoding="utf-8")
    for i in range(1, 6):
        check(f"Question {i} present", f"Question {i}" in notes)
    check("Length > 200 lines", len(notes.splitlines()) > 200,
          f"{len(notes.splitlines())} lines")
except Exception as e:
    check("DOMAIN_NOTES.md readable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 1B — ContractRegistry
# ─────────────────────────────────────────────────────────────────
section("Phase 1B - ContractRegistry")
reg_path = ROOT / "contract_registry" / "subscriptions.yaml"
check("contract_registry/subscriptions.yaml exists", reg_path.exists())

if reg_path.exists():
    try:
        import yaml
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f)
        subs = reg.get("subscriptions", [])
        check(f"At least 4 subscriptions", len(subs) >= 4, f"{len(subs)} found")
        for cid, sid in [
            ("week3-document-refinery-extractions", "week4-cartographer"),
            ("week4-cartographer-lineage",           "week7-enforcer"),
            ("week5-ledger-events",                  "week7-enforcer"),
            ("langsmith-traces",                     "week7-enforcer"),
        ]:
            found = any(s.get("contract_id") == cid and s.get("subscriber_id") == sid
                        for s in subs)
            check(f"  {cid[:38]} -> {sid}", found)
    except ImportError:
        print("  [SKIP] PyYAML not available")
    except Exception as e:
        check("subscriptions.yaml parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 2A — ValidationRunner output schema
# ─────────────────────────────────────────────────────────────────
section("Phase 2A - ValidationRunner output schema")

summary_path = ROOT / "validation_reports" / "validation_summary.json"
check("validation_summary.json exists", summary_path.exists())

if summary_path.exists():
    try:
        summary = _load_json(summary_path)
        for field in ("total_checks", "passed", "failed", "warned",
                      "overall_status", "contract_reports"):
            check(f"  summary.{field}", field in summary)
        check("  total_checks >= 1", summary.get("total_checks", 0) >= 1,
              str(summary.get("total_checks")))
    except Exception as e:
        check("validation_summary.json parseable", False, str(e))

SKIP = ("ai_extensions", "validation_summary", "migration_impact", "schema_evolution")
report_files = sorted(
    p for p in (ROOT / "validation_reports").glob("*_report.json")
    if not any(p.name.startswith(x) for x in SKIP)
)
check(f"Individual *_report.json files present ({len(report_files)})",
      len(report_files) >= 1)

if report_files:
    try:
        rep = _load_json(report_files[0])
        for field in ("report_id", "contract_id", "run_timestamp",
                      "total_checks", "passed", "failed", "results"):
            check(f"  report.{field}", field in rep)
        if rep.get("results"):
            r = rep["results"][0]
            for field in ("check_id", "column_name", "check_type",
                          "status", "severity", "message"):
                check(f"  result_item.{field}", field in r)
    except Exception as e:
        check("Individual report parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 2B — ViolationAttributor
# ─────────────────────────────────────────────────────────────────
section("Phase 2B - ViolationAttributor output schema")

viol_path = ROOT / "violation_log" / "violations.jsonl"
check("violation_log/violations.jsonl exists", viol_path.exists())

if viol_path.exists():
    try:
        with open(viol_path, encoding="utf-8", errors="replace") as f:
            violations = [json.loads(l.replace("\x00", ""))
                          for l in f if l.strip() and not l.startswith("#")]
        check(f"At least 1 violation logged", len(violations) >= 1,
              f"{len(violations)} found")
        if violations:
            v = violations[0]
            for field in ("violation_id", "check_id", "detected_at",
                          "blame_chain", "blast_radius"):
                check(f"  violation.{field}", field in v)
            if v.get("blame_chain"):
                bc = v["blame_chain"][0]
                for field in ("rank", "file_path", "commit_hash", "author",
                              "commit_timestamp", "confidence_score"):
                    check(f"  blame_chain[0].{field}", field in bc)
            br = v.get("blast_radius", {})
            check("  blast_radius.affected_nodes", "affected_nodes" in br)
            check("  blast_radius.registry_subscribers", "registry_subscribers" in br)
            has_registry = any(
                vv.get("blast_radius", {}).get("registry_subscribers")
                for vv in violations
            )
            check("  Registry-sourced subscribers present", has_registry)
    except Exception as e:
        check("violations.jsonl parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 2 — Statistical Drift Rule
# ─────────────────────────────────────────────────────────────────
section("Phase 2 - Statistical Drift Rule (baselines)")

baselines_path = ROOT / "schema_snapshots" / "baselines.json"
check("schema_snapshots/baselines.json exists", baselines_path.exists())
if baselines_path.exists():
    try:
        bl = _load_json(baselines_path)
        check("Baselines non-empty", len(bl) >= 1, f"{len(bl)} contracts")
    except Exception as e:
        check("baselines.json parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 3 — SchemaEvolutionAnalyzer
# ─────────────────────────────────────────────────────────────────
section("Phase 3 - SchemaEvolutionAnalyzer")

evo_files = sorted((ROOT / "validation_reports").glob("schema_evolution_*.json"))
check(f"schema_evolution_*.json reports ({len(evo_files)})", len(evo_files) >= 1)

migration_files = list((ROOT / "validation_reports").glob("migration_impact_*.json"))
check(f"migration_impact_*.json for breaking change ({len(migration_files)})",
      len(migration_files) >= 1)

snapshots = list((ROOT / "schema_snapshots").rglob("*.yaml"))
check(f"Schema snapshots present ({len(snapshots)})", len(snapshots) >= 1)

if evo_files:
    try:
        evo = _load_json(evo_files[0])
        for field in ("contract_id", "generated_at", "diffs", "summary"):
            check(f"  evolution_report.{field}", field in evo)
    except Exception as e:
        check("schema_evolution file parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Phase 4A — AI Contract Extensions
# ─────────────────────────────────────────────────────────────────
section("Phase 4A - AI Contract Extensions")

ai_path = ROOT / "validation_reports" / "ai_extensions_report.json"
check("ai_extensions_report.json exists", ai_path.exists())

if ai_path.exists():
    try:
        ai = _load_json(ai_path)
        exts = ai.get("extensions", {})
        for ext in ("embedding_drift", "prompt_input_schema", "output_schema_violations"):
            check(f"  extension.{ext} present", ext in exts)
            if ext in exts:
                check(f"  extension.{ext}.status", "status" in exts[ext])
        check("  overall_status present", "overall_status" in ai)
    except Exception as e:
        check("ai_extensions_report.json parseable", False, str(e))

embedding_baseline = ROOT / "schema_snapshots" / "ai_baselines" / "embedding_centroid.npz"
check("Embedding baseline centroid (.npz) exists", embedding_baseline.exists())


# ─────────────────────────────────────────────────────────────────
# Phase 4B — Enforcer Report PDF
# ─────────────────────────────────────────────────────────────────
section("Phase 4B - Enforcer Report PDF")

pdf_files = sorted((ROOT / "enforcer_report").glob("report_*.pdf"))
check(f"enforcer_report/report_*.pdf exists ({len(pdf_files)})", len(pdf_files) >= 1)
if pdf_files:
    with open(pdf_files[0], "rb") as f:
        header = f.read(8)
    check("  PDF has valid %PDF header", header.startswith(b"%PDF"), pdf_files[0].name)
    size_kb = pdf_files[0].stat().st_size // 1024
    check("  PDF size > 10 KB", size_kb > 10, f"{size_kb} KB")

json_files = sorted((ROOT / "enforcer_report").glob("report_*.json"))
check("enforcer_report/report_*.json exists", len(json_files) >= 1)
if json_files:
    try:
        rpt = _load_json(json_files[0])
        for key in ("health_score", "violations", "schema_changes",
                    "ai_risk", "recommended_actions"):
            check(f"  report.{key}", key in rpt)
        check("  health_score is numeric",
              isinstance(rpt.get("health_score", {}).get("score"), (int, float)))
        actions = rpt.get("recommended_actions", [])
        check(f"  3 recommended actions", len(actions) == 3, f"{len(actions)} found")
    except Exception as e:
        check("enforcer report JSON parseable", False, str(e))


# ─────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────
total  = len(results)
passed = sum(results)
failed = total - passed

print(f"\n{'=' * 62}")
print(f"  SYSTEM VERIFICATION SUMMARY")
print(f"{'=' * 62}")
print(f"  Total checks : {total}")
print(f"  Passed       : {passed}  [OK]")
print(f"  Failed       : {failed}  {'[FAIL]' if failed else '[OK]'}")
print(f"{'─' * 62}")
if failed == 0:
    print("  ALL CHECKS PASSED - system is fully operational.")
else:
    print(f"  {failed} check(s) failed - review output above.")
    print()
    print("  TIP: Run the full pipeline first, then re-run this script:")
    print("    python contracts/runner.py --contracts generated_contracts/ --outputs outputs/ --report validation_reports/")
    print("    python contracts/schema_analyzer.py --all")
    print("    python contracts/ai_extensions.py")
    print("    python contracts/report_generator.py")
print(f"{'=' * 62}\n")

sys.exit(0 if failed == 0 else 1)

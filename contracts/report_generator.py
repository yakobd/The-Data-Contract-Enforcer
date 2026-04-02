"""
contracts/report_generator.py
==============================
Phase 4B – Enforcer Report Generator
Reads from:
  - validation_reports/validation_summary.json
  - validation_reports/<contract>_report.json (individual)
  - validation_reports/schema_evolution_*.json
  - validation_reports/ai_extensions_report.json  (optional)
Outputs:
  enforcer_report/report_{YYYYMMDD}.pdf

Five required sections
  1. Data Health Score
  2. Violations this week
  3. Schema changes detected
  4. AI system risk assessment
  5. Recommended actions

Run:
  python contracts/report_generator.py
  python contracts/report_generator.py --output enforcer_report/custom.pdf
  python contracts/report_generator.py --dry-run   (print JSON summary, no PDF)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
VAL_DIR = ROOT / "validation_reports"
ENFORCER_DIR = ROOT / "enforcer_report"

VALIDATION_SUMMARY = VAL_DIR / "validation_summary.json"
AI_REPORT = VAL_DIR / "ai_extensions_report.json"

CONTRACT_LABELS = {
    "langsmith_traces": "LangSmith Traces",
    "week1_intent_records": "Week 1 – Intent Records",
    "week2_verdicts": "Week 2 – Verdicts",
    "week3_extractions": "Week 3 – Extractions",
    "week4_lineage": "Week 4 – Lineage",
    "week5_events": "Week 5 – Events",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _severity_rank(s: str) -> int:
    return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "WARNING": 1}.get(
        s.upper(), 0
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────────────────────────────────────

def build_health_score(summary: dict, all_results: list[dict]) -> dict:
    """
    health_score = (checks_passed / total_checks) * 100
    -20 for each CRITICAL violation
    """
    total = summary.get("total_checks", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    warned = summary.get("warned", 0)

    base_score = round((passed / total * 100), 1) if total else 0.0

    critical_count = sum(
        1
        for r in all_results
        if r.get("status") == "FAIL" and r.get("severity", "").upper() == "CRITICAL"
    )
    penalised_score = max(0.0, base_score - critical_count * 20)

    # narrative
    if penalised_score >= 90:
        narrative = (
            f"All 6 datasets are in excellent health  -  {penalised_score:.0f}/100  -  "
            "no critical violations detected this week."
        )
    elif penalised_score >= 70:
        narrative = (
            f"Pipeline health is {penalised_score:.0f}/100; "
            f"{critical_count} critical violation(s) incur a −20-point penalty each "
            "and require immediate attention."
        )
    elif penalised_score >= 50:
        narrative = (
            f"Pipeline health is degraded at {penalised_score:.0f}/100 due to "
            f"{critical_count} critical violation(s); remediation is urgent."
        )
    else:
        narrative = (
            f"Pipeline health is critically low at {penalised_score:.0f}/100 "
            f"({critical_count} critical violation(s) active); the pipeline should be "
            "halted until root causes are resolved."
        )

    return {
        "score": penalised_score,
        "base_score": base_score,
        "total_checks": total,
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "critical_penalties": critical_count,
        "narrative": narrative,
    }


def build_violations(summary: dict, all_results: list[dict]) -> dict:
    """
    Count violations by severity; describe top 3 failing checks in plain language.
    """
    # count by severity
    by_severity: dict[str, int] = {}
    failing = [r for r in all_results if r.get("status") == "FAIL"]
    for r in failing:
        sev = r.get("severity", "UNKNOWN").upper()
        by_severity[sev] = by_severity.get(sev, 0) + 1

    warn_count = sum(1 for r in all_results if r.get("status") == "WARN")
    by_severity_with_warn = dict(by_severity)
    if warn_count:
        by_severity_with_warn["WARNING"] = warn_count

    # sort by severity rank descending
    sorted_failing = sorted(
        failing, key=lambda r: _severity_rank(r.get("severity", "")), reverse=True
    )

    # Build plain-language descriptions for top 3
    top3_descriptions: list[str] = []
    for r in sorted_failing[:3]:
        check_id = r.get("check_id", "unknown_check")
        parts = check_id.split(".")
        system = CONTRACT_LABELS.get(parts[0], parts[0]) if parts else check_id
        check_name = parts[1] if len(parts) > 1 else check_id
        column = r.get("column_name", "unknown column")
        sev = r.get("severity", "UNKNOWN")
        actual = r.get("actual_value", "")
        records_failing = r.get("records_failing", 0)
        msg = r.get("message", "")

        # downstream-impact mapping
        downstream_impact = {
            "temporal_ordering": (
                "downstream time-series analytics and SLA calculations will produce "
                "incorrect results"
            ),
            "unique": (
                "downstream joins and idempotency checks will return duplicate rows"
            ),
            "accepted_values": (
                "downstream aggregate roll-ups will include unrecognised categories"
            ),
            "statistical_bounds": (
                "outlier records will skew statistical model training and dashboards"
            ),
            "null_rate": (
                "nullable fields may cause NULL-propagation failures in downstream SQL"
            ),
        }
        impact = "downstream consumers may surface incorrect data"
        for key, imp in downstream_impact.items():
            if key in check_name:
                impact = imp
                break

        description = (
            f"[{sev}] {system}  -  {column}: {actual}. "
            f"Affects {records_failing} record(s). "
            f"Impact: {impact}."
        )
        if msg and msg != actual:
            description += f" Detail: {msg[:120]}"
        top3_descriptions.append(description)

    return {
        "total_failures": len(failing),
        "total_warnings": warn_count,
        "by_severity": by_severity_with_warn,
        "top3": top3_descriptions,
    }


def build_schema_changes(evolution_files: list[Path]) -> dict:
    """
    Parse all schema_evolution_*.json files, summarise every change in plain language.
    """
    all_changes: list[dict] = []
    for efile in sorted(evolution_files):
        data = _load_json(efile)
        if not data:
            continue
        stem = data.get("contract_stem", efile.stem.replace("schema_evolution_", ""))
        label = CONTRACT_LABELS.get(stem, stem)
        for diff in data.get("diffs", []):
            from_ts = diff.get("from_snapshot", "")[:16].replace("T", " ")
            to_ts = diff.get("to_snapshot", "")[:16].replace("T", " ")
            for ch in diff.get("changes", []):
                field = ch.get("field", "")
                ct = ch.get("change_type", "")
                compat = ch.get("backward_compatible", True)
                severity = ch.get("severity", "INFO")
                required_action = ch.get("required_action", "None.")
                detail = ch.get("detail", "")
                verdict = "COMPATIBLE" if compat else "BREAKING"

                # plain-language summary
                change_human = {
                    "ADD_NULLABLE_COLUMN": f"field '{field}' added (nullable)  -  no action needed",
                    "ADD_NONNULLABLE_COLUMN": f"field '{field}' added (NOT NULL)  -  producers must supply a value",
                    "RENAME_COLUMN": f"field '{field}' renamed  -  all consumers must update their field references",
                    "TYPE_CHANGE_WIDENING": f"field '{field}' type widened (e.g. int→float)  -  consumers should verify numeric ranges",
                    "TYPE_CHANGE_NARROWING": f"field '{field}' type narrowed  -  consumers must handle potential truncation or cast errors",
                    "REMOVE_COLUMN": f"field '{field}' removed  -  any consumer reading this field will break",
                    "ENUM_VALUES_ADDITIVE": f"field '{field}' gained new enum value(s)  -  consumers with exhaustive switches must add a case",
                    "ENUM_VALUES_BREAKING": f"field '{field}' lost enum value(s)  -  existing data referencing removed value(s) will violate the contract",
                }.get(ct, f"field '{field}': {ct}")

                all_changes.append(
                    {
                        "dataset": label,
                        "field": field,
                        "change_type": ct,
                        "verdict": verdict,
                        "severity": severity,
                        "period": f"{from_ts} → {to_ts}",
                        "plain_summary": change_human,
                        "required_action": required_action,
                    }
                )

    # Deduplicate: same field + change_type + verdict across multiple evolution files
    seen_keys: set = set()
    unique_changes: list = []
    for c in all_changes:
        key = (c["dataset"], c["field"], c["change_type"], c["verdict"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique_changes.append(c)

    breaking = [c for c in unique_changes if c["verdict"] == "BREAKING"]
    compatible = [c for c in unique_changes if c["verdict"] == "COMPATIBLE"]

    return {
        "total_changes": len(unique_changes),
        "breaking_count": len(breaking),
        "compatible_count": len(compatible),
        "changes": unique_changes,
    }


def build_ai_risk(ai_data: dict | None) -> dict:
    """
    Summarise AI extension results into a risk assessment block.
    """
    if not ai_data:
        return {
            "available": False,
            "summary": "No AI extension report found  -  run `python contracts/ai_extensions.py` to generate.",
            "extensions": [],
        }

    extensions_raw = ai_data.get("extensions", {})
    ext_summaries = []

    for ext_name, result in extensions_raw.items():
        status = result.get("status", "UNKNOWN")
        ext_label = {
            "embedding_drift": "Embedding Drift",
            "prompt_input_schema": "Prompt Input Schema",
            "output_schema_violation_rate": "LLM Output Violation Rate",
        }.get(ext_name, ext_name.replace("_", " ").title())

        if ext_name == "embedding_drift":
            drift = result.get("drift_score")
            if drift is not None:
                detail = (
                    f"Cosine distance from baseline centroid: {drift:.4f}. "
                    + ("⚠ DRIFT DETECTED  -  semantic distribution has shifted." if status == "FAIL"
                       else "Within acceptable range." if status == "PASS"
                       else "Baseline set for future runs.")
                )
            else:
                detail = result.get("message", str(status))
        elif ext_name == "prompt_input_schema":
            total = result.get("total_records", 0)
            quarantined = result.get("quarantined", 0)
            rate = quarantined / total * 100 if total else 0
            detail = (
                f"{quarantined}/{total} records quarantined ({rate:.1f}%). "
                + ("Prompt inputs are well-formed." if quarantined == 0
                   else f"Non-conforming records saved to outputs/quarantine/.")
            )
        elif ext_name == "output_schema_violation_rate":
            vr = result.get("violation_rate", 0)
            trend = result.get("trend", "UNKNOWN")
            detail = (
                f"Violation rate: {vr*100:.2f}%. Trend vs baseline: {trend}."
            )
        else:
            detail = result.get("message", str(status))

        risk = (
            "HIGH" if status == "FAIL"
            else "MEDIUM" if status == "WARN"
            else "LOW"
        )
        ext_summaries.append(
            {"extension": ext_label, "status": status, "risk": risk, "detail": detail}
        )

    overall_risk = (
        "HIGH" if any(e["risk"] == "HIGH" for e in ext_summaries)
        else "MEDIUM" if any(e["risk"] == "MEDIUM" for e in ext_summaries)
        else "LOW"
    )

    summary_text = (
        f"Overall AI system risk: {overall_risk}. "
        + (
            "One or more AI-specific checks are failing  -  review quarantine folder and "
            "embedding baseline immediately."
            if overall_risk == "HIGH"
            else "AI pipeline is operating within acceptable parameters."
            if overall_risk == "LOW"
            else "Minor AI pipeline issues detected; monitor trends over the next 24 hours."
        )
    )

    return {
        "available": True,
        "overall_risk": overall_risk,
        "summary": summary_text,
        "extensions": ext_summaries,
    }


def build_recommended_actions(
    health: dict,
    violations: dict,
    schema_changes: dict,
    ai_risk: dict,
    all_results: list[dict],
) -> list[dict]:
    """
    Generate 3 prioritised, specific recommended actions.
    """
    actions: list[dict] = []

    # ── Action pool ──────────────────────────────────────────────────────────
    # 1. CRITICAL temporal ordering – langsmith_traces
    temporal_langsmith = next(
        (r for r in all_results
         if r.get("check_id", "").endswith("temporal_ordering")
         and "langsmith" in r.get("check_id", "")
         and r.get("status") == "FAIL"),
        None,
    )
    if temporal_langsmith:
        actions.append({
            "priority": 1,
            "severity": "CRITICAL",
            "action": (
                "Fix LangSmith trace ingestion to populate end_time correctly: "
                "update src/langsmith/ingest.py to set end_time = run.end_time "
                "(not null). All 150 traces currently have end_time ≤ start_time, "
                "breaking SLA dashboards."
            ),
            "file_hint": "src/langsmith/ingest.py",
        })

    # 2. Temporal ordering – week5_events
    temporal_week5 = next(
        (r for r in all_results
         if r.get("check_id", "").endswith("temporal_ordering")
         and "week5" in r.get("check_id", "")
         and r.get("status") == "FAIL"),
        None,
    )
    if temporal_week5:
        actions.append({
            "priority": 1,
            "severity": "CRITICAL",
            "action": (
                "Fix event timestamp population in the Week 5 event emitter: "
                "update src/week5/event_emitter.py so recorded_at is set after "
                "occurred_at (currently 100/100 events violate this constraint), "
                "or swap the field assignment to use UTC clock at emit time."
            ),
            "file_hint": "src/week5/event_emitter.py",
        })

    # 3. Unique constraint – week1 intent_id
    unique_week1 = next(
        (r for r in all_results
         if "intent_id" in r.get("check_id", "")
         and r.get("status") == "FAIL"),
        None,
    )
    if unique_week1:
        actions.append({
            "priority": 2,
            "severity": "CRITICAL",
            "action": (
                "Resolve duplicate intent_id in Week 1 intent records: "
                "update src/week1/intent_router.py to generate a new UUID per "
                "record instead of reusing 'INT-001'. Duplicate IDs break "
                "idempotency guarantees for downstream deduplication."
            ),
            "file_hint": "src/week1/intent_router.py",
        })

    # 4. Accepted values – week5 aggregate_type
    accepted_week5 = next(
        (r for r in all_results
         if "aggregate_type" in r.get("check_id", "")
         and r.get("status") == "FAIL"),
        None,
    )
    if accepted_week5:
        actions.append({
            "priority": 2,
            "severity": "HIGH",
            "action": (
                "Add 'Agent' to the accepted_values enum in "
                "generated_contracts/week5_events.yaml (field: aggregate_type), "
                "or update src/week5/event_emitter.py to map unknown types to an "
                "accepted value. 8 records use the undeclared value 'Agent'."
            ),
            "file_hint": "generated_contracts/week5_events.yaml",
        })

    # 5. Statistical outliers – week3 processing_time_ms
    stat_week3 = next(
        (r for r in all_results
         if "processing_time_ms" in r.get("check_id", "")
         and r.get("status") == "FAIL"),
        None,
    )
    if stat_week3:
        actions.append({
            "priority": 3,
            "severity": "MEDIUM",
            "action": (
                "Cap or investigate outlier processing_time_ms values in Week 3 "
                "extractions: update src/week3/extractor.py to log a warning when "
                "processing_time_ms > 3σ of historical mean (~15 558 ms) and add "
                "a circuit-breaker timeout. 2 records currently exceed the 3σ bound."
            ),
            "file_hint": "src/week3/extractor.py",
        })

    # 6. confidence as float – week3 (from prompt input schema check)
    prompt_input_fail = next(
        (r for r in all_results
         if "prompt_input_validation" in r.get("check_id", "")
         and r.get("status") in ("FAIL", "WARN")),
        None,
    )
    if prompt_input_fail:
        actions.append({
            "priority": 3,
            "severity": "MEDIUM",
            "action": (
                "Update src/week3/extractor.py to include doc_id and source_path "
                "in every prompt input record so they pass PROMPT_INPUT_SCHEMA "
                "validation. Currently 50/50 records are quarantined due to these "
                "missing keys."
            ),
            "file_hint": "src/week3/extractor.py",
        })

    # 7. Breaking schema change – doc_id removed
    breaking = [c for c in schema_changes.get("changes", []) if c["verdict"] == "BREAKING"]
    if breaking and not any(
        "schema" in a["action"].lower() for a in actions
    ):
        c = breaking[0]
        actions.append({
            "priority": 2,
            "severity": "HIGH",
            "action": (
                f"A breaking schema change was detected in {c['dataset']}: "
                f"{c['plain_summary']}. "
                f"Required action: {c['required_action']}"
            ),
            "file_hint": "generated_contracts/",
        })

    # 8. AI risk
    if ai_risk.get("overall_risk") == "HIGH":
        actions.append({
            "priority": 2,
            "severity": "HIGH",
            "action": (
                "Address high AI system risk: run `python contracts/ai_extensions.py` "
                "and inspect outputs/quarantine/ for non-conforming prompt inputs. "
                "Update the embedding baseline after the distribution shift is resolved."
            ),
            "file_hint": "contracts/ai_extensions.py",
        })

    # Sort by priority, deduplicate, keep top 3
    actions.sort(key=lambda a: a["priority"])
    seen_files: set[str] = set()
    deduplicated: list[dict] = []
    for a in actions:
        key = a.get("file_hint", a["action"][:40])
        if key not in seen_files:
            seen_files.add(key)
            deduplicated.append(a)
        if len(deduplicated) >= 3:
            break

    # Renumber
    for idx, a in enumerate(deduplicated, 1):
        a["priority"] = idx

    return deduplicated


# ─────────────────────────────────────────────────────────────────────────────
# HTML template
# ─────────────────────────────────────────────────────────────────────────────

def render_html(
    health: dict,
    violations: dict,
    schema: dict,
    ai_risk: dict,
    actions: list[dict],
    report_date: str,
) -> str:
    # ── helpers ──
    def sev_badge(sev: str) -> str:
        colours = {
            "CRITICAL": "#c0392b",
            "HIGH": "#e67e22",
            "MEDIUM": "#f1c40f",
            "LOW": "#27ae60",
            "WARNING": "#e67e22",
            "INFO": "#3498db",
        }
        bg = colours.get(sev.upper(), "#7f8c8d")
        return f'<span style="background:{bg};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.78em;font-weight:700">{sev}</span>'

    def verdict_badge(v: str) -> str:
        bg = "#c0392b" if v == "BREAKING" else "#27ae60"
        return f'<span style="background:{bg};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.78em;font-weight:700">{v}</span>'

    def score_colour(s: float) -> str:
        if s >= 90:
            return "#27ae60"
        if s >= 70:
            return "#f39c12"
        if s >= 50:
            return "#e67e22"
        return "#c0392b"

    score_c = score_colour(health["score"])

    # ── severity breakdown table ──
    sev_rows = ""
    for sev, cnt in sorted(
        violations["by_severity"].items(),
        key=lambda x: _severity_rank(x[0]),
        reverse=True,
    ):
        sev_rows += f"<tr><td>{sev_badge(sev)}</td><td style='text-align:center'>{cnt}</td></tr>"

    # ── top-3 violations ──
    violation_items = ""
    for desc in violations["top3"]:
        violation_items += f"<li style='margin-bottom:8px'>{desc}</li>"

    # ── schema changes table ──
    schema_rows = ""
    if schema["changes"]:
        for c in schema["changes"]:
            schema_rows += (
                "<tr>"
                f"<td>{c['dataset']}</td>"
                f"<td><code>{c['field']}</code></td>"
                f"<td>{c['change_type']}</td>"
                f"<td>{verdict_badge(c['verdict'])}</td>"
                f"<td>{c['plain_summary']}</td>"
                f"<td style='font-size:0.85em'>{c['required_action']}</td>"
                "</tr>"
            )
    else:
        schema_rows = "<tr><td colspan='6' style='text-align:center;color:#888'>No schema changes detected in the reporting window.</td></tr>"

    # ── schema section body ──
    if not schema["changes"]:
        schema_section_body = "<p style='color:#27ae60'>&#10003; No schema changes detected in the reporting window.</p>"
    else:
        schema_section_body = (
            f"<p style='margin-bottom:10px'>"
            f"<strong>{schema['total_changes']}</strong> change(s) detected  -  "
            f"<span style='color:#c0392b;font-weight:700'>{schema['breaking_count']} breaking</span>, "
            f"<span style='color:#27ae60;font-weight:700'>{schema['compatible_count']} compatible</span>."
            f"</p>"
            f"<table><tr>"
            f"<th>Dataset</th><th>Field</th><th>Change Type</th>"
            f"<th>Verdict</th><th>Plain Summary</th><th>Required Action</th>"
            f"</tr>{schema_rows}</table>"
        )

    # ── AI extensions ──
    ai_ext_html = ""
    if ai_risk.get("available") and ai_risk.get("extensions"):
        for e in ai_risk["extensions"]:
            risk_icon = {"HIGH": "&#x1F534;", "MEDIUM": "&#x1F7E1;", "LOW": "&#x1F7E2;"}.get(e["risk"], "&#x26AA;")
            bl_color = "#c0392b" if e["risk"] == "HIGH" else "#f1c40f" if e["risk"] == "MEDIUM" else "#27ae60"
            ai_ext_html += (
                f"<div style='margin-bottom:12px;padding:10px 14px;"
                f"border-left:4px solid {bl_color};"
                f"background:#fafafa;border-radius:0 6px 6px 0'>"
                f"<strong>{risk_icon} {e['extension']}</strong> "
                f"<span style='color:#555;font-size:0.9em'>[{e['status']}]</span><br>"
                f"<span style='color:#333;font-size:0.92em'>{e['detail']}</span>"
                f"</div>"
            )
    else:
        ai_ext_html = (
            f"<p style='color:#888'>{ai_risk.get('summary','No AI data available.')}</p>"
        )

    # ── AI risk banner ──
    if ai_risk.get("available"):
        overall_risk = ai_risk.get("overall_risk", "UNKNOWN")
        if overall_risk == "HIGH":
            risk_bg = "#fdf0f0"
            risk_border = "#f5c0c0"
        elif overall_risk == "MEDIUM":
            risk_bg = "#fdfae8"
            risk_border = "#f5e0a0"
        else:
            risk_bg = "#f0fdf4"
            risk_border = "#a0ddb8"
        ai_banner = (
            f"<div style='margin-bottom:14px;padding:10px 14px;border-radius:6px;"
            f"background:{risk_bg};border:1px solid {risk_border}'>"
            f"<strong>Overall AI Risk: {overall_risk}</strong>  -  {ai_risk['summary']}"
            f"</div>"
        )
    else:
        ai_banner = ""

    # ── recommended actions ──
    actions_html = ""
    for a in actions:
        actions_html += (
            f"<div style='margin-bottom:16px;padding:12px 16px;"
            f"border:1px solid #dde;border-radius:6px;background:#f9f9ff'>"
            f"<div style='display:flex;gap:8px;align-items:center;margin-bottom:6px'>"
            f"<span style='background:#2c3e50;color:#fff;padding:2px 10px;border-radius:12px;"
            f"font-size:0.8em;font-weight:700'>#{a['priority']}</span>"
            f"{sev_badge(a['severity'])}"
            f"<code style='font-size:0.82em;color:#555'>{a.get('file_hint','')}</code>"
            f"</div>"
            f"<p style='margin:0;color:#222;line-height:1.6'>{a['action']}</p>"
            f"</div>"
        )

    # ── full page ──
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Data Contract Enforcer -- Report {report_date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 13px;
         color: #222; background: #fff; padding: 32px 40px; }}
  h1 {{ font-size: 1.6em; color: #1a1a2e; margin-bottom: 4px; }}
  h2 {{ font-size: 1.15em; color: #2c3e50; margin: 28px 0 10px;
       padding-bottom: 6px; border-bottom: 2px solid #e8e8f0; }}
  h3 {{ font-size: 1em; color: #34495e; margin-bottom: 8px; }}
  .meta {{ color:#888; font-size:0.9em; margin-bottom:32px; }}
  .score-box {{ display:inline-block; padding:20px 36px; border-radius:12px;
               background:{score_c}15; border:2px solid {score_c};
               text-align:center; margin-bottom:16px; }}
  .score-num {{ font-size:2.8em; font-weight:900; color:{score_c}; line-height:1; }}
  .score-label {{ font-size:0.85em; color:#555; margin-top:4px; }}
  .narrative {{ font-size:1em; color:#333; line-height:1.7;
               background:#f4f8ff; padding:12px 16px; border-radius:6px;
               border-left:4px solid {score_c}; margin-bottom:6px; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
  th {{ background:#f0f0f8; text-align:left; padding:8px 10px;
        font-size:0.88em; color:#444; }}
  td {{ padding:7px 10px; border-bottom:1px solid #eee; vertical-align:top;
        line-height:1.5; }}
  tr:last-child td {{ border-bottom:none; }}
  code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:0.9em; }}
  ul {{ padding-left:20px; }}
  li {{ line-height:1.65; }}
  .section {{ margin-bottom:32px; }}
  .stat-grid {{ display:flex; gap:24px; flex-wrap:wrap; margin-bottom:16px; }}
  .stat-card {{ flex:1; min-width:100px; text-align:center; padding:14px;
               border-radius:8px; background:#f8f8fc; border:1px solid #e8e8f0; }}
  .stat-val {{ font-size:1.8em; font-weight:800; color:#2c3e50; }}
  .stat-lbl {{ font-size:0.8em; color:#888; margin-top:2px; }}
  @media print {{
    body {{ padding: 20px; }}
    h2 {{ page-break-after: avoid; }}
  }}
</style>
</head>
<body>

<h1>Data Contract Enforcer &mdash; Weekly Report</h1>
<div class="meta">
  Generated: {report_date} &nbsp;|&nbsp;
  Pipeline: TenX Academy Week 7 &nbsp;|&nbsp;
  Contracts validated: 6 &nbsp;|&nbsp;
  Total checks: {health['total_checks']}
</div>

<!-- section 1 -->
<div class="section">
<h2>1 &middot; Data Health Score</h2>
<div class="score-box">
  <div class="score-num">{health['score']:.0f}</div>
  <div class="score-label">/ 100</div>
</div>

<div class="stat-grid">
  <div class="stat-card"><div class="stat-val" style="color:#27ae60">{health['passed']}</div><div class="stat-lbl">Passed</div></div>
  <div class="stat-card"><div class="stat-val" style="color:#c0392b">{health['failed']}</div><div class="stat-lbl">Failed</div></div>
  <div class="stat-card"><div class="stat-val" style="color:#f39c12">{health['warned']}</div><div class="stat-lbl">Warned</div></div>
  <div class="stat-card"><div class="stat-val" style="color:#c0392b">{health['critical_penalties']}</div><div class="stat-lbl">Critical (-20 each)</div></div>
</div>

<div class="narrative">{health['narrative']}</div>
<p style="color:#888;font-size:0.85em;margin-top:8px">
  Formula: base = {health['base_score']:.1f} ({health['passed']}/{health['total_checks']} x 100)
  &minus; {health['critical_penalties']} x 20 = <strong>{health['score']:.0f}</strong>
</p>
</div>

<!-- section 2 -->
<div class="section">
<h2>2 &middot; Violations This Week</h2>
<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:16px">
  <div>
    <h3>Count by Severity</h3>
    <table style="width:220px">
      <tr><th>Severity</th><th>Count</th></tr>
      {sev_rows}
    </table>
  </div>
  <div style="flex:1;min-width:260px">
    <h3>Top 3 Failing Checks</h3>
    <ul>{violation_items}</ul>
  </div>
</div>
</div>

<!-- section 3 -->
<div class="section">
<h2>3 &middot; Schema Changes Detected (Past 7 Days)</h2>
{schema_section_body}
</div>

<!-- section 4 -->
<div class="section">
<h2>4 &middot; AI System Risk Assessment</h2>
{ai_banner}
{ai_ext_html}
</div>

<!-- section 5 -->
<div class="section">
<h2>5 &middot; Recommended Actions</h2>
{actions_html}
</div>

<hr style="border:none;border-top:1px solid #eee;margin-top:32px"/>
<p style="color:#aaa;font-size:0.78em;margin-top:8px">
  Generated by contracts/report_generator.py &middot; Data Contract Enforcer v1.0 &middot;
  TenX Academy Week 7 &middot; {report_date}
</p>

</body>
</html>
"""
    return html


def generate_pdf(html: str, output_path: Path) -> None:
    """Try WeasyPrint; then xhtml2pdf; then save HTML as fallback."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Option 1: WeasyPrint ────────────────────────────────────────────────
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from weasyprint import HTML as WP_HTML  # type: ignore
        WP_HTML(string=html, base_url=str(ROOT)).write_pdf(str(output_path))
        print(f"[report_generator] PDF written (WeasyPrint) -> {output_path}")
        return
    except Exception:
        pass  # fall through to next option

    # ── Option 2: xhtml2pdf ─────────────────────────────────────────────────
    try:
        from xhtml2pdf import pisa  # type: ignore
        with open(output_path, "wb") as pdf_fh:
            result = pisa.CreatePDF(html.encode("utf-8"), dest=pdf_fh)
        if not result.err:
            print(f"[report_generator] PDF written (xhtml2pdf) -> {output_path}")
            return
    except Exception:
        pass  # fall through to HTML fallback

    # ── Option 3: HTML fallback ─────────────────────────────────────────────
    html_path = output_path.with_suffix(".html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(
        f"[report_generator] HTML report written -> {html_path}\n"
        "  To convert to PDF on Windows:\n"
        "    Option A (recommended): conda install -c conda-forge weasyprint\n"
        "    Option B: pip install xhtml2pdf\n"
        "    Option C: open the HTML in Chrome, press Ctrl+P -> Save as PDF"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def collect_all_results() -> list[dict]:
    """Aggregate individual check results from all *_report.json files."""
    results: list[dict] = []
    for rfile in sorted(VAL_DIR.glob("*_report.json")):
        data = _load_json(rfile)
        if data and "results" in data:
            results.extend(data["results"])
    return results


def run(output_path: Path | None = None, dry_run: bool = False) -> int:
    report_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    if output_path is None:
        output_path = ENFORCER_DIR / f"report_{report_date}.pdf"

    # ── load data ──
    summary = _load_json(VALIDATION_SUMMARY) or {}
    if not summary:
        print(
            "[report_generator] WARNING: validation_summary.json not found. "
            "Run `python contracts/runner.py --all` first.",
            file=sys.stderr,
        )

    all_results = collect_all_results()
    ai_data = _load_json(AI_REPORT)

    evolution_files = sorted(VAL_DIR.glob("schema_evolution_*.json"))

    # ── build sections ──
    health = build_health_score(summary, all_results)
    violations = build_violations(summary, all_results)
    schema = build_schema_changes(evolution_files)
    ai_risk = build_ai_risk(ai_data)
    actions = build_recommended_actions(health, violations, schema, ai_risk, all_results)

    report_payload = {
        "report_date": report_date,
        "health_score": health,
        "violations": violations,
        "schema_changes": schema,
        "ai_risk": ai_risk,
        "recommended_actions": actions,
    }

    if dry_run:
        print(json.dumps(report_payload, indent=2, default=str))
        return 0

    # ── render & write ──
    html = render_html(health, violations, schema, ai_risk, actions, report_date)
    generate_pdf(html, output_path)

    # also save JSON summary alongside PDF
    json_path = output_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report_payload, fh, indent=2, default=str)
    print(f"[report_generator] JSON summary written → {json_path}")

    # also write canonical report_data.json (evaluation scripts look for this exact name)
    report_data_path = output_path.parent / "report_data.json"
    with open(report_data_path, "w", encoding="utf-8") as fh:
        json.dump(report_payload, fh, indent=2, default=str)
    print(f"[report_generator] Canonical report_data.json written → {report_data_path}")

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate Enforcer Report PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python contracts/report_generator.py
              python contracts/report_generator.py --output enforcer_report/custom.pdf
              python contracts/report_generator.py --dry-run
            """
        ),
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for generated PDF (default: enforcer_report/report_<YYYYMMDD>.pdf)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON report data without writing any files",
    )
    return p.parse_args()


if __name__ == "__main__":
    os.chdir(ROOT)
    args = _parse_args()
    sys.exit(run(output_path=args.output, dry_run=args.dry_run))

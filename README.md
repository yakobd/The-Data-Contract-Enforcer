# Week 7 — Data Contract Enforcer
**TenX Academy TRP1 | Yakob Dereje | yakob@10academy.org**

Schema Integrity & Lineage Attribution System for a 5-week integrated AI platform.
Implements the [Bitol Open Data Contract Standard v3.0.0](https://github.com/bitol-io/open-data-contract-standard).

---

## Quick Start (fresh clone)

```bash
git clone <your-repo-url>
cd data-contract-enforcer

pip install pyyaml pandas numpy scipy networkx reportlab weasyprint xhtml2pdf
# Optional (LLM annotation): pip install anthropic openai
```

All five entry-point scripts live in `contracts/`. Run them in order from the repo root.

---

## Script 1 — ContractGenerator

Reads JSONL outputs from Weeks 1–5, profiles schema structure and statistics, injects
lineage context from the Week 4 graph, and emits Bitol-compatible YAML contracts plus
dbt schema.yml companion files.

```bash
# Single-source mode (Week 3 example)
python contracts/generator.py \
    --source outputs/week3/extractions.jsonl \
    --output generated_contracts/

# Generate all six contracts at once
python contracts/generator.py --all --output generated_contracts/
```

**Expected output:**
```
ContractGenerator — generating contracts for 6 datasets
  [1/6] extractions       → generated_contracts/week3_extractions.yaml  (14 clauses)
  [2/6] events            → generated_contracts/week5_events.yaml        (12 clauses)
  ...
  All contracts written to generated_contracts/
```

Generated files:
- `generated_contracts/week1_intent_records.yaml` + `_dbt.yml`
- `generated_contracts/week2_verdicts.yaml` + `_dbt.yml`
- `generated_contracts/week3_extractions.yaml` + `_dbt.yml`
- `generated_contracts/week4_lineage.yaml` + `_dbt.yml`
- `generated_contracts/week5_events.yaml` + `_dbt.yml`
- `generated_contracts/langsmith_traces.yaml` + `_dbt.yml`
- `schema_snapshots/{contract_id}/{timestamp}.yaml` (one snapshot per contract)

---

## Script 2 — ValidationRunner

Executes every clause in a contract against a live JSONL snapshot. Applies a statistical
drift rule (WARN at 2σ, FAIL at 3σ) using baselines stored in
`schema_snapshots/baselines.json`.

Supports three enforcement modes via `--mode`:
- `AUDIT` *(default)* — log all results, never block the pipeline
- `WARN` — exit 1 if any CRITICAL violation found
- `ENFORCE` — exit 1 if any CRITICAL or HIGH violation found

```bash
# Single contract (evaluator command from spec)
python contracts/runner.py \
    --contract generated_contracts/week3_extractions.yaml \
    --data     outputs/week3/extractions.jsonl

# Single contract in ENFORCE mode
python contracts/runner.py \
    --contract generated_contracts/week3_extractions.yaml \
    --data     outputs/week3/extractions.jsonl \
    --mode     ENFORCE

# Batch mode — validate all contracts at once
python contracts/runner.py \
    --contracts generated_contracts/ \
    --outputs   outputs/ \
    --report    validation_reports/
```

**Expected output (single-file):**
```
──────────────────────────────────────────────────────────────────────
  ValidationRunner — Single-file mode
  Contract : generated_contracts/week3_extractions.yaml
  Data     : outputs/week3/extractions.jsonl
  Output   : validation_reports/week3_extractions_report.json
──────────────────────────────────────────────────────────────────────
  contract : week3-document-refinery-extractions
  checks   : 16   passed: 14   failed: 2   warned: 0   errored: 0
  status   : FAIL
  Enforcement mode : AUDIT

  Report written → validation_reports/week3_extractions_report.json
```

Output files: `validation_reports/{contract_stem}_report.json` per contract, plus
`validation_reports/validation_summary.json` in batch mode.

---

## Script 3 — ViolationAttributor

For each FAIL in the violation log, traverses the Week 4 lineage graph backwards to the
source file, runs `git log` to identify the responsible commit, ranks candidates by
temporal proximity, and computes blast radius using the ContractRegistry as primary source.

```bash
# Run against the full validation reports directory
python contracts/attributor.py \
    --reports    validation_reports/ \
    --lineage    outputs/week4/lineage_snapshots.jsonl \
    --registry   contract_registry/subscriptions.yaml \
    --output     violation_log/violations.jsonl
```

**Expected output:**
```
ViolationAttributor
  Loading lineage graph ... 66 nodes, 61 edges
  Loading registry     ... 6 subscriptions
  Processing 7 violation(s) from 6 report(s)
    [1] week3_extractions.statistical_bounds_processing_time_ms
        blame → contracts/migrate/migrate_week3.py (confidence: 0.70)
        blast radius: 4 nodes, 2 pipelines, 2 registry subscribers
  ...
  Written → violation_log/violations.jsonl  (3 records)
```

Output: `violation_log/violations.jsonl` — one JSON object per line, each with
`blame_chain[]` (ranked candidates with commit hash, author, confidence score) and
`blast_radius` (affected_nodes, affected_pipelines, registry_subscribers).

---

## Script 4 — SchemaEvolutionAnalyzer

Diffs consecutive timestamped snapshots for a contract, classifies every detected change
into the 8-type taxonomy (ADD_NULLABLE_COLUMN, ADD_NONNULLABLE_COLUMN,
RENAME_COLUMN, TYPE_CHANGE_WIDENING, TYPE_CHANGE_NARROWING,
REMOVE_COLUMN, ENUM_VALUES_ADDITIVE, ENUM_VALUES_BREAKING), and generates a
migration impact report for any breaking change.

Exit code: `0` = all changes STABLE or COMPATIBLE, `1` = BREAKING change detected.
The non-zero exit is intentional — CI gates should treat it as a deploy blocker.

```bash
# Analyse evolution for a specific contract
python contracts/schema_analyzer.py \
    --contract-id week3-document-refinery-extractions \
    --since "7 days ago" \
    --output validation_reports/schema_evolution_week3.json

# Analyse all contracts
python contracts/schema_analyzer.py --all \
    --since "7 days ago" \
    --output validation_reports/
```

**Expected output:**
```
SchemaEvolutionAnalyzer
  Contract : week3-document-refinery-extractions
  Snapshots: 2 found (comparing oldest → newest)
  Changes  : 2 detected
    [BREAKING] TYPE_CHANGE_NARROWING — extracted_facts[*].confidence
               float(0.0,1.0) → integer(0,100)  →  CRITICAL
    [STABLE]   ADD_NULLABLE_COLUMN  — processing_notes
  Exit code 1 (BREAKING changes detected)
  Report → validation_reports/schema_evolution_week3_extractions.json
  Migration impact → validation_reports/migration_impact_week3_extractions_*.json
```

---

## Script 5 — AI Contract Extensions

Runs three AI-specific contract checks not covered by standard data contracts:
1. **Embedding drift** — cosine distance from centroid baseline (WARN >0.15, FAIL >0.25)
2. **Prompt input schema validation** — JSON Schema draft-07 on Week 3 extraction inputs
3. **LLM output schema violation rate** — tracks rate for Week 2 verdict records

```bash
python contracts/ai_extensions.py \
    --extractions outputs/week3/extractions.jsonl \
    --verdicts    outputs/week2/verdicts.jsonl \
    --traces      outputs/traces/runs.jsonl \
    --output      validation_reports/ai_extensions_report.json
```

**Expected output:**
```
AI Contract Extensions
  [1/3] Embedding drift check ...
        samples: 50  drift_score: 0.0000  status: BASELINE_SET
        centroid saved → schema_snapshots/embedding_centroid.npz
  [2/3] Prompt input schema validation ...
        records checked: 50  violations: 0  status: PASS
  [3/3] LLM output schema violation rate ...
        total_outputs: 10  schema_violations: 0  violation_rate: 0.0%
        trend: stable  status: PASS
  Report → validation_reports/ai_extensions_report.json
```

---

## Script 6 — EnforcerReportGenerator

Aggregates all validation results, attribution data, schema evolution reports, and AI
extension metrics into a single PDF + JSON report for stakeholder review.

```bash
python contracts/report_generator.py
# or with explicit paths:
python contracts/report_generator.py \
    --output enforcer_report/report_$(date +%Y%m%d).pdf
```

**Expected output:**
```
[report_generator] Reading validation summary ... 96 checks, 86 passed
[report_generator] Reading AI extensions report ...
[report_generator] Reading 2 schema evolution files ...
[report_generator] Health score: 68.3 / 100  grade: C
[report_generator] PDF written → enforcer_report/report_20260402.pdf
[report_generator] JSON summary written → enforcer_report/report_20260402.json
[report_generator] Canonical report_data.json written → enforcer_report/report_data.json
```

Output files:
- `enforcer_report/report_{YYYYMMDD}.pdf` — stakeholder PDF (5 sections)
- `enforcer_report/report_{YYYYMMDD}.json` — machine-readable companion
- `enforcer_report/report_data.json` — canonical JSON with `data_health_score` field

---

## Running the Full Pipeline End-to-End

```bash
# 1. Generate contracts
python contracts/generator.py --all --output generated_contracts/

# 2. Run validation (batch, AUDIT mode)
python contracts/runner.py \
    --contracts generated_contracts/ \
    --outputs   outputs/ \
    --report    validation_reports/

# 3. Attribute violations
python contracts/attributor.py \
    --reports    validation_reports/ \
    --lineage    outputs/week4/lineage_snapshots.jsonl \
    --registry   contract_registry/subscriptions.yaml \
    --output     violation_log/violations.jsonl

# 4. Analyse schema evolution
python contracts/schema_analyzer.py --all \
    --since "7 days ago" \
    --output validation_reports/

# 5. Run AI extensions
python contracts/ai_extensions.py \
    --extractions outputs/week3/extractions.jsonl \
    --verdicts    outputs/week2/verdicts.jsonl \
    --traces      outputs/traces/runs.jsonl \
    --output      validation_reports/ai_extensions_report.json

# 6. Generate the Enforcer Report
python contracts/report_generator.py
```

---

## System Verification

Run the full 107-check test suite to verify all components:

```bash
python test_system.py
```

Expected output: `107 / 107 PASS`

---

## Directory Structure

```
data-contract-enforcer/
├── contracts/
│   ├── generator.py        # Phase 1A — ContractGenerator
│   ├── runner.py           # Phase 2A — ValidationRunner  (--mode AUDIT|WARN|ENFORCE)
│   ├── attributor.py       # Phase 2B — ViolationAttributor
│   ├── schema_analyzer.py  # Phase 3  — SchemaEvolutionAnalyzer
│   ├── ai_extensions.py    # Phase 4A — AI Contract Extensions
│   └── report_generator.py # Phase 4B — EnforcerReportGenerator
├── contract_registry/
│   └── subscriptions.yaml  # ContractRegistry — 6 subscriptions
├── generated_contracts/    # Bitol YAML contracts + dbt companion files
├── outputs/                # Week 1-5 JSONL data (50-100 records each)
├── validation_reports/     # Per-contract JSON reports + summary
├── violation_log/
│   └── violations.jsonl    # Blame chain + blast radius records
├── schema_snapshots/       # Timestamped schema snapshots + baselines.json
├── enforcer_report/        # report_{date}.pdf + report_data.json
├── test_system.py          # 107-check end-to-end verification suite
└── DOMAIN_NOTES.md         # Phase 0 domain analysis (5 questions)
```

---

## Key Design Decisions

- **Bitol v3.0.0** as the contract schema language (open standard, human-readable YAML).
- **ContractRegistry** (`contract_registry/subscriptions.yaml`) is the Tier 1 authority for blast radius; the Week 4 lineage graph enriches but does not replace it.
- **Statistical drift rule**: baselines stored in `schema_snapshots/baselines.json`; WARN at 2σ, FAIL at 3σ — catches the `confidence 0.0–1.0 → 0–100` silent corruption class.
- **Three enforcement modes** (`--mode`): AUDIT for initial deployment, WARN after calibration, ENFORCE for production-critical pipelines.
- **8-type schema change taxonomy** with BREAKING/COMPATIBLE verdict used to auto-generate migration impact reports and block CI on breaking changes.

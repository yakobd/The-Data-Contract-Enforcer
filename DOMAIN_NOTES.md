# DOMAIN_NOTES.md — Week 7: Data Contract Enforcer
Yakob Dereje
**Date:** 2026-04-01
**Platform:** TenX Academy — Weeks 1–5 Integrated System

---

## Question 1: Backward-Compatible vs Breaking Schema Changes

A **backward-compatible** schema change is one where every existing downstream consumer continues to work correctly without any modification. A **breaking** schema change is one where at least one downstream consumer will produce incorrect results, crash, or silently corrupt data — even if no exception is raised.

The most dangerous breaking changes are the ones that pass structural checks and only fail statistically — they look fine but produce wrong answers.

### Three Backward-Compatible Changes (from my Weeks 1–5 schemas)

**1. Adding `domain_confidence` as a nullable field to Week 3 DocumentProfile**

In my Week 3 Doc Intelligence Refinery, the `DocumentProfile` model in `src/models/document_schema.py` contains a `domain_confidence: float` field (observed value: `0.6667` in real output). This field was added at some point as an optional enrichment — it tells downstream consumers how confident the triage agent was about the domain classification. Any consumer that does not yet read this field simply ignores it. The Week 4 Cartographer, which reads `doc_id` and `extracted_facts`, is entirely unaffected. Contract clause: `required: false`.

**2. Adding a new `run_type` value to the LangSmith trace schema**

The LangSmith `trace_record` schema defines `run_type` as an enum: `llm | chain | tool | retriever | embedding`. Adding a new value like `"evaluation"` is backward compatible because all existing consumers that process known run types will simply treat the new value as an unrecognised-but-valid record and skip or log it. No existing logic breaks. The Week 7 AI Contract Extensions, which filters for `run_type == "llm"` records, continues to work correctly on all existing records.

**3. Widening `event_version` from `int` with value `1` to support higher integers in Week 5**

In my Week 5 The Ledger system, every event record carries `event_version: 1`. Changing this to allow `event_version: 2` for a new payload structure is a widening change — existing consumers that check `event_version == 1` simply skip version-2 events. No existing code path is disrupted. The canonical migration maps this as `schema_version: "1.0"` for version 1 events and `"2.0"` for version 2.

### Three Breaking Changes (from my Weeks 1–5 schemas)

**1. Changing `confidence_score` from float 0.0–1.0 to integer 0–100 in Week 3**

This is the canonical breaking change for this project. My Week 3 system outputs `profile.confidence_score` and `metadata.avg_confidence` as floats in range `[0.0, 1.0]` — confirmed in my real output: `"confidence_score": 0.9`, `"avg_confidence": 1.0`. If a developer changes the scale to `0–100` (e.g., outputs `90` instead of `0.9`), any downstream consumer that normalises this value by treating it as already in `[0, 1]` will compute wrong results. The Week 4 Cartographer stores this value as `confidence_score` in `cartography_trace.jsonl` (confirmed real value: `0.82`). A value of `82` instead of `0.82` corrupts every lineage edge weight and every downstream confidence threshold check — silently. No exception is raised. The ValidationRunner must catch this with a statistical drift rule, not just a type check.

**2. Renaming `audit_verdict` to `verdict` in Week 2 AuditReport**

My Week 2 Automaton Auditor outputs a Pydantic `AuditReport` with field `audit_verdict` taking values `"PASS"`, `"FAIL"`, or `"DISSENT_DETECTED"`. The field name `audit_verdict` is what `ui_app.py` reads via `final_state.get("audit_verdict")`. If this field is renamed to `verdict`, `ui_app.py` returns `None` silently and renders an empty verdict on the UI. No error is raised. This is a breaking rename with no deprecation alias and no migration script.

**3. Removing `semantic_change` from the Week 1 agent trace output**

My Week 1 Roo Code Master Thinker outputs `semantic_change: "REFACTOR" | "EVOLUTION"` in every `agent_trace.jsonl` record (confirmed in real output). The Week 7 `migrate_week1.py` migration script uses this field to assign `governance_tags` (`["evolution", "feature"]` or `["refactor"]`) in the canonical `intent_record`. If a developer removes the `semantic_change` field — perhaps deciding it is redundant — every downstream governance tag assignment becomes `None`. Any compliance system filtering for `governance_tags` containing `"pii"` or `"billing"` will silently miss records that should have been routed.

---

## Question 2: Tracing the confidence 0–100 Failure Through Week 4

### The Change
My Week 3 system (`src/agents/extractor.py`, function `_save_extraction_artifact`) writes `profile.confidence_score` as a float in `[0.0, 1.0]`. Confirmed real value: `"confidence_score": 0.9`. An update changes this to integer `0–100`, outputting `90` instead of `0.9`.

### How It Propagates to Week 4

The Week 4 Brownfield Cartographer writes a `cartography_trace.jsonl` during every codebase scan. Each trace entry carries `confidence_score: float` (confirmed real value: `0.82` for `ArchivistAgent`, `0.9` for data sources). The Cartographer reads Week 3 extraction outputs as input metadata when building the lineage graph — specifically, the `avg_confidence` from each extraction is used to weight `edge.confidence` in the `cartography_trace.jsonl`.

When `confidence_score` arrives as `90` instead of `0.9`:
- The Cartographer stores edge weights of `90.0` instead of `0.9`
- Any path-scoring algorithm that normalises by edge confidence now divides by `90` instead of `0.9`
- The resulting reliability scores are 100× too small
- High-confidence extraction paths appear to have near-zero reliability in the lineage graph
- The Week 7 ViolationAttributor uses these edge weights to compute `confidence_score` in its blame chain output (`confidence_score = 1.0 - (days_since_commit × 0.1) - (lineage_hops × 0.2)`). Corrupted edge weights produce blame confidence scores outside `[0, 1]`
- The system still runs. The lineage graph is produced. No exception is raised.

### The Data Contract Clause That Catches This (Bitol YAML)

```yaml
# generated_contracts/week3_extractions.yaml (relevant section)
schema:
  profile:
    type: object
    properties:
      confidence_score:
        type: number
        minimum: 0.0
        maximum: 1.0          # BREAKING CHANGE if changed to 0-100
        required: true
        description: >
          Triage confidence score. MUST be a float in [0.0, 1.0].
          A value of 1.0 means maximum confidence.
          Observed real values: 0.9 (FASTTEXT), 0.87 (LAYOUT).
          Changing scale to 0-100 is a CRITICAL breaking change —
          corrupts Week 4 lineage edge weights and Week 7 blame chain.
  metadata:
    type: object
    properties:
      avg_confidence:
        type: number
        minimum: 0.0
        maximum: 1.0          # Same constraint — same breaking risk
        required: true
        description: >
          Average confidence across all LDUs in the document.
          MUST match scale of profile.confidence_score.

quality:
  type: SodaChecks
  specification:
    checks for extractions:
      - min(profile_confidence_score) >= 0.0
      - max(profile_confidence_score) <= 1.0
      - avg(profile_confidence_score) < 0.99   # flag if clamped
      - avg(profile_confidence_score) > 0.01   # flag if broken/zero

lineage:
  downstream:
    - id: week4-brownfield-cartographer
      description: >
        Cartographer reads avg_confidence from extraction metadata
        to weight cartography_trace.jsonl confidence_score entries.
      fields_consumed: [doc_id, profile.confidence_score, metadata.avg_confidence]
      breaking_if_changed: [profile.confidence_score, metadata.avg_confidence]
```

---

## Question 3: How the ViolationAttributor Uses the Week 4 Lineage Graph

When the `ValidationRunner` reports a `FAIL` on a contract clause, the `ViolationAttributor` must answer: *"Which commit introduced this violation and who is responsible?"*

### Step-by-Step Graph Traversal

**Step 1 — Identify the failing schema element.**
Example: check `week3.profile.confidence_score.range` fails. The failing column is `profile.confidence_score` in `outputs/week3/extractions.jsonl`.

**Step 2 — Load the Week 4 lineage snapshot.**
Open `outputs/week4/lineage_snapshots.jsonl` and take the most recent record (highest `captured_at`). My real snapshot contains `66 nodes` and `61 edges` from the jaffle-shop analysis. Build an in-memory directed graph using Python's `networkx` library: nodes are file/dataset identifiers, edges are directed relationships (`IMPORTS`, `PRODUCES`, `READS`, `WRITES`, `FEEDS`).

**Step 3 — Find the upstream source node via BFS.**
Starting from the node representing the Week 3 output path (`file::outputs/week3/extractions.jsonl`), traverse edges in **reverse direction** — following `WRITES` and `PRODUCES` edges backwards. In my Week 4 graph, the relevant edge type is `PRODUCES` (mapped from `DEFINES` in the lineage graph). Stop at the first node whose `type == "FILE"` and `metadata.language == "python"`. This resolves to `file::src/agents/extractor.py` — the file containing `_save_extraction_artifact()` and `_append_ledger_record()`, confirmed from Copilot extraction of Week 3.

**Step 4 — Run git blame on the identified file.**
```bash
git log --follow --since="14 days ago" \
  --format='%H|%an|%ae|%ai|%s' -- src/agents/extractor.py

git blame -L 620,720 --porcelain src/agents/extractor.py
```
Lines 620–720 contain `_append_ledger_record()` — the function that writes `confidence_score` to the ledger. This gives the exact commit hash, author email, timestamp, and commit message for every change to that function.

**Step 5 — Score and rank blame candidates.**
For each commit found, compute:
`confidence_score = 1.0 − (days_since_commit × 0.1) − (lineage_hops × 0.2)`

The commit that changed the confidence scale will be the most recent commit touching lines 620–720 — highest score, ranked first. The lineage hop distance from `extractor.py` to the failing schema element is 1 hop (direct producer), so the confidence penalty is `0.2 × 1 = 0.2`.

**Step 6 — Compute blast radius.**
From the `extractor.py` node, traverse the lineage graph **forward** — following `PRODUCES` and `WRITES` edges. Every downstream node reached is part of the blast radius. In my Week 4 graph, the downstream nodes include: the `module_graph.json` consumer nodes, the `lineage_graph.json` archivist agent, and via that, all 66 nodes that depend on the cartography output.

**Step 7 — Write to `violation_log/violations.jsonl`.**
Output the full violation record including `blame_chain[]` with ranked candidates, `blast_radius` with `affected_nodes`, `affected_pipelines`, and `estimated_records`.

---

## Question 4: Data Contract for LangSmith trace_record (Bitol YAML)

```yaml
# generated_contracts/langsmith_traces.yaml
kind: DataContract
apiVersion: v3.0.0
id: langsmith-trace-records
info:
  title: LangSmith Trace Records — AI Pipeline Observability
  version: 1.0.0
  owner: week7-team
  description: >
    One record per LLM run exported from LangSmith. Covers all chain,
    tool, retriever, and embedding calls made during Weeks 1–5 processing.
    Generated by generate_traces.py from Week 3 extraction runs.

servers:
  local:
    type: local
    path: outputs/traces/runs.jsonl
    format: jsonl

terms:
  usage: Internal AI observability contract. Do not publish externally.
  limitations: >
    total_tokens MUST equal prompt_tokens + completion_tokens exactly.
    end_time MUST be strictly greater than start_time.
    total_cost MUST be >= 0. This is enforced by Phase 4 AI Extensions.

# ── STRUCTURAL CLAUSES ────────────────────────────────────────────────────
schema:
  id:
    type: string
    format: uuid
    required: true
    unique: true
    description: UUIDv4 primary key for this trace run.

  run_type:
    type: string
    required: true
    enum: [llm, chain, tool, retriever, embedding]
    description: >
      Must be one of the five registered run types.
      Observed distribution in my system: chain (68%), llm (22%),
      embedding (10%). A value outside this enum signals an
      unregistered pipeline component — contract violation.

  start_time:
    type: string
    format: date-time
    required: true
    description: ISO 8601 UTC timestamp when the run started.

  end_time:
    type: string
    format: date-time
    required: true
    description: >
      ISO 8601 UTC timestamp when the run ended.
      INVARIANT: end_time > start_time. A violation here indicates
      a clock synchronisation bug or a broken trace writer.

  total_tokens:
    type: integer
    required: true
    minimum: 0
    description: >
      INVARIANT: total_tokens = prompt_tokens + completion_tokens.
      Confirmed in my generated traces: llm runs use ~4200 prompt +
      890 completion = 5090 total. Violation signals accounting bug.

  prompt_tokens:
    type: integer
    required: true
    minimum: 0

  completion_tokens:
    type: integer
    required: true
    minimum: 0

  total_cost:
    type: number
    required: true
    minimum: 0.0
    description: >
      Cost in USD. Observed range: $0.000015 (embedding) to
      $0.0153 (full extraction chain). Must never be negative.

  error:
    type: string
    nullable: true
    description: Null on success. Non-null string on LLM failure.

  tags:
    type: array
    items:
      type: string
    description: >
      Week and operation tags. e.g. ["week3", "extraction", "fasttext"].
      Used for filtering by system origin in AI Contract Extensions.

# ── STATISTICAL CLAUSES ───────────────────────────────────────────────────
quality:
  type: SodaChecks
  specification:
    checks for runs:
      - missing_count(id) = 0
      - duplicate_count(id) = 0
      - min(total_cost) >= 0.0
      - max(total_cost) < 1.0            # flag runaway cost — $1 per run is anomalous
      - avg(total_tokens) < 100000       # flag token explosion
      - min(total_tokens) >= 0
      # Statistical drift baseline (stored in schema_snapshots/baselines.json):
      # WARN if avg(total_cost) shifts > 2 stddev from baseline
      # FAIL if avg(total_cost) shifts > 3 stddev from baseline

# ── AI-SPECIFIC CLAUSES ───────────────────────────────────────────────────
ai_extensions:
  embedding_drift:
    applies_to: inputs
    method: cosine_distance
    column: inputs.texts
    baseline_path: schema_snapshots/embedding_baselines.npz
    threshold: 0.15
    description: >
      Embed a random sample of 200 input text values per run.
      Compute cosine distance from the stored centroid.
      WARN if drift > 0.15. FAIL if drift > 0.25.
      Catches silent prompt distribution shifts between Week 3
      document types (financial vs technical vs legal).

  output_schema_enforcement:
    applies_to: outputs
    run_type_filter: llm
    violation_log_type: llm_output_schema
    description: >
      Every LLM run output must conform to the registered output schema
      for its associated prompt version. Track output_schema_violation_rate
      per prompt_hash. A rising rate signals model behaviour change.
      Observed baseline violation rate: 0.89% across Week 2 verdict runs.

  token_accounting_invariant:
    description: >
      Enforce total_tokens = prompt_tokens + completion_tokens on every record.
      A mismatch indicates a broken token counter in the LLM client wrapper.
      Write violations to violation_log/ with type = "token_accounting".

lineage:
  upstream:
    - id: week3-document-refinery
      description: Week 3 extraction LLM calls produce the majority of trace records.
    - id: week2-digital-courtroom
      description: Week 2 verdict LLM calls are logged as chain run_type records.
  downstream:
    - id: week7-ai-contract-extensions
      fields_consumed: [total_tokens, total_cost, inputs, outputs, run_type, tags]
      breaking_if_changed: [run_type, total_tokens, total_cost]
```

---

## Question 5: The Most Common Failure Mode — Contract Staleness

### The Problem

The most common production failure mode of contract enforcement systems is not technical — it is organisational. **Contracts go stale.** A contract is written once, describes the schema at that moment in time, and is never updated as the producing system evolves. Within weeks the contract no longer reflects reality. The enforcement system generates false positives (flagging valid data) or false negatives (passing corrupt data because the baseline itself drifted with the system).

### Why Contracts Get Stale — Three Root Causes

**1. The contract does not live with the code that produces the data.**
In my Week 3 system, the code that writes `confidence_score` is in `src/agents/extractor.py` at line 666 (function `_append_ledger_record`). If the contract YAML file lives in a separate repo — Week 7 — no developer sees it when they change the extractor. There is no CI gate that re-validates the contract on every merge to the Week 3 repo. This is the exact scenario that caused the `confidence 0–100` bug in the challenge brief.

**2. No schema snapshot discipline.**
Without timestamped snapshots, you can detect that a change happened but not *when* it happened. My Week 4 Brownfield Cartographer produces `cartography_trace.jsonl` with timestamps, which gives a temporal anchor. But without a corresponding schema snapshot at the same timestamp, you cannot narrow the git log query to the right time window. The blame chain becomes unreliable because you are searching 14 days of commits instead of a 2-hour window.

**3. Statistical baselines are never refreshed after planned migrations.**
After a legitimate model upgrade that changes the confidence distribution — say, a new version of Gemini Flash that returns higher confidence scores — the validation system floods with false `WARN` alerts because the baseline was established under the old model. Teams learn to ignore alerts. The contract is "enforced" in name only.

### How This Architecture Prevents It

**Prevention 1 — Automated contract regeneration on every CI run.**
`ContractGenerator` re-runs on every push against live `outputs/` JSONL files. If the inferred schema diverges from the committed contract by more than one field, the CI step fails and blocks the merge. Contracts cannot go stale without a deliberate human override. In practice, for my Week 3 system, this means `confidence_score` is re-profiled on every run: if `max(confidence_score)` exceeds `1.0`, the CI step emits a `CRITICAL` violation before any downstream system is affected.

**Prevention 2 — Timestamped schema snapshots creating an audit trail.**
Every `ContractGenerator` run writes to `schema_snapshots/{contract_id}/{timestamp}.yaml`. My Week 4 migration produced 2 snapshots: one from 7 days ago and one current, with an injected schema change (new `SERVICE` node). The `SchemaEvolutionAnalyzer` diffs these automatically and answers: *"When exactly did this field change?"* This narrows the git blame window from 14 days to minutes.

**Prevention 3 — Lineage-linked contracts with blast radius pre-computation.**
Every contract carries a `lineage.downstream[]` block listing which systems consume it and which fields are `breaking_if_changed`. For my Week 3 contract, `profile.confidence_score` and `metadata.avg_confidence` are listed as `breaking_if_changed` with `week4-brownfield-cartographer` as the downstream consumer. When a PR touches `_append_ledger_record()` in Week 3, the contract system can pre-compute the blast radius before merge — not after breakage.

**Prevention 4 — Statistical baseline versioning.**
Baselines are stored in `schema_snapshots/baselines.json` with a timestamp. After a planned schema migration is approved, the `ValidationRunner` re-seeds the baseline on the first passing run. The contract evolves with the data intentionally, not silently. The key discipline: the team lead must explicitly approve the new baseline, creating an audit record of the decision.

---

## Contract Quality Floor (Phase 1 Measurement)

Per the challenge requirement, I measured the fraction of auto-generated clauses that are correct without manual editing:

| Contract | Total Clauses | Correct Without Edit | Pass Rate |
|---|---|---|---|
| week3_extractions.yaml | 14 | 11 | 78.6% |
| week5_events.yaml | 12 | 9 | 75.0% |

Overall pass rate: **76.9%** — above the 70% target.

Failure patterns observed:
- LLM annotation occasionally generates overly broad business rules (e.g., `confidence > 0.5` when the real threshold is `0.75`)
- Lineage context injection misses nested field consumers (e.g., `metadata.escalation_history[*].confidence` not detected as a downstream dependency)
- Statistical ranges for financial fields (Decimal types) are sometimes flagged as float, requiring manual type correction
# RepoSeer — End-to-End Pipeline

> **Architecture specification (proposed, not implemented).** The diagrams define the system design, not verified model performance or data availability.

**Goal:** Predict the risk of **confirmed maintenance abandonment of a PyPI package within 12 months after `cutoff_at`**, using only information available at that cutoff. This is an operationally verified target, not a detector of every silent abandonment.

**Prediction modes:**
- **CORE:** D01 → D02 → D09 → D11 (Model A).
- **ENHANCED:** CORE + D03–D08 (Model B, only after validation and deployment gates).
- **External insights:** Optional evidence and narrative; never change a model probability.
- **Dependency impact (D12):** Optional, independent of abandonment risk.

**Global rules:**
- Prediction unit: canonical PyPI `package_id`; versioned package↔GitHub `repo_id` mapping; `repo_group_id` groups packages sharing a repository.
- Historical inputs require **`available_at <= cutoff_at`** and a valid source/mapping revision. `event_at` alone is insufficient.
- Official `already_abandoned` / `scheduled_cessation` statuses override predictions. No score is returned for either.
- Labels: `1` = independently verified cessation effective within the next 12 months; `0` = sufficiently observed and verified continued maintenance; otherwise `unknown/censored`.
- Unknown, missing, partial, and unavailable data are **not zero**. Unsupported or out-of-distribution (OOD) inputs may produce `insufficient_evidence`.
- Agent outputs are evidence-backed signals, **not labels or probabilities**. D09 independently verifies official cessation events.
- Calibrated probabilities apply to the evaluated population, not automatically to all PyPI packages.
- **E0 = CORE; E1 = CORE + deterministic external features; E2 = E1 + specialist-agent features.** E1 retrieval/relevance may use an LLM.

## D00 — System Overview

```mermaid
flowchart TB
  subgraph OFFLINE["OFFLINE — Build, Train, Evaluate"]
    C["D09 PyPI cohort, historical mappings, cutoffs"] --> H["D02 point-in-time source collection"]
    H --> F["D09 core features"]
    H --> L["D09 independent labels and status ledger"]
    H --> G{"D03–D06 historical external data feasible?"}
    G -->|Yes| X["D04–D08 E1 and E2 features"]
    G -->|No| CO["CORE-only; record limitation"]
    F --> EX["D10 temporal E0/E1/E2 experiments"]
    L --> EX
    X --> EX
    CO --> EX
    EX --> V["Validation: select, calibrate, freeze"]
    V --> T["Untouched chronological test"]
    T --> R["Registry: Model A, optional Model B"]
  end

  subgraph ONLINE["ONLINE — Analyze a Package"]
    Q["PyPI name / GitHub URL + mode + cutoff"] --> ID["D01 identity, mapping, status"]
    ID --> S{"Verified cessation or future plan?"}
    S -->|Yes| KS["Known status; no probability"]
    S -->|Mapping unresolved / unsupported| NS["No score; reason"]
    S -->|No known status/plan| SRC["D02 core sources"]
    SRC --> CF{"D09 valid core vector and applicability?"}
    CF -->|No| NS
    CF -->|Yes| A["D11 Model A: p_core"]
    A --> M{"Mode?"}
    M -->|CORE| CR["Use CORE"]
    M -->|ENHANCED| B0{"Model B registered and compatible?"}
    B0 -->|No| FB["CORE fallback + reason"]
    B0 -->|Yes| XP["D03–D08 external pipeline"]
    XP --> BC{"Coverage, schema, status valid?"}
    BC -->|Yes| B["Model B: p_enhanced"]
    BC -->|No| FB
    BC -->|Official status found| KS
    KS --> REP["D13 typed report"]
    NS --> REP
    CR --> REP
    FB --> REP
    B --> REP
    REP --> UI["Frontend"]
  end

  R -.-> A
  R -.-> B
  ID -.-> IMP["D12 optional dependency impact"]
  IMP -.-> REP
  CR -.->|On request| INS["External insights only"]
  FB -.->|On request| INS
  INS -.-> REP
  OPS["D14 orchestration and budgets"] -.-> XP
  ART["D15 artifacts and lineage"] -.-> R
  ART -.-> REP
```

**Time semantics:** `current_inference` uses the deployed model now; `retrospective_analysis` applies a current model to past features; only D10 produces a valid `prospective_backtest` with a model fitted before the simulated test period.

## D01 — Target Resolution and Official Status (T-003/T-011)

```mermaid
flowchart TB
  IN["AnalyzeRequest: identifier, mode, cutoff, purpose"] --> K{"Identifier type?"}
  K -->|PyPI| P["PEP-503 normalize; verify package"]
  K -->|GitHub| G["Resolve URL to numeric repo_id"]
  K -->|Other| BAD["invalid_input"]
  P --> C["Find package↔repo mapping evidence"]
  G --> C
  C --> N{"Verified candidates?"}
  N -->|One| MAP["Versioned mapping, scope, validity, revision"]
  N -->|Multiple| PICK["Choose and verify PyPI package"]
  N -->|None / ambiguous| UN["mapping_unresolved"]
  PICK -->|Verified| MAP
  PICK -->|Not verified| UN
  MAP --> AS{"Mapping valid at cutoff?"}
  AS -->|No proof| HU["mapping_history_unverified"]
  AS -->|Yes| E{"Eligible public PyPI project?"}
  E -->|No| UNS["unsupported"]
  E -->|Yes| O["Official plan/cessation ledger, as-of revisions"]
  O --> STOP{"Confirmed cessation effective and known by cutoff?"}
  STOP -->|Yes| AB["already_abandoned; probability=null"]
  STOP -->|No| PLAN{"Official future plan known and not revoked?"}
  PLAN -->|Yes| SC["scheduled_cessation; effective_at, horizon flag"]
  PLAN -->|No| OUT["ResolvedTarget → D02/D09"]
```

- A GitHub monorepo must resolve to a specific PyPI package; do not assume one repo equals one package.
- Only authoritative evidence available at cutoff sets status. Rumors and later discoveries cannot be backdated.

## D02 — Source Collection and Storage (T-012–T-020)

```mermaid
flowchart TB
  IN["ResolvedTarget + scope + cutoff + lookback"] --> CA{"Valid cached records?"}
  CA -->|Yes| READ["Load raw refs, normalized data, coverage"]
  CA -->|No / partial| PLAN["Endpoint windows, limits, quotas"]
  PLAN --> Q["Independent HTTPX queues; auth and Retry-After"]
  Q --> GH1["GitHub repo metadata — current only"]
  Q --> GH2["Commits + changed paths + timestamps"]
  Q --> GH3["Issues; exclude PR entries"]
  Q --> GH4["PRs, merges, changed files"]
  Q --> GH5["Releases, tags, contributors"]
  Q --> PY1["PyPI versions, uploads, requires_python"]
  Q --> PY2["Version-matched metadata, requires_dist"]
  Q --> OPT["Optional downloads / GH Archive"]
  Q --> DEP["Optional dependencies / OSV"]
  GH1 --> NEXT{"More pages/windows?"}
  GH2 --> NEXT
  GH3 --> NEXT
  GH4 --> NEXT
  GH5 --> NEXT
  PY1 --> NEXT
  PY2 --> NEXT
  OPT --> NEXT
  DEP --> NEXT
  NEXT -->|Yes| CP["Save endpoint cursor, time window, rate state"]
  CP --> Q
  NEXT -->|No| RAW["Immutable RawRecord: event/available/fetched_at"]
  Q -->|Retries exhausted| ERR["Partial/failed endpoint; never assume zero"]
  ERR --> RAW
  RAW --> VAL["Pydantic schemas, UTC, canonical IDs, dedup"]
  VAL --> DB["Versioned Parquet + DuckDB views"]
  READ --> DB
  DB --> SC["Attach package scope and repo_group_id"]
  SC --> PROF["SourceManifest: coverage, lag, errors, provenance"]
  PROF --> OUT["D03 / D09 / D12 / D13"]
```

**Historical rule:** Current repository metadata, current issue state, totals, or dependency graph cannot be used as historical features without versioned as-of proof. Each endpoint checkpoints independently.

## D03 — Repository Functional Profiler

```mermaid
flowchart TB
  IN["Verified package, repo, scope, cutoff"] --> TIME{"Historical analysis?"}
  TIME -->|Yes| OLD["Locate code/docs revision accessible at cutoff"]
  TIME -->|No| NOW["Fetch current revision and freshness"]
  OLD --> CHECK{"Revision/availability verified?"}
  CHECK -->|No| MISS["Profile unavailable; revision_unverified"]
  CHECK -->|Yes| DOC["Package README, docs, examples, manifest"]
  NOW --> DOC
  DOC --> META["Use as-of PyPI/repository descriptions"]
  META --> CLEAN["Clean text; retain purpose and use cases"]
  CLEAN --> SPAN["Evidence spans, offsets, source revisions"]
  SPAN --> CACHE{"Valid versioned profile cache?"}
  CACHE -->|Yes| PROF["FunctionalProfile"]
  CACHE -->|No| LLM["Structured LLM: functions, use cases, category, alternatives"]
  LLM --> V{"Schema and quote grounding valid?"}
  V -->|Yes| PROF
  V -->|No; retry available| FIX["One corrective call; revalidate"]
  FIX -->|Pass| PROF
  FIX -->|Fail| MISS
  V -->|No; exhausted| MISS
  PROF --> ALT["Verify functional alternatives as-of"]
  ALT --> SAVE["Versioned profile + evidence + uncertainty"]
  SAVE --> SEARCH["D04 function-aware queries"]
  MISS --> DIRECT["D04 direct exact-identifier queries only"]
```

**Boundary:** A functional profile comes from the target package's versioned official documentation—not later documents, dependency guesses, or external articles.

## D04 — Query Planning and External Discovery (T-016)

```mermaid
flowchart TB
  IN["D03 profile or direct-only fallback"] --> OK{"Profile usable?"}
  OK -->|Yes| INT["Four intents"]
  OK -->|No| DIR["Exact package/repo identifiers only"]
  INT --> Q1["Direct package mentions"]
  INT --> Q2["Functional need / use-case"]
  INT --> Q3["Verified alternatives"]
  INT --> Q4["Explicit migration/adoption"]
  DIR --> Q1
  Q1 --> VALID["Dedup and validate queries; query_set_hash"]
  Q2 --> VALID
  Q3 --> VALID
  Q4 --> VALID
  VALID --> WIND["As-of-safe historical or 30/90/180d windows"]
  WIND --> JOB["Source-query-window jobs + budgets"]
  JOB --> HTTP["HN / DEV / Stack Exchange; optional verified sources"]
  HTTP --> RES{"Result?"}
  RES -->|Success| PARSE["Source ID, URL, dates, native cursor"]
  RES -->|429 / 5xx / timeout| RET{"Retry budget?"}
  RET -->|Yes| WAIT["Retry same page with backoff"]
  WAIT --> HTTP
  RET -->|No| PART["Record partial/failed source and cursor"]
  PARSE --> UPSERT["Upsert candidate; preserve all query IDs"]
  UPSERT --> MORE{"More pages and budget?"}
  MORE -->|Yes| STEP["Save next source cursor"]
  STEP --> HTTP
  MORE -->|No| MAN["Query/window census and completeness"]
  PART --> MAN
  MAN --> FULL{"Complete comparable census?"}
  FULL -->|Yes| GOOD["Eligible for E1 coverage checks"]
  FULL -->|No| BAD["Partial/unknown; counts nullable"]
  GOOD --> OUT["D05 candidates + manifest"]
  BAD --> OUT
```

**Comparability:** Trend windows require the same *actual* query set, source, filters, and retrieval policy. Prefer shared source/window indexes; top-K results are not a complete census.

## D05 — External Documents and Relevance (T-017/T-018)

```mermaid
flowchart TB
  IN["ExternalCandidate + query/source provenance"] --> TYPE{"Source?"}
  TYPE -->|Native forum API| API["Fetch body, tags, edits, metadata"]
  TYPE -->|Article link| WEB["Safe HTTPX fetch + Trafilatura extraction"]
  API --> RAW["Raw versioned external_document"]
  WEB --> RAW
  RAW --> CLEAN["Canonical IDs/URLs, UTC, text metadata"]
  CLEAN --> DUP["Exact/near dedup; syndication tracking"]
  DUP --> MATCH["Resolve GitHub/PyPI links and entity"]
  MATCH --> DIRECT{"Verified direct entity link?"}
  DIRECT -->|Yes| CONT{"Substantive package context?"}
  DIRECT -->|No| LEX["As-of lexical/disambiguation filter"]
  CONT -->|Yes| SAVE["RelevanceLabel + spans + context_level"]
  CONT -->|No| LEX
  LEX --> POT{"Same use-case / alternative?"}
  POT -->|No| REJ["Irrelevant + reason"]
  POT -->|Yes| EMB["Passage embeddings and lexical score"]
  EMB --> TH{"Validated relevance thresholds"}
  TH -->|Low| REJ
  TH -->|High| ACCEPT["Same-function / alternative candidate"]
  TH -->|Ambiguous| LLM["LLM relevance judge + quoted span"]
  LLM --> GR{"Grounded?"}
  GR -->|Yes| ACCEPT
  GR -->|No| UN["Unresolved; exclude from ML"]
  REJ --> SAVE
  ACCEPT --> SAVE
  UN --> SAVE
  SAVE --> TIME["Attach document revision and availability proof"]
  TIME --> OUT["D06 eligibility; keep rejects for evaluation"]
```

**E1 isolation:** Relevance may use an LLM, but E1 counts do not use D07 specialist-agent outputs.

## D06 — Temporal Evidence and Leakage Gates

```mermaid
flowchart TB
  IN["Versioned document/claim + subject + timestamps"] --> USE{"Historical or current?"}
  USE -->|Historical| HIST["Check profile, query, document revisions as-of"]
  USE -->|Current| CUR["Check freshness and source revisions"]
  HIST --> PROOF{"Available at cutoff?"}
  PROOF -->|No / uncertain| MISS["Exclude feature; record missing_reason"]
  PROOF -->|Yes| T["Check event_at, effective_at, announced_at, available_at"]
  CUR --> T
  T --> AUTH{"Official cessation evidence?"}
  AUTH -->|Ceased and known by cutoff| AB["already_abandoned precedence"]
  AUTH -->|Future plan known by cutoff| SC["scheduled_cessation precedence"]
  AUTH -->|Earlier cessation discovered later| LED["D09 offline factual risk-set audit only"]
  AUTH -->|No| SHORT{"Reveals future outcome?"}
  SHORT -->|Yes| REPORT["Report-only; exclude predictive features"]
  SHORT -->|No| SAFE["D07 agents or E1 census"]
  AB --> STATUS["D13 known-status evidence"]
  SC --> STATUS
  LED --> VERIFY["D09 independent event verification"]
```

**Rule:** `published_at` alone does not validate an edited document's earlier contents. Known-outcome shortcuts are excluded from E1/E2 features, even if published before cutoff.

## D07 — Three Specialist Agents and Verification (T-024/T-025)

```mermaid
flowchart TB
  E["D06 eligible external evidence"] --> ROUTE["Deterministic router: entity, context, topic"]
  O["Versioned GitHub/PyPI official text"] --> OT["Equivalent D06 time and scope gates"]
  OT -->|Eligible| OFF["Official direct-package evidence"]
  OT -->|Invalid| MISS["Coverage / missing_reason"]
  ROUTE --> M["Maintenance / end-of-support"]
  ROUTE --> F["Function / use-case"]
  ROUTE --> A["Alternatives / migration"]
  M --> AG1
  OFF --> AG1
  F --> AG2
  A --> AG3
  subgraph AGENTS["THREE PARALLEL SPECIALISTS"]
    AG1["Agent 1 — Maintenance & Lifecycle"]
    AG2["Agent 2 — Functional Trend & Demand"]
    AG3["Agent 3 — Alternatives & Adoption"]
  end
  AG1 --> SIG["Typed ExtractedSignal + source quote/time"]
  AG2 --> SIG
  AG3 --> SIG
  SIG --> GR{"Schema, entity, quote, temporal proof valid?"}
  GR -->|No| REJ["Reject + audit reason"]
  GR -->|Yes| DUP["Dedup original event and syndicated copies"]
  DUP --> CON{"Conflicting evidence?"}
  CON -->|No| VAL["ValidatedSignal"]
  CON -->|Yes| REVIEW["One bounded LLM conflict review"]
  REVIEW --> RECHECK["Deterministic revalidation"]
  RECHECK -->|Resolved| VAL
  RECHECK -->|Unresolved| UN["Report-only; no ML feature"]
  VAL --> KIND{"Signal class?"}
  KIND -->|Cutoff-safe, non-outcome| E2["D08 E2 features"]
  KIND -->|Official cessation candidate| CAN["EventCandidate → D09 verifier"]
  KIND -->|Context only| REP["D13 report evidence"]
  CAN --> V["Uniform, independent D09 verification"]
  V -->|Confirmed| LED["Authoritative event/plan ledger"]
  V -->|Unclear| REP
  UN --> REP
```

**Agent boundaries:** Agent 1 handles maintenance/lifecycle, Agent 2 functional demand (not package user counts), Agent 3 alternatives and explicit migrations. No additional LLM coordinator agent is required. Agents cannot modify labels or risk scores.

## D08 — E1/E2 Features and Coverage (T-022)

```mermaid
flowchart TB
  C["D04–D06 as-of eligible source/query census"] --> CK{"Same query hash/source/policy and full windows?"}
  CK -->|No| N1["E1 affected features = null + reason"]
  CK -->|Yes| UNI["Dedup unique source items and origins"]
  UNI --> WIN["Equal recent / preceding 30/90/180d windows"]
  WIN --> COUNT["Counts by source, intent, context_level"]
  COUNT --> RATE["rate30 = 30 × count / observed_days"]
  RATE --> DELTA["delta = log1p(recent) - log1p(previous)"]
  COUNT --> E1["E1: direct/function/alternative counts and rates"]
  DELTA --> E1
  N1 --> E1
  S["D07 cutoff-safe ValidatedSignal"] --> DUP["Dedup by original event + subject"]
  DUP --> COVER{"Eligible vs processed documents sufficient?"}
  COVER -->|No| N2["E2 affected features = null + reason"]
  COVER -->|Yes| EVENTS["Distinct 90/180d maintenance/migration/trend events"]
  EVENTS --> E2["E2: nullable agent-event counts"]
  N2 --> E2
  E1 --> JOIN["Join on package_id + cutoff_at"]
  E2 --> JOIN
  JOIN --> META["Query/window lineage, completeness, processed coverage"]
  META --> OUT["D10: E0, E1, E2 experiments"]
```

**Constraints:** Full windows are required for comparable rates; never extrapolate agent counts from top-K documents. E2 adds verified specialist-agent signals to E1 without double-counting events or introducing revealed outcomes.

## D09 — Historical Cohort, Core Features, and Labels

```mermaid
flowchart TB
  IN["D02 events + versioned package↔repo mappings"] --> MODE{"Offline or online?"}
  MODE -->|Offline| CO["Build outcome-independent PyPI cohort"]
  CO --> MAP["Historical mapping, package scope, repo group"]
  MAP --> CUT["Quarterly cutoffs; 3/6/12m lookbacks"]
  MODE -->|Online| CUR["Single target + current / retrospective cutoff"]
  CUT --> ASOF["Point-in-time joins; available_at <= cutoff"]
  CUR --> ASOF
  ASOF --> SCOPE{"Package-level attribution supported?"}
  SCOPE -->|Yes| PK["Scoped commits/PRs + quality"]
  SCOPE -->|No| REPO["Repo-only activity; mark scope_quality"]
  PK --> GH["GitHub: human commits, contributors, issues/PRs"]
  REPO --> GH
  ASOF --> PY["PyPI: releases, cadence, age, Python support"]
  GH --> X["CoreFeatureVector + nullable coverage"]
  PY --> X

  CUT --> FIND["Independent official evidence search for entire cohort"]
  FIND --> LED["Event/plan ledger: effective, available, verified, revoked"]
  LED --> PRIOR{"Ceased by cutoff or official future plan public?"}
  PRIOR -->|Yes| DROP["Exclude from unknown-outcome risk-set"]
  PRIOR -->|No| H["Observe next 12 months after cutoff"]
  H --> L{"Outcome verification?"}
  L -->|Confirmed cessation in horizon| P["Label 1 + label_available_at"]
  L -->|Full follow-up + verified maintenance| N["Label 0 + label_available_at"]
  L -->|Silent / immature / ambiguous| U["Unknown / censored + reason"]
  P --> AUD["Bias audit: 1/0/unknown, year, age, activity, coverage"]
  N --> AUD
  U --> AUD
  DROP --> AUD
  X --> JOIN["Join strict 1/0 at package + cutoff"]
  AUD --> JOIN
  JOIN --> OUT["D10 strict labeled set + full eligibility audit universe"]
```

**Label policy:**
- `1`: Independently verified *effective cessation* within the 12-month horizon. An archive, inactivity, or unverified replacement alone is insufficient.
- `0`: Complete 12-month observation, consistent authoritative verification, and predefined evidence of ongoing maintenance/support. Absence of a cessation notice alone is insufficient.
- `unknown/censored`: Incomplete follow-up, silent project, uncertain mapping, contradictory evidence, or coverage gaps; excluded from supervised fitting.
- Discovery and verification must apply uniformly to the entire cohort; Agent 1 event candidates are only leads. Keep `label_available_at` separate from `effective_at`.

**Calibration scope:** Audit selection bias between the strict labeled cohort and the full eligible cohort. Do not claim ecosystem-wide calibrated risk without evidence.

## D10 — Leakage-Safe E0/E1/E2 Evaluation (T-023)

```mermaid
flowchart TB
  DATA["D09 strict labels + core; D08 E1/E2 lineage"] --> AUD["Audit class balance, unknowns, scope, drift"]
  AUD --> DESIGN["Predeclare cohorts, folds, metrics, gates"]
  DESIGN --> SPLIT["Chronological train / validation / future test"]
  SPLIT --> TIME["label_available_at <= fit/selection time"]
  TIME --> PURGE["Purge overlapping horizons; group repo/event leakage"]
  PURGE --> E0["E0 = CORE; logistic regression / GBDT"]
  PURGE --> FEAS{"Historical external feasibility and shared cohort?"}
  FEAS -->|Yes| E1["E1 = E0 + deterministic external features"]
  E1 --> E2["E2 = E1 + verified specialist-agent features"]
  FEAS -->|No| SKIP["CORE-only; document limitation"]
  E0 --> VAL["Train/validation: PR-AUC, P/R/F1, Brier, calibration, CIs"]
  E1 --> VAL
  E2 --> VAL
  VAL --> GATE["Predetermined gain, calibration, coverage, OOD gates"]
  GATE --> FREEZE["Freeze Model A and optional Model B=E2"]
  FREEZE --> TEST["One untouched future chronological holdout"]
  TEST --> CHECK{"Severe integrity/deployment failure?"}
  CHECK -->|Yes| VETO["Veto release; require new holdout for redesign"]
  CHECK -->|No| REG["Versioned models, calibrators, schemas, policies"]
  SKIP --> REP["Experiment report and limitations"]
  VETO --> REP
  REG --> REP
  REP --> OUT["D11 loads frozen artifacts only"]
```

**Temporal contract:** Each row stores `cutoff_at`, `outcome_end_at`, `label_available_at`, `repo_group_id`, and optional `event_id`. Each fold records `fold_fit_at`, `selection_freeze_at`, `test_start_at`, and `evaluation_as_of`.

- Fit and tune only with labels available at their respective decision times. Never randomly split snapshot rows.
- Compare E0/E1/E2 on the **same E2-eligible cohort**; also evaluate E0 on the broader core-eligible cohort.
- Keep primary chronological generalization and secondary unseen-repository generalization separate.
- Final test **confirms or vetoes** frozen decisions; it does not select models, thresholds, or calibration.
- Model B requires validation-proven improvement, acceptable calibration, sufficient coverage, and compatible deployment artifacts.

## D11 — Online Inference and Fallback (T-027)

```mermaid
flowchart TB
  IN["AnalyzeRequest: mode, cutoff, insights flag"] --> PURPOSE{"Analysis purpose?"}
  PURPOSE -->|Current| CUR["current_inference"]
  PURPOSE -->|Past with deployed model| RET["retrospective_analysis; not backtest"]
  CUR --> D1["D01 resolve identity and official status"]
  RET --> D1
  D1 --> ST{"As-of official status?"}
  ST -->|Ceased| AB["already_abandoned; no score"]
  ST -->|Future plan| SC["scheduled_cessation; no score"]
  ST -->|Unsupported| NO["No score + reason"]
  ST -->|Unverified| COL["D02 core collection/cache"]
  COL --> FEAT["D09 core vector, scope and nulls"]
  FEAT --> GATE{"Core schema, coverage, OOD pass?"}
  GATE -->|No| NO
  GATE -->|Yes| A["Model A + saved preprocessing/calibration + SHAP"]
  A --> MODE{"Requested mode?"}
  MODE -->|CORE| CORE["p_selected = p_core"]
  MODE -->|ENHANCED| B0{"Model B registry/schema/policy ready?"}
  B0 -->|No| FB["Use CORE + fallback reason"]
  B0 -->|Yes| EXT["D03–D08 external features"]
  EXT --> ST2{"New official status independently verified?"}
  ST2 -->|Ceased| AB
  ST2 -->|Future plan| SC
  ST2 -->|No| COV{"As-of E2 coverage and schema valid?"}
  COV -->|No| FB
  COV -->|Yes| B["Model B + saved calibration + SHAP"]
  B --> ENH["p_selected = p_enhanced; retain p_core"]
  CORE --> INS
  FB --> INS
  ENH --> INS
  INS{"Insights requested and not already available?"}
  INS -->|Yes| EVID["D03–D07 report-only evidence"]
  INS -->|No| REP["D13 report assembly"]
  EVID --> RECHECK{"New official status verified?"}
  RECHECK -->|Ceased| AB
  RECHECK -->|Future plan| SC
  RECHECK -->|No| REP
  AB --> REP
  SC --> REP
  NO --> REP
  REP --> UI["Frontend: purpose, mode, status, provenance"]
  REP -.-> IMP["D12 optional non-blocking impact"]
  IMP -.-> UI
```

**Precedence and fallback:** Official known status clears any previously computed probability. If Model B or external coverage fails, return valid CORE with `mode_used=CORE` and a reason. CORE never calls external LLMs by default; report-only insights never generate `p_enhanced`. If applicability fails, return an explicit no-score.

## D12 — Reverse Dependency Impact (Optional)

```mermaid
flowchart TB
  IN["Package + target version + graph_as_of + budget"] --> HAVE{"Versioned multi-package corpus available?"}
  HAVE -->|No| MISS["impact_status = unavailable"]
  HAVE -->|Yes| EDGE["Collect outbound requires_dist / deps.dev edges"]
  EDGE --> TIME{"Historical edge states verified?"}
  TIME -->|Yes| HIST["Snapshot-valid historical graph"]
  TIME -->|No| NOW["Current corpus graph only"]
  HIST --> NORM["Normalize versions, constraints, markers, IDs"]
  NOW --> NORM
  NORM --> RES{"Resolver/environment policy available?"}
  RES -->|Yes| SNAP["snapshot_resolved reverse index"]
  RES -->|No| POT["observed_potential reverse index"]
  SNAP --> REV["Reverse edges: corpus parents of target"]
  POT --> REV
  REV --> COV{"Target and corpus coverage sufficient?"}
  COV -->|No| MISS
  COV -->|Yes| D["Unique direct dependents"]
  D --> T["Bounded visited-set transitive traversal"]
  T --> SCORE["Optional heuristic; not probability"]
  SCORE --> OUT["ImpactResult: graph kind, as-of, corpus, counts"]
  IN --> OSV["Optional as-of OSV vulnerability context"]
  OSV --> OUT
  OUT --> REP["D13 independent impact panel"]
  MISS --> REP
```

**Meaning:** Reverse dependencies require a *corpus of outbound edges from many packages*. Without it, impact is unavailable—not zero. `observed_potential` is not a global PyPI count; `snapshot_resolved` requires explicit version/environment resolution. Impact and OSV data do not alter abandonment risk.

## D13 — Final Report and Frontend (T-021/T-026/T-028/T-029)

```mermaid
flowchart TB
  P["D11 score, known status, or no-score"] --> BUILD["Typed FinalReport union"]
  E["D07 grounded external evidence"] --> BUILD
  M["D02/D04/D09/D10 coverage, model, population"] --> BUILD
  G["D12 independent dependency impact"] --> BUILD
  BUILD --> STATE{"result_status?"}
  STATE -->|predicted| RISK["Probability + 12m horizon + model-specific SHAP"]
  STATE -->|already_abandoned| AB["Verified cessation; prediction fields omitted"]
  STATE -->|scheduled_cessation| SC["Official plan, effective date; prediction fields omitted"]
  STATE -->|unresolved / insufficient / failed| ERR["Reason, coverage gap, OOD; prediction fields omitted"]
  RISK --> FACT["Evidence-based explanation; score unchanged"]
  FACT --> VALID["Validate status/prediction exclusivity and lineage"]
  AB --> VALID
  SC --> VALID
  ERR --> VALID
  VALID --> JSON["reports/final_report.json + analysis_run_id"]
  JSON --> U1["UI: identity, cutoff, purpose, status"]
  JSON --> U2["UI: probability/SHAP only when predicted"]
  JSON --> U3["UI: external insights with citations"]
  JSON --> U4["UI: potential/resolved impact or unavailable"]
  JSON --> U5["UI: calibration scope, coverage, OOD, missing"]
```

| Field | Requirement |
|---|---|
| Identity | `package_id`, `repo_id`, `cutoff_at`, `analysis_run_id`, mapping provenance |
| Purpose | `current_inference` / `retrospective_analysis` / D10-only `prospective_backtest` |
| Status | `predicted`, `already_abandoned`, `scheduled_cessation`, or explicit unresolved/insufficient/failed reason |
| Execution | `mode_requested`, `mode_used`, `enhanced_status`, `fallback_reason`, `external_insights_status` |
| Prediction | Only for `predicted`: calibrated `probability`, 12-month horizon, risk level, model/schema/calibration versions |
| Explanation | Model-specific SHAP; source-backed evidence and external context |
| Applicability | Training/calibration population, OOD/coverage assessment, uncertainty |
| Impact | Observed corpus counts and graph semantics, or `unavailable` |
| Lineage | Source revisions, availability times, query hashes, missing reasons, policy/model versions |

**Invariant:** Only `result_status="predicted"` includes `prediction` and `prediction.probability`. Known-status/no-score branches omit `prediction`, `core_prediction`, and `probability` entirely, including null-valued fields. A retrospective score must not be presented as prospective historical accuracy.

## D14 — Orchestration, Caching, and Failures

```mermaid
flowchart TB
  REQ["Request: mode, cutoff, purpose, cache/insights flags"] --> ID["D01 mapping + official status"]
  ID --> ELIG{"Eligible for prediction?"}
  ELIG -->|No| DIRECT["Known-status / unresolved report"]
  ELIG -->|Yes| CACHE["Versioned report-cache lookup"]
  CACHE --> HIT{"Fresh and policy-compatible?"}
  HIT -->|Yes| READY["Return cached report + provenance"]
  HIT -->|No| JOB["Create run_id + stage-state ledger"]
  JOB --> SRC["D02 independent bounded queues"]
  SRC --> CORE["D09 core schema, coverage, OOD"]
  CORE --> OK{"Core valid?"}
  OK -->|No| NS["No-score + missing reasons"]
  OK -->|Yes| A["D11 Model A first"]
  A --> MODE{"ENHANCED requested?"}
  MODE -->|No| WANTS{"External insights requested?"}
  WANTS -->|No| FIN["Assemble selected result"]
  WANTS -->|Yes| INS["D03–D07 insights-only"]
  MODE -->|Yes| PREF{"Model B / policy / budget preflight?"}
  PREF -->|No| FALL["CORE fallback"]
  PREF -->|Yes| PIPE["D03 profile → D04 query → D05 fetch"]
  PIPE --> PIPE2["D06 time gate → D07 agents → D08 features"]
  PIPE2 --> ST{"Official status verified?"}
  ST -->|Yes| STOP["Known-status report; remove score"]
  ST -->|No| COV{"External schema/coverage valid?"}
  COV -->|Yes| B["D11 Model B"]
  COV -->|No| FALL
  FALL --> WANT2{"Insights requested?"}
  WANT2 -->|Yes| INS
  WANT2 -->|No| FIN
  INS --> RECHECK{"Official status verified?"}
  RECHECK -->|Yes| STOP
  RECHECK -->|No| FIN
  B --> FIN
  NS --> FIN
  STOP --> FIN
  DIRECT --> FIN
  FIN --> REP["D13 contract validation + persist"]
  JOB -.-> IMP["D12 optional, separate time/budget"]
  IMP -.-> REP
  REP --> MET["Metrics: latency, API/LLM cost, coverage, fallback"]
  MET --> UI["UI: stage state, mode used, limitations"]
  READY --> UI
```

**Cache key:** Target/cutoff/purpose, mapping+scope revision, sources/freshness, profile/query/feature/label policies, model+calibration versions, official plan status, and insights flag. All stages use bounded budgets and explicit partial/error states; D12 failures never block a valid prediction.

## D15 — Artifacts and Data Lineage

```mermaid
flowchart TB
  GH["D02 GitHub/PyPI source events"] --> RAW["data/raw/ immutable core JSONL"]
  MAP["D01/D09 mapping history and scope"] --> MAPH["data/processed/ mapping_history"]
  DOC["D02/D03 versioned official docs"] --> DOCS["data/raw/ document snapshots"]
  EXT["D04/D05 external source payloads"] --> ERAW["data/raw/ external documents/candidates"]
  RAW --> NORM["UTC, Pydantic, canonical IDs, dedup, null policy"]
  MAPH --> NORM
  DOCS --> NORM
  ERAW --> NORM
  NORM --> CORE["data/processed/ repo/package/core events"]
  NORM --> PDOC["data/processed/ official doc revisions"]
  NORM --> PEXT["data/processed/ external relevance/census"]
  PDOC --> PROF["D03 grounded FunctionalProfile"]
  PROF --> PROFILE["data/processed/ versioned profiles"]
  PROFILE --> QUERY["D04 SearchPlan + query_set_hash"]
  QUERY --> QM["metadata/ query/window/source manifests"]
  QM -.-> EXT
  PEXT --> SAFE["D05/D06 eligibility and leakage gates"]
  SAFE --> E1["data/features/ E1 rule features"]
  SAFE --> AG["D07 three specialists"]
  AG --> SIG["data/processed/ ValidatedSignal"]
  SIG --> E2["data/features/ E2 agent features"]
  AG -.-> CAN["data/labels/ EventCandidate"]
  CORE --> CF["data/features/ CoreFeatureVector"]
  CORE --> LABEL["D09 uniform verified labels/status ledger"]
  CAN -.-> LABEL
  LABEL --> TRAIN["D10 temporal folds + E0/E1/E2"]
  CF --> TRAIN
  E1 --> TRAIN
  E2 --> TRAIN
  TRAIN --> REG["models/ frozen A + optional B + manifests"]
  REG --> INF["D11 model-only online inference"]
  CF --> INF
  E1 --> INF
  E2 --> INF
  INF --> REP["reports/ final_report.json"]
  RAW --> META["metadata/ lineage, coverage, errors, costs"]
  DOCS --> META
  ERAW --> META
  META --> REP
```

**Artifact rules:** Keep immutable raw evidence and versioned processed/features/models. Join at `package_id + cutoff_at`; preserve mapping, source, query, label, model, purpose and calibration lineage. E1 must not depend on agent output; D09 labels must not depend on which items agents happen to find.

---

## Appendix A — Core Data Contracts

| Contract | Key fields / invariant |
|---|---|
| `ResolvedTarget` | `package_id`, `repo_id`, `repo_group_id`, scope, mapping revision/validity, `cutoff_at`; verified package mapping |
| `RawRecord` / `SourceManifest` | Source and revision, `event_at`, `available_at`, `fetched_at`, `run_id`, coverage/error status; missing ≠ zero |
| `FunctionalProfile` | Package/cutoff, versioned official docs, evidence spans, mapping and profiler revisions |
| `ExternalCandidate` / `DocumentCensus` | Query hash/version, source/window/policy, source IDs, revision, counts and completeness |
| `ValidatedSignal` | Subject, context, type, timestamps, cited offsets, source and agent versions; not a ground-truth label |
| `CoreFeatureVector` | Package/cutoff, as-of values, scope quality, mapping/feature revisions and coverage |
| `ExternalRuleFeatureVector` (E1) | Query/source comparability, nullable counts/rates; no specialist-agent outputs |
| `ExternalAgentFeatureVector` (E2) | Nullable event counts, processed/eligible document counts, event dedup and signal version |
| `VerifiedCessationEvent` / `OfficialPlan` | Official authority, event/effective/announced/available times, revision, source ref, revocation state |
| `HistoricalLabel` | Package/cutoff/horizon, `1/0/unknown`, `label_available_at`, policy, event/reason |
| `ModelManifest` | Fit time, schema/order, preprocess/calibration, label/source/query policies, population/OOD limits, test manifest |
| `AnalyzeRequest` / `FinalReport` | Cutoff, purpose, requested/used mode, status, evidence/coverage/lineage, optional prediction and impact |

**As-of status changes:** `status_unverified → scheduled_cessation` (verified future plan); `scheduled_cessation → status_unverified` (plan withdrawn); `scheduled_cessation → already_abandoned` (effective cessation verified); or `status_unverified → already_abandoned` (direct confirmation). Changes after cutoff do not rewrite historical known status.

## Appendix B — Release Gates

**Model A / CORE:** Valid operational labels and uniform discovery; historically valid core features/mapping; time-safe folds and label availability; missingness, selection-bias and OOD audits; validation-calibrated thresholds and abstention; untouched final test.

**Model B / ENHANCED:** Versioned historical text; comparable source-query-window census; documented agent-processing coverage; matched E0/E1/E2 cohorts; validated incremental gain and calibration; compatible registry/online schemas and reliable CORE fallback. Otherwise ship **CORE + optional external insights**.

**UI:** Distinct known status, prediction and no-score paths; accurate inference purpose; grounded source/time citations; separate insights and impact; visible fallback, coverage, calibration population and OOD limitations.

**Freeze before testing:** Cohorts/folds, label rules, relevance/census policy, sampling budget, coverage/OOD gates, calibration, thresholds and acceptance criteria must be fixed using train/validation, **not** the final holdout.

## Appendix C — Implementation Phases

1. **Foundation:** D01, D02, D09, D10/E0, D11 CORE, minimal D13–D15.
2. **External feasibility / E1:** D03–D06, D08/E1, D10/E1; document limitations if historical data are unavailable.
3. **Agents / E2 / UX:** D07, D08/E2, D10/E2, D11 ENHANCED/fallback, optional D12, full D13–D15.

**Unverified until implemented:** Positive/negative label volume, historical source availability, census completeness, E1/E2 lift, deployment-population calibration, and reverse-dependency coverage. Keep D00–D15 and **exactly three specialist agents**; deploy enhanced scoring only when its gates are met.

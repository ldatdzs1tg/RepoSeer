# Three-specialist orchestration (T-026)

The runtime calls exactly three specialists, in stable order:

1. **Maintenance & Lifecycle** (`maintenance_lifecycle`): interpret maintenance
   context, distinguish stability from possible support reduction, and identify
   missing maintainer/release history. Inactivity alone never proves abandonment.
2. **Functional Trend & Demand** (`functional_trend_demand`): understand purpose,
   capabilities and use cases; technical discussions do not measure package users.
3. **Alternatives & Migration** (`alternatives_migration`): source-backed alternatives,
   compatibility gaps, comparisons and explicit migration; recommendations are not migrations.

The [T-024 contracts](../../../docs/contracts/specialist_agents.md) are unchanged.
Research accepts unresolved relevance, missing dates/text and unresolved repository
mapping. Strict extraction consumes `AgentRequest` and always runs
`validate_agent_pair`; a JSON D06 receipt alone is not trusted verification.
No label, official status, probability, E1/E2 table or model result is produced.

## Setup and offline replay

From the repository root, using Python 3.12:

```powershell
uv sync --frozen --extra dev --extra functional
uv run --frozen python -m reposeer.agents.cli `
  --mode replay `
  --input examples/agents/multi_agent_demo.json `
  --responses examples/agents/multi_agent_replay.json `
  --output data/agent_runs/demo.json
```

The demo is **synthetic replay**, not a live LLM experiment: three stored content
responses produce three grounded insights, three information gaps and three enrichment
suggestions. All source availability is unknown. The replay file stores typed provider
content drafts keyed by coordinator request ID and specialist, plus optional strict
and review drafts. It never synthesizes fake LLM responses from heuristics, requires
no API key, and never imports/creates an OpenAI client. Every result says `replay`.

Output is exclusively created and must not already exist. Use a different filename
for a second run. Runtime inputs/outputs under `data/agent_runs/` are ignored and
must not be committed. The CLI reads only explicitly supplied paths and leaves raw
files unchanged. Invalid input/configuration exits 2; a failed run exits 1 after
writing its structured failure result; partial runs exit 0 with explicit audit gaps.

## Live provider

```powershell
$env:OPENAI_API_KEY = "<your-api-key>"
$env:REPOSEER_AGENT_MODEL = "<supported-model-id>"
uv run --frozen python -m reposeer.agents.cli `
  --mode live --input examples/agents/multi_agent_demo.json `
  --output data/agent_runs/live.json
```

`--model` overrides the environment model. There is no default model ID.
`OpenAISpecialistLLM` uses the existing optional SDK's
[`responses.parse` structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
content-only Pydantic drafts, `store=False`, a 20-second timeout, `max_retries=0`
and a default 3,200-token response budget. Missing configuration and provider failures
are sanitized; credentials and raw API error responses are not printed or persisted.
The coordinator owns identity, generation time, signal IDs and agent/model/prompt versions.
Live behavior is tested with injected clients; no real API call is part of the demo/tests.

## Retained canonical raw documents

```powershell
uv run --frozen python -m reposeer.agents.cli `
  --mode live --raw-documents data/raw/external/documents.jsonl `
  --package-id pypi:your-package --cutoff-at 2026-10-01T00:00:00.000Z `
  --output data/agent_runs/retained-live.json
```

Use the actual target package/cutoff and an existing canonical JSONL file. Optional
`--repo-id`, `--package-description` and repeated `--description-source-ref` supply
known context; a description requires references. `--analysis-run-id` fixes request
IDs when recording/replaying the same raw intake. Raw replay also accepts
`--responses` containing matching stored provider drafts; the raw CLI path is tested
offline with canonical synthetic captures, not hard-coded production observations.

`build_research_requests_from_raw` streams `JsonlStorage.iter_records`, validates
`RawRecord` and `ExternalDocumentPayload`/retained response bytes, and requires the
existing upstream `repository_scope` receipt to match the envelope and exact package.
It checks the current-discovery receipt, repository binding and mapping evidence
references. It does **not** independently authenticate a submitted receipt or establish
historical mapping/document relevance; producers must use trusted retained storage.
A package field, title or URL substring alone is insufficient. Unbound/mismatched
records are counted as rejected rather than treated as target observations.

Source references are original raw IDs; candidate text/dates are copied, never invented.
Missing text is kept as null; relevance stays `not_assessed`. Candidates without a
reliable topic go to all three specialists; explicit hints get priority when bounding
per-specialist inputs. Stable source-reference order makes intake reproducible.
Defaults are 12 candidates per agent and 5,000 characters per excerpt; truncation,
omitted candidates and rejected records are audited. No usable text/description means
three deterministic planning gaps and **zero provider calls**, not fabricated insights.

## Validation, synthesis and budgets

Research drafts must supply a source reference and an exact supporting quote from
that same candidate excerpt or cited package description. Missing/incorrect quotes
and unknown references are rejected with reason codes; valid findings from other
agents survive. Quote receipts preserve the text and distinguish `after_cutoff`,
`unknown_availability` and `as_of_not_independently_verified`. Even matching timestamps
never establish independently verified history. Textual support is not semantic truth.

Strict drafts cite only evidence ID and quote. The coordinator rejects missing or
multiply occurring quotes, calculates Unicode offsets and copies source/revision/time
metadata from the original passage before T-024 pair validation. Live strict extraction
requires an injected Python `D06ReceiptVerifier`; CLI provides no trust flag or production
verifier. Replay can check structure/quotes without it, but candidates remain report-only.
Even a trusted D06 result has `e2_eligible=false`: D08 coverage and the independent T-025
agent-event/processing verifier remain mandatory.

Python code deterministically merges identical research content while retaining agent
attribution, limitations and temporal uncertainty. Shared original-event identity,
subject and action form strict clusters; similar prose alone never merges events.
Conflicting content/times stay report-only. A bounded reviewer sees only the cluster's
original conflicting passages/signals and cannot create sources, signals or change dates.
Its recommendation is advisory: this runtime conservatively leaves conflicts unresolved
without independent semantic resolution. Original pairs and review lineage are rechecked
with T-024 validators. The reviewer is not a fourth specialist.

Defaults allow seven provider invocations total, at most one review invocation globally
and at most one attempt per conflicting cluster. SDK retries are disabled; no coordinator
retries occur. `--max-llm-calls` and `--max-review-calls` can reduce those budgets.
Strict contexts above 60,000 passage characters are rejected explicitly. Content lists
are capped at 20 items per response category with recorded truncation. Per-agent failures,
call counts and unprocessed candidate assignments remain visible. Task completion never
claims a complete source census, historical coverage or E2 eligibility.

## T-027 handoff

Import `MultiAgentRunRequest`, `MultiAgentCoordinator`, `MultiAgentRunResult` from
`reposeer.agents`. Inject a provider, optional trusted receipt backend and optional clock;
call `coordinator.analyze(request)`. Typed request/result models expose
`model_json_schema()` and serialize with schema version `0.1.0` and dedicated record types.
The result retains T-024 responses, grounding receipts, deterministic summaries, strict
candidates, conflict review/recommendations, per-agent statuses, intake/processing counts,
rejections and limitations. It is not a `FinalReport` or a trusted `ValidatedAgentEvent`.
T-027 must select report-eligible context and validate any final report separately.

Enrichment suggestions remain plans for D03/D04/D05; no automatic retrieval, crawling,
query execution or feedback loop is implemented. UI integration belongs to T-029.
No upstream contract, T-025 verifier, model, label or collection workflow is changed.

```powershell
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q tests/contracts/test_specialist_agents.py tests/unit/agents/test_multi_agent.py tests/unit/analysis/test_external_context.py
uv run --frozen python -m scripts.export_specialist_agents --check
uv run --frozen python -m scripts.validate_specialist_examples
```

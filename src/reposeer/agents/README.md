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

## Live provider: OpenRouter and .env

Add the LLM section from [`.env.example`](../../../.env.example) to your existing
`.env` at the repository root; preserve its other keys. OpenRouter is the default:

```dotenv
REPOSEER_AGENT_PROVIDER=openrouter
OPENROUTER_API_KEY=<your-openrouter-key>
REPOSEER_AGENT_MODEL=<provider>/<model-id>
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
REPOSEER_AGENT_TIMEOUT_SECONDS=20
REPOSEER_AGENT_MAX_OUTPUT_TOKENS=3200
OPENROUTER_HTTP_REFERER=
OPENROUTER_APP_TITLE=RepoSeer
```

```powershell
uv run --frozen python -m reposeer.agents.cli `
  --mode live --input examples/agents/multi_agent_demo.json `
  --output data/agent_runs/live.json
```

The model ID must explicitly identify a model supporting structured outputs; no model
is selected automatically. Process environment overrides `.env`, and `--model` overrides
both. Timeout must be finite in `(0, 120]` seconds; output tokens must be in `[256, 8000]`.
The referer and app-title headers are optional attribution, not source verification.

`create_specialist_llm()` reads live settings on demand and creates an
`OpenRouterSpecialistLLM`. Pydantic validates `OPENROUTER_BASE_URL`; the adapter
converts it to an SDK-compatible Python string at the provider boundary. It reuses
the optional OpenAI SDK and the existing content-only Pydantic drafts via OpenRouter's [Responses endpoint](https://openrouter.ai/docs/api/api-reference/responses/create-responses),
with `store=False`, SDK retries fixed at zero, bounded timeout/tokens, and
`provider.require_parameters=true`. Unsupported structured-output routing fails rather
than silently relaxing validation. SDK errors and configuration failures are sanitized;
API keys are redacted in settings and never persisted in results. Coordinator-owned
metadata, quote grounding and T-024 checks remain unchanged.

For direct OpenAI access, set `REPOSEER_AGENT_PROVIDER=openai`, `OPENAI_API_KEY` and an
explicit `REPOSEER_AGENT_MODEL`. `OpenAISpecialistLLM` remains available to Python callers.
Both transports support injected fake clients; no real API call is part of tests/demo.
Replay does not construct live settings or a client and works even with invalid live
configuration. No additional dependency or lockfile change is required.

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

`build_research_requests_from_raw` streams `JsonlStorage.iter_records` and validates
`RawRecord` and `ExternalDocumentPayload`/retained response bytes. Valid existing
`repository_scope` receipts must match the exact package/repository and mapping references;
these candidates retain `relevance_status="not_assessed"`. Without a receipt, explicit
`raw.package_id` association is accepted with `relevance_status="unresolved"`. If the raw
package is null, complete discovery lineage with a projection-verified reference to the
target package also qualifies. Repository identities must agree with the requested mapping.
Conflicting/invalid supplied receipts are rejected without fallback; foreign packages,
missing associations and title/URL substring matches do not qualify.

`coverage_summary.intake.unverified_scope_candidates` counts unique accepted discovery-only
observations before budget truncation, rather than counting specialist assignments.
Association never verifies document relevance, historical mapping or availability;
producers must use trusted retained storage. Coverage remains `unknown`/`unverified`,
with an explicit limitation when unverified associations are included.

Source references are original raw IDs; candidate text/dates are copied, never invented.
Missing text is kept as null. Candidates without a reliable topic go to all three
specialists; explicit hints get priority when bounding per-specialist inputs. Stable source-reference order makes intake reproducible.
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

Defaults allow seven provider invocations total; reviewer calls are disabled by default
(`--max-review-calls 0`). Opt in with `--max-review-calls 1` only when an advisory
recommendation is useful. Conflict records still retain unresolved/report-only status
and source references without a reviewer call. Each conflicting cluster gets at most one
attempt. SDK retries are disabled; no coordinator retries occur. `--max-llm-calls` bounds
the total, including any opted-in reviews.
Strict contexts above 60,000 passage characters are rejected explicitly. Content lists
are capped at 20 items per response category with recorded truncation. Per-agent failures,
call counts and unprocessed candidate assignments remain visible. Task completion never
claims a complete source census, historical coverage or E2 eligibility.

## Provider errors and diagnostics

Provider failures use sanitized codes: `provider_timeout`, `provider_connection_error`,
`provider_auth_failed`, `provider_permission_denied`, `provider_rate_limited`,
`provider_model_or_endpoint_not_found`, `provider_bad_request` and `provider_api_error`.
A generic HTTP 400/422 does not prove structured-output incompatibility. Missing or
schema-invalid parsed output uses `invalid_structured_output`; refusal and incomplete
responses retain `llm_refusal` and `llm_incomplete_output`. Unknown exceptions use
`llm_request_or_output_failed`. Agent status and rejection audits preserve these codes
without exception bodies or credentials; other specialists keep their valid results.

Read `provider_call_diagnostics` in the run JSON for each live adapter invocation's
`operation`, coordinator request/cluster `request_ref`, nonnegative `duration_ms`,
`success` and sanitized `error_code`. Token fields `input_tokens`, `output_tokens`
and `total_tokens` come directly from provider usage when present and are otherwise
null, including failures without a response. Diagnostics are isolated per run even
when reusing a provider. Replay/fake providers need no telemetry and produce an empty
list unless they supply it; no tokens or USD costs are estimated. Adapter success
means schema parsing succeeded; downstream grounding and semantic checks still apply.

Optional `parse_failure` distinguishes `invalid_json`, `schema_validation_failed`,
`missing_parsed_output`, typed `sdk_parse_failed`, and `unknown_parse_failure`.
`validation_errors` contains bounded, sanitized field paths and Pydantic error types;
unknown field names are redacted. Messages, inputs and response bodies are never stored.
The general `invalid_structured_output` code remains compatible with earlier results.

## Prompt design v0.2.1

[`prompts.py`](prompts.py) combines common evidence-analysis, temporal, authority and
injection-resistance instructions with one of the three specialist rubrics and a
separate research or strict-extraction task. The bounded reviewer gets common/review
instructions only, without a fourth specialist role. Both live transports use the
same pure builder; package/source JSON stays in the user message.

Research v0.2.1 explicitly requires verbatim contiguous quotes from the supplied excerpt,
including Unicode and whitespace, and one concise ResearchDraft with all required fields.
Exact substring/schema validators remain authoritative; the prompt cannot guarantee
model compliance, and no automatic repair or retry is performed.

Observations are stated from supplied text; interpretations explain their limited
meaning; hypotheses retain uncertainty rather than becoming independently verified
facts. Research should recover useful grounded leads despite missing relevance or
dates. When source text cannot support an insight, abstain from that insight and
identify the precise gap, why it matters, and a focused D03/D04/D05 enrichment action.
Strict instructions cover the allowed signal types, exact quotes, null unknown dates,
and provisional use without bypassing D06 gates. They do not replace code validation.

`prompt_version="0.2.1"` identifies the runtime instruction revision. Contract and replay
schemas stay `0.1.0`. Replay keeps stored content and does not regenerate responses or
prove they were produced by v2. Fake-client/replay tests check composition, schema,
grounding and promotion behavior, **not live model quality or improvement over v1**.

For real-document evaluation, pin model/provider/configuration and the same source
revisions/cutoff for both prompt versions. Manually review quote support, relationship
scope, unsupported claims, missing-information specificity and enrichment usefulness.
Use a small explicit API budget; do not run this evaluation automatically in CI.

| Specialist | Case | Expected review criterion |
| --- | --- | --- |
| Maintenance | Few releases but ongoing issue support | No automatic abandonment conclusion |
| Maintenance | Maintainer lacks time | Supported maintenance-capacity concern |
| Maintenance | Scheduled support ending | Separate announcement and effective dates |
| Maintenance | Contradictory sources | Retain uncertainty and request resolving evidence |
| Functional | Same-function discussion | Do not infer package adoption |
| Functional | Official feature documentation | Source-grounded capability insight |
| Functional | Historical capability unclear | Request version/availability evidence |
| Functional | Broad technology popularity | Do not assign package demand |
| Alternatives | User recommendation | Do not claim completed migration |
| Alternatives | Explicit migration account | Identify the reported source-to-destination move |
| Alternatives | Similar function, unknown API | Retain compatibility gap |
| Alternatives | Unverified replacement report | Do not assign official status |

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

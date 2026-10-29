# External context analysis (T-025)

This additive offline module consumes exact canonical T-018 Article observations and
their bound RelevanceAssessment. It returns T-021 SourceProvenance, AnalysisEvidence,
AnalysisCoverage, deterministic E1 counts/rates/trends and gated E2 event counts.
It does not collect, call an LLM, assign labels, change official status, train, load
a model or alter any prediction. Existing collection/storage commands are unchanged.

## Run the sample

From the repository root, with its own environment:

~~~powershell
uv sync --extra dev --frozen
uv run --frozen python -m scripts.analyze_external_context --input examples/external_context/current_capture_request.json
uv run --frozen python -m scripts.analyze_external_context --input examples/external_context/current_capture_request.json --output data/external_context.json
~~~

The optional output must not already exist; its parent must exist. Without output,
only stdout is written. Inputs are read-only, errors exit nonzero, and no neighboring
files or Drive submissions are scanned. All committed examples are synthetic and
use a fake package/repository, .test URLs and explicitly synthetic provenance.
The current-capture example intentionally returns report-only context and null
features rather than asserting historical/corpus coverage.

## Python handoff

~~~python
from reposeer.analysis.external import ExternalContextRequest, analyze_external_context

request = ExternalContextRequest.model_validate_json(request_bytes)
result = analyze_external_context(request)  # conservative default: no trusted verifier
fields_for_t027 = result.report_fields()
~~~

T-027 merges sources/evidence/coverage by their unique IDs, then validates the complete
FinalReport with T-021. Do not replace an existing report's core sources/coverage.
Only external_insights_status/reason and the additive evidence fields are supplied;
probabilities, model selection, thresholds, mapping and official status stay with
their existing producers. T-022/T-023 consume feature lineage and decide whether it
matches a frozen Model B policy; a nonnull count is not model deployment approval.

Each ExternalDocument contains:
- A canonical Article with exact text/hash, observation provenance and raw IDs.
- Its T-018 RelevanceAssessment bound to the same article, content revision, raw IDs
  and Unicode quote offsets.
- Explicit provider, query intent and actual query-set hash from the upstream
  discovery projection/manifest. No inference from keywords or a candidate URL.
- Optional trusted origin_id for syndication deduplication and outcome_disclosure
  from the upstream D06/status review. That flag only excludes; it cannot certify.

Get articles from canonical_normalization_results and assessments from the T-018
NormalizationPipelineResult, joining exact revision/raw references. Preserve all
unknown fields from T-020 rather than backfilling published dates as availability.
For persisted T-020 output, read `processed/external_documents/external_documents.parquet`,
`external_relevance/external_relevance.parquet` and `external_lineage/external_lineage.parquet`.
Join article/assessment by `article_id`, `raw_record_id`, content hash and target package;
join lineage by `(article_id, raw_record_id)`, never title or URL. Restore `record_type`,
JSON list columns and observation-specific `Provenance.raw_record_ids`, then validate
`Article`, `RelevanceAssessment` and `validate_assessment_evidence()` before constructing
an `ExternalDocument`. Copy provider/intent/query hash from the actual lineage metadata;
missing provider/intent may be `"unknown"`, while missing query hash stays null. This
sentinel is not verified identity. Preserve all timestamps/nulls; absent window/census
proof means no census, and no historical verifier means no invented historical features.
The offline T-020 integration test demonstrates this round trip without a new adapter.
A raw discovery candidate is not an Article. The module does not promote T-018's
unresolved/technology-only decisions into same-function or alternative evidence.

## Temporal and source gates

Relevant documents produce verbatim cited context. They are feature-eligible only
with full text, precise publication time at/before cutoff, no later edited revision,
known source and content availability at/before cutoff, and a bound content hash.
Historical-reconstruction text additionally requires an injected trusted verifier.
Unknown availability, snippets, date-only publication, late revisions and outcome
disclosures remain report-only, with explicit exclusion reasons. Package and
observation/revision mismatches fail validation instead of selecting a nearby match.

A conservative outcome phrase screen supplements the upstream D06 disclosure flag.
It is not a semantic cessation detector: upstream D06 must identify other outcome
shortcuts, including non-English/paraphrased announcements. Official events still
require the independent ledger/status verifier; context never establishes truth.

WindowCensus specifies a provider, intent, context, exact (start,end] window,
persisted source revision, actual query-set hash, retrieval policy and filter hash,
execution/source coverage, and exact document source IDs. Counts remain null unless:
- Census and source coverage are complete.
- Its source and all referenced observations are usable as-of cutoff.
- Every member exists, is classified, has a precise in-window timestamp and the
  same provider/intent/query hash; relevant members pass D06 and match the context.
- Known matching observations are not silently left out.
- A trusted backend verifies the complete declared source/query/window census.

Exhausted current API pagination, a READY marker, a query hash, an input
status="complete", and a publication timestamp alone do not establish these facts.
A historical verifier must bind actual archived bytes/revision and availability;
the census verifier must bind source policy, filters, window and enumerated items.
Verifier objects are trusted backend dependencies, never selectable JSON flags.
No production archive/census verifier is bundled. With current T-020 observations
these missing proofs normally yield null E1 features, not fabricated historical data.

Dedup uses connected components of canonical article identity, exact content hash
and trusted origin identity within each census. It preserves all observations and
citations without counting rediscovery, revisions or syndicated copies twice.
Count is distinct relevant original items in the declared corpus. rate_per_30_days
is 30 * count / full window days. Zero requires a genuinely verified empty census;
missing/partial scope is null. A verified empty census can complete with no relevant
evidence: count/rate are zero and `external_insights_status="complete"` describes only
the declared scope, not package health or the whole Internet. With no evidence and no
verified census, insights remain unavailable with a reason; report-only context and
partially verified scopes remain partial. Adjacent windows produce log1p(recent)-log1p(previous)
only with identical provider, context, intent, query-set, policy and filters and
equal duration. A mismatch yields a null trend and reason, never a popularity claim.

## E2 boundary

D07 passes ValidatedAgentEvent plus complete AnalysisCoverage for the same eligible
document set and window. The module rechecks package, agent provenance, exact quote,
source context, event time, outcome exclusion, uncertainty and a trusted D07 event
verifier. It does not implement/launch the three specialists or a coordinator agent.
Every relevant input observation must have been processed; top-K is insufficient.

The verifier must also validate processing coverage (including a reviewed empty
event set), so complete JSON flags cannot manufacture zero counts. For an empty
document set, agent coverage references the census source instead of an empty
historical source list. E2 counts distinct event IDs separately for maintenance,
functional_demand and migration in each declared window; it is independent of E1.
Repeated events are counted once. Contradictory claims/times for the same event,
context and signal fail closed for upstream bounded conflict review; rejected
events carry audit reasons and invalidate E2 only for censuses attributed by their bound
citation source membership. Citations spanning multiple censuses affect each one;
unknown/unassigned source identity conservatively invalidates every possible census.
Rejected context/time claims cannot narrow that scope. E1 counts/rates/trends are
independent of these agent failures. No coverage extrapolation
or abandonment/ground-truth labeling happens here.

## Verification

~~~powershell
uv run --frozen ruff check src tests scripts
uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/unit/analysis/test_external_context.py tests/contracts/test_analysis_contract.py -q
uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/integration/test_collection_pipeline.py -q -k external_context
uv run --frozen python -m scripts.export_analysis_contract --check
uv run --frozen python -m scripts.validate_analysis_examples
~~~

Tests use explicit fake trusted verifiers for synthetic positive census/event cases.
They cover null versus zero, temporal revision proof, grounded quotes and targets,
source/query membership, dedup bridges, comparison policy, complete agent processing,
rejected/conflicting events, deterministic replay, unchanged model outputs and the
offline CLI's refusal to overwrite existing files. They establish implementation
behavior, not availability of real historical corpora or model effectiveness.
